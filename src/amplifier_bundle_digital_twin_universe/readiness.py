# Copyright (c) Microsoft. All rights reserved.

"""Readiness check execution.

Runs declarative readiness checks inside an Incus container and returns
a structured result.  Each check is executed via ``incus exec``.
"""

from __future__ import annotations

import json

from amplifier_bundle_digital_twin_universe import incus
from amplifier_bundle_digital_twin_universe.profile import ReadinessCheck


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
    """Run a command readiness check inside the container."""
    assert check.command is not None
    exit_code, _stdout, stderr = incus.exec_command(
        container_name,
        ["bash", "-c", check.command],
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
