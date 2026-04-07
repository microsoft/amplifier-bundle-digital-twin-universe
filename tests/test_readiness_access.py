# Copyright (c) Microsoft. All rights reserved.

"""Unit tests for host-side access port verification.

These tests spin up a real local HTTP server on an ephemeral port --
no Incus or Docker required.
"""

import http.server
import socket
import threading
import time

import pytest

from amplifier_bundle_digital_twin_universe.profile import PortMapping
from amplifier_bundle_digital_twin_universe.readiness import (
    _poll_port,
    verify_access_ports,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture()
def http_server():
    """Start a throwaway HTTP server, yield its port, shut down after."""
    port = _free_port()
    handler = http.server.SimpleHTTPRequestHandler
    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_poll_port_retries_until_available(http_server):
    """Test that _poll_port retries and succeeds once a server is up,
    and fails with a clear message when nothing is listening."""
    # Happy path: server is already running.
    result = _poll_port(http_server, "/", timeout=5, interval=1)
    assert result["passed"] is True

    # Delayed start: launch a server after a short delay.
    delayed_port = _free_port()
    delayed_server = None

    def _start_delayed():
        nonlocal delayed_server
        time.sleep(2)
        handler = http.server.SimpleHTTPRequestHandler
        delayed_server = http.server.HTTPServer(("127.0.0.1", delayed_port), handler)
        delayed_server.serve_forever()

    t = threading.Thread(target=_start_delayed, daemon=True)
    t.start()

    try:
        result = _poll_port(delayed_port, "/", timeout=10, interval=1)
        assert result["passed"] is True
    finally:
        if delayed_server:
            delayed_server.shutdown()

    # Failure path: nothing listening, should timeout.
    dead_port = _free_port()
    result = _poll_port(dead_port, "/", timeout=3, interval=1)
    assert result["passed"] is False
    assert "message" in result


def test_verify_access_ports_skips_verify_false(http_server):
    """Ports with verify=False should be excluded from results."""
    ports = [
        PortMapping(
            host=http_server,
            container=80,
            path="/",
            verify=True,
            verify_timeout=5,
            verify_interval=1,
        ),
        PortMapping(host=_free_port(), container=80, path="/", verify=False),
    ]
    result = verify_access_ports(ports)
    assert result["verified"] is True
    # Only the verified port should be in results.
    assert str(http_server) in result["ports"]
    assert len(result["ports"]) == 1
