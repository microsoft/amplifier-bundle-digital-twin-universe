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

from . import admission, collector, lifecycle
from .bound_backend import capture as capture_backend, from_dict as restore_backend, BackendFence
from .profile import find_profile_path, load_profile_from_content

_ACTIVE = ContextVar("dtu_collected_worker", default=None)
_INPUT_VERSION = 1


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
    raw = path.read_text()
    if len(raw.encode()) > 65536 or not isinstance(variables, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in variables.items()
    ):
        raise admission.AdmissionError("collect_unsupported", "Collected profile inputs exceed the supported shape")
    profile = load_profile_from_content(raw, variables, path=path)
    if (not re.fullmatch(r"local:[a-f0-9]{64}", profile.base.image)
            or profile.url_rewrites or profile.mock_services or profile.pypi_overrides
            or profile.access or profile.passthrough or profile.provision and profile.provision.files):
        raise admission.AdmissionError("collect_unsupported", "Collection currently supports exact local offline images without host files, passthrough, access, packages or mocks")
    return str(path), raw


def launch(profile_arg, variables, *, wait_seconds=600, **options):
    """Prepare/dispatch at most once, then observe. Timeout never cancels worker."""
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
            if lifecycle.read_marker(root, name) is not None:
                raise admission.AdmissionError("name_conflict", "A collected attempt already reserves this name")
            generation = store.prepare()
            payload = {"version": _INPUT_VERSION, "binding": binding, "generation": generation,
                       "backend": backend.as_dict(), "runtime": _runtime(), "root": str(root),
                       "profile_path": path, "profile_text": raw, "variables": dict(variables),
                       "limits": limits, "launch_timeout": __import__("amplifier_bundle_digital_twin_universe.incus", fromlist=["x"])._get_launch_timeout_seconds()}
            # No raw bootstrap/profile/environment appears in public journal.
            admission._save_private_json(_input_path(root, request), payload)
            admission._save_private_json(lifecycle.marker_path(root, name), {"request_id": request, "name": name,
                                          "generation": generation, "input_digest": collector._digest(payload)})
            store.dispatch_once(generation, lambda _: spawn_worker(payload))
    deadline = time.monotonic() + wait_seconds
    while True:
        observed = store.observe()
        if "result" in observed:
            return observed["result"]
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
    finally:
        os.close(fd)
    # Reap when observer remains alive; observer loss does not kill worker.
    threading.Thread(target=process.wait, daemon=True).start()


def _load(root, request):
    payload = admission._read_private_json(_input_path(root, request), object_pairs_hook=collector._unique_object)
    if not isinstance(payload, dict) or payload.get("version") != _INPUT_VERSION:
        raise BackendFence("Unsupported collected bootstrap")
    marker = lifecycle.read_marker(root, payload["binding"]["name"])
    if (not marker or marker.get("request_id") != request or marker.get("input_digest") != collector._digest(payload)
            or marker.get("generation") != payload.get("generation") or payload.get("root") != str(root)):
        raise BackendFence("Collected bootstrap binding changed")
    return payload


def check_cancel():
    active = _ACTIVE.get()
    if active is None:
        return
    payload, stopped = active
    path = _cancel_path(Path(payload["root"]), payload["binding"]["request_id"])
    if stopped[0]:
        raise CollectionCancelled("Collector received an explicit termination signal; outcome remains uncertain")
    try:
        value = admission._read_private_json(path)
    except FileNotFoundError:
        return
    if value != {"generation": payload["generation"], "owner_root": payload["binding"]["owner_root"]}:
        raise BackendFence("Cancellation binding changed")
    raise CollectionCancelled("Cancellation requested; no backend cancellation or cleanup is inferred")


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
        with backend.activate(), lifecycle.custody(backend.scope, binding["name"]):
            backend.verify()
            with store.owned_worker(payload["generation"]) as submission:
                check_cancel()
                from . import engine
                result = engine._launch_impl(payload["profile_path"], payload["variables"],
                    name=binding["name"], owner_root=binding["owner_root"], request_id=binding["request_id"],
                    offline=True, **payload["limits"], _frozen_profile=payload["profile_text"],
                    _submission_hook=submission)
                return result
    except Exception:
        # No raw exception/env/argv persisted. The worker scope records unknown.
        return None
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        _ACTIVE.reset(token)


def cancel(request_id, owner_root):
    """Explicit cooperative cancellation request, not resource disposition."""
    ctx = admission.context()
    root = admission.state_directory(ctx)
    payload = _load(root, request_id)
    if payload["binding"]["owner_root"] != owner_root:
        raise admission.AdmissionError("ownership_conflict", "Cancellation owner does not match")
    backend = restore_backend(payload["backend"])
    backend.verify()
    with admission.locked(ctx):
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
