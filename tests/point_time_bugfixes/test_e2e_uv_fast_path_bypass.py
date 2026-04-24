# Copyright (c) Microsoft. All rights reserved.

"""POINT-IN-TIME BUG FIX TEST -- NOT MAINTAINED LONG-TERM.

This test was written to triage a specific bug fix (uv's GitHub fast path
bypassing ``url_rewrites``). It exists to prove the bug is real and that the
fix actually fixes it, captured at the moment of the fix.

Because it depends on external state that will drift over time -- live
microsoft/amplifier-app-cli and microsoft/amplifier-foundation repos on
GitHub, the real Gitea mirror + migrate API, uv's current fast-path
implementation, GitHub's API response format -- this test is NOT expected
to be maintained alongside the rest of the suite. Treat it as a historical
artifact: if it stops passing, that's a signal to investigate the change
and either update the test, delete it, or replace it with something more
stable. Do not gate CI on it.

For the durable protection against this bug class, rely on the production
code's behavior (``UV_NO_GITHUB_FAST_PATH=true`` in the DTU container env)
and a validator-side SHA-match assertion, not this test.

------------------------------------------------------------------------

Regression test for uv's GitHub fast-path bypassing url_rewrites.

When a DTU profile rewrites ``github.com/<owner>/<repo>`` to a Gitea mirror,
``uv tool install`` silently installs the **upstream GitHub** commit instead
of the **Gitea mirror** commit. The cause:

* uv's GitHub fast path resolves ``@<ref>`` -> SHA by calling
  ``api.github.com/repos/<owner>/<repo>/commits/<ref>`` directly and fetches
  ``pyproject.toml`` from ``raw.githubusercontent.com``.
* Neither host is covered by the DTU's current ``url_rewrites`` engine (they
  are not in ``--allow-hosts``, so mitmproxy TCP-tunnels them).
* uv pins to whatever SHA GitHub returns, then does a ``git fetch`` which
  IS rewritten to Gitea. Because the Gitea mirror was seeded from GitHub it
  has that upstream SHA in its history, so the install succeeds -- at the
  wrong commit.

This test proves the bug end-to-end by:

  1. Mirroring microsoft/amplifier-app-cli and its only git-sourced dep,
     microsoft/amplifier-foundation, into Gitea.
  2. Adding a marker commit on top of each Gitea mirror so Gitea HEAD is
     strictly ahead of GitHub HEAD.
  3. Launching a minimal DTU profile that rewrites both repos.
  4. Running ``uv tool install`` against the app-cli git URL.
  5. Reading PEP 610 ``direct_url.json`` from each installed package.
  6. Asserting the installed ``commit_id`` equals **Gitea HEAD**, not
     GitHub HEAD -- for both the root package and the transitive git dep.

Until the fix lands (``UV_NO_GITHUB_FAST_PATH=true`` in DTU container env)
the assertions fail, showing the installed SHAs match GitHub instead.

Run with:
    uv run pytest tests/test_e2e_uv_fast_path_bypass.py --run-e2e -v -s
"""

import json
import sys
from pathlib import Path

import pytest

from conftest import register_dtu_instance
from helpers import (
    commit_all,
    git_checked,
    gitea_clone_url,
    github_repo_name,
    mirror_repo_to_gitea,
    push_repo_to_gitea,
    run_cli,
    run_cli_json,
    run_gitea_cli,
    run_gitea_cli_json,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_CLI_GITHUB_REPO = "https://github.com/microsoft/amplifier-app-cli"
FOUNDATION_GITHUB_REPO = "https://github.com/microsoft/amplifier-foundation"

# The packages installed inside the tool venv (underscore form for dist-info).
APP_CLI_PKG_MODULE = "amplifier_app_cli"
FOUNDATION_PKG_MODULE = "amplifier_foundation"

# Marker file added to each Gitea mirror so HEAD diverges from GitHub HEAD.
MARKER_FILENAME = ".dtu-fast-path-test-marker"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gitea_env(free_port):
    """Start an ephemeral Gitea instance for the module."""
    print(
        f"[E2E-fast-path] Creating Gitea on port {free_port}...",
        file=sys.stderr,
    )
    data, _ = run_gitea_cli_json("create", "--port", str(free_port), timeout=120)
    assert isinstance(data, dict)
    yield data
    run_gitea_cli("destroy", data["id"], timeout=30)


def _github_head_sha(github_repo: str) -> str:
    """Get the upstream GitHub HEAD SHA for a repo's default branch via ls-remote.

    Uses git (not the API) so we get the actual SHA the real git fetch would
    see -- and so we don't depend on additional HTTP auth setup for public
    repos in CI.
    """
    result = git_checked("ls-remote", github_repo, "HEAD", timeout=30)
    # Output: "<sha>\tHEAD"
    return result.stdout.strip().split()[0]


def _mirror_and_add_marker(
    gitea: dict,
    github_repo: str,
    github_token: str,
    tmp_dir: Path,
) -> tuple[str, str]:
    """Mirror a repo from GitHub into Gitea, then push a marker commit on top.

    Returns (github_head_sha, gitea_head_sha) AFTER the marker push.
    """
    repo_name = github_repo_name(github_repo)

    # 1. Capture GitHub's current HEAD before any mutation.
    github_sha = _github_head_sha(github_repo)

    # 2. Mirror from GitHub. After this, Gitea HEAD == GitHub HEAD.
    print(
        f"[E2E-fast-path] Mirroring {github_repo} into Gitea...",
        file=sys.stderr,
    )
    mirror_repo_to_gitea(gitea["id"], github_repo, github_token)

    # 3. Clone the Gitea mirror (not a local working copy -- amplifier-app-cli
    #    isn't in this workspace) so we can add a marker commit.
    clone_url = gitea_clone_url(gitea["port"], gitea["token"], repo_name)
    clone_dir = tmp_dir / f"{repo_name}-gitea-clone"
    git_checked("clone", clone_url, str(clone_dir), timeout=180)

    # 4. Add a trivial marker file. Contents include a uuid so this never
    #    collides across test runs if the mirror survives somehow.
    import uuid

    marker_path = clone_dir / MARKER_FILENAME
    marker_path.write_text(f"dtu-fast-path-bypass-test: {uuid.uuid4()}\n")

    # 5. Commit and force-push to Gitea. This is the divergence point.
    commit_all(clone_dir, "test: DTU fast-path bypass marker")
    push_repo_to_gitea(clone_dir, clone_url)

    # 6. Capture Gitea HEAD AFTER the marker push. Use git rev-parse on the
    #    local clone -- it's the exact SHA we just pushed and is deterministic,
    #    avoiding Gitea API quirks with just-mirrored + force-pushed repos.
    gitea_sha = git_checked("rev-parse", "HEAD", cwd=clone_dir).stdout.strip()

    return github_sha, gitea_sha


@pytest.fixture(scope="module")
def mirrored_repos(gitea_env, require_github_token, tmp_path_factory):
    """Mirror both app-cli and foundation into Gitea with marker commits.

    Returns a dict with:
      - gitea: the gitea_env dict
      - gitea_head: {repo_name: sha, ...} -- SHAs AFTER marker push
      - github_head: {repo_name: sha, ...} -- SHAs from upstream GitHub
    """
    tmp_dir = tmp_path_factory.mktemp("fast-path-mirrors")

    repos = {
        "amplifier-app-cli": APP_CLI_GITHUB_REPO,
        "amplifier-foundation": FOUNDATION_GITHUB_REPO,
    }

    github_head: dict[str, str] = {}
    gitea_head: dict[str, str] = {}

    for name, url in repos.items():
        gh_sha, gt_sha = _mirror_and_add_marker(
            gitea_env, url, require_github_token, tmp_dir
        )
        github_head[name] = gh_sha
        gitea_head[name] = gt_sha

        # Sanity check: marker must have produced divergent SHAs. If it didn't,
        # the whole test is meaningless -- fail the fixture, not the test.
        assert gt_sha != gh_sha, (
            f"Marker push for {name} did not change HEAD "
            f"(both SHAs are {gt_sha}). The fixture is broken."
        )

    print(
        "[E2E-fast-path] Divergent SHAs established:\n"
        f"  amplifier-app-cli    github={github_head['amplifier-app-cli']} "
        f"gitea={gitea_head['amplifier-app-cli']}\n"
        f"  amplifier-foundation github={github_head['amplifier-foundation']} "
        f"gitea={gitea_head['amplifier-foundation']}",
        file=sys.stderr,
    )

    return {
        "gitea": gitea_env,
        "github_head": github_head,
        "gitea_head": gitea_head,
    }


@pytest.fixture(scope="module")
def fast_path_profile(tmp_path_factory):
    """Write a minimal profile that rewrites app-cli + foundation only."""
    profile_dir = tmp_path_factory.mktemp("fast-path-profiles")
    profile_path = profile_dir / "uv-fast-path-bypass.yaml"
    profile_path.write_text(
        """\
name: uv-fast-path-bypass
description: >
  Minimal profile for the uv GitHub fast-path bypass regression test.
  Rewrites two microsoft/* repos to a Gitea mirror. The test itself runs
  `uv tool install` after launch -- there is no install in setup_cmds so
  the test can observe `direct_url.json` under controlled timing.

base:
  image: ubuntu:24.04

url_rewrites:
  auth:
    username: admin
    token_var: GITEA_TOKEN
  rules:
    - match: github.com/microsoft/amplifier-app-cli
      target: ${GITEA_URL}/admin/amplifier-app-cli
    - match: github.com/microsoft/amplifier-foundation
      target: ${GITEA_URL}/admin/amplifier-foundation

passthrough:
  allow_external: true

provision:
  setup_cmds:
    - apt-get update && apt-get install -y git curl jq
    - curl -LsSf https://astral.sh/uv/install.sh | sh
"""
    )
    return str(profile_path)


@pytest.fixture(scope="module")
def dtu_env(mirrored_repos, fast_path_profile):
    """Launch the DTU with both mirrors configured via --var."""
    gitea = mirrored_repos["gitea"]
    print(
        "[E2E-fast-path] Launching DTU with rewrites for app-cli + foundation...",
        file=sys.stderr,
    )
    data, _ = run_cli_json(
        "launch",
        fast_path_profile,
        "--var",
        f"GITEA_URL={gitea['gitea_url']}",
        "--var",
        f"GITEA_TOKEN={gitea['token']}",
        timeout=900,
    )
    assert isinstance(data, dict)
    register_dtu_instance(data["id"])
    yield {**data, **mirrored_repos}
    run_cli("destroy", data["id"], timeout=60)


# ---------------------------------------------------------------------------
# The install (runs once, results checked by multiple tests)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def installed_amplifier_app_cli(dtu_env):
    """Run `uv tool install` for amplifier-app-cli inside the DTU.

    This is the command that triggers the fast-path bug. Split out from the
    assertions so it only runs once even if we have multiple test functions.
    """
    instance_id = dtu_env["id"]
    print(
        "[E2E-fast-path] Running `uv tool install` inside DTU...",
        file=sys.stderr,
    )
    data, _ = run_cli_json(
        "exec",
        instance_id,
        "--",
        "bash",
        "-lc",
        'export PATH="/root/.local/bin:$PATH" && '
        "uv tool install -vv "
        '"amplifier-app-cli @ git+https://github.com/microsoft/amplifier-app-cli@main" '
        "2>&1 | tail -40",
        timeout=600,
    )
    assert isinstance(data, dict)
    assert data["exit_code"] == 0, (
        f"uv tool install failed (exit {data['exit_code']}):\n"
        f"stdout: {data['stdout']}\n"
        f"stderr: {data['stderr']}"
    )
    print(
        f"[E2E-fast-path] uv tool install output (last lines):\n{data['stdout']}",
        file=sys.stderr,
    )
    return data


def _read_direct_url(instance_id: str, package_module: str) -> dict:
    """Read PEP 610 direct_url.json for a package in the amplifier-app-cli tool venv."""
    script = f"""
        set -eu
        TOOL_DIR=$(/root/.local/bin/uv tool dir)/amplifier-app-cli
        DIST_INFO=$(ls -d "$TOOL_DIR"/lib/python*/site-packages/{package_module}-*.dist-info 2>/dev/null | head -1)
        if [ -z "$DIST_INFO" ]; then
            echo "ERROR: no dist-info for {package_module} in $TOOL_DIR" >&2
            ls -la "$TOOL_DIR"/lib/python*/site-packages/ >&2 || true
            exit 2
        fi
        cat "$DIST_INFO/direct_url.json"
    """
    data, _ = run_cli_json(
        "exec",
        instance_id,
        "--",
        "bash",
        "-lc",
        script,
        timeout=30,
    )
    assert isinstance(data, dict)
    assert data["exit_code"] == 0, (
        f"Failed to read direct_url.json for {package_module}:\n"
        f"stdout: {data['stdout']}\n"
        f"stderr: {data['stderr']}"
    )
    return json.loads(data["stdout"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_root_package_installs_gitea_sha_not_github_sha(
    dtu_env, installed_amplifier_app_cli
):
    """The root package (amplifier-app-cli) must reflect the Gitea mirror HEAD,
    not the upstream GitHub HEAD. Fails today because uv's fast path resolves
    the ref via api.github.com (not rewritten) and pins to GitHub's SHA."""
    gitea_sha = dtu_env["gitea_head"]["amplifier-app-cli"]
    github_sha = dtu_env["github_head"]["amplifier-app-cli"]

    # Precondition: the fixture MUST have produced divergent SHAs,
    # otherwise the test is meaningless.
    assert gitea_sha != github_sha, "fixture precondition: SHAs must diverge"

    direct_url = _read_direct_url(dtu_env["id"], APP_CLI_PKG_MODULE)
    installed_sha = direct_url["vcs_info"]["commit_id"]

    assert installed_sha == gitea_sha, (
        "uv installed the WRONG commit for amplifier-app-cli. "
        "This means uv's GitHub fast path bypassed url_rewrites.\n"
        f"  installed: {installed_sha}\n"
        f"  Gitea HEAD: {gitea_sha}  <- expected\n"
        f"  GitHub HEAD: {github_sha}  <- what uv fell into"
    )


@pytest.mark.e2e
def test_transitive_git_dep_installs_gitea_sha_not_github_sha(
    dtu_env, installed_amplifier_app_cli
):
    """The transitive git dep (amplifier-foundation) must also reflect the
    Gitea mirror HEAD. This is the case explicitly called out in the bug
    report: fixes to app-cli itself pass, but transitive git deps silently
    install the pre-fix version."""
    gitea_sha = dtu_env["gitea_head"]["amplifier-foundation"]
    github_sha = dtu_env["github_head"]["amplifier-foundation"]

    assert gitea_sha != github_sha, "fixture precondition: SHAs must diverge"

    direct_url = _read_direct_url(dtu_env["id"], FOUNDATION_PKG_MODULE)
    installed_sha = direct_url["vcs_info"]["commit_id"]

    assert installed_sha == gitea_sha, (
        "uv installed the WRONG commit for amplifier-foundation "
        "(transitive git dep of amplifier-app-cli). "
        "This means uv's GitHub fast path bypassed url_rewrites for "
        "transitive deps resolved from a downstream pyproject.toml.\n"
        f"  installed: {installed_sha}\n"
        f"  Gitea HEAD: {gitea_sha}  <- expected\n"
        f"  GitHub HEAD: {github_sha}  <- what uv fell into"
    )
