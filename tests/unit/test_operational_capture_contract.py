"""Test-first operational capture boundaries; no new output-budget policy.

The stream guard detects whole-output reads without allocating large payloads.
All subprocess/backend effects are fakes. These are not installed/live proofs.
"""
# ruff: noqa: F811
import io
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from amplifier_bundle_digital_twin_universe import admission, engine, incus
from amplifier_bundle_digital_twin_universe.incus import exec_command as native_exec
from .test_admission import launch_infra  # noqa: F401
from .test_collected_launch import collected  # noqa: F401
from .test_collector_observations import launch


@contextmanager
def fake_spools(monkeypatch, *, stdout="complete stdout\n", stderr="complete stderr\n",
                returncode=0, read_error=None, require_sized_reads=False):
    """Two finite streams expose capture behavior without an OS child."""
    streams = []
    calls = []

    class Spool(io.StringIO):
        def read(self, size=-1):
            if read_error is not None:
                raise read_error
            if require_sized_reads and (size is None or size < 0):
                raise AssertionError("successful provisioning used a whole-output read")
            return super().read(size)

    def temporary_file(*args, **kwargs):
        stream = Spool()
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


def test_successful_provisioning_does_not_materialize_discarded_output(monkeypatch):
    # This asserts finite reads, not a proposed public byte limit. Small finite
    # payloads plus the sentinel discriminate without relying on memory pressure.
    monkeypatch.setattr(incus, "exec_command", native_exec)
    with fake_spools(monkeypatch, require_sized_reads=True) as (calls, _):
        engine._run_provisioning("candidate", ["fixture-step"])
        assert calls == [(["incus", "exec", "candidate", "--", "bash", "-lc", "fixture-step"], 600)]


@pytest.mark.parametrize("read_error", [OSError("PRIVATE-capture-read"), UnicodeError("PRIVATE-decode")],
                         ids=["io", "decode"])
def test_collected_capture_fault_after_exit_zero_never_authorizes_delete(collected, monkeypatch, read_error):
    path = Path(collected[1])
    path.write_text(path.read_text() + "provision:\n  setup_cmds:\n    - fixture-step\n")
    monkeypatch.setattr(incus, "exec_command", native_exec)
    deleted = MagicMock()
    monkeypatch.setattr(incus, "delete_container", deleted)
    with fake_spools(monkeypatch, read_error=read_error) as (calls, streams):
        with pytest.raises(admission.AdmissionError):
            launch(collected)
        # The child returned zero before the local read error. A capture fault
        # cannot retroactively establish failed provisioning or permit deletion.
        assert calls == [(["incus", "exec", "observation-case", "--", "bash", "-lc", "fixture-step"], 600)]
        assert all(stream.closed for stream in streams)
        assert deleted.call_count == 0
        status = engine.launch_status("observation-request")
        assert status["state"] == "unknown" and "result" not in status
        assert "PRIVATE" not in str(status)
        with pytest.raises(admission.AdmissionError):
            launch(collected)
        assert collected[3].call_count == 1 and len(collected[4]) == 1
        assert len(calls) == 1 and deleted.call_count == 0


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
