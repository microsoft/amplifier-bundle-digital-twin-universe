"""Future collected attempts retain acknowledgment stages, never replay authority."""
# ruff: noqa: F811
# Imported pytest fixtures are registered by name and intentionally requested
# as test parameters; the parameters do not replace the module-level fixture.
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from amplifier_bundle_digital_twin_universe import admission, engine, incus
from .test_admission import launch_infra  # noqa: F401
from .test_collected_launch import collected  # noqa: F401

STEPS = ["creation_returned", "provisioning_entered", "provisioning_completed",
         "checkpoint_saved", "ready_marker_returned", "final_receipt_saved"]


def launch(data):
    return engine.launch(data[1], {}, name="observation-case", request_id="observation-request",
                         owner_root="observation-owner", offline=True, collect=True)


def record(data):
    runtime = data[0]
    root, payload = runtime.locate("observation-request")
    return runtime._input_path(root, "observation-request").with_suffix(".observations"), payload


def provision_counter(data, monkeypatch):
    path = Path(data[1])
    path.write_text(path.read_text() + "provision:\n  setup_cmds:\n    - observation-fixture-step\n")
    executed = MagicMock(return_value=(0, "", ""))
    monkeypatch.setattr(incus, "exec_command", executed)
    return executed


def test_success_has_distinct_acknowledgments_and_read_only_projection(collected):
    result = launch(collected)
    path, payload = record(collected)
    before = path.read_bytes()
    value = json.loads(before)
    assert value["steps"] == STEPS
    assert value["creation_transport"] == "cli"
    assert value["binding"] == payload["binding"] and value["generation"] == payload["generation"]
    assert value["revision"] == 7 and value["result_digest"]
    status = engine.launch_status("observation-request")
    assert status["observations"] == {
        "available": True, "revision": 7, "acknowledged": STEPS,
        "creation_transport": "cli", "all_acknowledged": True,
    }
    assert status["result"] == result
    assert path.read_bytes() == before
    assert launch(collected)["reused"] and collected[3].call_count == 1


@pytest.mark.parametrize("stage", STEPS)
def test_observation_failure_never_triggers_cleanup_or_replay(collected, monkeypatch, stage):
    provisioning = provision_counter(collected, monkeypatch)
    save = admission._save_private_json
    deleted, mocks = MagicMock(), MagicMock()
    monkeypatch.setattr(incus, "delete_container", deleted)
    monkeypatch.setattr(engine, "_stop_mock_containers", mocks)

    def fail(path, value):
        if path.suffix == ".observations" and value["steps"] and value["steps"][-1] == stage:
            raise OSError("sentinel-secret-storage-error")
        save(path, value)

    monkeypatch.setattr(admission, "_save_private_json", fail)
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    deleted.assert_not_called()
    mocks.assert_not_called()
    assert collected[3].call_count == 1
    expected_provisions = 0 if stage in STEPS[:2] else 1
    assert provisioning.call_count == expected_provisions
    path, _ = record(collected)
    value = json.loads(path.read_bytes())
    assert value["steps"] == STEPS[:STEPS.index(stage)]
    before = path.read_bytes()
    monkeypatch.setattr(admission, "_save_private_json", save)
    status = engine.launch_status("observation-request")
    assert status["observations"]["acknowledged"] == value["steps"]
    assert "sentinel-secret" not in json.dumps(status)
    # A ready receipt can be reused; an unresolved attempt refuses. Neither
    # outcome permits another child, creation or provisioning invocation.
    launches = len(collected[4])
    try:
        launch(collected)
    except admission.AdmissionError:
        pass
    assert collected[3].call_count == 1 and len(collected[4]) == launches
    assert provisioning.call_count == expected_provisions
    assert path.read_bytes() == before


def test_existing_collector_without_observations_is_not_backfilled(collected):
    launch(collected)
    path, _ = record(collected)
    path.unlink()  # Model a v1 historical record, not a production repair.
    files = {p: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
    status = engine.launch_status("observation-request")
    assert status["observations"] == {"available": False}
    assert not path.exists()
    assert {p: p.read_bytes() for p in files} == files


@pytest.mark.parametrize("mutation", ["generation", "binding", "version", "steps", "revision", "extra", "duplicate"])
def test_malformed_observation_refuses_without_rewrite(collected, mutation):
    launch(collected)
    path, _ = record(collected)
    value = json.loads(path.read_bytes())
    if mutation == "generation":
        value["generation"] = "0" * 32
    elif mutation == "binding":
        value["binding"]["owner_root"] = "different"
    elif mutation == "version":
        value["version"] = True
    elif mutation == "steps":
        value["steps"] = ["final_receipt_saved"]
    elif mutation == "revision":
        value["revision"] = True
    elif mutation == "extra":
        value["extra"] = "private-sentinel"
    raw = json.dumps(value)
    if mutation == "duplicate":
        raw = raw[:-1] + ', "version": 1}'
    path.write_text(raw)
    before = path.read_bytes()
    with pytest.raises(admission.AdmissionError):
        engine.launch_status("observation-request")
    assert path.read_bytes() == before


def test_marker_landed_but_failed_call_has_no_acknowledgment(collected, monkeypatch):
    original = incus.set_config

    def apply_then_fail(name, key, value):
        original(name, key, value)
        if key == "user.dtu.launch-state" and value == "ready":
            raise OSError("marker acknowledgement lost")

    monkeypatch.setattr(incus, "set_config", apply_then_fail)
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    path, _ = record(collected)
    before = path.read_bytes()
    status = engine.launch_status("observation-request")
    assert status["state"] == "ready" and status["result_recovered"]
    assert status["observations"]["acknowledged"] == STEPS[:4]
    assert status["observations"]["all_acknowledged"] is False
    assert path.read_bytes() == before


def test_observation_path_stays_inside_collector_directory(collected):
    launch(collected)
    path, payload = record(collected)
    assert path.parent == Path(payload["root"]) / "collectors"
    assert not list(Path(payload["root"]).glob("*.observations"))


@pytest.mark.parametrize("stage", STEPS)
@pytest.mark.parametrize("boundary", ["file", "directory"])
def test_real_fsync_failure_stops_without_cleanup(collected, monkeypatch, stage, boundary):
    save, sync = admission._save_private_json, admission.os.fsync
    deleted = MagicMock()
    monkeypatch.setattr(incus, "delete_container", deleted)
    calls = []

    def write(path, value):
        if path.suffix != ".observations" or not value["steps"] or value["steps"][-1] != stage:
            return save(path, value)
        def fail(fd):
            calls.append(fd)
            if len(calls) == (1 if boundary == "file" else 2):
                raise OSError("injected fsync failure")
            return sync(fd)
        with monkeypatch.context() as local:
            local.setattr(admission.os, "fsync", fail)
            return save(path, value)

    monkeypatch.setattr(admission, "_save_private_json", write)
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    assert len(calls) == (1 if boundary == "file" else 2)
    assert collected[3].call_count == 1
    deleted.assert_not_called()
    path, _ = record(collected)
    before = path.read_bytes()
    expected = STEPS[:STEPS.index(stage) + (boundary == "directory")]
    assert json.loads(before)["steps"] == expected
    # A visible post-replace observation records an acknowledgment of the
    # preceding action, not proof that the observation's own fsync succeeded.
    assert engine.launch_status("observation-request")["observations"]["acknowledged"] == expected
    assert path.read_bytes() == before


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "permissions", "oversize"])
def test_private_observation_file_safety(collected, unsafe, tmp_path):
    import os
    launch(collected)
    path, _ = record(collected)
    if unsafe == "symlink":
        target = tmp_path / "original"
        path.rename(target)
        path.symlink_to(target)
    elif unsafe == "hardlink":
        os.link(path, tmp_path / "another-link")
    elif unsafe == "permissions":
        path.chmod(0o644)
    else:
        path.write_text(" " * 32769)
    before = path.read_bytes()
    with pytest.raises(admission.AdmissionError):
        engine.launch_status("observation-request")
    assert path.read_bytes() == before


def test_observation_initialization_failure_dispatches_nothing(collected, monkeypatch):
    save = admission._save_private_json
    def fail(path, value):
        if path.suffix == ".observations":
            raise OSError("initialization failure")
        save(path, value)
    monkeypatch.setattr(admission, "_save_private_json", fail)
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    assert not collected[4] and not collected[3].called
    status = engine.launch_status("observation-request")
    assert status["state"] == "unknown" and status["collector_phase"] == "prepared"
    assert status["observations"] == {"available": False}
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    assert not collected[4] and not collected[3].called


def test_recorder_refuses_stale_revision_without_overwrite(collected, monkeypatch):
    from amplifier_bundle_digital_twin_universe import collector_observation as observation
    advance = observation.Recorder.mark
    intercepted = []
    def stale(self, step, **kwargs):
        if step == "provisioning_entered":
            path = self.store.path.with_suffix(".observations")
            value = json.loads(path.read_bytes())
            value["steps"].append(step)
            value["revision"] += 1
            admission._save_private_json(path, value)
            intercepted.append(path.read_bytes())
        return advance(self, step, **kwargs)
    monkeypatch.setattr(observation.Recorder, "mark", stale)
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    path, _ = record(collected)
    assert intercepted == [path.read_bytes()]
    assert collected[3].call_count == 1


def test_operation_completion_is_not_reported_as_cli_completion(collected, monkeypatch):
    create = collected[3]
    native_create = create.side_effect
    def submit(endpoint, name, image, config, pool):
        native_create(name, image, config)
        return {"status_code": 200}
    monkeypatch.setattr(engine.operations, "local_endpoint", lambda ctx: {"synthetic": True})
    submitted = MagicMock(side_effect=submit)
    monkeypatch.setattr(engine.operations, "submit", submitted)
    monkeypatch.setattr(engine, "_wait_for_creation", lambda *args: None)
    launch(collected)
    assert submitted.call_count == 1 and create.call_count == 0
    status = engine.launch_status("observation-request")
    assert status["observations"]["creation_transport"] == "operation"
    assert status["observations"]["acknowledged"] == STEPS


def test_operation_wait_timeout_has_no_success_ack_or_provisioning(collected, monkeypatch):
    provisioning = provision_counter(collected, monkeypatch)
    deleted = MagicMock()
    submitted = MagicMock(return_value={"status_code": 103})
    monkeypatch.setattr(incus, "delete_container", deleted)
    monkeypatch.setattr(engine.operations, "local_endpoint", lambda ctx: {"synthetic": True})
    monkeypatch.setattr(engine.operations, "submit", submitted)
    monkeypatch.setattr(engine, "_wait_for_creation", MagicMock(side_effect=TimeoutError("wait expired")))
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    path, _ = record(collected)
    before = path.read_bytes()
    assert engine.launch_status("observation-request")["observations"]["acknowledged"] == []
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    assert submitted.call_count == 1
    assert not provisioning.called and not deleted.called and not collected[3].called
    assert path.read_bytes() == before


@pytest.mark.parametrize("change, expected", [
    ("stopped", "stopped"), ("absent", "removed"), ("foreign", "conflict"),
    ("failed", "failed"), ("removed", "removed"),
])
def test_complete_acknowledgments_do_not_promote_current_state(collected, change, expected):
    launch(collected)
    path, payload = record(collected)
    root = Path(payload["root"])
    receipt_path = admission.receipt_path(root, "observation-request")
    if change == "stopped":
        collected[2][0]["status"] = "Stopped"
    elif change == "absent":
        collected[2].clear()
    elif change == "foreign":
        collected[2][0]["config"]["user.dtu.owner-root"] = "foreign"
    else:
        value = admission.read_receipt(root, "observation-request")
        value["state"] = change
        admission.save_receipt(root, value)
    before = path.read_bytes(), receipt_path.read_bytes()
    status = engine.launch_status("observation-request")
    assert status["state"] == expected
    assert status["observations"]["all_acknowledged"] is True
    assert (path.read_bytes(), receipt_path.read_bytes()) == before


def test_creation_timeout_retains_no_invented_success_acknowledgment(collected):
    collected[3].side_effect = TimeoutError("private backend diagnostic")
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    status = engine.launch_status("observation-request")
    assert status["state"] == "unknown"
    assert status["observations"]["acknowledged"] == []
    assert status["observations"]["creation_transport"] is None
    assert "private backend diagnostic" not in json.dumps(status)


def test_genuine_provision_failure_keeps_cleanup_and_partial_evidence(collected, monkeypatch):
    deleted = MagicMock(side_effect=lambda *a, **k: collected[2].clear())
    monkeypatch.setattr(incus, "delete_container", deleted)
    monkeypatch.setattr(engine, "_write_env", MagicMock(side_effect=RuntimeError("provision failed")))
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    assert deleted.call_count == 1
    status = engine.launch_status("observation-request")
    assert status["state"] == "failed"
    assert status["observations"]["acknowledged"] == STEPS[:2]
    assert not status["observations"]["all_acknowledged"]


def test_cancel_after_last_provision_command_keeps_returned_acknowledgments(collected, monkeypatch):
    provisioning = provision_counter(collected, monkeypatch)
    runtime = collected[0]
    execute = engine._run_provisioning
    set_config = incus.set_config
    deleted = MagicMock()

    def cancel_after_command(*args, **kwargs):
        execute(*args, **kwargs)
        runtime.cancel("observation-request", "observation-owner")

    def guarded_marker(*args):
        runtime.check_cancel()
        set_config(*args)

    monkeypatch.setattr(engine, "_run_provisioning", cancel_after_command)
    monkeypatch.setattr(incus, "set_config", guarded_marker)
    monkeypatch.setattr(incus, "delete_container", deleted)
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    status = engine.launch_status("observation-request")
    assert status["state"] == "unknown" and "result" not in status
    assert status["cancellation_requested"]
    assert status["observations"]["acknowledged"] == STEPS[:4]
    path, payload = record(collected)
    assert runtime._outcome(Path(payload["root"]), "observation-request") == "cancel_requested"
    before = path.read_bytes()
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    assert path.read_bytes() == before and collected[3].call_count == 1
    assert provisioning.call_count == 1
    deleted.assert_not_called()
