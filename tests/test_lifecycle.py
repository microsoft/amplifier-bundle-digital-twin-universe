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
# Phase 1b: launch stores Incus metadata
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_launch_sets_incus_config_keys(dtu_env):
    """Verify launch stores metadata as Incus user config keys.

    This is the foundation for status and list -- if config keys are not
    set, neither command can work.
    """
    import subprocess

    env_id = dtu_env["id"]

    for key, expected in [
        ("user.dtu.managed-by", "amplifier-digital-twin"),
        ("user.dtu.profile", "lifecycle-smoke"),
    ]:
        result = subprocess.run(
            ["incus", "config", "get", env_id, key],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"Failed to get {key}: {result.stderr}"
        assert result.stdout.strip() == expected, (
            f"{key}: expected {expected!r}, got {result.stdout.strip()!r}"
        )

    # created-at should be a non-empty ISO 8601 string
    result = subprocess.run(
        ["incus", "config", "get", env_id, "user.dtu.created-at"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip(), "user.dtu.created-at should be non-empty"


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
# Phase 3: status
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_status_returns_valid_json(dtu_env):
    """Verify status returns the expected fields for a running environment."""
    data, _ = run_cli_json("status", dtu_env["id"])
    required_keys = {"id", "profile", "status", "created_at"}
    assert required_keys.issubset(data.keys()), (
        f"Missing keys: {required_keys - data.keys()}"
    )


@pytest.mark.integration
def test_status_matches_launch(dtu_env):
    """Verify status fields are consistent with what launch returned."""
    data, _ = run_cli_json("status", dtu_env["id"])
    assert data["id"] == dtu_env["id"]
    assert data["profile"] == dtu_env["profile"]
    assert data["status"] == "Running"


@pytest.mark.integration
def test_status_nonexistent_id():
    """Status with a nonexistent ID should fail."""
    result = run_cli("status", "nonexistent-id-12345")
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# Phase 4: list
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_list_includes_running_environment(dtu_env):
    """Verify list includes the environment we launched."""
    data, _ = run_cli_json("list")
    assert isinstance(data, list)
    ids = [env["id"] for env in data]
    assert dtu_env["id"] in ids


@pytest.mark.integration
def test_list_entry_matches_status(dtu_env):
    """Verify the list entry has the same shape and values as status."""
    list_data, _ = run_cli_json("list")
    status_data, _ = run_cli_json("status", dtu_env["id"])
    entry = next(e for e in list_data if e["id"] == dtu_env["id"])
    assert entry == status_data


# ---------------------------------------------------------------------------
# Phase 5: destroy removes from list (standalone, creates own environment)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_destroy_removes_from_list():
    """Launch, destroy, verify the environment is gone from list."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="dtu-lifecycle-list-") as tmp:
        profile_path = Path(tmp) / "list-smoke.yaml"
        profile_path.write_text(
            """\
name: lifecycle-smoke-list
description: Minimal profile for list removal test

base:
  image: ubuntu:24.04
"""
        )
        data, _ = run_cli_json("launch", str(profile_path), timeout=120)
        env_id = data["id"]

        # Confirm it shows up in list
        list_data, _ = run_cli_json("list")
        ids_before = [e["id"] for e in list_data]
        assert env_id in ids_before

        # Destroy and confirm it's gone
        run_cli_json("destroy", env_id)
        list_data, _ = run_cli_json("list")
        ids_after = [e["id"] for e in list_data]
        assert env_id not in ids_after


# ---------------------------------------------------------------------------
# Phase 6: destroy (standalone, original test)
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
