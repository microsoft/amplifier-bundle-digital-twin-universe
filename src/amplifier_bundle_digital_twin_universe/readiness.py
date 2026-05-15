# Copyright (c) Microsoft. All rights reserved.

"""Readiness check execution.

Runs declarative readiness checks inside an Incus container and returns
a structured result.  In-container checks are executed via ``incus exec``.
Host-side access verification runs from the host Python process to confirm
that port-forwarded services are reachable through Incus proxy devices.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.request
import urllib.error

from amplifier_bundle_digital_twin_universe import incus
from amplifier_bundle_digital_twin_universe.profile import PortMapping, ReadinessCheck


def _run_http_check(container_name: str, check: ReadinessCheck) -> dict:
    """Run an HTTP readiness check inside the container."""
    assert check.http is not None
    url = check.http.url

    if check.http.expect_json is not None:
        # Capture the body and compare.
        exit_code, stdout, stderr = incus.exec_command(
            container_name,
            ["bash", "-c", f"curl -sf {url}"],
            timeout=10,
        )
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or f"exit {exit_code}"
            return {"passed": False, "message": error_msg}

        try:
            body = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return {
                "passed": False,
                "message": f"invalid JSON response: {stdout[:200]}",
            }

        # Subset match: every key in expect_json must match.
        for key, expected_val in check.http.expect_json.items():
            if body.get(key) != expected_val:
                return {
                    "passed": False,
                    "message": (
                        f"expected {json.dumps(check.http.expect_json)}, "
                        f"got {json.dumps(body)}"
                    ),
                }
        return {"passed": True}
    else:
        # Simple HTTP 200 check.
        exit_code, _stdout, stderr = incus.exec_command(
            container_name,
            ["bash", "-c", f"curl -sf {url} > /dev/null"],
            timeout=10,
        )
        if exit_code != 0:
            error_msg = stderr.strip() or f"exit {exit_code}"
            return {"passed": False, "message": error_msg}
        return {"passed": True}


def _run_tcp_check(container_name: str, check: ReadinessCheck) -> dict:
    """Run a TCP port readiness check inside the container."""
    assert check.tcp is not None
    port = check.tcp.port
    exit_code, _stdout, stderr = incus.exec_command(
        container_name,
        ["bash", "-c", f"echo > /dev/tcp/localhost/{port}"],
        timeout=10,
    )
    if exit_code != 0:
        error_msg = stderr.strip() or "connection refused"
        return {"passed": False, "message": error_msg}
    return {"passed": True}


def _run_command_check(container_name: str, check: ReadinessCheck) -> dict:
    """Run a command readiness check inside the container.

    Uses ``bash -lc`` (login shell) so the command sees the same environment
    that ``provision.setup_cmds`` and ``exec``/``exec --stream`` run under,
    including the PATH and passthrough env vars written to
    ``/etc/profile.d/dtu-env.sh``. Without ``-l``, readiness commands run in
    a bare non-login shell and cannot find binaries installed to
    ``/root/.local/bin`` (uv, amplifier) without an inline ``PATH=...`` prefix.
    """
    assert check.command is not None
    exit_code, _stdout, stderr = incus.exec_command(
        container_name,
        ["bash", "-lc", check.command],
        timeout=30,
    )
    if exit_code != 0:
        last_line = stderr.strip().splitlines()[-1] if stderr.strip() else ""
        error_msg = f"exit {exit_code}" + (f": {last_line}" if last_line else "")
        return {"passed": False, "message": error_msg}
    return {"passed": True}


def check_readiness(container_name: str, checks: list[ReadinessCheck]) -> dict:
    """Run all readiness checks and return the aggregated result.

    Returns a dict with ``ready`` (bool), ``message`` (str), and
    optionally ``checks`` (dict of per-check results when not ready).
    """
    results: dict[str, dict] = {}
    for check in checks:
        if check.http is not None:
            results[check.name] = _run_http_check(container_name, check)
        elif check.tcp is not None:
            results[check.name] = _run_tcp_check(container_name, check)
        elif check.command is not None:
            results[check.name] = _run_command_check(container_name, check)

    passed_count = sum(1 for r in results.values() if r["passed"])
    total = len(results)

    if passed_count == total:
        return {"ready": True, "message": "all checks passed"}

    return {
        "ready": False,
        "message": f"{passed_count}/{total} checks passed",
        "checks": results,
    }


# ---------------------------------------------------------------------------
# Host-side access verification
# ---------------------------------------------------------------------------


def _check_port_once(host_port: int, path: str) -> dict:
    """Try TCP connect + HTTP GET from the host process.  Single attempt."""
    # TCP connect
    try:
        sock = socket.create_connection(("localhost", host_port), timeout=5)
        sock.close()
    except OSError as exc:
        return {"passed": False, "message": f"tcp connect failed: {exc}"}

    # HTTP check (if path is set)
    if path:
        url = f"http://localhost:{host_port}{path}"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
            if 200 <= status < 400:
                return {"passed": True}
            return {"passed": False, "message": f"http {status} from {url}"}
        except urllib.error.HTTPError as exc:
            if 200 <= exc.code < 400:
                return {"passed": True}
            return {"passed": False, "message": f"http {exc.code} from {url}"}
        except OSError as exc:
            return {"passed": False, "message": f"http request failed: {exc}"}

    return {"passed": True}


def _poll_port(
    host_port: int,
    path: str,
    timeout: int,
    interval: int,
) -> dict:
    """Retry :func:`_check_port_once` until it passes or *timeout* expires."""
    deadline = time.monotonic() + timeout
    result: dict = {"passed": False, "message": "timeout before first attempt"}
    while time.monotonic() < deadline:
        result = _check_port_once(host_port, path)
        if result["passed"]:
            return result
        time.sleep(interval)
    return result


def verify_access_ports(ports: list[PortMapping]) -> dict:
    """Verify ``access.ports`` are reachable from the host.

    For each port with ``verify=True``, performs a TCP connect and optional
    HTTP GET from the host Python process (outside the container), retrying
    up to ``verify_timeout`` seconds.

    Returns a dict with ``verified`` (bool), ``message`` (str), and
    ``ports`` (per-port results keyed by host port number).
    """
    results: dict[str, dict] = {}
    verifiable = [pm for pm in ports if pm.verify]

    if not verifiable:
        return {"verified": True, "message": "no ports to verify"}

    for pm in verifiable:
        results[str(pm.host)] = _poll_port(
            pm.host, pm.path, pm.verify_timeout, pm.verify_interval
        )

    passed = all(r["passed"] for r in results.values())
    return {
        "verified": passed,
        "message": "all access ports reachable" if passed else "some ports unreachable",
        "ports": results,
    }
