# Copyright (c) Microsoft. All rights reserved.

"""Thin subprocess wrapper around the Incus CLI.

All functions invoke ``incus`` as a child process, parse its output, and raise
:class:`IncusError` on failure.  Same pattern amplifier-bundle-gitea uses for
Docker.
"""

from __future__ import annotations

import re
import subprocess


class IncusError(Exception):
    """Raised when an Incus command fails."""


# ---------------------------------------------------------------------------
# Daemon checks
# ---------------------------------------------------------------------------


def check_incus() -> None:
    """Verify the ``incus`` CLI is available and the daemon is reachable."""
    try:
        result = subprocess.run(
            ["incus", "version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise IncusError(f"Incus daemon unreachable: {result.stderr.strip()}")
    except FileNotFoundError:
        raise IncusError(
            "Incus CLI not found.  "
            "Install: https://linuxcontainers.org/incus/docs/main/installing/"
        )


def diagnose_network_failure(container_name: str) -> str:
    """Diagnose why a container can't reach the internet.

    Called when a provisioning command fails with network errors.
    Returns a human-readable diagnostic message with repair instructions.

    On WSL2, Incus's nftables NAT rules are sometimes lost after a host
    restart or ``wsl --shutdown``.  Containers can ping the bridge gateway
    but cannot reach the internet.  Restarting the Incus service
    regenerates the rules.
    """
    # 1. Check if the container can reach its gateway.
    ec, stdout, _ = exec_command(
        container_name, ["ip", "route", "show", "default"], timeout=5
    )
    if ec != 0:
        return (
            "Container has no default route.  Incus networking may not be initialized."
        )

    gateway = ""
    m = _GATEWAY_RE.search(stdout)
    if m:
        gateway = m.group(1)

    if gateway:
        ec, _, _ = exec_command(
            container_name, ["ping", "-c1", "-W2", gateway], timeout=10
        )
        if ec != 0:
            return (
                f"Container cannot reach bridge gateway ({gateway}).\n"
                "The Incus bridge may be down.  Try: sudo systemctl restart incus"
            )

    # 2. Gateway reachable but internet is not -> NAT rules missing.
    #    On WSL2, nftables rules are silently dropped.  Docker (if present)
    #    also sets the FORWARD chain to DROP, blocking Incus bridge traffic.
    fix_cmds = [
        "sudo systemctl restart incus",
        "",
        "# Add masquerade rules (nftables often fails silently on WSL2)",
        "SUBNET=$(incus network get incusbr0 ipv4.address | cut -d/ -f1)",
        'NETWORK="${SUBNET%.*}.0/24"',
        "sudo iptables -t nat -A POSTROUTING -s $NETWORK ! -d $NETWORK -j MASQUERADE",
        "sudo iptables -A FORWARD -i incusbr0 -j ACCEPT",
        "sudo iptables -A FORWARD -o incusbr0 "
        "-m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
    ]

    # Detect Docker -- it sets FORWARD policy to DROP.
    r = subprocess.run(["docker", "version"], capture_output=True, timeout=5)
    if r.returncode == 0:
        fix_cmds.extend(
            [
                "",
                "# Docker sets FORWARD policy to DROP -- allow Incus traffic",
                "sudo iptables -I DOCKER-USER -i incusbr0 -j ACCEPT",
                "sudo iptables -I DOCKER-USER -o incusbr0 "
                "-m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
            ]
        )

    return (
        "Containers cannot reach the internet (NAT/masquerade rules missing).\n"
        "This is common on WSL2 after a restart"
        + (
            " (Docker detected — it blocks Incus FORWARD traffic)."
            if r.returncode == 0
            else "."
        )
        + "\n\nFix:\n  "
        + "\n  ".join(fix_cmds)
        + "\n\nSee the README 'WSL2 networking' section for persistent fixes."
    )


# ---------------------------------------------------------------------------
# Container lifecycle
# ---------------------------------------------------------------------------


def create_container(
    name: str,
    image: str,
    config: dict[str, str] | None = None,
) -> None:
    """``incus launch <image> <name> [--config k=v ...]``"""
    cmd = ["incus", "launch", image, name]
    if config:
        for k, v in config.items():
            cmd.extend(["--config", f"{k}={v}"])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise IncusError(f"Failed to create container {name}: {result.stderr.strip()}")


def stop_container(name: str) -> None:
    """``incus stop <name>`` -- silently ignores already-stopped containers."""
    subprocess.run(
        ["incus", "stop", name],
        capture_output=True,
        text=True,
        timeout=30,
    )


def delete_container(name: str, force: bool = False) -> None:
    """``incus delete <name> [--force]``"""
    cmd = ["incus", "delete", name]
    if force:
        cmd.append("--force")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise IncusError(f"Failed to delete container {name}: {result.stderr.strip()}")


def container_exists(name: str) -> bool:
    """Return *True* if an Incus instance with *name* exists."""
    result = subprocess.run(
        ["incus", "info", name],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def exec_command(
    name: str,
    command: list[str],
    env: dict[str, str] | None = None,
    timeout: int = 600,
) -> tuple[int, str, str]:
    """Run *command* inside *name*.  Returns ``(exit_code, stdout, stderr)``.

    Does **not** allocate a PTY -- output is captured.
    """
    cmd: list[str] = ["incus", "exec", name]
    if env:
        for k, v in env.items():
            cmd.extend(["--env", f"{k}={v}"])
    cmd.extend(["--", *command])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


def exec_interactive(name: str) -> int:
    """Attach an interactive shell to *name*.

    Uses ``--force-interactive`` to allocate a PTY inside the container even
    when our own stdin is a pipe (required for the E2E test harness).
    stdin/stdout/stderr are inherited -- not captured.
    """
    result = subprocess.run(
        ["incus", "exec", "--force-interactive", name, "--", "bash", "-l"],
    )
    return result.returncode


# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

_GATEWAY_RE = re.compile(r"default via (\S+)")


def get_host_gateway_ip(name: str) -> str:
    """Detect the bridge gateway IP from inside *name*.

    Runs ``ip route show default`` and parses the ``via`` address.  This IP
    is how the container reaches services running on the host (e.g. Gitea).
    """
    exit_code, stdout, stderr = exec_command(
        name, ["ip", "route", "show", "default"], timeout=10
    )
    if exit_code != 0:
        raise IncusError(f"Failed to get gateway IP: {stderr.strip()}")

    m = _GATEWAY_RE.search(stdout)
    if not m:
        raise IncusError(f"Could not parse gateway IP from: {stdout.strip()!r}")
    return m.group(1)


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------


def push_file(name: str, local_path: str, container_path: str) -> None:
    """``incus file push <local> <name>/<container_path>``"""
    # Strip leading slash -- Incus path syntax is <instance>/<path-from-root>.
    dest = f"{name}/{container_path.lstrip('/')}"
    result = subprocess.run(
        ["incus", "file", "push", local_path, dest],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise IncusError(f"Failed to push file: {result.stderr.strip()}")
