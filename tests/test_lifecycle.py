# Copyright (c) Microsoft. All rights reserved.

"""Integration tests for lifecycle commands.

These tests exercise the container lifecycle only; they do not depend on
the amplifier-user-sim profile or any external services.

Run with: uv run pytest tests/test_lifecycle.py --run-integration -v
"""

import pytest

from helpers import run_cli, run_cli_json


@pytest.fixture(scope="module")
def lifecycle_profile(tmp_path_factory):
    """Write a minimal profile for lifecycle smoke tests."""
    profile_dir = tmp_path_factory.mktemp("lifecycle-profile")
    profile_path = profile_dir / "lifecycle-smoke.yaml"
    profile_path.write_text(
        """\
name: lifecycle-smoke
description: Minimal profile for lifecycle smoke tests

base:
  image: ubuntu:24.04
"""
    )
    return str(profile_path)


@pytest.fixture(scope="module")
def dtu_env(lifecycle_profile):
    """Create a minimal DTU environment shared by this module."""
    data, _ = run_cli_json("launch", lifecycle_profile, timeout=120)
    assert isinstance(data, dict), "Expected launch to return a JSON object"
    yield data
    run_cli("destroy", data["id"], timeout=30)


# ---------------------------------------------------------------------------
# Phase 1: launch
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_launch_returns_valid_json(dtu_env):
    """Verify the launch command returns all required fields."""
    required_keys = {"id", "name", "profile", "status", "created_at"}
    assert required_keys.issubset(dtu_env.keys()), (
        f"Missing keys: {required_keys - dtu_env.keys()}"
    )


@pytest.mark.integration
def test_launch_status_is_running(dtu_env):
    """Verify the launched environment reports status as running."""
    assert dtu_env["status"] == "running"


@pytest.mark.integration
def test_launch_profile_matches(dtu_env):
    """Verify the profile field matches what was requested."""
    assert dtu_env["profile"] == "lifecycle-smoke"


# ---------------------------------------------------------------------------
# Phase 2: exec
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_exec_command_returns_json(dtu_env):
    """Verify exec with a command returns JSON with exit_code and stdout."""
    data, _ = run_cli_json("exec", dtu_env["id"], "--", "echo", "hello")
    assert data["exit_code"] == 0
    assert "hello" in data["stdout"]


@pytest.mark.integration
def test_exec_nonzero_exit(dtu_env):
    """Verify exec propagates non-zero exit codes."""
    data, _ = run_cli_json("exec", dtu_env["id"], "--", "false")
    assert data["exit_code"] != 0


# ---------------------------------------------------------------------------
# Phase 3: destroy (standalone, creates own environment)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_destroy_removes_environment():
    """Standalone: launch an environment, destroy it, verify it's gone."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="dtu-lifecycle-destroy-") as tmp:
        profile_path = Path(tmp) / "destroy-smoke.yaml"
        profile_path.write_text(
            """\
name: lifecycle-smoke-destroy
description: Minimal profile for destroy smoke test

base:
  image: ubuntu:24.04
"""
        )
        data, _ = run_cli_json("launch", str(profile_path), timeout=120)
        env_id = data["id"]

        destroy_data, _ = run_cli_json("destroy", env_id)
        assert destroy_data["id"] == env_id
        assert destroy_data["destroyed"] is True

        result = run_cli("exec", env_id, "--", "echo", "hello")
        assert result.returncode != 0, "exec should fail for destroyed environment"




# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_destroy_nonexistent_id():
    """Destroy with a nonexistent ID should fail with a clear error."""
    result = run_cli("destroy", "nonexistent-id-12345")
    assert result.returncode != 0


@pytest.mark.integration
def test_exec_nonexistent_id():
    """Exec with a nonexistent ID should fail with a clear error."""
    result = run_cli("exec", "nonexistent-id-12345", "--", "echo", "hello")
    assert result.returncode != 0
