"""Prepared results survive interrupted publication, without lifecycle replay.

These are mocked interruption and filesystem checks, not process-death or real
Incus fault acceptance. The same fixture guards used by admission tests apply.
"""
import json
from unittest.mock import MagicMock

import pytest

from amplifier_bundle_digital_twin_universe import admission, engine, incus
from .test_admission import launch_infra  # noqa: F401


class Interrupted(BaseException):
    """Model an abrupt foreground interruption without running error cleanup."""


@pytest.fixture
def result_launch(monkeypatch, launch_infra):  # noqa: F811
    profile, observed, create = launch_infra
    deleted = MagicMock()
    mocks_stopped = MagicMock()
    monkeypatch.setattr(incus, "delete_container", deleted)
    monkeypatch.setattr(engine, "_stop_mock_containers", mocks_stopped)
    monkeypatch.setattr(incus, "exec_command", MagicMock(return_value=(0, "", "")))
    monkeypatch.setattr(engine, "_write_env", MagicMock())
    return profile, observed, create, deleted, mocks_stopped


def launch(data):
    return engine.launch(data[0], {}, name="result-case", request_id="result-request", owner_root="result-owner")


def saved():
    root = admission.state_directory(admission.context())
    path = admission.receipt_path(root, "result-request")
    return root, path, admission.read_receipt(root, "result-request")


def interrupt_final(data, monkeypatch):
    save = admission.save_receipt

    def write(root, value):
        if value["state"] == "ready":
            raise Interrupted()
        save(root, value)

    monkeypatch.setattr(admission, "save_receipt", write)
    with pytest.raises(Interrupted):
        launch(data)
    monkeypatch.setattr(admission, "save_receipt", save)
    return saved()


def test_result_is_checkpointed_before_ready_marker(result_launch, monkeypatch):
    set_config = incus.set_config
    checkpoint = []

    def set_value(name, key, value):
        if key == "user.dtu.launch-state" and value == "ready":
            checkpoint.append(saved()[2].get("prepared_result"))
        set_config(name, key, value)

    monkeypatch.setattr(incus, "set_config", set_value)
    result = launch(result_launch)
    assert checkpoint and checkpoint[0] is not None
    assert checkpoint[0]["result"] == result
    assert checkpoint[0]["daemon"] == admission.context()["daemon"]
    assert checkpoint[0]["project"] == admission.context()["project"]
    record = saved()[2]
    assert record["result"] == result and record["state"] == "ready"
    assert "prepared_result" not in record
    result_launch[3].assert_not_called()


@pytest.mark.parametrize("after_marker", [False, True])
def test_interrupted_ready_publication_recovers_only_with_marker(result_launch, monkeypatch, after_marker):
    set_config = incus.set_config

    def stop(name, key, value):
        if key == "user.dtu.launch-state" and value == "ready":
            if after_marker:
                set_config(name, key, value)
            raise Interrupted()
        set_config(name, key, value)

    monkeypatch.setattr(incus, "set_config", stop)
    with pytest.raises(Interrupted):
        launch(result_launch)
    _root, path, record = saved()
    assert "prepared_result" in record
    original = path.read_bytes()
    observed = engine.launch_status("result-request")
    if after_marker:
        assert observed["result"] == record["prepared_result"]["result"]
        assert observed["result_recovered"] is True
        assert observed["state"] == "ready"
    else:
        assert observed["state"] == "unknown"
        assert "result" not in observed
    assert "prepared_result" not in observed
    assert path.read_bytes() == original
    with pytest.raises(admission.AdmissionError):
        launch(result_launch)
    assert result_launch[2].call_count == 1
    result_launch[3].assert_not_called()
    result_launch[4].assert_not_called()


@pytest.mark.parametrize("stage", ["checkpoint", "final", "marker"])
def test_publication_failure_never_deletes_completed_provisioning(result_launch, monkeypatch, stage):
    save, set_config = admission.save_receipt, incus.set_config

    def write(root, value):
        if (stage == "checkpoint" and "prepared_result" in value or
                stage == "final" and value["state"] == "ready"):
            raise OSError("synthetic private-path/token not for public result")
        save(root, value)

    def mark(name, key, value):
        if stage == "marker" and key == "user.dtu.launch-state" and value == "ready":
            raise OSError("marker reply lost")
        set_config(name, key, value)

    monkeypatch.setattr(admission, "save_receipt", write)
    monkeypatch.setattr(incus, "set_config", mark)
    with pytest.raises(admission.AdmissionError) as error:
        launch(result_launch)
    assert error.value.code == "launch_unknown"
    assert "private-path" not in json.dumps(error.value.as_dict())
    result_launch[3].assert_not_called()
    result_launch[4].assert_not_called()
    assert result_launch[2].call_count == 1
    status = engine.launch_status("result-request")
    assert ("result" in status) == (stage == "final")
    assert len(result_launch[1]) == 1


@pytest.mark.parametrize("fsync_number", [1, 2])
def test_checkpoint_fsync_failure_does_not_publish_ready(result_launch, monkeypatch, fsync_number):
    save = admission.save_receipt
    calls = []

    def write(root, value):
        if "prepared_result" not in value:
            return save(root, value)
        real_fsync = admission.os.fsync

        def sync(fd):
            calls.append(fd)
            if len(calls) == fsync_number:
                raise OSError("synthetic fsync interruption")
            return real_fsync(fd)

        with monkeypatch.context() as patch:
            patch.setattr(admission.os, "fsync", sync)
            save(root, value)

    monkeypatch.setattr(admission, "save_receipt", write)
    with pytest.raises(admission.AdmissionError) as error:
        launch(result_launch)
    assert error.value.code == "launch_unknown"
    assert len(calls) == fsync_number
    assert result_launch[1][0]["config"]["user.dtu.launch-state"] == "provisioning"
    assert "result" not in engine.launch_status("result-request")
    result_launch[3].assert_not_called()
    # File-fsync failure precedes replace; directory-fsync failure follows it.
    assert ("prepared_result" in saved()[2]) == (fsync_number == 2)


@pytest.mark.parametrize("field", ["daemon", "project", "intent_digest", "request_id", "owner_root", "name", "created_at", "id"])
def test_result_recovery_rejects_changed_checkpoint_binding(result_launch, monkeypatch, field):
    root, path, record = interrupt_final(result_launch, monkeypatch)
    assert "prepared_result" in record
    target = record["prepared_result"] if field in {"daemon", "project", "intent_digest"} else record["prepared_result"]["result"]
    target[field] = "different"
    admission.save_receipt(root, record)
    original = path.read_bytes()
    status = engine.launch_status("result-request")
    assert "result" not in status and "result_recovered" not in status
    assert "prepared_result" not in status
    assert path.read_bytes() == original
    assert result_launch[2].call_count == 1
    result_launch[3].assert_not_called()


@pytest.mark.parametrize("case", ["absent", "stopped", "wrong-owner", "wrong-request", "not-ready", "ownerless"])
def test_result_recovery_requires_current_complete_ready_identity(result_launch, monkeypatch, case):
    root, path, record = interrupt_final(result_launch, monkeypatch)
    observed = result_launch[1]
    if case == "absent":
        observed.clear()
    elif case == "stopped":
        observed[0]["status"] = "Stopped"
    elif case == "ownerless":
        record["owner_root"] = ""
        record["prepared_result"]["result"]["owner_root"] = ""
        observed[0]["config"]["user.dtu.owner-root"] = ""
        admission.save_receipt(root, record)
    else:
        key, value = {
            "wrong-owner": ("user.dtu.owner-root", "foreign"),
            "wrong-request": ("user.dtu.request-id", "foreign"),
            "not-ready": ("user.dtu.launch-state", "provisioning"),
        }[case]
        observed[0]["config"][key] = value
    original = path.read_bytes()
    status = engine.launch_status("result-request")
    assert "result" not in status and "result_recovered" not in status
    assert path.read_bytes() == original
    assert result_launch[2].call_count == 1
    result_launch[3].assert_not_called()


@pytest.mark.parametrize("state", ["removed", "failed"])
def test_late_result_publication_preserves_terminal_receipt(result_launch, monkeypatch, state):
    set_config = incus.set_config
    terminal_bytes = []

    def mark(name, key, value):
        set_config(name, key, value)
        if key == "user.dtu.launch-state" and value == "ready":
            root, path, current = saved()
            current["state"] = state
            admission.save_receipt(root, current)
            terminal_bytes.append(path.read_bytes())

    monkeypatch.setattr(incus, "set_config", mark)
    with pytest.raises(admission.AdmissionError) as error:
        launch(result_launch)
    assert error.value.code == "launch_unknown"
    assert saved()[1].read_bytes() == terminal_bytes[0]
    status = engine.launch_status("result-request")
    assert status["state"] == state and "result" not in status
    assert saved()[1].read_bytes() == terminal_bytes[0]
    result_launch[3].assert_not_called()


def test_legacy_unknown_without_checkpoint_is_not_rewritten(result_launch, monkeypatch):
    _root, path, record = interrupt_final(result_launch, monkeypatch)
    record.pop("prepared_result", None)
    # Direct legacy fixture bytes, independent of the current writer.
    path.write_text(json.dumps(record))
    original = path.read_bytes()
    status = engine.launch_status("result-request")
    assert status["state"] == "ready" and "result" not in status
    assert "result_recovered" not in status
    assert path.read_bytes() == original
    with pytest.raises(admission.AdmissionError):
        launch(result_launch)
    assert result_launch[2].call_count == 1


@pytest.mark.parametrize("case", ["null", "list", "missing-result", "missing-scope", "result-list", "missing-created"])
def test_malformed_result_checkpoint_is_never_promoted(result_launch, monkeypatch, case):
    root, path, record = interrupt_final(result_launch, monkeypatch)
    checkpoint = record["prepared_result"]
    if case in {"null", "list"}:
        record["prepared_result"] = None if case == "null" else []
    elif case == "missing-result":
        checkpoint.pop("result")
    elif case == "missing-scope":
        checkpoint.pop("daemon")
    elif case == "result-list":
        checkpoint["result"] = []
    else:
        record.pop("created_at")
        checkpoint["result"].pop("created_at")
    admission.save_receipt(root, record)
    original = path.read_bytes()
    status = engine.launch_status("result-request")
    assert "result" not in status and "result_recovered" not in status
    assert "prepared_result" not in status
    assert path.read_bytes() == original


@pytest.mark.parametrize("stage", ["checkpoint", "final"])
@pytest.mark.parametrize("change", ["missing", "binding", "checkpoint", "removed"])
def test_result_publisher_refuses_intervening_receipt(result_launch, monkeypatch, stage, change):
    publish, set_config = engine._publish_launch_result, incus.set_config
    intervening = []
    markers = []

    def change_receipt():
        root, path, current = saved()
        if change == "missing":
            path.unlink()
            intervening.append(None)
            return
        if change == "binding":
            current["intent_digest"] = "foreign-intent"
        elif change == "checkpoint":
            current["prepared_result"] = {"result": "different"}
        else:
            current["state"] = "removed"
        admission.save_receipt(root, current)
        intervening.append(path.read_bytes())

    def begin(ctx, record, result):
        if stage == "checkpoint":
            change_receipt()
        publish(ctx, record, result)

    def mark(name, key, value):
        set_config(name, key, value)
        if key == "user.dtu.launch-state" and value == "ready":
            markers.append(True)
            if stage == "final":
                change_receipt()

    monkeypatch.setattr(engine, "_publish_launch_result", begin)
    monkeypatch.setattr(incus, "set_config", mark)
    with pytest.raises(admission.AdmissionError) as error:
        launch(result_launch)
    assert error.value.code == "launch_unknown"
    path = saved()[1]
    assert (path.read_bytes() if path.exists() else None) == intervening[0]
    assert bool(markers) == (stage == "final")
    result_launch[3].assert_not_called()
    result_launch[4].assert_not_called()


@pytest.mark.parametrize("size", [262144, 262145])
def test_result_receipt_size_bound_preserves_previous_bytes(result_launch, size):
    root = admission.state_directory(admission.context())
    record = {"request_id": "bounded", "state": "unknown"}
    admission.save_receipt(root, record)
    path = admission.receipt_path(root, "bounded")
    before = path.read_bytes()
    record["prepared_result"] = {"result": {"info": [""]}}
    overhead = len(json.dumps(record).encode("utf-8"))
    record["prepared_result"]["result"]["info"] = ["x" * (size - overhead)]
    assert len(json.dumps(record).encode("utf-8")) == size
    if size > 262144:
        with pytest.raises(admission.AdmissionError):
            engine._save_result_receipt(root, record)
        assert path.read_bytes() == before
    else:
        engine._save_result_receipt(root, record)
        assert path.stat().st_size == size
        assert admission.read_receipt(root, "bounded") == record


def test_oversize_checkpoint_does_not_mark_ready_or_delete(result_launch, monkeypatch):
    publish = engine._publish_launch_result
    before = []

    def oversized(ctx, record, result):
        before.append(saved()[1].read_bytes())
        publish(ctx, record, {**result, "info": ["x" * 262144]})

    monkeypatch.setattr(engine, "_publish_launch_result", oversized)
    with pytest.raises(admission.AdmissionError):
        launch(result_launch)
    assert saved()[1].read_bytes() == before[0]
    assert result_launch[1][0]["config"]["user.dtu.launch-state"] == "provisioning"
    result_launch[3].assert_not_called()
    result_launch[4].assert_not_called()


@pytest.mark.parametrize("stage", ["marker", "final-fsync"])
def test_lost_publication_acknowledgement_recovers_without_reprovisioning(result_launch, monkeypatch, stage):
    set_config, save = incus.set_config, admission.save_receipt

    def mark(name, key, value):
        set_config(name, key, value)
        if stage == "marker" and key == "user.dtu.launch-state" and value == "ready":
            raise OSError("reply lost after marker")

    def write(root, value):
        if stage != "final-fsync" or value["state"] != "ready":
            return save(root, value)
        fsync = admission.os.fsync
        calls = []

        def sync(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError("directory fsync failed after replace")
            return fsync(fd)

        with monkeypatch.context() as patch:
            patch.setattr(admission.os, "fsync", sync)
            save(root, value)

    monkeypatch.setattr(incus, "set_config", mark)
    monkeypatch.setattr(admission, "save_receipt", write)
    with pytest.raises(admission.AdmissionError):
        launch(result_launch)
    counts = (result_launch[2].call_count, incus.exec_command.call_count, engine._write_env.call_count)
    before = saved()[1].read_bytes()
    status = engine.launch_status("result-request")
    assert status["state"] == "ready" and status["result"]["id"] == "result-case"
    assert status.get("result_recovered", False) == (stage == "marker")
    if stage == "final-fsync":
        assert launch(result_launch)["reused"] is True
    else:
        with pytest.raises(admission.AdmissionError):
            launch(result_launch)
    assert counts == (result_launch[2].call_count, incus.exec_command.call_count, engine._write_env.call_count)
    assert saved()[1].read_bytes() == before
    result_launch[3].assert_not_called()
    result_launch[4].assert_not_called()


def test_tracked_creation_reconciliation_preserves_prepared_result(result_launch, monkeypatch):
    from amplifier_bundle_digital_twin_universe import operations

    root, _path, record = interrupt_final(result_launch, monkeypatch)
    record["create_operation"] = {
        **admission.context(), "socket": "/synthetic/never-contacted.sock",
        "id": "12345678-1234-1234-1234-123456789abc",
        **{key: record[key] for key in ("name", "request_id", "owner_root")},
    }
    checkpoint = record["prepared_result"]
    admission.save_receipt(root, record)
    observe = MagicMock(return_value={"status_code": 200})
    monkeypatch.setattr(operations, "observe", observe)
    status = engine.launch_status("result-request")
    assert status["result"] == checkpoint["result"] and status["result_recovered"]
    current = saved()[2]
    assert current["state"] == "created"
    assert current["creation_status"] == 200
    assert current["prepared_result"] == checkpoint
    assert "result" not in current
    observe.assert_called_once()
    with pytest.raises(admission.AdmissionError):
        launch(result_launch)
    assert result_launch[2].call_count == 1
