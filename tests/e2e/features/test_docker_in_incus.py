# Copyright (c) Microsoft. All rights reserved.

"""E2E tests for Docker-in-Incus nested container networking.

Launches the docker-in-incus profile and verifies every networking path
that a Docker-spawning application needs:

1. Host -> Incus proxy -> Docker container (inbound)
2. Docker container -> host service (outbound to host)
3. Docker container -> Docker container (inter-container)

Prerequisites:
    - Incus running with ``security.nesting=true`` on the default profile
    - No API keys required

Run with::

    uv run pytest tests/test_e2e_docker_in_incus.py --run-e2e -v -s
"""

import http.server
import json
import threading
import urllib.request

import pytest

from conftest import register_dtu_instance
from helpers import find_free_port, poll_readiness, run_cli, run_cli_json


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def nginx_port() -> int:
    """Port for the nginx-via-Docker proxy device."""
    return find_free_port()


@pytest.fixture(scope="module")
def host_server_port() -> int:
    """Port for the host-side HTTP server used in Docker-to-host tests."""
    return find_free_port()


@pytest.fixture(scope="module")
def host_http_server(host_server_port):
    """Run a minimal HTTP server on the host for Docker-to-host tests.

    Returns (port, url) and tears down after the module.
    """
    handler = http.server.SimpleHTTPRequestHandler
    server = http.server.HTTPServer(("0.0.0.0", host_server_port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield host_server_port
    server.shutdown()


@pytest.fixture(scope="module")
def dtu_env(nginx_port):
    """Launch docker-in-incus profile and destroy after all tests."""
    data, _ = run_cli_json(
        "launch",
        "docker-in-incus",
        "--var",
        f"PORT={nginx_port}",
        timeout=600,
    )
    assert isinstance(data, dict), f"Expected dict, got {type(data)}"
    register_dtu_instance(data["id"])
    poll_readiness(data["id"], timeout=120, interval=3)
    yield data
    run_cli("destroy", data["id"], timeout=60)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_docker_daemon_running(dtu_env):
    """Docker daemon should be running inside the Incus container."""
    data, _ = run_cli_json("exec", dtu_env["id"], "--", "docker", "info")
    assert data["exit_code"] == 0, f"docker info failed: {data['stderr']}"


@pytest.mark.e2e
def test_host_to_docker_via_incus_proxy(dtu_env, nginx_port):
    """Host can reach nginx running in Docker inside Incus via the proxy device."""
    url = f"http://localhost:{nginx_port}/"
    with urllib.request.urlopen(url, timeout=10) as resp:
        body = resp.read().decode()
    assert resp.status == 200
    assert "nginx" in body.lower()


@pytest.mark.e2e
def test_docker_to_host(dtu_env, host_http_server):
    """A Docker container inside Incus can reach a service on the host.

    This exercises the outbound path:
      Docker container -> docker0 bridge -> Incus container -> Incus bridge -> host
    """
    port = host_http_server

    # The Incus container's default gateway is the host.
    # Discover it dynamically, then curl from a Docker container inside Incus.
    script = (
        f"HOST_GW=$(ip route | grep default | awk '{{print $3}}') && "
        f"docker run --rm alpine:latest "
        f"  wget -qO- --timeout=5 http://$HOST_GW:{port}/ > /dev/null 2>&1 "
        f"&& echo OK || echo FAIL"
    )
    data, _ = run_cli_json("exec", dtu_env["id"], "--", "bash", "-c", script)
    assert data["exit_code"] == 0
    assert "OK" in data["stdout"], (
        f"Docker-to-host failed. stdout={data['stdout']}, stderr={data['stderr']}"
    )


@pytest.mark.e2e
def test_docker_to_docker(dtu_env):
    """Two Docker containers can communicate via the Docker bridge network.

    This exercises inter-container networking inside Incus, which is needed
    when an application spawns multiple Docker containers that communicate.
    """
    script = (
        # Start an nginx listener
        "docker run -d --name dtu-listener -p 9090:80 nginx:alpine && "
        "sleep 2 && "
        # From a separate container, hit the listener via the Docker bridge IP
        "docker run --rm alpine:latest "
        "  wget -qO- --timeout=5 http://172.17.0.1:9090/ > /dev/null 2>&1 "
        "&& echo OK || echo FAIL; "
        # Cleanup
        "docker rm -f dtu-listener > /dev/null 2>&1"
    )
    data, _ = run_cli_json("exec", dtu_env["id"], "--", "bash", "-c", script)
    assert data["exit_code"] == 0
    assert "OK" in data["stdout"], (
        f"Docker-to-Docker failed. stdout={data['stdout']}, stderr={data['stderr']}"
    )
