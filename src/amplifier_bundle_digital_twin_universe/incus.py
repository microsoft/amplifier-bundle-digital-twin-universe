# Copyright (c) Microsoft. All rights reserved.

"""Thin subprocess wrapper around the Incus CLI.

All functions invoke ``incus`` as a child process, parse its output, and raise
:class:`IncusError` on failure.  Same pattern amplifier-bundle-gitea uses for
Docker.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile


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
# Configurable incus launch timeout
#
# On a lightly loaded host the hardcoded 120 s is ample.  On hosts with many
# running containers (30+), ``incus launch`` can take 130–170 s, exceeding
# the default and raising TimeoutExpired.  Set the env var below to override.
#
# AMPLIFIER_DTU_INCUS_LAUNCH_TIMEOUT_SECONDS
#   Override the incus launch timeout (positive integer, 1–3600 s).
#   Unset, empty, non-integer, zero/negative, or >3600 values all fall back
#   to the default (120 s) with a warning to stderr.
#   Naming follows AMPLIFIER_CONTAINER_RUNTIME / AMPLIFIER_REALITY_CHECK_RUNTIME.
#   See docs/OPERATIONS.md §2 for the broader container runtime config context.
# ---------------------------------------------------------------------------

_DEFAULT_INCUS_LAUNCH_TIMEOUT: int = 120
_MAX_INCUS_LAUNCH_TIMEOUT: int = 3600
_INCUS_LAUNCH_TIMEOUT_ENV_VAR: str = "AMPLIFIER_DTU_INCUS_LAUNCH_TIMEOUT_SECONDS"


def _get_launch_timeout_seconds() -> int:
    """Return the incus launch timeout in seconds, read from the environment.

    Reads ``AMPLIFIER_DTU_INCUS_LAUNCH_TIMEOUT_SECONDS``.  Falls back to the
    default (120 s) for unset/empty/invalid values; emits a warning to stderr
    for non-empty invalid values.
    """
    raw = os.environ.get(_INCUS_LAUNCH_TIMEOUT_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_INCUS_LAUNCH_TIMEOUT
    try:
        value = int(raw)
    except ValueError:
        print(
            f"Warning: {_INCUS_LAUNCH_TIMEOUT_ENV_VAR}={raw!r} is not a valid integer; "
            f"using default {_DEFAULT_INCUS_LAUNCH_TIMEOUT}s.",
            file=sys.stderr,
        )
        return _DEFAULT_INCUS_LAUNCH_TIMEOUT
    if value < 1 or value > _MAX_INCUS_LAUNCH_TIMEOUT:
        print(
            f"Warning: {_INCUS_LAUNCH_TIMEOUT_ENV_VAR}={raw!r} is out of range "
            f"(must be 1\u2013{_MAX_INCUS_LAUNCH_TIMEOUT}); "
            f"using default {_DEFAULT_INCUS_LAUNCH_TIMEOUT}s.",
            file=sys.stderr,
        )
        return _DEFAULT_INCUS_LAUNCH_TIMEOUT
    return value


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
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=_get_launch_timeout_seconds())
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
    timeout: int | None = 600,
) -> tuple[int, str, str]:
    """Run *command* inside *name*.  Returns ``(exit_code, stdout, stderr)``.

    Does **not** allocate a PTY -- output is captured.

    Uses temporary files instead of ``capture_output=True`` pipes so that
    ``subprocess.run`` returns as soon as the direct child (``incus exec``)
    exits, without waiting for grandchildren (e.g. ``lxc monitor`` spawned
    by a nested ``incus launch``) to close inherited file descriptors.
    ``stdin=DEVNULL`` prevents the child from blocking on inherited input.
    """
    cmd: list[str] = ["incus", "exec", name]
    if env:
        for k, v in env.items():
            cmd.extend(["--env", f"{k}={v}"])
    cmd.extend(["--", *command])
    with tempfile.TemporaryFile("w+") as out_f, tempfile.TemporaryFile("w+") as err_f:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=out_f,
            stderr=err_f,
            text=True,
            timeout=timeout,
        )
        out_f.seek(0)
        err_f.seek(0)
        return result.returncode, out_f.read(), err_f.read()


def exec_stream(
    name: str,
    command: list[str],
    env: dict[str, str] | None = None,
    timeout: int | None = 600,
) -> int:
    """Run *command* inside *name* with real-time output.  Returns exit code.

    stdout and stderr are inherited from the calling process so output
    streams to the terminal as it is produced.  No output is captured.

    ``timeout`` is the maximum number of seconds to wait for the command
    to complete.  Pass ``None`` to disable the timeout entirely.  Default
    is 600 seconds.
    """
    cmd: list[str] = ["incus", "exec", name]
    if env:
        for k, v in env.items():
            cmd.extend(["--env", f"{k}={v}"])
    cmd.extend(["--", *command])
    result = subprocess.run(cmd, timeout=timeout)
    return result.returncode


def exec_interactive(
    name: str,
    command: list[str] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> int:
    """Attach an interactive shell to *name*.

    Uses ``--force-interactive`` to allocate a PTY inside the container even
    when our own stdin is a pipe (required for the E2E test harness).
    stdin/stdout/stderr are inherited -- not captured.

    *command* defaults to ``["bash", "-l"]``.  Pass a custom command to launch
    bash with additional flags.

    *env* is forwarded as ``incus exec --env KEY=VALUE`` flags, exposing the
    variables to the shell at attach time.  Used by ``engine.exec_interactive``
    to set ``DTU_VISUAL_ID`` for the prompt-prefix profile.d script.
    """
    if command is None:
        command = ["bash", "-l"]
    cmd: list[str] = ["incus", "exec", "--force-interactive", name]
    if env:
        for k, v in env.items():
            cmd.extend(["--env", f"{k}={v}"])
    cmd.extend(["--", *command])
    result = subprocess.run(cmd)
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


def file_push(
    name: str,
    local_paths: list[str],
    container_path: str,
    *,
    recursive: bool = False,
    create_dirs: bool = False,
    mode: str | None = None,
    uid: int | None = None,
    gid: int | None = None,
    timeout: int = 120,
) -> None:
    """``incus file push <path>... <name>/<container_path>``"""
    dest = f"{name}/{container_path.lstrip('/')}"
    cmd = ["incus", "file", "push"]
    if recursive:
        cmd.append("--recursive")
    if create_dirs:
        cmd.append("--create-dirs")
    if mode is not None:
        cmd.extend(["--mode", mode])
    if uid is not None:
        cmd.extend(["--uid", str(uid)])
    if gid is not None:
        cmd.extend(["--gid", str(gid)])
    cmd.extend([*local_paths, dest])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise IncusError(f"Failed to push file: {result.stderr.strip()}")


def file_pull(
    name: str,
    container_paths: list[str],
    local_path: str,
    *,
    recursive: bool = False,
    create_dirs: bool = False,
    timeout: int = 120,
) -> None:
    """``incus file pull <name>/<path>... <local_path>``"""
    srcs = [f"{name}/{p.lstrip('/')}" for p in container_paths]
    cmd = ["incus", "file", "pull"]
    if recursive:
        cmd.append("--recursive")
    if create_dirs:
        cmd.append("--create-dirs")
    cmd.extend([*srcs, local_path])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise IncusError(f"Failed to pull file: {result.stderr.strip()}")


# ---------------------------------------------------------------------------
# Instance config (metadata)
# ---------------------------------------------------------------------------


def set_config(name: str, key: str, value: str) -> None:
    """``incus config set <name> <key>=<value>``"""
    result = subprocess.run(
        ["incus", "config", "set", name, f"{key}={value}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise IncusError(
            f"Failed to set config {key} on {name}: {result.stderr.strip()}"
        )


def get_config(name: str, key: str) -> str:
    """``incus config get <name> <key>`` -- returns the value or empty string."""
    result = subprocess.run(
        ["incus", "config", "get", name, key],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise IncusError(
            f"Failed to get config {key} on {name}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Instance listing / status
# ---------------------------------------------------------------------------


def get_instance_state(name: str) -> str:
    """Return the Incus status string for *name* (e.g. ``"Running"``)."""
    result = subprocess.run(
        ["incus", "list", name, "--format=json"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise IncusError(f"Failed to query instance {name}: {result.stderr.strip()}")
    instances = json.loads(result.stdout)
    for inst in instances:
        if inst["name"] == name:
            return inst["status"]
    raise IncusError(f"Instance {name} not found in incus list output")


def list_instances(config_key: str, config_value: str) -> list[dict]:
    """Return all instances where ``config_key == config_value``.

    Each entry is the raw Incus JSON dict (keys: name, status, config, ...).
    """
    result = subprocess.run(
        ["incus", "list", f"{config_key}={config_value}", "--format=json"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise IncusError(
            f"Failed to list instances ({config_key}={config_value}): "
            f"{result.stderr.strip()}"
        )
    return json.loads(result.stdout)


def get_container_ip(name: str) -> str:
    """Return the first global IPv4 address of *name*.

    Parses ``incus list <name> --format=json`` and finds the first
    ``inet`` address with ``global`` scope on any interface.
    """
    result = subprocess.run(
        ["incus", "list", name, "--format=json"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise IncusError(f"Failed to query container {name}: {result.stderr.strip()}")
    instances = json.loads(result.stdout)
    for inst in instances:
        if inst["name"] != name:
            continue
        network = inst.get("state", {}).get("network", {})
        for _iface, info in network.items():
            for addr in info.get("addresses", []):
                if addr.get("family") == "inet" and addr.get("scope") == "global":
                    return addr["address"]
    raise IncusError(f"No global IPv4 address found for container {name}")


def add_proxy_device(
    name: str,
    device_name: str,
    host_port: int,
    container_port: int,
    *,
    connect_host: str = "127.0.0.1",
) -> None:
    """Add a TCP proxy device forwarding host_port -> container_port.

    Default behaviour (``connect_host="127.0.0.1"``): the proxy listens on
    ``0.0.0.0:<host_port>`` on the host and forwards to
    ``127.0.0.1:<container_port>`` inside the container.  The device is
    automatically removed when the container is deleted.

    Pass a non-loopback *connect_host* (e.g. a sibling container's global
    IPv4) to configure a **self-proxy** on the calling instance.  In this
    mode the proxy listens on the instance's own loopback
    (``127.0.0.1:<host_port>``) and connects to
    ``<connect_host>:<container_port>``.  ``bind=container`` is added so
    the listen address lives in the container's network namespace, making
    ``localhost:<host_port>`` work from inside the caller — the same
    contract callers rely on from the host.
    """
    if connect_host == "127.0.0.1":
        # Default host-side proxy: expose a service running inside the container.
        listen_addr = f"tcp:0.0.0.0:{host_port}"
        extra_args: list[str] = []
    else:
        # Self-proxy: forward caller's loopback to a sibling container's IP.
        listen_addr = f"tcp:127.0.0.1:{host_port}"
        extra_args = ["bind=container"]

    result = subprocess.run(
        [
            "incus",
            "config",
            "device",
            "add",
            name,
            device_name,
            "proxy",
            f"listen={listen_addr}",
            f"connect=tcp:{connect_host}:{container_port}",
            *extra_args,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise IncusError(
            f"Failed to add proxy device {device_name} on {name}: "
            f"{result.stderr.strip()}"
        )


def running_inside_incus_instance() -> str | None:
    """Return the calling instance's name if running inside an Incus container
    with parent-daemon access; otherwise return ``None``.

    **Detection signal**: ``INCUS_SOCKET`` environment variable pointing at an
    existing unix socket.  This is the explicit configuration the parent host
    sets to expose its daemon inside the instance.  Plain instance hosting
    (without parent-daemon socket access) returns ``None`` — there is nothing
    useful we can do without the parent daemon socket anyway.

    **Instance name**: ``socket.gethostname()`` — Incus sets each instance's
    hostname to its instance name by default.  Callers that override the
    hostname are outside the scope of this detection.

    Returns the instance name as a non-empty string, or ``None`` if:

    * ``INCUS_SOCKET`` is unset, or
    * ``INCUS_SOCKET`` points to a path that does not exist, or
    * the path exists but is not a unix socket.
    """
    incus_socket = os.environ.get("INCUS_SOCKET")
    if not incus_socket:
        return None
    try:
        st = os.stat(incus_socket)
    except OSError:
        return None
    if not stat.S_ISSOCK(st.st_mode):
        return None
    name = socket.gethostname()
    return name or None
