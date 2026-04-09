# Copyright (c) Microsoft. All rights reserved.

"""Hostname registration via Avahi mDNS.

Registers ``.local`` hostnames so DTU access URLs use human-readable names
instead of ``localhost``.  Works on native Linux and within WSL2.

macOS and Windows are not supported -- a warning is printed and access URLs
fall back to ``localhost``.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path


def _detect_platform() -> str:
    """Return a platform tag: ``"wsl2"``, ``"linux"``, or the raw ``sys.platform``."""
    try:
        with open("/proc/version") as f:
            if "microsoft" in f.read().lower():
                return "wsl2"
    except FileNotFoundError:
        pass
    if sys.platform == "linux":
        return "linux"
    return sys.platform  # "darwin", "win32", etc.


def _pid_file(container_id: str) -> Path:
    """Return the PID file path for the avahi-publish-address process."""
    return Path(f"/tmp/dtu-avahi-{container_id}.pid")


class HostnameManager:
    """Register and unregister ``.local`` hostnames via Avahi mDNS."""

    def __init__(self, hostname: str, container_id: str) -> None:
        self.hostname = hostname
        self.fqdn = f"{hostname}.local"
        self.container_id = container_id
        self.platform = _detect_platform()

    def register(self) -> dict[str, str] | None:
        """Register the hostname via Avahi.

        Returns ``{"method": "avahi", "hostname": "<fqdn>"}`` on success,
        or ``None`` if registration was skipped (unsupported platform or
        missing ``avahi-publish-address``).
        """
        if self.platform not in ("linux", "wsl2"):
            print(
                f"  hostname: .local hostname registration is not supported "
                f"on {self.platform}",
                file=sys.stderr,
            )
            print("  access URLs will use localhost", file=sys.stderr)
            return None

        if not self.check_available():
            print(
                "  hostname: avahi-publish-address not found "
                "(install avahi-utils for .local hostname support)",
                file=sys.stderr,
            )
            print("  access URLs will use localhost", file=sys.stderr)
            return None

        print(
            f"  registering hostname: {self.fqdn} (avahi)",
            file=sys.stderr,
        )

        proc = subprocess.Popen(
            ["avahi-publish-address", "-R", self.fqdn, "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        pid_path = _pid_file(self.container_id)
        pid_path.write_text(str(proc.pid))

        return {"method": "avahi", "hostname": self.fqdn}

    def unregister(self) -> None:
        """Kill the ``avahi-publish-address`` process if running.

        No-op if the PID file does not exist or the process is already gone.
        """
        pid_path = _pid_file(self.container_id)
        if not pid_path.exists():
            return

        try:
            pid = int(pid_path.read_text().strip())
        except (ValueError, OSError):
            pid_path.unlink(missing_ok=True)
            return

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # already gone
        except OSError:
            # Best effort -- e.g. permission denied on someone else's process.
            pass

        pid_path.unlink(missing_ok=True)

    @staticmethod
    def check_available() -> bool:
        """Return *True* if ``avahi-publish-address`` is on PATH."""
        return shutil.which("avahi-publish-address") is not None
