# Copyright (c) Microsoft. All rights reserved.
"""Explicit destroy needs no Docker only when its launch recorded no mocks.

All backend effects here are mocks. Real installed Incus acceptance is separate.
"""
# ruff: noqa: F811
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from amplifier_bundle_digital_twin_universe import admission, engine, incus
from amplifier_bundle_digital_twin_universe.cli import main
from .test_admission import launch_infra  # noqa: F401

KEY = "user.dtu.mock-cleanup"


@pytest.fixture
def owned(launch_infra, monkeypatch):
    profile, observed, create = launch_infra
    engine.launch(profile, {}, name="owned", request_id="r", owner_root="o")
    stop = MagicMock()
    delete = MagicMock(side_effect=lambda *a, **k: observed.clear())
    monkeypatch.setattr(incus, "stop_container", stop)
    monkeypatch.setattr(incus, "delete_container", delete)
    root = admission.state_directory(admission.context())
    path = admission.receipt_path(root, "r")
    return profile, observed, create, stop, delete, path


def docker(monkeypatch, replies):
    run = MagicMock(side_effect=replies)
    monkeypatch.setattr(engine.subprocess, "run", run)
    return run


def response(code=0, output="", error=""):
    return subprocess.CompletedProcess([], code, stdout=output, stderr=error)


@pytest.mark.parametrize("state", ["Running", "Stopped"])
def test_no_mock_launch_destroys_without_docker(owned, monkeypatch, state):
    _, observed, _, stop, delete, path = owned
    observed[0]["status"] = state
    run = docker(monkeypatch, [FileNotFoundError("docker missing")])
    result = engine.destroy("owned", owner_root="o", request_id="r")
    assert result == {"id": "owned", "destroyed": True}
    stop.assert_called_once_with("owned")
    delete.assert_called_once_with("owned")
    assert json.loads(path.read_text())["state"] == "removed"
    assert run.call_count == 1


@pytest.mark.parametrize("marker", [None, "", "required", "unexpected"])
def test_missing_docker_with_uncertain_or_required_mocks_refuses(owned, monkeypatch, marker):
    _, observed, _, stop, delete, path = owned
    if marker is None:
        observed[0]["config"].pop(KEY, None)
    else:
        observed[0]["config"][KEY] = marker
    before = path.read_bytes()
    docker(monkeypatch, [FileNotFoundError("docker missing")])
    with pytest.raises((RuntimeError, FileNotFoundError)):
        engine.destroy("owned", owner_root="o", request_id="r")
    assert not stop.called and not delete.called and path.read_bytes() == before


@pytest.mark.parametrize("error", [response(1, error="daemon unavailable"), PermissionError("denied"),
    subprocess.TimeoutExpired(["docker", "ps"], 10), UnicodeError("decode")])
def test_inventory_failure_never_reports_destroyed(owned, monkeypatch, error):
    _, _, _, stop, delete, path = owned
    before = path.read_bytes()
    docker(monkeypatch, [error])
    result = CliRunner().invoke(main, ["destroy", "owned", "--owner-root", "o", "--request-id", "r"])
    assert result.exit_code != 0 and '"destroyed": true' not in result.output
    assert not stop.called and not delete.called and path.read_bytes() == before


@pytest.mark.parametrize("marker", ["none", "required", ""])
def test_real_inventory_is_not_suppressed_by_marker(owned, monkeypatch, marker):
    _, observed, _, stop, delete, path = owned
    observed[0]["config"][KEY] = marker
    run = docker(monkeypatch, [response(output="mock-a\nmock-b\n"), response(), response()])
    assert engine.destroy("owned")["destroyed"]
    calls = [c.args[0] for c in run.call_args_list]
    assert calls == [["docker", "ps", "-a", "--filter", "label=dtu.env-id=owned", "--format", "{{.Names}}"],
                     ["docker", "rm", "-f", "mock-a"], ["docker", "rm", "-f", "mock-b"]]
    assert stop.call_count == delete.call_count == 1 and json.loads(path.read_text())["state"] == "removed"


@pytest.mark.parametrize("second", [False, True])
@pytest.mark.parametrize("error", [response(1, error="remove failed"), FileNotFoundError("docker disappeared")])
def test_remove_failure_preserves_incus_and_receipt(owned, monkeypatch, second, error):
    _, _, _, stop, delete, path = owned
    before = path.read_bytes()
    replies = [response(output="mock-a\nmock-b\n")]
    if second:
        replies.append(response())
    replies.append(error)
    run = docker(monkeypatch, replies)
    with pytest.raises((RuntimeError, FileNotFoundError)):
        engine.destroy("owned")
    assert run.call_count == (3 if second else 2)
    assert not stop.called and not delete.called and path.read_bytes() == before


def test_owner_mismatch_refuses_before_docker(owned, monkeypatch):
    _, _, _, stop, delete, path = owned
    before = path.read_bytes()
    run = docker(monkeypatch, [AssertionError("must not touch Docker")])
    with pytest.raises(admission.AdmissionError) as exc:
        engine.destroy("owned", owner_root="foreign", request_id="r")
    assert exc.value.code == "ownership_conflict"
    assert not run.called and not stop.called and not delete.called
    assert path.read_bytes() == before


def test_delete_failure_does_not_retire_receipt(owned, monkeypatch):
    _, _, _, stop, delete, path = owned
    before = path.read_bytes()
    docker(monkeypatch, [response()])
    delete.side_effect = incus.IncusError("delete failed")
    with pytest.raises(incus.IncusError):
        engine.destroy("owned")
    assert stop.call_count == delete.call_count == 1
    assert path.read_bytes() == before


def test_unknown_marker_never_reads_snapshot_to_infer_no_mocks(owned, monkeypatch):
    _, observed, _, stop, delete, path = owned
    observed[0]["config"].pop(KEY, None)
    snapshot = MagicMock(side_effect=AssertionError("snapshot is not historical cleanup proof"))
    monkeypatch.setattr(engine, "_read_profile_snapshot", snapshot)
    docker(monkeypatch, [FileNotFoundError("docker missing")])
    before = path.read_bytes()
    with pytest.raises((RuntimeError, FileNotFoundError)):
        engine.destroy("owned")
    assert not snapshot.called and not stop.called and not delete.called
    assert path.read_bytes() == before


def test_marker_overrides_profile_config_and_is_not_downgraded(launch_infra, monkeypatch):
    profile, observed, create = launch_infra
    path = Path(profile)
    path.write_text("name: mock-test\nbase:\n  image: ubuntu:24.04\n  config:\n    user.dtu.mock-cleanup: none\nmock_services:\n  - source: fixture-mock\n")
    def setup(name, parsed):
        assert observed[0]["config"][KEY] == "required"
        return []
    monkeypatch.setattr(engine, "_setup_services", setup)
    engine.launch(profile, {}, name="owned", request_id="r", owner_root="o")
    assert create.call_args.kwargs["config"][KEY] == "required"
    assert observed[0]["config"][KEY] == "required"


@pytest.mark.parametrize("metadata_fails", [False, True])
def test_reloaded_mocks_are_marked_before_any_setup(launch_infra, monkeypatch, metadata_fails):
    profile, observed, _ = launch_infra
    load = engine.load_profile
    def changed(*args, **kwargs):
        value = load(*args, **kwargs)
        if kwargs.get("validate") is False:
            value.mock_services = [object()]
        return value
    monkeypatch.setattr(engine, "load_profile", changed)
    setup = MagicMock(return_value=[])
    def start(name, value):
        assert observed[0]["config"][KEY] == "required"
        return setup(name, value)
    monkeypatch.setattr(engine, "_setup_services", start)
    setter = incus.set_config
    def set_config(name, key, value):
        if key == KEY and metadata_fails:
            raise incus.IncusError("metadata unavailable")
        setter(name, key, value)
    monkeypatch.setattr(incus, "set_config", set_config)
    monkeypatch.setattr(engine, "_stop_mock_containers", MagicMock())
    monkeypatch.setattr(incus, "delete_container", MagicMock())
    if metadata_fails:
        with pytest.raises(incus.IncusError):
            engine.launch(profile, {}, name="owned", request_id="r", owner_root="o")
        assert not setup.called
    else:
        engine.launch(profile, {}, name="owned", request_id="r", owner_root="o")
        assert setup.call_count == 1 and observed[0]["config"][KEY] == "required"
