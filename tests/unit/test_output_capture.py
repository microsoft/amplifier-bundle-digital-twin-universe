# Copyright (c) Microsoft. All rights reserved.
"""Ordinary exec/launch capture faults are not provisioning failure evidence.

Backend calls below are mocked. Live installed acceptance is a separate check.
There is no collector, bound backend, operator authority or output-budget policy.
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


@contextmanager
def spools(monkeypatch, *, code=0, error=None, channel=0, stage="read",
           stdout="complete stdout\n", stderr="complete stderr\n"):
    streams, calls = [], []

    class Spool(io.StringIO):
        faults = 0

        def seek(self, *args):
            if self.channel == channel and stage == "seek" and error is not None:
                self.faults += 1
                raise error
            return super().seek(*args)

        def read(self, size=-1):
            if self.channel == channel and stage == "read" and error is not None:
                self.faults += 1
                raise error
            return super().read(size)

    def temporary_file(*args, **kwargs):
        stream = Spool()
        stream.channel = len(streams) % 2
        streams.append(stream)
        return stream

    def run(command, **kwargs):
        calls.append((command, kwargs.get("timeout")))
        assert kwargs["stdin"] == subprocess.DEVNULL
        kwargs["stdout"].write(stdout)
        kwargs["stderr"].write(stderr)
        return subprocess.CompletedProcess(command, code)

    with monkeypatch.context() as patch:
        patch.setattr(incus.tempfile, "TemporaryFile", temporary_file)
        patch.setattr(incus, "_run", run)
        yield calls, streams
    assert streams and len(streams) % 2 == 0 and all(stream.closed for stream in streams)


@pytest.mark.parametrize("code", [0, 37, -9])
@pytest.mark.parametrize("channel", [0, 1], ids=["stdout", "stderr"])
@pytest.mark.parametrize("stage,kind", [("seek", OSError), ("read", OSError), ("read", UnicodeError)])
def test_capture_error_retains_observed_code_and_original_cause(monkeypatch, code, channel, stage, kind):
    error = kind("PRIVATE output or path")
    with spools(monkeypatch, code=code, error=error, channel=channel, stage=stage) as (calls, streams):
        # Includes native failures to make the predecessor fail on the real
        # distinction, rather than on a reference to its missing exception type.
        with pytest.raises((OSError, UnicodeError, incus.IncusError)) as raised:
            native_exec("candidate", ["PRIVATE command"], timeout=123)
        assert type(raised.value).__name__ == "OutputCaptureError"
        assert raised.value.returncode == code and raised.value.__cause__ is error
        assert "PRIVATE" not in str(raised.value)
        assert len(calls) == 1 and sum(s.faults for s in streams) == 1
        assert all(s.closed for s in streams)


def prepare_launch(launch_infra, monkeypatch):
    profile, observed, create = launch_infra
    path = Path(profile)
    path.write_text(path.read_text() + "provision:\n  setup_cmds:\n    - fixture-step\n    - must-not-run\n")
    monkeypatch.setattr(incus, "exec_command", native_exec)
    deleted, stopped, mocks_stopped = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(incus, "delete_container", deleted)
    monkeypatch.setattr(incus, "stop_container", stopped)
    monkeypatch.setattr(engine, "_stop_mock_containers", mocks_stopped)
    args = dict(name="capture-case", request_id="capture-request", owner_root="capture-owner")
    return profile, observed, create, deleted, stopped, mocks_stopped, args


@pytest.mark.parametrize("code", [0, 37, -9])
@pytest.mark.parametrize("kind", [OSError, UnicodeError])
def test_ordinary_capture_fault_does_not_delete_publish_or_replay(launch_infra, monkeypatch, code, kind):
    profile, observed, create, deleted, stopped, mocks_stopped, args = prepare_launch(launch_infra, monkeypatch)
    error = kind("PRIVATE capture error")
    with spools(monkeypatch, code=code, error=error) as (calls, streams):
        with pytest.raises((OSError, UnicodeError, incus.IncusError)) as raised:
            engine.launch(profile, {}, **args)
        assert deleted.call_count == stopped.call_count == mocks_stopped.call_count == 0
        assert type(raised.value).__name__ == "OutputCaptureError"
        assert raised.value.returncode == code and raised.value.__cause__ is error
        assert calls == [(["incus", "exec", "capture-case", "--", "bash", "-lc", "fixture-step"], 600)]
        assert sum(s.faults for s in streams) == 1
        assert observed[0]["config"]["user.dtu.launch-state"] == "provisioning"
        root = admission.state_directory(admission.context())
        path = admission.receipt_path(root, args["request_id"])
        before = path.read_bytes()
        status = engine.launch_status(args["request_id"])
        assert status["state"] == "unknown" and "result" not in status
        assert "PRIVATE" not in json.dumps(status)
        assert engine.capacity()["counts"]["total"] == 1
        with pytest.raises(admission.AdmissionError) as retry:
            engine.launch(profile, {}, **args)
        assert retry.value.code == "launch_unknown"
        assert create.call_count == 1 and len(calls) == 1
        assert deleted.call_count == stopped.call_count == 0
        assert path.read_bytes() == before


@pytest.mark.parametrize("diagnostic_fault", [False, True])
def test_complete_failed_command_still_cleans_up(launch_infra, monkeypatch, diagnostic_fault):
    profile, _, create, deleted, _, mocks_stopped, args = prepare_launch(launch_infra, monkeypatch)
    secondary = None
    if diagnostic_fault:
        # A fully captured failure precedes an optional diagnostic capture fault.
        # The established command failure, not the missing diagnostic, owns cleanup.
        def consume(name, commands):
            engine._exec_checked(name, commands[0])
        monkeypatch.setattr(engine, "_run_provisioning", consume)
        def diagnostic(name):
            nonlocal secondary
            try:
                with spools(monkeypatch, error=OSError("PRIVATE diagnostic")):
                    native_exec(name, ["diagnose"])
            except incus.IncusError as exc:
                secondary = exc
                raise
        monkeypatch.setattr(incus, "diagnose_network_failure", diagnostic)
    with spools(monkeypatch, code=37, stdout="FULL original output", stderr="Could not resolve host") as (calls, _):
        with pytest.raises(RuntimeError) as raised:
            engine.launch(profile, {}, **args)
        assert "exit 37" in str(raised.value) and "FULL original output" in str(raised.value)
        assert "Could not resolve host" in str(raised.value)
        if diagnostic_fault:
            assert raised.value.__cause__ is secondary
            assert "Network diagnostic unavailable" in raised.value.__notes__
            assert "PRIVATE" not in str(raised.value)
        assert len(calls) == 1 and create.call_count == 1
        deleted.assert_called_once_with("capture-case", force=True)
        mocks_stopped.assert_called_once_with("capture-case")
        root = admission.state_directory(admission.context())
        assert admission.read_receipt(root, args["request_id"])["state"] == "failed"


@pytest.mark.parametrize("code", [0, 37, -9])
def test_complete_strings_are_unchanged(monkeypatch, code):
    out, err = "output\n\u03bb\x00\n" * 32, "error\n\u6f22\n" * 32
    with spools(monkeypatch, code=code, stdout=out, stderr=err) as (calls, _):
        assert native_exec("candidate", ["fixture-step"], timeout=123) == (code, out, err)
        assert calls == [(["incus", "exec", "candidate", "--", "fixture-step"], 123)]


def test_complete_success_publishes_ready(launch_infra, monkeypatch):
    profile, observed, create, deleted, stopped, mocks_stopped, args = prepare_launch(launch_infra, monkeypatch)
    with spools(monkeypatch) as (calls, _):
        result = engine.launch(profile, {}, **args)
        assert result["id"] == "capture-case"
        assert create.call_count == 1 and len(calls) == 2
        assert observed[0]["config"]["user.dtu.launch-state"] == "ready"
        assert engine.launch_status(args["request_id"])["state"] == "ready"
        assert not deleted.called and not stopped.called and not mocks_stopped.called


def test_timeout_never_reaches_capture_reads(monkeypatch):
    error = subprocess.TimeoutExpired(["incus", "exec"], 123)
    streams = []
    class NeverRead(io.StringIO):
        def seek(self, *args):
            pytest.fail("capture seek after execution timeout")
        def read(self, *args):
            pytest.fail("capture read after execution timeout")
    def temporary_file(*args, **kwargs):
        stream = NeverRead()
        streams.append(stream)
        return stream
    run = MagicMock(side_effect=error)
    monkeypatch.setattr(incus.tempfile, "TemporaryFile", temporary_file)
    monkeypatch.setattr(incus, "_run", run)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        native_exec("candidate", ["fixture-step"], timeout=123)
    assert raised.value is error and run.call_count == 1
    assert len(streams) == 2 and all(s.closed for s in streams)


def test_gateway_capture_fault_is_not_retried(monkeypatch):
    def gateway(name):
        return native_exec(name, ["route"])[1]
    monkeypatch.setattr(incus, "get_host_gateway_ip", gateway)
    sleep = MagicMock(side_effect=AssertionError("capture fault must not retry"))
    monkeypatch.setattr(engine.time, "sleep", sleep)
    with spools(monkeypatch, error=OSError("PRIVATE")) as (calls, _):
        with pytest.raises((OSError, incus.IncusError)) as raised:
            engine._wait_for_gateway("candidate")
        assert type(raised.value).__name__ == "OutputCaptureError"
        assert len(calls) == 1 and not sleep.called


def test_snapshot_capture_fault_is_not_absence_or_fallback(monkeypatch):
    # Real file_pull enters its exec-based directory probe. No transfer or
    # profile fallback is reached if that probe's output cannot be captured.
    fallback = MagicMock(side_effect=AssertionError("must not select host profile"))
    monkeypatch.setattr(incus, "check_incus", lambda: None)
    monkeypatch.setattr(engine, "load_profile", fallback)
    monkeypatch.setattr(incus, "container_exists", lambda name: True)
    monkeypatch.setattr(incus, "get_instance_state", lambda name: "Running")
    monkeypatch.setattr(engine, "_wait_for_gateway", lambda name: "10.1.0.1")
    monkeypatch.setattr(incus, "get_config", lambda *a: "old-profile")
    with spools(monkeypatch, error=UnicodeError("PRIVATE"), code=1) as (calls, _):
        with pytest.raises((UnicodeError, incus.IncusError)) as raised:
            engine.update("candidate", {})
        assert type(raised.value).__name__ == "OutputCaptureError"
        assert len(calls) == 1 and not fallback.called


def test_gateway_missing_route_still_retries(monkeypatch):
    gateway = MagicMock(side_effect=[incus.IncusError("not ready"), "10.1.0.1"])
    sleep = MagicMock()
    monkeypatch.setattr(incus, "get_host_gateway_ip", gateway)
    monkeypatch.setattr(engine.time, "sleep", sleep)
    assert engine._wait_for_gateway("candidate") == "10.1.0.1"
    assert gateway.call_count == 2
    sleep.assert_called_once_with(1)


def test_missing_snapshot_remains_eligible_for_fallback(monkeypatch):
    monkeypatch.setattr(incus, "file_pull", MagicMock(side_effect=incus.IncusError("absent")))
    assert engine._read_profile_snapshot("candidate") is None
