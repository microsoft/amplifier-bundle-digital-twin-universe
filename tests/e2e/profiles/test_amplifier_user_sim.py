# Copyright (c) Microsoft. All rights reserved.

"""Full end-to-end test for the amplifier-user-sim profile.

This suite mirrors amplifier-core and amplifier-module-provider-anthropic from
GitHub into Gitea, pushes local temp-clone mutations on top, then verifies the
launched environment picks up both changes.

Run with:
    uv run pytest tests/test_e2e_amplifier_user_sim.py --run-e2e -v -s
"""

import re
import sys
from pathlib import Path

import pytest

from conftest import register_dtu_instance
from helpers import (
    clone_local_repo,
    commit_all,
    gitea_clone_url,
    github_repo_name,
    mirror_repo_to_gitea,
    push_repo_to_gitea,
    run_cli,
    run_cli_json,
    run_gitea_cli,
    run_gitea_cli_json,
)

AMPLIFIER_CORE_GITHUB_REPO = "https://github.com/microsoft/amplifier-core"
PROVIDER_GITHUB_REPO = (
    "https://github.com/microsoft/amplifier-module-provider-anthropic"
)
AMPLIFIER_CORE_VERSION = "99.0.0"
UPDATED_CORE_VERSION = "99.1.0"
PROVIDER_MARKER = "AMPLIFIER_PROVIDER_ANTHROPIC_TEST_MARKER"
UPDATED_PROVIDER_MARKER = "AMPLIFIER_PROVIDER_ANTHROPIC_UPDATED_MARKER"
EXPECTED_RESPONSE = "HELLO_DTU_PROVIDER"
WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
AMPLIFIER_CORE_LOCAL_REPO = WORKSPACE_ROOT / "amplifier-core"
PROVIDER_LOCAL_REPO = WORKSPACE_ROOT / "amplifier-module-provider-anthropic"


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"Expected to find {old!r} in {path}"
    path.write_text(text.replace(old, new, 1))


def _replace_first_version(path: Path, prefix: str, new_value: str) -> None:
    """Find the first ``<prefix> = "..."`` assignment in ``path`` and rewrite its value.

    ``prefix`` is either ``version`` (for TOML manifests) or ``__version__`` (for
    Python modules). Asserts a match was found so the test fails loudly if the file
    shape changes upstream (field renamed, removed, etc.) -- but tolerates whatever
    the current version string happens to be, so upstream version bumps don't break
    the test.
    """
    text = path.read_text()
    pattern = re.compile(rf'{re.escape(prefix)}\s*=\s*"([^"]+)"')
    match = pattern.search(text)
    assert match, f'No {prefix} = "..." assignment found in {path}'
    path.write_text(text.replace(match.group(0), f'{prefix} = "{new_value}"', 1))


def _mutate_amplifier_core(repo_dir: Path) -> None:
    _replace_first_version(
        repo_dir / "pyproject.toml", "version", AMPLIFIER_CORE_VERSION
    )
    _replace_first_version(
        repo_dir / "bindings/python/Cargo.toml", "version", AMPLIFIER_CORE_VERSION
    )
    _replace_first_version(
        repo_dir / "crates/amplifier-core/Cargo.toml", "version", AMPLIFIER_CORE_VERSION
    )
    _replace_first_version(
        repo_dir / "python/amplifier_core/__init__.py",
        "__version__",
        AMPLIFIER_CORE_VERSION,
    )


def _inject_provider_marker(repo_dir: Path) -> None:
    init_py = repo_dir / "amplifier_module_provider_anthropic/__init__.py"
    _replace_once(
        init_py,
        "    if not api_key:\n",
        f'    logger.warning("{PROVIDER_MARKER}")\n\n    if not api_key:\n',
    )


@pytest.fixture(scope="module")
def gitea_env(free_port):
    print(f"[E2E-full] Creating Gitea on port {free_port}...", file=sys.stderr)
    data, _ = run_gitea_cli_json("create", "--port", str(free_port), timeout=120)
    assert isinstance(data, dict)
    yield data
    run_gitea_cli("destroy", data["id"], timeout=30)


@pytest.fixture(scope="module")
def mirrored_gitea_env(gitea_env, require_github_token, tmp_path_factory):
    print("[E2E-full] Mirroring amplifier-core into Gitea...", file=sys.stderr)
    mirror_repo_to_gitea(
        gitea_env["id"], AMPLIFIER_CORE_GITHUB_REPO, require_github_token
    )
    print(
        "[E2E-full] Mirroring amplifier-module-provider-anthropic into Gitea...",
        file=sys.stderr,
    )
    mirror_repo_to_gitea(gitea_env["id"], PROVIDER_GITHUB_REPO, require_github_token)

    core_repo_dir = clone_local_repo(
        AMPLIFIER_CORE_LOCAL_REPO,
        tmp_path_factory.mktemp("amplifier-core-clone") / "amplifier-core",
    )
    _mutate_amplifier_core(core_repo_dir)
    commit_all(core_repo_dir, "test: override amplifier-core version")
    push_repo_to_gitea(
        core_repo_dir,
        gitea_clone_url(
            gitea_env["port"],
            gitea_env["token"],
            github_repo_name(AMPLIFIER_CORE_GITHUB_REPO),
        ),
    )

    provider_repo_dir = clone_local_repo(
        PROVIDER_LOCAL_REPO,
        tmp_path_factory.mktemp("provider-clone")
        / "amplifier-module-provider-anthropic",
    )
    _inject_provider_marker(provider_repo_dir)
    commit_all(provider_repo_dir, "test: inject provider warning marker")
    push_repo_to_gitea(
        provider_repo_dir,
        gitea_clone_url(
            gitea_env["port"],
            gitea_env["token"],
            github_repo_name(PROVIDER_GITHUB_REPO),
        ),
    )

    return gitea_env


@pytest.fixture(scope="module")
def dtu_env(mirrored_gitea_env, require_anthropic_key):
    gitea_url = mirrored_gitea_env["gitea_url"]
    print(f"[E2E-full] Launching amplifier-user-sim ({gitea_url})...", file=sys.stderr)
    data, _ = run_cli_json(
        "launch",
        "amplifier-user-sim",
        "--var",
        f"GITEA_URL={gitea_url}",
        "--var",
        f"GITEA_TOKEN={mirrored_gitea_env['token']}",
        timeout=900,
    )
    assert isinstance(data, dict)
    register_dtu_instance(data["id"])
    yield data
    run_cli("destroy", data["id"], timeout=60)


@pytest.mark.e2e
def test_amplifier_user_sim_uses_overridden_dependencies(dtu_env):
    """Verify amplifier-core and provider-anthropic both come from Gitea."""
    version_data, _ = run_cli_json(
        "exec",
        dtu_env["id"],
        "--",
        "bash",
        "-lc",
        (
            "TOOL_PYTHON=$(find /root/.local/share/uv/tools/amplifier "
            '-path "*/bin/python3" | head -1); '
            '"$TOOL_PYTHON" -c '
            "'import amplifier_core._engine as engine; print(engine.__version__)'"
        ),
        timeout=120,
    )
    assert version_data["exit_code"] == 0, (
        f"Failed to read amplifier_core version:\n"
        f"stdout: {version_data['stdout']}\nstderr: {version_data['stderr']}"
    )
    assert AMPLIFIER_CORE_VERSION in version_data["stdout"]

    run_data, _ = run_cli_json(
        "exec",
        dtu_env["id"],
        "--",
        "bash",
        "-lc",
        (
            "cd /home/user/project && "
            f"amplifier run 'respond with exactly: {EXPECTED_RESPONSE}'"
        ),
        timeout=180,
    )
    combined_output = f"{run_data['stdout']}\n{run_data['stderr']}"
    assert run_data["exit_code"] == 0, (
        f"amplifier run failed (exit {run_data['exit_code']}):\n"
        f"stdout: {run_data['stdout']}\nstderr: {run_data['stderr']}"
    )
    assert EXPECTED_RESPONSE in run_data["stdout"]
    assert PROVIDER_MARKER in combined_output


# -- Update helpers ---------------------------------------------------------


def _mutate_amplifier_core_for_update(repo_dir: Path) -> None:
    """Replace the initial test version (AMPLIFIER_CORE_VERSION) with UPDATED_CORE_VERSION."""
    _replace_first_version(repo_dir / "pyproject.toml", "version", UPDATED_CORE_VERSION)
    _replace_first_version(
        repo_dir / "bindings/python/Cargo.toml", "version", UPDATED_CORE_VERSION
    )
    _replace_first_version(
        repo_dir / "crates/amplifier-core/Cargo.toml", "version", UPDATED_CORE_VERSION
    )
    _replace_first_version(
        repo_dir / "python/amplifier_core/__init__.py",
        "__version__",
        UPDATED_CORE_VERSION,
    )


def _inject_updated_provider_marker(repo_dir: Path) -> None:
    """Replace the initial marker with the updated marker."""
    init_py = repo_dir / "amplifier_module_provider_anthropic/__init__.py"
    _replace_once(
        init_py,
        f'    logger.warning("{PROVIDER_MARKER}")',
        f'    logger.warning("{UPDATED_PROVIDER_MARKER}")',
    )


# -- Update test ------------------------------------------------------------


@pytest.mark.e2e
def test_update_picks_up_new_code(dtu_env, mirrored_gitea_env, tmp_path_factory):
    """Verify ``amplifier-digital-twin update`` pulls fresh code changes.

    Phase 1: verify initial state (same as launch test).
    Phase 2: push *new* mutations to Gitea (updated version + marker).
    Phase 3: run ``update`` and check return JSON.
    Phase 4: verify the updated code is live inside the same container.
    """
    env_id = dtu_env["id"]
    gitea_url = mirrored_gitea_env["gitea_url"]
    token = mirrored_gitea_env["token"]
    port = mirrored_gitea_env["port"]

    # -- Phase 1: verify initial state --
    version_data, _ = run_cli_json(
        "exec",
        env_id,
        "--",
        "bash",
        "-lc",
        (
            "TOOL_PYTHON=$(find /root/.local/share/uv/tools/amplifier "
            '-path "*/bin/python3" | head -1); '
            '"$TOOL_PYTHON" -c '
            "'import amplifier_core._engine as engine; print(engine.__version__)'"
        ),
        timeout=120,
    )
    assert version_data["exit_code"] == 0
    assert AMPLIFIER_CORE_VERSION in version_data["stdout"]

    # -- Phase 2: push updated mutations to Gitea --
    print("[E2E-update] Pushing updated amplifier-core to Gitea...", file=sys.stderr)
    core_repo_dir = clone_local_repo(
        AMPLIFIER_CORE_LOCAL_REPO,
        tmp_path_factory.mktemp("core-update-clone") / "amplifier-core",
    )
    _mutate_amplifier_core(core_repo_dir)  # initial mutation (99.0.0)
    _mutate_amplifier_core_for_update(core_repo_dir)  # then 99.0.0 -> 99.1.0
    commit_all(core_repo_dir, "test: update amplifier-core version for update test")
    push_repo_to_gitea(
        core_repo_dir,
        gitea_clone_url(port, token, github_repo_name(AMPLIFIER_CORE_GITHUB_REPO)),
    )

    print("[E2E-update] Pushing updated provider marker to Gitea...", file=sys.stderr)
    provider_repo_dir = clone_local_repo(
        PROVIDER_LOCAL_REPO,
        tmp_path_factory.mktemp("provider-update-clone")
        / "amplifier-module-provider-anthropic",
    )
    _inject_provider_marker(provider_repo_dir)  # initial marker
    _inject_updated_provider_marker(provider_repo_dir)  # replace with updated
    commit_all(
        provider_repo_dir, "test: inject updated provider marker for update test"
    )
    push_repo_to_gitea(
        provider_repo_dir,
        gitea_clone_url(port, token, github_repo_name(PROVIDER_GITHUB_REPO)),
    )

    # -- Phase 3: run update --
    print("[E2E-update] Running amplifier-digital-twin update...", file=sys.stderr)
    update_data, _ = run_cli_json(
        "update",
        env_id,
        "--var",
        f"GITEA_URL={gitea_url}",
        "--var",
        f"GITEA_TOKEN={token}",
        timeout=900,
    )
    assert update_data["status"] == "updated"
    assert update_data["pypi_refreshed"] is True
    assert update_data["cmds_run"] >= 1

    # -- Phase 4: verify updated state --
    print("[E2E-update] Verifying updated amplifier-core version...", file=sys.stderr)
    version_data, _ = run_cli_json(
        "exec",
        env_id,
        "--",
        "bash",
        "-lc",
        (
            "TOOL_PYTHON=$(find /root/.local/share/uv/tools/amplifier "
            '-path "*/bin/python3" | head -1); '
            '"$TOOL_PYTHON" -c '
            "'import amplifier_core._engine as engine; print(engine.__version__)'"
        ),
        timeout=120,
    )
    assert version_data["exit_code"] == 0, (
        f"Failed to read updated version:\n"
        f"stdout: {version_data['stdout']}\nstderr: {version_data['stderr']}"
    )
    assert UPDATED_CORE_VERSION in version_data["stdout"], (
        f"Expected {UPDATED_CORE_VERSION} in version output, "
        f"got: {version_data['stdout']}"
    )

    print("[E2E-update] Verifying updated provider marker...", file=sys.stderr)
    run_data, _ = run_cli_json(
        "exec",
        env_id,
        "--",
        "bash",
        "-lc",
        (
            "cd /home/user/project && "
            f"amplifier run 'respond with exactly: {EXPECTED_RESPONSE}'"
        ),
        timeout=180,
    )
    combined_output = f"{run_data['stdout']}\n{run_data['stderr']}"
    assert run_data["exit_code"] == 0, (
        f"amplifier run failed (exit {run_data['exit_code']}):\n"
        f"stdout: {run_data['stdout']}\nstderr: {run_data['stderr']}"
    )
    assert UPDATED_PROVIDER_MARKER in combined_output, (
        f"Expected {UPDATED_PROVIDER_MARKER} in output, got:\n{combined_output}"
    )
