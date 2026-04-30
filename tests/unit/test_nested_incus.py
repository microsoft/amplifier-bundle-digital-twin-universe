# Copyright (c) Microsoft. All rights reserved.

"""Unit tests for nested-Incus self-proxy support.

Tests the following features:
- incus.add_proxy_device gains an optional connect_host= parameter
- incus.running_inside_incus_instance() detects whether we're inside an
  Incus instance with parent-daemon access via INCUS_SOCKET
- engine.launch() adds a self-proxy device on the calling instance when
  running nested, and is a no-op when not running nested

No Incus, Docker, or any real container runtime is required. All subprocess
calls are mocked.

Run with: uv run pytest tests/unit/test_nested_incus.py -v
"""

from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from amplifier_bundle_digital_twin_universe import incus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_subprocess_result(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> MagicMock:
    """Build a mock subprocess.CompletedProcess."""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


@pytest.fixture()
def real_unix_socket(tmp_path: Path) -> str:
    """Create a real UNIX socket file, yield its path, then clean up."""
    sock_path = str(tmp_path / "incus.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(sock_path)
        yield sock_path
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Tests 1-2: add_proxy_device with default and custom connect_host
# ---------------------------------------------------------------------------


def test_add_proxy_device_default_connect_host():
    """Regression: default connect_host=127.0.0.1 preserves existing argv."""
    ok = _make_subprocess_result(0)
    with patch(
        "amplifier_bundle_digital_twin_universe.incus.subprocess.run",
        return_value=ok,
    ) as mock_run:
        incus.add_proxy_device("my-container", "proxy-8080", 8080, 80)

    mock_run.assert_called_once()
    argv = mock_run.call_args[0][0]
    assert "listen=tcp:0.0.0.0:8080" in argv
    assert "connect=tcp:127.0.0.1:80" in argv
    # Default behavior: no bind=container, no loopback listen override
    assert "bind=container" not in argv


def test_add_proxy_device_custom_connect_host():
    """New: custom connect_host changes the connect address and enables container bind."""
    ok = _make_subprocess_result(0)
    with patch(
        "amplifier_bundle_digital_twin_universe.incus.subprocess.run",
        return_value=ok,
    ) as mock_run:
        incus.add_proxy_device(
            "caller-instance",
            "sut-proxy-8080",
            8080,
            80,
            connect_host="10.119.176.48",
        )

    mock_run.assert_called_once()
    argv = mock_run.call_args[0][0]
    # Self-proxy listens on loopback of the calling instance
    assert "listen=tcp:127.0.0.1:8080" in argv
    # Connect goes to the DTU container IP
    assert "connect=tcp:10.119.176.48:80" in argv
    # Must bind in container network namespace so loopback is the instance's own
    assert "bind=container" in argv


# ---------------------------------------------------------------------------
# Tests 3-5: running_inside_incus_instance detection
# ---------------------------------------------------------------------------


def test_running_inside_incus_instance_no_env_var(monkeypatch: pytest.MonkeyPatch):
    """Returns None when INCUS_SOCKET is not set."""
    monkeypatch.delenv("INCUS_SOCKET", raising=False)
    result = incus.running_inside_incus_instance()
    assert result is None


def test_running_inside_incus_instance_nonexistent_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Returns None when INCUS_SOCKET points to a path that does not exist."""
    sock_path = str(tmp_path / "does_not_exist.sock")
    monkeypatch.setenv("INCUS_SOCKET", sock_path)
    result = incus.running_inside_incus_instance()
    assert result is None


def test_running_inside_incus_instance_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Returns None when INCUS_SOCKET points to a regular file, not a socket."""
    reg_file = tmp_path / "not_a_socket.txt"
    reg_file.write_text("hello")
    monkeypatch.setenv("INCUS_SOCKET", str(reg_file))
    result = incus.running_inside_incus_instance()
    assert result is None


def test_running_inside_incus_instance_real_socket(
    real_unix_socket: str, monkeypatch: pytest.MonkeyPatch
):
    """Returns hostname when INCUS_SOCKET points to a real unix socket."""
    monkeypatch.setenv("INCUS_SOCKET", real_unix_socket)
    # Patch socket.gethostname at the source so it's visible regardless of
    # how the function imports it (module-level or lazy import)
    with patch("socket.gethostname", return_value="test-runner"):
        result = incus.running_inside_incus_instance()
    assert result == "test-runner"


# ---------------------------------------------------------------------------
# Tests 6-8: engine.launch integration with nested-Incus self-proxy
# ---------------------------------------------------------------------------


@pytest.fixture()
def minimal_profile_with_ports(tmp_path: Path) -> str:
    """Write a minimal profile YAML with one access.ports entry."""
    profile_path = tmp_path / "test-sut.yaml"
    profile_path.write_text(
        """\
name: test-sut
description: Minimal SUT profile for nested-Incus self-proxy tests

base:
  image: ubuntu:24.04

access:
  ports:
    - host: 8080
      container: 80
      label: web
"""
    )
    return str(profile_path)


@pytest.fixture()
def _patch_launch_infra(monkeypatch: pytest.MonkeyPatch):
    """Patch all infrastructure calls in launch() that require real Incus/Docker.

    Yields the patched incus module so tests can add further monkeypatches.
    """
    import amplifier_bundle_digital_twin_universe.engine as engine_mod
    import amplifier_bundle_digital_twin_universe.incus as incus_mod

    # No-op incus infrastructure calls
    monkeypatch.setattr(incus_mod, "check_incus", MagicMock())
    monkeypatch.setattr(incus_mod, "create_container", MagicMock())
    monkeypatch.setattr(incus_mod, "set_config", MagicMock())
    monkeypatch.setattr(incus_mod, "file_push", MagicMock())
    # exec_command: (exit_code, stdout, stderr) — used by _write_env -> _exec_checked
    monkeypatch.setattr(incus_mod, "exec_command", MagicMock(return_value=(0, "", "")))
    # get_container_ip: return a stable test IP
    monkeypatch.setattr(
        incus_mod, "get_container_ip", MagicMock(return_value="10.119.176.48")
    )

    # _wait_for_gateway: return a fake host IP without sleeping
    monkeypatch.setattr(
        engine_mod, "_wait_for_gateway", MagicMock(return_value="10.0.0.1")
    )

    # HostnameManager: register() returns None to skip mDNS wiring
    with patch(
        "amplifier_bundle_digital_twin_universe.hostname.HostnameManager"
    ) as mock_hm_cls:
        mock_hm_instance = MagicMock()
        mock_hm_instance.register.return_value = None
        mock_hm_cls.return_value = mock_hm_instance
        yield incus_mod


def test_launch_nested_incus_adds_caller_proxy(
    minimal_profile_with_ports: str,
    _patch_launch_infra,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested path: add_proxy_device is called for the calling instance too."""
    import amplifier_bundle_digital_twin_universe.incus as incus_mod
    from amplifier_bundle_digital_twin_universe import engine

    add_proxy_calls: list[dict] = []

    def _track_add_proxy(
        name: str,
        device_name: str,
        host_port: int,
        container_port: int,
        *,
        connect_host: str = "127.0.0.1",
    ) -> None:
        add_proxy_calls.append(
            {
                "name": name,
                "device_name": device_name,
                "host_port": host_port,
                "container_port": container_port,
                "connect_host": connect_host,
            }
        )

    monkeypatch.setattr(incus_mod, "add_proxy_device", _track_add_proxy)
    monkeypatch.setattr(
        incus_mod,
        "running_inside_incus_instance",
        MagicMock(return_value="test-runner"),
    )

    result = engine.launch(minimal_profile_with_ports, {}, name="dtu-test-001")

    # Expect exactly two calls: DTU host-side proxy + caller self-proxy
    assert len(add_proxy_calls) == 2, (
        f"Expected 2 add_proxy_device calls, got: {add_proxy_calls}"
    )

    dtu_call = add_proxy_calls[0]
    assert dtu_call["name"] == "dtu-test-001"
    assert dtu_call["device_name"] == "proxy-8080"
    assert dtu_call["host_port"] == 8080
    assert dtu_call["container_port"] == 80
    assert dtu_call["connect_host"] == "127.0.0.1"  # default preserved

    caller_call = add_proxy_calls[1]
    assert caller_call["name"] == "test-runner"
    assert caller_call["device_name"] == "sut-proxy-8080"
    assert caller_call["host_port"] == 8080
    assert caller_call["container_port"] == 80
    assert caller_call["connect_host"] == "10.119.176.48"  # DTU container IP

    assert result["status"] == "running"


def test_launch_non_nested_no_caller_proxy(
    minimal_profile_with_ports: str,
    _patch_launch_infra,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: no extra device added when not running nested (None returned)."""
    import amplifier_bundle_digital_twin_universe.incus as incus_mod
    from amplifier_bundle_digital_twin_universe import engine

    devices_added_to: list[str] = []

    def _track_add_proxy(
        name: str,
        device_name: str,
        host_port: int,
        container_port: int,
        *,
        connect_host: str = "127.0.0.1",
    ) -> None:
        devices_added_to.append(name)

    monkeypatch.setattr(incus_mod, "add_proxy_device", _track_add_proxy)
    monkeypatch.setattr(
        incus_mod, "running_inside_incus_instance", MagicMock(return_value=None)
    )

    result = engine.launch(minimal_profile_with_ports, {}, name="dtu-test-002")

    # Only the DTU host-side proxy call — no caller self-proxy
    assert len(devices_added_to) == 1
    assert devices_added_to[0] == "dtu-test-002"
    assert result["status"] == "running"


def test_launch_self_proxy_failure_is_nonfatal(
    minimal_profile_with_ports: str,
    _patch_launch_infra,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Self-proxy failure: launch succeeds; a warning is emitted to stderr."""
    import amplifier_bundle_digital_twin_universe.incus as incus_mod
    from amplifier_bundle_digital_twin_universe import engine

    def _failing_for_caller(
        name: str,
        device_name: str,
        host_port: int,
        container_port: int,
        *,
        connect_host: str = "127.0.0.1",
    ) -> None:
        if name == "test-runner":
            raise incus.IncusError("device already exists")
        # DTU host-side proxy succeeds normally

    monkeypatch.setattr(incus_mod, "add_proxy_device", _failing_for_caller)
    monkeypatch.setattr(
        incus_mod,
        "running_inside_incus_instance",
        MagicMock(return_value="test-runner"),
    )

    # Launch must succeed despite the self-proxy failure
    result = engine.launch(minimal_profile_with_ports, {}, name="dtu-test-003")
    assert result["status"] == "running"

    # A warning must be present in stderr output
    captured = capsys.readouterr()
    stderr_lower = captured.err.lower()
    assert any(
        token in stderr_lower
        for token in ("warning", "sut-proxy", "nested", "self-proxy", "test-runner")
    ), f"Expected a warning in stderr, got:\n{captured.err}"
