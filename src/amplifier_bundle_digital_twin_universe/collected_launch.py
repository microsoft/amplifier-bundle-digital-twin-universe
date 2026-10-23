# Copyright (c) Microsoft. All rights reserved.
"""Explicit bounded collected launch. Default synchronous launch is unchanged."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

from . import admission, collector, lifecycle, incus
from .bound_backend import capture as capture_backend, from_dict as restore_backend, BackendFence
from .profile import find_profile_path, load_profile_from_content

_ACTIVE = ContextVar("dtu_collected_worker", default=None)
_INPUT_VERSION = 1
_NONADMISSION = {"approved_image_missing", "approved_image_unavailable", "execution_capacity",
                 "retained_capacity", "storage_pressure", "storage_unknown", "name_conflict",
                 "request_conflict", "backend_unknown"}


class CollectionCancelled(BackendFence):
    pass


def _input_path(root, request_id):
    return root / "collectors" / (hashlib.sha256(request_id.encode()).hexdigest() + ".input")


def _cancel_path(root, request_id):
    return _input_path(root, request_id).with_suffix(".cancel")


def _runtime():
    package = Path(__file__).parent.resolve()
    return {"python": str(Path(sys.executable).absolute()),
            "python_digest": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
            "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(package.glob("*.py"))}}


def _intent(raw, variables, name, owner):
    return hashlib.sha256(json.dumps({"profile": raw, "variables": variables, "name": name,
                                    "hostname": None, "owner_root": owner, "offline": True}, sort_keys=True).encode()).hexdigest()


def _supported(profile_arg, variables, options):
    if (not options.get("offline") or options.get("hostname") is not None
            or not isinstance(options.get("owner_root"), str) or not options["owner_root"].strip()):
        raise admission.AdmissionError("collect_unsupported", "Collection requires offline mode and a nonblank owner, without hostname")
    path = find_profile_path(profile_arg)
    with path.open() as stream:
        raw = stream.read(65537)
    if len(raw.encode()) > 65536 or not isinstance(variables, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in variables.items()
    ):
        raise admission.AdmissionError("collect_unsupported", "Collected profile inputs exceed the supported shape")
    profile = load_profile_from_content(raw, variables, path=path)
    unresolved = load_profile_from_content(raw, {}, path=path)
    if (not re.fullmatch(r"local:[a-f0-9]{64}", profile.base.image)
            or profile.url_rewrites or profile.mock_services or profile.pypi_overrides
            or profile.access or profile.passthrough or profile.provision and profile.provision.files
            or profile.name != unresolved.name or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", profile.name)):
        raise admission.AdmissionError("collect_unsupported", "Collection currently supports exact local offline images without host files, passthrough, access, packages or mocks")
    return str(path), raw


def launch(profile_arg, variables, *, wait_seconds=600, **options):
    """Prepare/dispatch at most once, then observe. Timeout never cancels worker."""
    if not isinstance(variables, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in variables.items()):
        raise admission.AdmissionError("collect_unsupported", "Collected variables must be strings")
    variables = dict(variables)
    if len(json.dumps(variables).encode()) > 65536:
        raise admission.AdmissionError("collect_unsupported", "Collected variables exceed their bound")
    path, raw = _supported(profile_arg, variables, options)
    if type(wait_seconds) not in (int, float) or not 0 <= wait_seconds <= 86400:
        raise ValueError("wait_seconds must be between 0 and 86400")
    request = options.get("request_id") or str(uuid.uuid4())
    if not isinstance(request, str) or not request.strip() or len(request) > 256:
        raise ValueError("Invalid request identity")
    name = options.get("name") or "dtu-" + hashlib.sha256(request.encode()).hexdigest()[:16]
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]{0,62}", name):
        raise ValueError("Invalid instance name")
    owner = options["owner_root"]
    if len(owner) > 256:
        raise ValueError("Invalid owner identity")
    limits = admission.policy(options.get("max_running"), options.get("max_instances"), options.get("min_free_bytes"))
    backend = capture_backend()
    with backend.activate():
        backend.verify()
        root = admission.state_directory(backend.scope)
        binding = dict(backend.scope, request_id=request, name=name, owner_root=owner,
                       intent_digest=_intent(raw, variables, name, owner), destination_digest=backend.digest)
        store = collector.AttemptStore(root, binding)
        with lifecycle.custody(backend.scope, name):
            previous = store._read()
            if previous is not None:
                # No generation reset or dispatch replay, even if no PID exists.
                observed = store.observe()
                if "result" in observed:
                    from .engine import launch_status
                    status = launch_status(request)
                    if status["state"] == "ready":
                        return {**status["result"], "reused": True}
                raise admission.AdmissionError("launch_unknown", "The collected attempt already exists; observe its original request", {"request_id": request, "id": name})
            from .collector_evidence import preflight
            preflight(binding)
            if lifecycle.read_marker(root, name) is not None:
                raise admission.AdmissionError("name_conflict", "A collected attempt already reserves this name")
            payload = {"version": _INPUT_VERSION, "binding": binding, "generation": "0" * 32,
                       "backend": backend.as_dict(), "runtime": _runtime(), "root": str(root),
                       "profile_path": path, "profile_text": raw, "variables": dict(variables),
                       "limits": limits, "launch_timeout": incus._get_launch_timeout_seconds()}
            if len(json.dumps(payload).encode()) > 262144:
                raise admission.AdmissionError("collect_unsupported", "Collected bootstrap exceeds its private bound")
            # Definite pre-admission refusals are not lost worker outcomes. The
            # worker still rechecks authoritatively under its original lock.
            with admission.locked(backend.scope):
                previous_receipt = admission.read_receipt(root, request)
                if previous_receipt is not None:
                    raise admission.AdmissionError("request_conflict", "An existing receipt cannot be adopted into collection")
                if incus.container_exists(name):
                    raise admission.AdmissionError("name_conflict", "The collected instance name is already present")
                profile = load_profile_from_content(raw, variables, path=Path(path))
                if not incus.local_image_available(profile.base.image[6:]):
                    raise admission.AdmissionError("approved_image_missing", "The exact offline image is absent; no dispatch occurred")
                capacity = admission.snapshot(backend.scope, limits, root)
                if not capacity["available"]:
                    raise admission.AdmissionError(capacity["blockers"][0]["code"], "Capacity unavailable; no collector dispatched")
            generation = store.prepare()
            payload["generation"] = generation
            # No raw bootstrap/profile/environment appears in public journal.
            admission._save_private_json(_input_path(root, request), payload)
            admission._save_private_json(lifecycle.marker_path(root, name), {"request_id": request, "name": name,
                                          "generation": generation, "input_digest": collector._digest(payload)})
            from .collector_observation import initialize
            # Preserve public request discovery if observation initialization
            # fails. No dispatch is consumed until the new record is acknowledged.
            initialize(store, generation)
            from .collector_evidence import initialize as initialize_evidence
            initialize_evidence(store, generation)
            store.dispatch_once(generation, lambda _: None)
    # Dispatch was consumed durably, but the parent releases name custody first.
    # A crash in this gap remains unknown; it never refunds the generation.
    spawn_worker(payload)
    deadline = time.monotonic() + wait_seconds
    while True:
        observed = store.observe()
        refusal = _outcome(root, request)
        if refusal in _NONADMISSION:
            raise admission.AdmissionError(refusal, "Collected request was rejected before instance submission",
                                           {"request_id": request, "id": name, "admitted": False})
        if "result" in observed:
            current = public_status(request)
            if current["state"] != "ready":
                raise admission.AdmissionError("launch_unknown", "Completion is recorded but the current instance is not verified ready",
                                               {"request_id": request, "id": name})
            return current["result"]
        if observed.get("phase") in {"unknown", "fenced"} or time.monotonic() >= deadline:
            raise admission.AdmissionError("launch_unknown", "Collected launch has no confirmed completed result; no replay occurred",
                                           {"request_id": request, "id": name})
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def spawn_worker(payload):
    """Fixed entry point; private input travels through one read-only descriptor."""
    if _runtime() != payload["runtime"]:
        raise BackendFence("Worker runtime changed before dispatch")
    path = _input_path(Path(payload["root"]), payload["binding"]["request_id"])
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        admission._private_file(os.fstat(fd))
        process = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).with_name("collector_worker.py")), str(fd)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   close_fds=True, pass_fds=(fd,), start_new_session=True,
                                   env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"})
        threading.Thread(target=process.wait, daemon=True).start()
        admission._save_private_json(path.with_suffix(".dispatch"),
                                    {"generation": payload["generation"], "pid": process.pid,
                                     "parent_pid": os.getpid(), "meaning": "observed_spawn_not_execution_authority"})
    finally:
        os.close(fd)
    # Reaper starts immediately on a returned child, even if dispatch evidence
    # publication fails. Observer loss does not kill the worker.


def _load(root, request):
    admission._private_directory(root)
    admission._private_directory(root / "collectors")
    payload = admission._read_private_json(_input_path(root, request), object_pairs_hook=collector._unique_object)
    if (not isinstance(payload, dict) or type(payload.get("version")) is not int
            or payload["version"] != _INPUT_VERSION
            or set(payload) != {"version", "binding", "generation", "backend", "runtime", "root",
                                "profile_path", "profile_text", "variables", "limits", "launch_timeout"}):
        raise BackendFence("Unsupported collected bootstrap")
    collector.AttemptStore(root, payload["binding"])
    marker = lifecycle.read_marker(root, payload["binding"]["name"])
    if (not marker or marker.get("request_id") != request or marker.get("input_digest") != collector._digest(payload)
            or marker.get("generation") != payload.get("generation") or payload.get("root") != str(root)):
        raise BackendFence("Collected bootstrap binding changed")
    return payload


def locate(request_id):
    """Find only this request's immutable bootstrap without ambient daemon calls."""
    name = hashlib.sha256(request_id.encode()).hexdigest() + ".input"
    matches = list(lifecycle.base_directory().glob("*/collectors/" + name))
    if not matches:
        return None
    if len(matches) != 1:
        raise BackendFence("Collected request scope is ambiguous")
    root = matches[0].parent.parent
    return root, _load(root, request_id)


def public_status(request_id):
    found = locate(request_id)
    if found is None:
        return None
    root, payload = found
    binding = payload["binding"]
    state = collector.AttemptStore(root, binding).observe()
    from . import engine
    backend = restore_backend(payload["backend"])
    with backend.activate():
        backend.verify()
        observed = engine._launch_status_legacy(request_id, _reconcile=False)
    allowed = {"schema_version", "request_id", "name", "owner_root", "state",
               "created_at", "instance_status", "result", "result_recovered"}
    result = {k: v for k, v in observed.items() if k in allowed}
    result["collector_phase"] = state["phase"]
    from .collector_observation import projection
    result["observations"] = projection(collector.AttemptStore(root, binding), payload["generation"])
    from .collector_evidence import projection as evidence_projection
    result["failure_evidence"] = evidence_projection(collector.AttemptStore(root, binding), payload["generation"])
    if result.get("state") == "not_recorded":
        result["state"] = "unknown"
    refusal = _outcome(root, request_id)
    if refusal in _NONADMISSION:
        result.update(state="failed", admitted=False, failure_cause={"stage": "admission", "code": refusal})
    if refusal == "cancel_requested" or _cancellation(payload):
        result["cancellation_requested"] = True
    return result


def _outcome(root, request):
    try:
        value = admission._read_private_json(_input_path(root, request).with_suffix(".outcome"))
    except FileNotFoundError:
        return None
    payload = _load(root, request)
    attempt = collector.AttemptStore(root, payload["binding"])._read()
    if (not isinstance(value, dict) or set(value) != {"generation", "code"}
            or value.get("code") not in _NONADMISSION | {"unknown", "cancel_requested"}
            or value.get("generation") != payload["generation"]
            or not attempt or attempt["generation"] != value["generation"]):
        raise BackendFence("Unsupported collector outcome")
    return value["code"]


def check_cancel():
    active = _ACTIVE.get()
    if active is None:
        return
    payload, stopped = active
    if stopped[0]:
        raise CollectionCancelled("Collector received an explicit termination signal; outcome remains uncertain")
    if _cancellation(payload):
        raise CollectionCancelled("Cancellation requested; no backend cancellation or cleanup is inferred")


def _cancellation(payload):
    path = _cancel_path(Path(payload["root"]), payload["binding"]["request_id"])
    try:
        value = admission._read_private_json(path)
    except FileNotFoundError:
        return False
    if value != {"generation": payload["generation"], "owner_root": payload["binding"]["owner_root"]}:
        raise BackendFence("Cancellation binding changed")
    return True


def worker_run(payload):
    """Accept one dispatched generation and run the existing engine lifecycle."""
    root = Path(payload["root"])
    binding = payload["binding"]
    if _load(root, binding["request_id"]) != payload or payload["runtime"] != _runtime():
        raise BackendFence("Worker input or runtime identity changed")
    backend = restore_backend(payload["backend"])
    if backend.scope != {k: binding[k] for k in ("daemon", "project", "storage_pool")} or backend.digest != binding["destination_digest"]:
        raise BackendFence("Worker backend binding changed")
    store = collector.AttemptStore(root, binding)
    stopped = [False]
    token = _ACTIVE.set((payload, stopped))
    old_handlers = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            old_handlers[sig] = signal.signal(sig, lambda *_: stopped.__setitem__(0, True))
    try:
        with backend.activate(), lifecycle.custody(backend.scope, binding["name"], timeout=30):
            backend.verify()
            from .collector_evidence import Recorder as EvidenceRecorder
            evidence = EvidenceRecorder(store, payload["generation"])
            with store.owned_worker(payload["generation"], preserve_error=True, failure_recorder=evidence) as submission:
                check_cancel()
                from . import engine
                from .collector_observation import Recorder
                observations = Recorder(store, payload["generation"])
                result = engine._launch_impl(payload["profile_path"], payload["variables"],
                    name=binding["name"], owner_root=binding["owner_root"], request_id=binding["request_id"],
                    offline=True, **payload["limits"], _frozen_profile=payload["profile_text"],
                    _submission_hook=submission, _observations=observations, _evidence=evidence)
                return result
    except Exception as exc:
        # No raw exception/env/argv persisted. The worker scope records unknown.
        try:
            with admission._locked_file(root / "admission.lock"):
                causes = []
                cause = exc
                for _ in range(8):
                    if cause is None:
                        break
                    causes.append(cause)
                    cause = cause.__cause__
                code = (exc.code if isinstance(exc, admission.AdmissionError) and exc.code in _NONADMISSION
                        and admission.read_receipt(root, binding["request_id"]) is None else
                        "cancel_requested" if any(isinstance(c, CollectionCancelled) for c in causes) else "unknown")
                admission._save_private_json(_input_path(root, binding["request_id"]).with_suffix(".outcome"),
                                            {"generation": payload["generation"], "code": code})
        except Exception:
            exc.add_note("Collector outcome evidence was not acknowledged")
            raise exc
        return None
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        _ACTIVE.reset(token)


def cancel(request_id, owner_root):
    """Explicit cooperative cancellation request, not resource disposition."""
    found = locate(request_id)
    if found is None:
        raise admission.AdmissionError("request_conflict", "No collected request is recorded")
    root, payload = found
    if payload["binding"]["owner_root"] != owner_root:
        raise admission.AdmissionError("ownership_conflict", "Cancellation owner does not match")
    backend = restore_backend(payload["backend"])
    backend.verify()
    with admission._locked_file(root / "admission.lock"):
        admission._save_private_json(_cancel_path(root, request_id),
                                    {"generation": payload["generation"], "owner_root": owner_root})
    return {"request_id": request_id, "cancellation_requested": True, "outcome": "unknown"}


@contextmanager
def mutation(marker, root, name):
    payload = _load(root, marker["request_id"])
    binding = payload["binding"]
    if binding["name"] != name:
        raise BackendFence("Collected mutation name changed")
    backend = restore_backend(payload["backend"])
    store = collector.AttemptStore(root, binding)
    with backend.activate(), store.mutation_guard(payload["generation"]):
        backend.verify()
        lifecycle.verify_instance(binding)
        yield
