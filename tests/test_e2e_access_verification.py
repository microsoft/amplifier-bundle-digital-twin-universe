# Copyright (c) Microsoft. All rights reserved.

"""E2E test for host-side access port verification via check-readiness.

Launches the docker-in-incus profile (nginx behind a port forward) and
verifies that ``check-readiness`` includes host-side access verification
in its output.  Also confirms that stopping the service causes access
verification to fail.

Prerequisites:
    - Incus running with ``security.nesting=true`` on the default profile
    - No API keys required

Run with::

    uv run pytest tests/test_e2e_access_verification.py --run-e2e -v -s
"""

import json
import socket

import pytest

from conftest import register_dtu_instance
from helpers import poll_readiness, run_cli, run_cli_json


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def nginx_port() -> int:
    return _free_port()


@pytest.fixture(scope="module")
def dtu_env(nginx_port):
    """Launch docker-in-incus and destroy after tests."""
    data, _ = run_cli_json(
        "launch",
        "docker-in-incus",
        "--var",
        f"PORT={nginx_port}",
        timeout=600,
    )
    assert isinstance(data, dict), f"Expected dict, got {type(data)}"
    register_dtu_instance(data["id"])
    # Wait for in-container readiness first (skip access check during polling
    # since the helper uses the CLI which now includes access checks).
    poll_readiness(data["id"], timeout=120, interval=3)
    yield data
    run_cli("destroy", data["id"], timeout=60)


@pytest.mark.e2e
def test_check_readiness_includes_access_verification(dtu_env, nginx_port):
    """check-readiness should include host-side access verification and pass."""
    result = run_cli("check-readiness", dtu_env["id"], timeout=30)
    assert result.returncode == 0, (
        f"check-readiness failed:\n  stdout: {result.stdout}\n  stderr: {result.stderr}"
    )
    data = json.loads(result.stdout)

    # Overall readiness should be True.
    assert data["ready"] is True

    # The access section should be present and verified.
    assert "access" in data, f"Missing 'access' key in output: {data}"
    assert data["access"]["verified"] is True

    # The specific port should be listed and passed.
    port_key = str(nginx_port)
    assert port_key in data["access"]["ports"], (
        f"Port {nginx_port} not in access.ports: {data['access']}"
    )
    assert data["access"]["ports"][port_key]["passed"] is True


@pytest.mark.e2e
def test_access_verification_fails_when_service_down(dtu_env, nginx_port):
    """Stopping the service should cause access verification to fail."""
    # Stop nginx inside the container.
    stop_result, _ = run_cli_json(
        "exec",
        dtu_env["id"],
        "--",
        "docker",
        "stop",
        "nginx-test",
        timeout=30,
    )
    assert stop_result["exit_code"] == 0, f"Failed to stop nginx: {stop_result}"

    # check-readiness should now report failure.
    result = run_cli("check-readiness", dtu_env["id"], timeout=60)

    # Exit code 1 = not ready.
    assert result.returncode == 1, (
        f"Expected exit 1, got {result.returncode}:\n"
        f"  stdout: {result.stdout}\n  stderr: {result.stderr}"
    )
    data = json.loads(result.stdout)
    assert data["ready"] is False

    # Access verification should have failed.
    assert "access" in data
    assert data["access"]["verified"] is False
