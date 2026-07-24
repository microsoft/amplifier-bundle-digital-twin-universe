# Copyright (c) Microsoft. All rights reserved.

"""E2E tests for exec --stream.

Launches a minimal container and exercises the --stream flag on the exec
command, verifying real-time output passthrough and backward compatibility
with the default JSON mode.

Prerequisites: Incus only. No Docker, Gitea, or API keys.

Run with:
    uv run pytest tests/test_e2e_exec_stream.py --run-e2e -v -s
"""

import json
import sys

import pytest

from conftest import register_dtu_instance
from helpers import run_cli, run_cli_json


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stream_profile(tmp_path_factory):
    """Write a minimal profile for exec --stream tests."""
    d = tmp_path_factory.mktemp("dtu-exec-stream-profiles")
    profile = d / "exec-stream-test.yaml"
    profile.write_text(
        """\
name: exec-stream-test
description: Minimal container for exec --stream E2E tests

base:
  image: ubuntu:24.04
"""
    )
    return str(profile)


@pytest.fixture(scope="module")
def dtu_env(stream_profile):
    """Launch a DTU from the minimal profile, yield metadata, destroy on teardown."""
    print("[E2E-exec-stream] Launching exec-stream-test profile...", file=sys.stderr)
    data, _ = run_cli_json("launch", stream_profile, timeout=600)
    assert isinstance(data, dict), "Expected launch to return a JSON object"
    register_dtu_instance(data["id"])
    yield data
    run_cli("destroy", data["id"], timeout=60)


# ---------------------------------------------------------------------------
# --stream tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestExecStream:
    """Verify exec --stream passes output through in real-time."""

    def test_stream_stdout(self, dtu_env):
        """Basic stdout passthrough."""
        result = run_cli(
            "exec", "--stream", dtu_env["id"], "--", "echo", "hello-stream"
        )
        assert result.returncode == 0
        assert "hello-stream" in result.stdout

    def test_stream_stderr(self, dtu_env):
        """stderr is passed through separately."""
        result = run_cli(
            "exec",
            "--stream",
            dtu_env["id"],
            "--",
            "bash",
            "-c",
            "echo err-marker >&2",
        )
        assert result.returncode == 0
        assert "err-marker" in result.stderr

    def test_stream_mixed_output(self, dtu_env):
        """stdout and stderr arrive on their respective streams."""
        result = run_cli(
            "exec",
            "--stream",
            dtu_env["id"],
            "--",
            "bash",
            "-c",
            "echo out-marker; echo err-marker >&2",
        )
        assert result.returncode == 0
        assert "out-marker" in result.stdout
        assert "err-marker" in result.stderr

    def test_stream_multiline(self, dtu_env):
        """Multiple lines of output are all captured."""
        result = run_cli(
            "exec",
            "--stream",
            dtu_env["id"],
            "--",
            "bash",
            "-c",
            "for i in 1 2 3; do echo line-$i; done",
        )
        assert result.returncode == 0
        for i in range(1, 4):
            assert f"line-{i}" in result.stdout

    def test_stream_exit_code_propagated(self, dtu_env):
        """Non-zero exit code is forwarded to the CLI process."""
        result = run_cli("exec", "--stream", dtu_env["id"], "--", "false")
        assert result.returncode != 0

    def test_stream_no_json_wrapper(self, dtu_env):
        """--stream output is raw text, not a JSON envelope."""
        result = run_cli("exec", "--stream", dtu_env["id"], "--", "echo", "raw-text")
        assert result.returncode == 0
        try:
            parsed = json.loads(result.stdout)
            # If it parses as JSON and looks like the exec envelope, fail
            assert "exit_code" not in parsed, (
                "Expected raw text output, got JSON exec envelope"
            )
        except (json.JSONDecodeError, ValueError):
            pass  # Expected: raw text, not JSON


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestExecDefaultJson:
    """Verify default exec (no --stream) still returns JSON."""

    def test_default_exec_returns_json(self, dtu_env):
        """Without --stream, exec returns the JSON envelope."""
        data, _ = run_cli_json("exec", dtu_env["id"], "--", "echo", "json-mode")
        assert data["exit_code"] == 0
        assert "json-mode" in data["stdout"]
        assert "stderr" in data
