# Copyright (c) Microsoft. All rights reserved.

"""Fast E2E test for provider rewrite with a single rewritten module.

This suite mirrors amplifier-module-provider-anthropic into Gitea, pushes a
local warning marker on top, launches the single-module profile, and verifies
that running Amplifier loads the rewritten provider.

Run with:
    uv run pytest tests/test_e2e_amplifier_user_sim_single_module.py --run-e2e -v -s
"""

import sys
from pathlib import Path

import pytest

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

PROVIDER_GITHUB_REPO = (
    "https://github.com/microsoft/amplifier-module-provider-anthropic"
)
PROVIDER_MARKER = "AMPLIFIER_PROVIDER_ANTHROPIC_SINGLE_MODULE_TEST_MARKER"
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROVIDER_LOCAL_REPO = WORKSPACE_ROOT / "amplifier-module-provider-anthropic"


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"Expected to find {old!r} in {path}"
    path.write_text(text.replace(old, new, 1))


def _inject_provider_marker(repo_dir: Path) -> None:
    init_py = repo_dir / "amplifier_module_provider_anthropic/__init__.py"
    _replace_once(
        init_py,
        "    if not api_key:\n",
        f'    logger.warning("{PROVIDER_MARKER}")\n'
        "\n"
        "    if not api_key:\n",
    )


@pytest.fixture(scope="module")
def gitea_env(free_port):
    print(f"[E2E-single] Creating Gitea on port {free_port}...", file=sys.stderr)
    data, _ = run_gitea_cli_json("create", "--port", str(free_port), timeout=120)
    assert isinstance(data, dict)
    yield data
    run_gitea_cli("destroy", data["id"], timeout=30)


@pytest.fixture(scope="module")
def mirrored_gitea_env(gitea_env, require_github_token, tmp_path_factory):
    print(
        "[E2E-single] Mirroring amplifier-module-provider-anthropic into Gitea...",
        file=sys.stderr,
    )
    mirror_repo_to_gitea(gitea_env["id"], PROVIDER_GITHUB_REPO, require_github_token)

    provider_repo_dir = clone_local_repo(
        PROVIDER_LOCAL_REPO,
        tmp_path_factory.mktemp("single-provider-clone")
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
def dtu_env(mirrored_gitea_env):
    data, _ = run_cli_json(
        "launch",
        "amplifier-user-sim-single-module",
        "--var",
        f"GITEA_URL={mirrored_gitea_env['gitea_url']}",
        "--var",
        f"GITEA_TOKEN={mirrored_gitea_env['token']}",
        timeout=900,
    )
    assert isinstance(data, dict)
    yield data
    run_cli("destroy", data["id"], timeout=60)


@pytest.mark.e2e
def test_single_module_profile_loads_rewritten_provider(dtu_env):
    """Verify amplifier loads the provider from the rewritten Gitea source."""
    data, _ = run_cli_json(
        "exec",
        dtu_env["id"],
        "--",
        "bash",
        "-lc",
        "cd /home/user/project && amplifier run 'respond with exactly: UNUSED_MARKER'",
        timeout=180,
    )
    combined_output = f"{data['stdout']}\n{data['stderr']}"
    assert PROVIDER_MARKER in combined_output, (
        "Expected the rewritten provider warning marker to appear in output:\n"
        f"stdout: {data['stdout']}\nstderr: {data['stderr']}"
    )
