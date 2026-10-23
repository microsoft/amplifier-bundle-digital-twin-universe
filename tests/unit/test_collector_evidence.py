"""Observed future failure facts never authorize cleanup, replay or settlement."""
# ruff: noqa: F811
import hashlib
import json
import subprocess
from unittest.mock import MagicMock

import pytest

from amplifier_bundle_digital_twin_universe import admission, engine, incus
from .test_admission import launch_infra  # noqa: F401
from .test_collected_launch import collected  # noqa: F401
from .test_collector_observations import launch, provision_counter, record


def evidence(data):
    path, payload = record(data)
    return path.with_suffix(".evidence"), payload


def test_future_evidence_preserves_exact_initial_bytes(collected, monkeypatch):
    saved = {}
    real_save = admission._save_private_json

    def save(path, value):
        real_save(path, value)
        if path.suffix == ".json" and value.get("phase") == "prepared":
            saved["collector"] = path.read_bytes()
        if path.suffix == ".json" and value.get("state") == "launching":
            saved.setdefault("reservation", path.read_bytes())

    monkeypatch.setattr(admission, "_save_private_json", save)
    result = launch(collected)
    path, payload = evidence(collected)
    value = json.loads(path.read_bytes())
    assert value["binding"] == payload["binding"] and value["generation"] == payload["generation"]
    for key, raw in saved.items():
        assert bytes.fromhex(value["baselines"][key]["bytes_hex"]) == raw
        assert value["baselines"][key]["sha256"] == hashlib.sha256(raw).hexdigest()
    before = path.read_bytes()
    status = engine.launch_status("observation-request")
    assert status["failure_evidence"]["cli"] == {"outcome": "returned", "returncode": 0}
    assert status["failure_evidence"]["primary"] is None
    assert "baselines" not in status["failure_evidence"]
    assert status["result"] == result and path.read_bytes() == before


@pytest.mark.parametrize("kind", ["timeout", "error"])
def test_cli_failure_is_not_daemon_terminal_evidence(collected, monkeypatch, kind):
    error = subprocess.TimeoutExpired("SECRET-ARGV", 1, output="SECRET-OUTPUT") if kind == "timeout" else incus.IncusError("SECRET-STDERR")
    collected[3].side_effect = error
    deleted = MagicMock()
    monkeypatch.setattr(incus, "delete_container", deleted)
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    status = engine.launch_status("observation-request")
    facts = status["failure_evidence"]
    assert facts["cli"] == {"outcome": kind, "returncode": None}
    assert facts["operation"] is None
    assert facts["primary"] == {"stage": "creation", "category": kind}
    assert status["state"] == "unknown" and "SECRET" not in json.dumps(status)
    deleted.assert_not_called()
    before = evidence(collected)[0].read_bytes()
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    assert collected[3].call_count == 1 and evidence(collected)[0].read_bytes() == before


@pytest.mark.parametrize("delete_fails", [False, True])
def test_provision_failure_and_actual_cleanup_are_separate(collected, monkeypatch, delete_fails):
    counted = provision_counter(collected, monkeypatch)
    counted.side_effect = RuntimeError("SECRET-primary-provision")
    deleted = MagicMock(side_effect=OSError("SECRET-delete") if delete_fails else None)
    monkeypatch.setattr(incus, "delete_container", deleted)
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    facts = engine.launch_status("observation-request")["failure_evidence"]
    assert facts["primary"] == {"stage": "provisioning", "category": "error"}
    assert facts["cleanup"] == {"delete_call": "raised" if delete_fails else "returned",
                                "category": "os_error" if delete_fails else None}
    assert deleted.call_count == 1 and counted.call_count == 1
    assert "SECRET" not in json.dumps(facts)


def test_successful_delete_is_not_relabelled_by_failed_receipt_save(collected, monkeypatch):
    counted = provision_counter(collected, monkeypatch)
    counted.side_effect = RuntimeError("primary-provision")
    deleted = MagicMock()
    monkeypatch.setattr(incus, "delete_container", deleted)
    save = admission.save_receipt

    def fail(root, value):
        if value["state"] == "failed":
            raise OSError("SECRET-receipt-write")
        return save(root, value)

    monkeypatch.setattr(admission, "save_receipt", fail)
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    facts = engine.launch_status("observation-request")["failure_evidence"]
    assert facts["cleanup"] == {"delete_call": "returned", "category": None}
    assert facts["receipt_failure"] == {"stage": "cleanup", "category": "os_error"}
    assert facts["primary"] == {"stage": "provisioning", "category": "error"}
    assert deleted.call_count == 1


def test_historical_missing_evidence_is_not_backfilled(collected):
    launch(collected)
    path, _ = evidence(collected)
    path.unlink()
    before = {p: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
    assert engine.launch_status("observation-request")["failure_evidence"] == {"available": False}
    assert {p: p.read_bytes() for p in before} == before and not path.exists()
