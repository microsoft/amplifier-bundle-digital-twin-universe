"""Public collected launch integration against the existing mocked native engine."""
import importlib
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from amplifier_bundle_digital_twin_universe import admission, engine, incus
from amplifier_bundle_digital_twin_universe.collected_launch import spawn_worker as real_spawn_worker
from .test_admission import launch_infra  # noqa: F401


@pytest.fixture
def collected(monkeypatch, launch_infra):  # noqa: F811
    runtime = importlib.import_module("amplifier_bundle_digital_twin_universe.collected_launch")
    profile, instances, create = launch_infra
    Path(profile).write_text("name: collected\nbase:\n  image: local:" + "a" * 64 + "\n")
    old_query = incus.query
    monkeypatch.setattr(incus, "query", lambda path: ({"environment": {"certificate_fingerprint": "b" * 64,
                       "project": "default"}} if path == "/1.0" else old_query(path)))
    monkeypatch.setattr(incus, "local_image_available", lambda _: True)
    class FakeBackend:
        scope = admission.context()
        digest = "c" * 64

        def as_dict(self):
            return {"synthetic": True}

        def verify(self):
            pass

        @contextmanager
        def activate(self):
            yield self

    monkeypatch.setattr(runtime, "capture_backend", FakeBackend)
    monkeypatch.setattr(runtime, "restore_backend", lambda _: FakeBackend())
    launches = []

    def spawn(payload):
        launches.append(payload)
        runtime.worker_run(payload)

    monkeypatch.setattr(runtime, "spawn_worker", spawn)
    original = create.side_effect
    create.side_effect = lambda name, image, config, isolated_pool=None: original(name, image, config)
    return runtime, profile, instances, create, launches


def test_public_collect_executes_full_existing_engine_once(collected):
    runtime, profile, _, create, launches = collected
    result = engine.launch(profile, {}, name="collected", request_id="collected-r", owner_root="owner", offline=True, collect=True)
    assert result["status"] == "running"
    assert len(launches) == 1 and create.call_count == 1
    assert engine.launch(profile, {}, name="collected", request_id="collected-r", owner_root="owner", offline=True, collect=True)["reused"]
    assert len(launches) == 1 and create.call_count == 1
    assert engine.launch_status("collected-r")["result"] == result


@pytest.mark.parametrize("extra", ["passthrough:\n  allow_external: true\n", "provision:\n  files:\n    - src: ./secret\n      dest: /secret\n", "mock_services:\n  - source: /secret\n"])
def test_collect_unsupported_profile_refuses_before_dispatch(collected, extra):
    _, profile, _, create, launches = collected
    with open(profile, "a") as f:
        f.write(extra)
    with pytest.raises(admission.AdmissionError) as error:
        engine.launch(profile, {}, request_id="unsupported", owner_root="owner", offline=True, collect=True)
    assert error.value.code == "collect_unsupported"
    assert not launches and not create.called


def test_collect_timeout_observes_without_respawn(collected, monkeypatch):
    runtime, profile, _, create, launches = collected
    monkeypatch.setattr(runtime, "spawn_worker", launches.append)
    args = dict(name="delayed", request_id="delayed-r", owner_root="owner", offline=True, collect=True, wait_seconds=0)
    with pytest.raises(admission.AdmissionError) as error:
        engine.launch(profile, {}, **args)
    assert error.value.code == "launch_unknown" and len(launches) == 1
    with pytest.raises(admission.AdmissionError):
        engine.launch(profile, {}, **args)
    assert len(launches) == 1 and not create.called
    runtime.worker_run(launches[0])
    assert create.call_count == 1


def test_collected_cancel_is_not_disconnect_or_replay(collected, monkeypatch):
    runtime, profile, _, create, launches = collected
    monkeypatch.setattr(runtime, "spawn_worker", launches.append)
    args = dict(name="cancelled", request_id="cancel-r", owner_root="owner", offline=True, collect=True, wait_seconds=0)
    with pytest.raises(admission.AdmissionError):
        engine.launch(profile, {}, **args)
    with pytest.raises(admission.AdmissionError):
        runtime.cancel("cancel-r", "other")
    runtime.cancel("cancel-r", "owner")
    runtime.worker_run(launches[0])
    assert not create.called
    with pytest.raises(admission.AdmissionError):
        engine.launch(profile, {}, **args)
    assert len(launches) == 1


@pytest.mark.parametrize("qualified", [False, True])
@pytest.mark.parametrize("operation", ["destroy", "update", "exec_command", "exec_stream", "exec_interactive", "file_push", "file_pull", "check_readiness"])
def test_all_mutators_refuse_while_worker_owns_launch(collected, monkeypatch, operation, qualified):
    runtime, profile, _, create, _ = collected
    entered, release = Event(), Event()
    original = engine._write_env

    def pause(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "_write_env", pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(engine.launch, profile, {}, name="busy", request_id="busy-r", owner_root="owner", offline=True, collect=True)
        assert entered.wait(5)
        try:
            args = {"destroy": (), "update": ({},), "exec_command": (["true"],), "exec_stream": (["true"],),
                    "exec_interactive": (), "file_push": (["unused"], "/unused"),
                    "file_pull": (["/unused"], "unused"), "check_readiness": ()}[operation]
            with pytest.raises(admission.AdmissionError) as error:
                getattr(engine, operation)("local:busy" if qualified else "busy", *args)
            assert error.value.code == "admission_busy"
        finally:
            release.set()
        task.result(timeout=5)
    assert create.call_count == 1


@pytest.mark.parametrize("reason", ["oversize", "missing-image", "capacity"])
def test_collection_preflight_does_not_consume_attempt(collected, monkeypatch, reason):
    _, profile, _, create, launches = collected
    variables = {}
    options = {}
    if reason == "oversize":
        variables["unused"] = "x" * 300000
    elif reason == "missing-image":
        monkeypatch.setattr(incus, "local_image_available", lambda _: False)
    else:
        options["min_free_bytes"] = 200 * 1024**3
    with pytest.raises(admission.AdmissionError):
        engine.launch(profile, variables, name="preflight", request_id="preflight-r", owner_root="owner",
                      offline=True, collect=True, **options)
    assert not create.called and not launches
    root = admission.state_directory(admission.context())
    assert not list(root.glob("collectors/*.json"))


def test_worker_dispatch_is_outside_parent_custody(collected, monkeypatch):
    from contextvars import Context
    runtime, profile, _, _, launches = collected

    def spawn(payload):
        launches.append(payload)
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(Context().run, runtime.worker_run, payload).result(timeout=5)

    monkeypatch.setattr(runtime, "spawn_worker", spawn)
    assert engine.launch(profile, {}, name="handoff", request_id="handoff-r", owner_root="owner",
                         offline=True, collect=True)["status"] == "running"
    assert len(launches) == 1


def test_keyword_mutator_argument_contract(monkeypatch, tmp_path):
    from amplifier_bundle_digital_twin_universe import lifecycle
    from contextlib import contextmanager
    monkeypatch.setenv("AMPLIFIER_DTU_ADMISSION_DIR", str(tmp_path / "keyword"))
    calls = []

    @contextmanager
    def observe(name):
        calls.append(name)
        yield

    monkeypatch.setattr(lifecycle, "mutation", observe)

    @lifecycle.guarded
    def example(container_id, command):
        return container_id, command

    assert example(container_id="same", command=["true"]) == ("same", ["true"])
    assert calls == ["same"]


def test_collected_status_no_private_bootstrap_or_operation_socket(collected, monkeypatch):
    runtime, profile, _, _, launches = collected
    monkeypatch.setattr(runtime, "spawn_worker", launches.append)
    with pytest.raises(admission.AdmissionError):
        engine.launch(profile, {}, name="private", request_id="private-r", owner_root="owner",
                      offline=True, collect=True, wait_seconds=0)
    status = engine.launch_status("private-r")
    assert status["state"] == "unknown" and status["collector_phase"] == "dispatched"
    assert not {"backend", "runtime", "profile_text", "variables", "create_operation"} & status.keys()


def test_collected_provision_failure_never_calls_ambient_docker(collected, monkeypatch):
    runtime, profile, instances, _, _ = collected
    docker_calls = []
    monkeypatch.setattr(engine, "_stop_mock_containers", lambda name: docker_calls.append(name))
    monkeypatch.setattr(engine, "_write_env", lambda *args: (_ for _ in ()).throw(RuntimeError("provision failed")))
    monkeypatch.setattr(incus, "delete_container", lambda *args, **kwargs: instances.clear())
    with pytest.raises(admission.AdmissionError):
        engine.launch(profile, {}, name="failure", request_id="failure-r", owner_root="owner", offline=True, collect=True)
    assert not docker_calls and not instances


def test_outcome_generation_cannot_claim_false_nonadmission(collected, monkeypatch):
    runtime, profile, _, _, launches = collected
    monkeypatch.setattr(runtime, "spawn_worker", launches.append)
    with pytest.raises(admission.AdmissionError):
        engine.launch(profile, {}, name="outcome", request_id="outcome-r", owner_root="owner",
                      offline=True, collect=True, wait_seconds=0)
    root, payload = runtime.locate("outcome-r")
    path = runtime._input_path(root, "outcome-r").with_suffix(".outcome")
    admission._save_private_json(path, {"generation": "0" * 32, "code": "approved_image_missing"})
    before = path.read_bytes()
    with pytest.raises(runtime.BackendFence):
        engine.launch_status("outcome-r")
    assert path.read_bytes() == before
    admission._save_private_json(path, {"generation": payload["generation"], "code": "cancel_requested"})
    assert engine.launch_status("outcome-r")["cancellation_requested"] is True
    assert engine.launch_status("outcome-r")["state"] == "unknown"


def test_completion_is_not_current_ready_after_owner_changes(collected, monkeypatch):
    runtime, profile, instances, _, _ = collected

    def spawn(payload):
        runtime.worker_run(payload)
        instances[0]["config"]["user.dtu.owner-root"] = "other"

    monkeypatch.setattr(runtime, "spawn_worker", spawn)
    with pytest.raises(admission.AdmissionError):
        engine.launch(profile, {}, name="changed", request_id="changed-r", owner_root="owner",
                      offline=True, collect=True)
    assert engine.launch_status("changed-r")["state"] == "conflict"


def test_no_marker_mutator_cannot_race_first_collected_admission(collected):
    from amplifier_bundle_digital_twin_universe import lifecycle
    _, profile, _, create, launches = collected
    with lifecycle.mutation("racing"):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(engine.launch, profile, {}, name="racing", request_id="racing-r",
                                 owner_root="owner", offline=True, collect=True)
            with pytest.raises(admission.AdmissionError) as error:
                future.result(timeout=5)
            assert error.value.code == "admission_busy"
    assert not create.called and not launches


def test_cold_worker_import_executes_only_the_verified_bytes(tmp_path):
    import hashlib
    import importlib.util
    from amplifier_bundle_digital_twin_universe.collector_worker import VerifiedPackage
    source = tmp_path / "engine.py"
    source.write_text("value = 'original'\n")
    loader = VerifiedPackage(tmp_path, {"engine.py": hashlib.sha256(source.read_bytes()).hexdigest()})
    spec = loader.find_spec("amplifier_bundle_digital_twin_universe.engine")
    module = importlib.util.module_from_spec(spec)
    source.write_text("raise RuntimeError('unverified code ran')\n")
    with pytest.raises(ImportError, match="changed before import"):
        loader.exec_module(module)
    assert "value" not in module.__dict__


def test_worker_native_spawn_requires_exact_one_use_owned_constructor(monkeypatch, tmp_path):
    """Exercise audit decisions without dispatching a process or installing a hook."""
    import json
    import signal
    from types import SimpleNamespace
    from amplifier_bundle_digital_twin_universe import worker_guard

    hooks, effects = [], []
    environment = {"PATH": "/approved"}
    executable = "/approved/incus"
    monkeypatch.setattr(worker_guard.sys, "addaudithook", hooks.append)
    monkeypatch.setattr(worker_guard, "_environment", lambda record: environment)
    for name in ("raise_signal", "pthread_kill", "pidfd_send_signal"):
        if hasattr(signal, name):
            monkeypatch.setattr(signal, name, getattr(signal, name))

    class FakePopen:
        def __init__(self, argv, env):
            hooks[0]("subprocess.Popen", (argv[0], argv, None, env))
            self._posix_spawn(argv, argv[0], env)

        def _posix_spawn(self, argv, program, env):
            hooks[0]("os.posix_spawn", (program, argv, env))
            # A second event must not reuse this native constructor permit.
            with pytest.raises(worker_guard.BackendFence):
                hooks[0]("os.posix_spawn", (program, argv, env))
            effects.append("owned native construction")

    monkeypatch.setattr(worker_guard.subprocess, "Popen", FakePopen)
    retain = worker_guard.install(SimpleNamespace(as_dict=lambda: {
        "executable": {"path": executable}, "socket": "/approved/incus.sock"
    }), tmp_path / "guard.json")
    event = (executable, [executable, "query", "/1.0"], environment)
    with pytest.raises(worker_guard.BackendFence):
        hooks[0]("os.posix_spawn", event)
    worker_guard.subprocess.Popen(event[1], env=environment)
    with pytest.raises(worker_guard.BackendFence):
        hooks[0]("os.posix_spawn", event)
    with pytest.raises(worker_guard.BackendFence):
        worker_guard.subprocess.Popen(["/foreign/incus"], env=environment)
    retain()
    recorded = json.loads((tmp_path / "guard.json").read_text())
    assert effects == ["owned native construction"]
    assert recorded["allowed_counts"] == {"subprocess.Popen": 1, "os.posix_spawn": 1}
    assert recorded["denied"] == ["os.posix_spawn"] * 3 + ["subprocess.Popen"]


def test_lost_spawn_ack_retains_original_generation_without_second_child(collected, monkeypatch):
    """A returned synthetic child precedes the failed durable spawn acknowledgement."""
    runtime, profile, _, create, _ = collected
    calls, reaped = [], Event()

    class Child:
        pid = 12345

        def wait(self):
            reaped.set()
            return 0

    monkeypatch.setattr(runtime, "spawn_worker", real_spawn_worker)
    real_save = admission._save_private_json

    def fail_ack(path, value):
        if path.suffix == ".dispatch":
            raise OSError("synthetic spawn acknowledgement loss")
        return real_save(path, value)

    def fake_spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        return Child()

    monkeypatch.setattr(runtime.subprocess, "Popen", fake_spawn)
    monkeypatch.setattr(admission, "_save_private_json", fail_ack)
    with pytest.raises(OSError, match="acknowledgement"):
        engine.launch(profile, {}, name="lost-ack", request_id="lost-ack-r",
                      owner_root="owner", offline=True, collect=True)
    root, payload = runtime.locate("lost-ack-r")
    state = runtime.collector.AttemptStore(root, payload["binding"])
    assert state.observe()["phase"] == "dispatched"
    assert len(calls) == 1 and not create.called and reaped.wait(timeout=2)
    with pytest.raises(admission.AdmissionError) as error:
        engine.launch(profile, {}, name="lost-ack", request_id="lost-ack-r",
                      owner_root="owner", offline=True, collect=True)
    assert error.value.code == "launch_unknown"
    assert len(calls) == 1 and not create.called
    runtime.worker_run(payload)
    assert state.observe()["phase"] == "completed" and create.call_count == 1
    assert engine.launch(profile, {}, name="lost-ack", request_id="lost-ack-r",
                         owner_root="owner", offline=True, collect=True)["reused"]
    assert len(calls) == 1 and create.call_count == 1
