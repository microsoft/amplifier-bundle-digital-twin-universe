"""Public collected launch integration against the existing mocked native engine."""
import importlib
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from amplifier_bundle_digital_twin_universe import admission, engine, incus
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


@pytest.mark.parametrize("operation", ["destroy", "update", "exec_command", "exec_stream", "exec_interactive", "file_push", "file_pull", "check_readiness"])
def test_all_mutators_refuse_while_worker_owns_launch(collected, monkeypatch, operation):
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
                getattr(engine, operation)("busy", *args)
            assert error.value.code == "admission_busy"
        finally:
            release.set()
        task.result(timeout=5)
    assert create.call_count == 1
