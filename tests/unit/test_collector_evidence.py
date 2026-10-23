"""Observed future failure facts never authorize cleanup, replay or settlement."""
# ruff: noqa: F811
import hashlib
import json
import subprocess
from unittest.mock import MagicMock

import pytest

from amplifier_bundle_digital_twin_universe import admission, engine, incus
from amplifier_bundle_digital_twin_universe.incus import create_container as native_create_container
from .test_admission import launch_infra  # noqa: F401
from .test_collected_launch import collected  # noqa: F401
from .test_collector_observations import launch, provision_counter, record
from .test_collector_custody import attempt  # noqa: F401


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


@pytest.mark.parametrize("code", [2, 127, -9])
def test_native_cli_nonzero_retains_numeric_code_without_diagnostics(collected, monkeypatch, code):
    # Exercise the real create wrapper; only its transport is mocked.
    monkeypatch.setattr(incus, "create_container", native_create_container)
    monkeypatch.setattr(incus, "_run", lambda *a, **k: subprocess.CompletedProcess(a, code, "", "SECRET-error"))
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    facts = engine.launch_status("observation-request")["failure_evidence"]
    assert facts["cli"] == {"outcome": "nonzero", "returncode": code}
    assert facts["operation"] is None and facts["primary"]["category"] == "cli_nonzero"
    assert "SECRET" not in json.dumps(facts)


@pytest.mark.parametrize("stage", ["checkpoint", "ready_marker", "final_receipt", "observations"])
def test_publication_errors_are_not_provision_failures(collected, monkeypatch, stage):
    counted = provision_counter(collected, monkeypatch)
    save, marker = admission.save_receipt, incus.set_config
    deleted = MagicMock()
    monkeypatch.setattr(incus, "delete_container", deleted)
    if stage in {"checkpoint", "final_receipt"}:
        def fail(root, value):
            if ("prepared_result" in value if stage == "checkpoint" else value["state"] == "ready"):
                raise OSError("SECRET-publication")
            return save(root, value)
        monkeypatch.setattr(admission, "save_receipt", fail)
    elif stage == "ready_marker":
        def fail(*args):
            if args[1:] == ("user.dtu.launch-state", "ready"):
                raise OSError("SECRET-marker")
            return marker(*args)
        monkeypatch.setattr(incus, "set_config", fail)
    else:
        from amplifier_bundle_digital_twin_universe import collector_observation as observation
        mark = observation.Recorder.mark
        def fail(self, step, **kwargs):
            if step == "provisioning_completed":
                raise observation.ObservationError()
            return mark(self, step, **kwargs)
        monkeypatch.setattr(observation.Recorder, "mark", fail)
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    facts = engine.launch_status("observation-request")["failure_evidence"]
    assert facts["primary"] == {"stage": stage, "category": "observation_error" if stage == "observations" else "os_error"}
    assert facts["cleanup"] is None and counted.call_count == 1
    deleted.assert_not_called()


@pytest.mark.parametrize("mutation", ["version", "revision", "binding", "generation", "extra", "duplicate", "baseline", "oversize"])
def test_evidence_refuses_malformed_without_rewrite(collected, mutation):
    launch(collected)
    path, _ = evidence(collected)
    value = json.loads(path.read_bytes())
    if mutation == "version":
        value["version"] = True
    elif mutation == "revision":
        value["revision"] = False
    elif mutation == "binding":
        value["binding"]["owner_root"] = "other"
    elif mutation == "generation":
        value["generation"] = "0" * 32
    elif mutation == "extra":
        value["unexpected"] = 1
    elif mutation == "baseline":
        value["baselines"]["collector"]["bytes_hex"] = "00"
    elif mutation == "oversize":
        value["baselines"]["collector"]["bytes_hex"] = "00" * 32768
    raw = json.dumps(value)
    if mutation == "duplicate":
        raw = raw[:-1] + ', "version": 1}'
    path.write_text(raw)
    before = path.read_bytes()
    with pytest.raises(admission.AdmissionError):
        engine.launch_status("observation-request")
    assert path.read_bytes() == before


@pytest.mark.parametrize("boundary", ["reservation", "cli", "primary"])
@pytest.mark.parametrize("which", ["file", "directory"])
def test_evidence_fsync_failure_no_delete_or_replay(collected, monkeypatch, boundary, which):
    import os
    import stat
    save, fsync = admission._save_private_json, os.fsync
    deleted = MagicMock()
    counted = provision_counter(collected, monkeypatch)
    monkeypatch.setattr(incus, "delete_container", deleted)
    primary = incus.IncusError("SECRET-primary")
    if boundary == "primary":
        collected[3].side_effect = primary
    armed = [False]
    failed = [False]
    def fault(fd):
        isdir = stat.S_ISDIR(os.fstat(fd).st_mode)
        if armed[0] and not failed[0] and isdir == (which == "directory"):
            failed[0] = True
            raise OSError("SECRET-fsync")
        return fsync(fd)
    def intercept(path, value):
        selected = path.suffix == ".evidence" and (
            value["baselines"]["reservation"] is not None if boundary == "reservation"
            else value[boundary] is not None)
        armed[0] = selected
        try:
            return save(path, value)
        finally:
            armed[0] = False
    monkeypatch.setattr(os, "fsync", fault)
    monkeypatch.setattr(admission, "_save_private_json", intercept)
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    assert failed[0] and counted.call_count == 0
    deleted.assert_not_called()
    creates = collected[3].call_count
    assert creates == (0 if boundary == "reservation" else 1)
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    assert collected[3].call_count == creates and len(collected[4]) == 1


def test_stale_evidence_handles_cannot_overwrite(attempt):
    from amplifier_bundle_digital_twin_universe import collector_evidence as ev
    _, store, _, _, reserve, _ = attempt
    generation = store.prepare()
    ev.initialize(store, generation)
    store.dispatch_once(generation, lambda _: None)
    with pytest.raises(RuntimeError):
        with store.owned_worker(generation):
            first, stale = ev.Recorder(store, generation), ev.Recorder(store, generation)
            reserve()
            first.baseline()
            before = store.path.with_suffix(".evidence").read_bytes()
            with pytest.raises(ev.EvidenceError):
                stale.baseline()
            assert store.path.with_suffix(".evidence").read_bytes() == before
            raise RuntimeError("stop fixture")


@pytest.mark.parametrize("initial,terminal", [(200, None), (400, None), (401, None), (103, 200), (103, 400), (103, 401)])
def test_tracked_terminal_provenance_is_not_cli_failure(collected, monkeypatch, initial, terminal):
    native = collected[3].side_effect
    def submit(endpoint, name, image, config, pool):
        native(name, image, config)
        return {"id": "11111111-1111-4111-8111-111111111111", "status_code": initial,
                "daemon": "b" * 64, "project": "default", "name": name,
                "request_id": config["user.dtu.request-id"], "owner_root": config["user.dtu.owner-root"]}
    monkeypatch.setattr(engine.operations, "local_endpoint", lambda ctx: {"synthetic": True})
    submitted = MagicMock(side_effect=submit)
    monkeypatch.setattr(engine.operations, "submit", submitted)
    observed = MagicMock(return_value={"status_code": terminal, "id": "11111111-1111-4111-8111-111111111111"})
    monkeypatch.setattr(engine.operations, "observe", observed)
    if (terminal or initial) == 200:
        launch(collected)
    else:
        with pytest.raises(admission.AdmissionError):
            launch(collected)
    facts = engine.launch_status("observation-request")["failure_evidence"]
    assert facts["cli"] is None
    assert facts["operation"] == {"id": "11111111-1111-4111-8111-111111111111", "initial_status": initial,
                                  "terminal_status": terminal or initial, "terminal_source": "observe" if terminal else "submit"}
    assert submitted.call_count == 1 and observed.call_count == (1 if terminal else 0)
    assert facts["cleanup"] is None


def test_observation_before_operation_receipt_failure_is_retained(collected, monkeypatch):
    op = {"id": "11111111-1111-4111-8111-111111111111", "status_code": 400,
          "daemon": "b" * 64, "project": "default", "name": "observation-case",
          "request_id": "observation-request", "owner_root": "observation-owner"}
    monkeypatch.setattr(engine.operations, "local_endpoint", lambda ctx: {"synthetic": True})
    monkeypatch.setattr(engine.operations, "submit", MagicMock(return_value=op))
    save = admission.save_receipt
    def fail(root, value):
        if "create_operation" in value:
            raise OSError("SECRET-operation-save")
        return save(root, value)
    monkeypatch.setattr(admission, "save_receipt", fail)
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    facts = engine.launch_status("observation-request")["failure_evidence"]
    assert facts["operation"]["terminal_status"] == 400
    assert facts["primary"] == {"stage": "operation_receipt", "category": "os_error"}
    assert facts["cleanup"] is None and facts["cli"] is None


def test_secondary_record_and_outcome_failures_preserve_primary_exception(collected, monkeypatch):
    primary = incus.IncusError("SECRET-primary")
    collected[3].side_effect = primary
    save = admission._save_private_json
    def fail(path, value):
        if (path.suffix == ".evidence" and value["primary"] is not None
                or path.suffix == ".json" and value.get("phase") == "unknown"
                or path.suffix == ".outcome"):
            raise OSError("SECRET-secondary")
        return save(path, value)
    monkeypatch.setattr(admission, "_save_private_json", fail)
    with pytest.raises(admission.AdmissionError) as caught:
        launch(collected)
    assert caught.value.__cause__ is primary
    assert collected[3].call_count == 1


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode"])
def test_unsafe_evidence_is_refused_without_writes(collected, kind):
    import os
    launch(collected)
    path, _ = evidence(collected)
    raw = path.read_bytes()
    other = path.with_suffix(".original")
    other.write_bytes(raw)
    other.chmod(0o600)
    if kind == "mode":
        path.chmod(0o644)
    else:
        path.unlink()
        if kind == "symlink":
            path.symlink_to(other)
        else:
            os.link(other, path)
    with pytest.raises(admission.AdmissionError):
        engine.launch_status("observation-request")
    assert path.read_bytes() == raw and other.read_bytes() == raw
