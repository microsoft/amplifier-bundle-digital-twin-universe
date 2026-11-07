"""Operational capture safety boundaries; no new output-budget policy.

The deferred unsized-read oracle lives in tests/deferred/operational_capture_contract.py.
It is explicit opt-in, not a default safety test or a bounded-memory claim.
All subprocess/backend effects are fakes. These are not installed/live proofs.
"""
# ruff: noqa: F811
import io
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from amplifier_bundle_digital_twin_universe import admission, engine, incus
from amplifier_bundle_digital_twin_universe.incus import exec_command as native_exec
from .test_admission import launch_infra  # noqa: F401
from .test_collected_launch import collected  # noqa: F401
from .test_collector_observations import launch, record


@contextmanager
def fake_spools(monkeypatch, *, stdout="complete stdout\n", stderr="complete stderr\n",
                returncode=0, read_error=None, require_sized_reads=False,
                fault_channel=0, fault_stage="read"):
    """Two finite streams expose capture behavior without an OS child."""
    streams = []
    calls = []

    class Spool(io.StringIO):
        read_calls = 0
        fault_calls = 0

        def seek(self, *args):
            if self.channel == fault_channel and fault_stage == "seek" and read_error is not None:
                self.fault_calls += 1
                raise read_error
            return super().seek(*args)

        def read(self, size=-1):
            self.read_calls += 1
            if self.channel == fault_channel and fault_stage == "read" and read_error is not None:
                self.fault_calls += 1
                raise read_error
            if require_sized_reads and (size is None or size < 0):
                raise AssertionError("successful provisioning used a whole-output read")
            return super().read(size)

    def temporary_file(*args, **kwargs):
        stream = Spool()
        stream.channel = len(streams)
        streams.append(stream)
        return stream

    def run(command, **kwargs):
        calls.append((command, kwargs.get("timeout")))
        assert kwargs["stdin"] == subprocess.DEVNULL
        kwargs["stdout"].write(stdout)
        kwargs["stderr"].write(stderr)
        return subprocess.CompletedProcess(command, returncode)

    with monkeypatch.context() as patch:
        patch.setattr(incus.tempfile, "TemporaryFile", temporary_file)
        patch.setattr(incus, "_run", run)
        yield calls, streams
    assert len(streams) == 2 and all(stream.closed for stream in streams)


@pytest.mark.parametrize("read_error", [OSError("PRIVATE-capture-read"), UnicodeError("PRIVATE-decode")],
                         ids=["io", "decode"])
@pytest.mark.parametrize("returncode", [0, 37, -9])
def test_collected_capture_fault_after_completion_never_authorizes_delete(collected, monkeypatch, read_error, returncode):
    path = Path(collected[1])
    path.write_text(path.read_text() + "provision:\n  setup_cmds:\n    - fixture-step\n    - must-not-run\n")
    monkeypatch.setattr(incus, "exec_command", native_exec)
    # Deliberately use an output-consuming engine helper at the provisioning
    # boundary. Skipping unused success output in ordinary setup_cmds would be
    # valid, but cannot replace handling a capture error when output IS needed.
    # This fake step does not pretend the offline profile uses _exec_checked.
    consumed = []

    def consume(name, commands):
        for command in commands:
            consumed.append(engine._exec_checked(name, command))

    monkeypatch.setattr(engine, "_run_provisioning", consume)
    deleted = MagicMock()
    monkeypatch.setattr(incus, "delete_container", deleted)
    with fake_spools(monkeypatch, read_error=read_error, returncode=returncode) as (calls, streams):
        with pytest.raises(admission.AdmissionError):
            launch(collected)
        # The known child exit remains evidence, not a complete execution result
        # usable by a caller whose next step depends on the missing output.
        assert calls == [(["incus", "exec", "observation-case", "--", "bash", "-c", "fixture-step"], 600)]
        assert sum(stream.read_calls for stream in streams) == 1 and not consumed
        assert all(stream.closed for stream in streams)
        assert deleted.call_count == 0
        status = engine.launch_status("observation-request")
        assert status["state"] == "unknown" and "result" not in status
        assert "PRIVATE" not in str(status)
        assert status["failure_evidence"]["primary"] == {"stage": "provisioning", "category": "observation_error"}
        assert status["failure_evidence"]["cleanup"] is None
        assert status["failure_evidence"]["cli"] == {"outcome": "returned", "returncode": 0}  # Creation, not exec.
        before = record(collected)[0].with_suffix(".evidence").read_bytes()
        with pytest.raises(admission.AdmissionError):
            launch(collected)
        assert collected[3].call_count == 1 and len(collected[4]) == 1
        assert len(calls) == 1 and deleted.call_count == 0
        assert record(collected)[0].with_suffix(".evidence").read_bytes() == before


@pytest.mark.parametrize("returncode", [0, 37, -9])
@pytest.mark.parametrize("channel", [0, 1], ids=["stdout", "stderr"])
@pytest.mark.parametrize("stage,kind", [("seek", OSError), ("read", OSError), ("read", UnicodeError)],
                         ids=["seek-io", "read-io", "read-decode"])
def test_native_capture_error_preserves_returncode_and_cause(monkeypatch, returncode, channel, stage, kind):
    error = kind("PRIVATE-raw-capture")
    with fake_spools(monkeypatch, returncode=returncode, read_error=error,
                     fault_channel=channel, fault_stage=stage) as (calls, streams):
        with pytest.raises(incus.OutputCaptureError) as raised:
            native_exec("candidate", ["PRIVATE-command"], timeout=123)
        assert raised.value.returncode == returncode and raised.value.__cause__ is error
        assert "PRIVATE" not in str(raised.value)
        assert len(calls) == 1 and sum(s.fault_calls for s in streams) == 1
        assert all(s.closed for s in streams)


def test_complete_nonzero_provisioning_still_cleans_up_once(collected, monkeypatch):
    path = Path(collected[1])
    path.write_text(path.read_text() + "provision:\n  setup_cmds:\n    - fixture-step\n")
    monkeypatch.setattr(incus, "exec_command", native_exec)
    deleted = MagicMock()
    monkeypatch.setattr(incus, "delete_container", deleted)
    with fake_spools(monkeypatch, returncode=37) as (calls, _):
        with pytest.raises(admission.AdmissionError):
            launch(collected)
        facts = engine.launch_status("observation-request")["failure_evidence"]
        assert facts["primary"] == {"stage": "provisioning", "category": "error"}
        assert facts["cleanup"] == {"delete_call": "returned", "category": None}
        assert deleted.call_count == 1 and len(calls) == 1


@pytest.mark.parametrize("secondary", ["evidence", "evidence-and-outcome"])
def test_capture_primary_survives_failure_evidence_write_fault(collected, monkeypatch, secondary):
    path = Path(collected[1])
    path.write_text(path.read_text() + "provision:\n  setup_cmds:\n    - fixture-step\n")
    monkeypatch.setattr(incus, "exec_command", native_exec)
    deleted = MagicMock()
    monkeypatch.setattr(incus, "delete_container", deleted)
    save = admission._save_private_json
    writes = []

    def fail(path, value):
        if ((path.suffix == ".evidence" and value["primary"] is not None)
                or (secondary == "evidence-and-outcome" and path.suffix == ".outcome")):
            writes.append(path.suffix)
            raise OSError("PRIVATE-secondary-write")
        return save(path, value)

    monkeypatch.setattr(admission, "_save_private_json", fail)
    runtime, _, _, create, launches = collected
    monkeypatch.setattr(runtime, "spawn_worker", launches.append)
    with pytest.raises(admission.AdmissionError):
        engine.launch(collected[1], {}, name="observation-case", request_id="observation-request",
                      owner_root="observation-owner", offline=True, collect=True, wait_seconds=0)
    error = OSError("PRIVATE-primary-read")
    seen = []
    original = engine._launch_impl

    def observe(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except incus.OutputCaptureError as exc:
            seen.append(exc)
            raise

    monkeypatch.setattr(engine, "_launch_impl", observe)
    with fake_spools(monkeypatch, read_error=error) as (calls, _):
        if secondary == "evidence-and-outcome":
            with pytest.raises(incus.OutputCaptureError) as raised:
                runtime.worker_run(launches[0])
            assert raised.value is seen[0]
        else:
            assert runtime.worker_run(launches[0]) is None
        assert len(seen) == 1 and seen[0].__cause__ is error and seen[0].returncode == 0
        assert writes.count(".evidence") == 1  # Uncertain recorder write is not retried.
        assert deleted.call_count == 0 and len(calls) == 1
        status = engine.launch_status("observation-request")
        assert status["state"] == "unknown" and "result" not in status
        assert "PRIVATE" not in json.dumps(status)
        with pytest.raises(admission.AdmissionError):
            launch(collected)
        assert create.call_count == 1 and len(launches) == 1 and len(calls) == 1


def test_gateway_capture_error_is_not_retried(monkeypatch):
    error = incus.OutputCaptureError(0)
    gateway = MagicMock(side_effect=error)
    sleep = MagicMock(side_effect=AssertionError("gateway capture fault must not retry"))
    monkeypatch.setattr(incus, "get_host_gateway_ip", gateway)
    monkeypatch.setattr(engine.time, "sleep", sleep)
    with pytest.raises(incus.OutputCaptureError) as raised:
        engine._wait_for_gateway("candidate")
    assert raised.value is error and gateway.call_count == 1 and not sleep.called


def test_snapshot_capture_error_is_not_missing_profile(monkeypatch):
    error = incus.OutputCaptureError(1)
    pull = MagicMock(side_effect=error)
    monkeypatch.setattr(incus, "file_pull", pull)
    with pytest.raises(incus.OutputCaptureError) as raised:
        engine._read_profile_snapshot("candidate")
    assert raised.value is error and pull.call_count == 1


def test_optional_capture_diagnostic_does_not_erase_known_failure(monkeypatch):
    error = incus.OutputCaptureError(0)
    monkeypatch.setattr(incus, "exec_command", lambda *a, **k: (37, "original output", "Could not resolve host"))
    monkeypatch.setattr(incus, "diagnose_network_failure", MagicMock(side_effect=error))
    with pytest.raises(RuntimeError) as raised:
        engine._exec_checked("candidate", "original command")
    assert "exit 37" in str(raised.value) and "original output" in str(raised.value)
    assert raised.value.__cause__ is error
    assert "Network diagnostic unavailable" in raised.value.__notes__


@pytest.mark.parametrize("returncode", [0, 37, -9])
def test_complete_string_exec_contract_is_preserved(monkeypatch, returncode):
    out, err = "output\n\u03bb\x00\n" * 32, "error\n\u6f22\n" * 32
    with fake_spools(monkeypatch, stdout=out, stderr=err, returncode=returncode) as (calls, _):
        assert native_exec("candidate", ["fixture-step"], timeout=123) == (returncode, out, err)
        assert calls == [(["incus", "exec", "candidate", "--", "fixture-step"], 123)]


def test_nonzero_provisioning_retains_original_complete_diagnostics(monkeypatch):
    monkeypatch.setattr(incus, "exec_command", native_exec)
    out, err = "FULL stdout\n\u03bb", "FULL stderr\n\u6f22"
    with fake_spools(monkeypatch, stdout=out, stderr=err, returncode=37) as (calls, _):
        with pytest.raises(RuntimeError) as raised:
            engine._run_provisioning("candidate", ["fixture-step"])
        assert str(raised.value) == f"Provisioning failed (exit 37): fixture-step\nstdout: {out}\nstderr: {err}"
        assert len(calls) == 1


def test_execution_timeout_is_not_replaced_by_capture_read(monkeypatch):
    error = subprocess.TimeoutExpired(["incus", "exec"], 123)
    reads = []

    class NeverRead(io.StringIO):
        def read(self, size=-1):
            reads.append(size)
            raise AssertionError("capture read after transport timeout")

    streams = []

    def temporary_file(*args, **kwargs):
        stream = NeverRead()
        streams.append(stream)
        return stream

    run = MagicMock(side_effect=error)
    monkeypatch.setattr(incus.tempfile, "TemporaryFile", temporary_file)
    monkeypatch.setattr(incus, "_run", run)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        native_exec("candidate", ["fixture-step"], timeout=123)
    assert raised.value is error and not reads
    assert len(streams) == 2 and all(stream.closed for stream in streams)
    assert run.call_count == 1 and run.call_args.kwargs["timeout"] == 123
