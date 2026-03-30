# Copyright (c) Microsoft. All rights reserved.

"""E2E test for the amplifier-chat profile.

Launches the amplifier-chat profile, verifies:
- The launch JSON includes access URLs and container IP
- The health endpoint is reachable from the host via the proxy device
- The chat UI serves a 200 response
- A session can be created and a prompt executed through the LLM

Prerequisites:
- Incus running
- ANTHROPIC_API_KEY set

Run with::

    uv run pytest tests/test_e2e_amplifier_chat.py --run-e2e -v -s
"""

import json
import urllib.request
import urllib.error

import pytest

from helpers import poll_readiness, run_cli, run_cli_json


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def dtu_env(require_anthropic_key):
    """Launch the amplifier-chat profile and tear down after all tests."""
    data, _ = run_cli_json("launch", "amplifier-chat", timeout=900)
    assert isinstance(data, dict), "Expected launch to return a JSON object"
    # Wait for amplifierd prewarm to finish so the full app is ready.
    poll_readiness(data["id"], timeout=120, interval=3)
    yield data
    run_cli("destroy", data["id"], timeout=60)


# -- Tests ------------------------------------------------------------------


@pytest.mark.e2e
def test_launch_includes_access_urls(dtu_env):
    """Launch JSON should include access URLs with the proxy-forwarded port."""
    assert "access" in dtu_env, f"Missing 'access' in launch output: {dtu_env}"
    urls = dtu_env["access"]
    assert len(urls) >= 1, "Expected at least one access URL"
    assert urls[0]["label"] == "Chat UI"
    assert "localhost:8410" in urls[0]["url"]


@pytest.mark.e2e
def test_launch_includes_container_ip(dtu_env):
    """Launch JSON should include the container IP."""
    assert "container_ip" in dtu_env, (
        f"Missing 'container_ip' in launch output: {dtu_env}"
    )
    ip = dtu_env["container_ip"]
    assert ip.count(".") == 3, f"Expected IPv4 address, got: {ip}"


@pytest.mark.e2e
def test_health_via_localhost(dtu_env):
    """Health endpoint should be reachable via localhost proxy device."""
    url = "http://localhost:8410/chat/health"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read())
    assert body["status"] == "ok", f"Unexpected health response: {body}"
    assert body["plugin"] == "chat"


@pytest.mark.e2e
def test_chat_ui_serves_200(dtu_env):
    """The chat SPA should serve a 200 via the proxy device."""
    url = "http://localhost:8410/chat/"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
        body = resp.read()
    # The SPA is ~429KB -- sanity check it's not an error page
    assert len(body) > 10_000, f"Response too small ({len(body)} bytes)"


@pytest.mark.e2e
def test_health_via_container_ip(dtu_env):
    """Health endpoint should also be reachable via the direct container IP."""
    ip = dtu_env["container_ip"]
    url = f"http://{ip}:8410/chat/health"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read())
    assert body["status"] == "ok"


@pytest.mark.e2e
def test_session_execute(dtu_env):
    """Create a session and execute a prompt to verify the LLM provider works."""
    ip = dtu_env["container_ip"]
    base = f"http://{ip}:8410"

    # Create session
    req = urllib.request.Request(
        f"{base}/sessions",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        session = json.loads(resp.read())
    session_id = session["session_id"]

    # Execute prompt
    payload = json.dumps({"prompt": "Reply with exactly: DTU_OK"}).encode()
    req = urllib.request.Request(
        f"{base}/sessions/{session_id}/execute",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
    assert "response" in result, f"No response in execute result: {result}"
    assert len(result["response"]) > 0, "Empty response from LLM"
