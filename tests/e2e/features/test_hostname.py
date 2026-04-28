# Copyright (c) Microsoft. All rights reserved.

"""E2E test for .local hostname registration via Avahi mDNS.

Launches the docker-in-incus profile with a custom hostname and verifies
that the hostname appears in launch/status/list output, that the hostname
resolves and is reachable over HTTP, and that destroy cleans up the
registration.

Prerequisites:
    - Incus running with ``security.nesting=true`` on the default profile
    - ``avahi-daemon`` running and ``avahi-utils`` installed
    - No API keys required

Run with::

    uv run pytest tests/test_e2e_hostname.py --run-e2e -v -s
"""

import subprocess
import time
import urllib.request

import pytest

from conftest import register_dtu_instance
from helpers import find_free_port, poll_readiness, run_cli, run_cli_json


@pytest.fixture(scope="module")
def nginx_port() -> int:
    return find_free_port()


@pytest.fixture(scope="module")
def dtu_env_with_hostname(nginx_port):
    """Launch docker-in-incus with a custom hostname and destroy after tests."""
    hostname = f"dtu-hostname-test-{nginx_port}"
    data, _ = run_cli_json(
        "launch",
        "docker-in-incus",
        "--var",
        f"PORT={nginx_port}",
        "--hostname",
        hostname,
        timeout=600,
    )
    assert isinstance(data, dict), f"Expected dict, got {type(data)}"
    register_dtu_instance(data["id"])
    poll_readiness(data["id"], timeout=120, interval=3)
    yield data
    run_cli("destroy", data["id"], timeout=60)


@pytest.mark.e2e
def test_hostname_in_launch_and_status(dtu_env_with_hostname, nginx_port):
    """Launch JSON, status, and list should all include the hostname."""
    fqdn = f"dtu-hostname-test-{nginx_port}.local"

    # Launch output -- url is always localhost, mdns_url uses the hostname
    assert dtu_env_with_hostname["hostname"] == fqdn
    access = dtu_env_with_hostname["access"][0]
    assert "localhost" in access["url"]
    assert "mdns_url" in access
    assert fqdn in access["mdns_url"]

    # status -- should have same access shape
    status, _ = run_cli_json("status", dtu_env_with_hostname["id"], timeout=30)
    assert status["hostname"] == fqdn
    s_access = status["access"][0]
    assert "localhost" in s_access["url"]
    assert fqdn in s_access["mdns_url"]

    # list -- should have same access shape
    envs, _ = run_cli_json("list", timeout=30)
    match = [e for e in envs if e["id"] == dtu_env_with_hostname["id"]]
    assert len(match) == 1
    assert match[0]["hostname"] == fqdn
    l_access = match[0]["access"][0]
    assert "localhost" in l_access["url"]
    assert fqdn in l_access["mdns_url"]


@pytest.mark.e2e
def test_hostname_reachable_over_http(dtu_env_with_hostname, nginx_port):
    """HTTP GET to hostname.local:<port> should return 200 (nginx)."""
    fqdn = dtu_env_with_hostname["hostname"]
    url = f"http://{fqdn}:{nginx_port}/"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200


@pytest.mark.e2e
def test_destroy_cleans_up_hostname(nginx_port):
    """After destroy, the hostname should no longer resolve."""
    port = find_free_port()
    hostname = f"dtu-hostname-destroy-{port}"
    data, _ = run_cli_json(
        "launch",
        "docker-in-incus",
        "--var",
        f"PORT={port}",
        "--hostname",
        hostname,
        timeout=600,
    )
    register_dtu_instance(data["id"])
    poll_readiness(data["id"], timeout=120, interval=3)

    # Resolves before destroy
    result = subprocess.run(
        ["avahi-resolve-host-name", f"{hostname}.local"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0

    # Destroy and verify cleanup
    run_cli("destroy", data["id"], timeout=60)
    time.sleep(2)
    result = subprocess.run(
        ["avahi-resolve-host-name", f"{hostname}.local"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0 or "127.0.0.1" not in result.stdout
