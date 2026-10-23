"""Future collected attempts retain acknowledgment stages, never replay authority."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from amplifier_bundle_digital_twin_universe import admission, engine, incus
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
    else:
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
