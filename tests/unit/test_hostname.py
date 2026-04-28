# Copyright (c) Microsoft. All rights reserved.

"""Unit tests for hostname registration via Avahi mDNS.

All tests mock subprocess/filesystem calls -- no Avahi daemon, Incus, or
Docker required.
"""

from __future__ import annotations

import signal
from unittest.mock import MagicMock, mock_open, patch


from amplifier_bundle_digital_twin_universe.hostname import (
    HostnameManager,
    _detect_platform,
)


# ---------------------------------------------------------------------------
# _detect_platform()
# ---------------------------------------------------------------------------


def test_detect_platform_wsl2():
    """Should return 'wsl2' when /proc/version contains 'microsoft'."""
    content = "Linux version 5.15.167.4-microsoft-standard-WSL2"
    with patch("builtins.open", mock_open(read_data=content)):
        assert _detect_platform() == "wsl2"


def test_detect_platform_native_linux():
    """Should return 'linux' when /proc/version exists but is not WSL2."""
    content = "Linux version 6.8.0-45-generic (Ubuntu)"
    with patch("builtins.open", mock_open(read_data=content)):
        assert _detect_platform() == "linux"


def test_detect_platform_no_proc_version():
    """Should return sys.platform when /proc/version doesn't exist."""
    with patch("builtins.open", side_effect=FileNotFoundError):
        with patch("amplifier_bundle_digital_twin_universe.hostname.sys") as mock_sys:
            mock_sys.platform = "darwin"
            assert _detect_platform() == "darwin"


# ---------------------------------------------------------------------------
# HostnameManager.check_available()
# ---------------------------------------------------------------------------


def test_check_available_true():
    """Should return True when avahi-publish-address is on PATH."""
    with patch(
        "amplifier_bundle_digital_twin_universe.hostname.shutil.which",
        return_value="/usr/bin/avahi-publish-address",
    ):
        assert HostnameManager.check_available() is True


def test_check_available_false():
    """Should return False when avahi-publish-address is not on PATH."""
    with patch(
        "amplifier_bundle_digital_twin_universe.hostname.shutil.which",
        return_value=None,
    ):
        assert HostnameManager.check_available() is False


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------


def test_register_unsupported_platform():
    """register() should return None on an unsupported platform."""
    mgr = HostnameManager("my-project", "dtu-abc12345")
    mgr.platform = "darwin"
    result = mgr.register()
    assert result is None


def test_register_avahi_not_on_path():
    """register() should return None when avahi-publish-address is missing."""
    mgr = HostnameManager("my-project", "dtu-abc12345")
    mgr.platform = "linux"
    with patch.object(HostnameManager, "check_available", return_value=False):
        result = mgr.register()
    assert result is None


def test_register_spawns_avahi_process(tmp_path):
    """register() should spawn avahi-publish-address and write PID file."""
    mgr = HostnameManager("my-project", "dtu-abc12345")
    mgr.platform = "linux"

    mock_proc = MagicMock()
    mock_proc.pid = 42

    pid_path = tmp_path / "dtu-avahi-dtu-abc12345.pid"

    with (
        patch.object(HostnameManager, "check_available", return_value=True),
        patch(
            "amplifier_bundle_digital_twin_universe.hostname.subprocess.Popen",
            return_value=mock_proc,
        ) as mock_popen,
        patch(
            "amplifier_bundle_digital_twin_universe.hostname._pid_file",
            return_value=pid_path,
        ),
    ):
        result = mgr.register()

    assert result == {"method": "avahi", "hostname": "my-project.local"}
    mock_popen.assert_called_once()
    call_args = mock_popen.call_args[0][0]
    assert call_args == [
        "avahi-publish-address",
        "-R",
        "my-project.local",
        "127.0.0.1",
    ]
    assert pid_path.read_text() == "42"


# ---------------------------------------------------------------------------
# unregister()
# ---------------------------------------------------------------------------


def test_unregister_kills_process_and_removes_pid(tmp_path):
    """unregister() should send SIGTERM and remove the PID file."""
    pid_path = tmp_path / "dtu-avahi-dtu-abc12345.pid"
    pid_path.write_text("12345")

    mgr = HostnameManager("my-project", "dtu-abc12345")
    with (
        patch(
            "amplifier_bundle_digital_twin_universe.hostname._pid_file",
            return_value=pid_path,
        ),
        patch("amplifier_bundle_digital_twin_universe.hostname.os.kill") as mock_kill,
    ):
        mgr.unregister()

    mock_kill.assert_called_once_with(12345, signal.SIGTERM)
    assert not pid_path.exists()


def test_unregister_noop_when_no_pid_file(tmp_path):
    """unregister() should be a no-op when PID file doesn't exist."""
    pid_path = tmp_path / "dtu-avahi-dtu-abc12345.pid"
    mgr = HostnameManager("my-project", "dtu-abc12345")
    with patch(
        "amplifier_bundle_digital_twin_universe.hostname._pid_file",
        return_value=pid_path,
    ):
        mgr.unregister()  # should not raise
