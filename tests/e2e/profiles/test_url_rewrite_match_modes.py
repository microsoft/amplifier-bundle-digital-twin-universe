# Copyright (c) Microsoft. All rights reserved.

"""End-to-end verification that ``match_mode: boundary`` prevents the bare-prefix
collision that today's ``url_rewrites`` rules silently exhibit.

Setup: an ephemeral Gitea hosts a mirror of ``microsoft/amplifier`` ONLY.
``microsoft/amplifier-foundation`` is *not* mirrored. The DTU is launched
with a single rewrite rule:

    match: github.com/microsoft/amplifier
    match_mode: boundary
    target: ${GITEA_URL}/admin/amplifier

Inside the DTU we then run two ``git ls-remote`` calls against the proxy:

  1. against ``github.com/microsoft/amplifier`` -- must hit the Gitea mirror
     (proves the rule still matches its intended target)
  2. against ``github.com/microsoft/amplifier-foundation`` -- must pass
     through to the upstream GitHub (proves ``match_mode: boundary`` correctly
     refuses to capture a sibling repo whose path merely shares a prefix)

Today the ``match_mode`` field is unrecognised by the loader -- it is dropped
with an ``UnknownProfileFieldWarning`` and the rule operates in default
``prefix`` mode. The bare ``amplifier`` prefix then captures the
``amplifier-foundation`` URL and rewrites it to ``${GITEA_URL}/admin/
amplifier-foundation`` which does not exist in Gitea. ``ls-remote`` returns a
non-zero exit code and assertion #2 below fails -- this is the RED state we
want this test to demonstrate before the implementation lands.
"""

from __future__ import annotations

import sys

import pytest

from conftest import register_dtu_instance
from helpers import (
    git_checked,
    mirror_repo_to_gitea,
    run_cli,
    run_cli_json,
    run_gitea_cli,
    run_gitea_cli_json,
)

AMPLIFIER_GITHUB_REPO = "https://github.com/microsoft/amplifier"
SIBLING_GITHUB_REPO = "https://github.com/microsoft/amplifier-foundation"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gitea_env(free_port):
    """Start an ephemeral Gitea instance for the module."""
    print(
        f"[match-modes] Creating Gitea on port {free_port}...",
        file=sys.stderr,
    )
    data, _ = run_gitea_cli_json("create", "--port", str(free_port), timeout=120)
    assert isinstance(data, dict)
    yield data
    run_gitea_cli("destroy", data["id"], timeout=30)


@pytest.fixture(scope="module")
def amplifier_in_gitea(gitea_env, require_github_token):
    """Mirror ``amplifier`` into Gitea. The sibling repo is intentionally
    NOT mirrored -- if the rewrite rule incorrectly captures sibling URLs we
    want them to 404 against Gitea so the failure is visible."""
    print(
        f"[match-modes] Mirroring {AMPLIFIER_GITHUB_REPO} into Gitea...",
        file=sys.stderr,
    )
    mirror_repo_to_gitea(gitea_env["id"], AMPLIFIER_GITHUB_REPO, require_github_token)
    return gitea_env


@pytest.fixture(scope="module")
def upstream_sibling_sha():
    """Capture upstream HEAD for ``amplifier-foundation`` BEFORE launching
    the DTU. Pins the value so the in-DTU ls-remote can be compared against
    a known-good SHA without depending on GitHub state during the test."""
    result = git_checked("ls-remote", SIBLING_GITHUB_REPO, "HEAD", timeout=30)
    return result.stdout.strip().split()[0]


@pytest.fixture(scope="module")
def boundary_mode_profile(tmp_path_factory):
    """Minimal profile with ONE rule using ``match_mode: boundary``."""
    profile_dir = tmp_path_factory.mktemp("match-modes-profiles")
    profile_path = profile_dir / "boundary-mode.yaml"
    profile_path.write_text(
        """\
name: boundary-mode-test
description: >
  Verifies match_mode=boundary prevents the bare-prefix collision.

base:
  image: ubuntu:24.04

url_rewrites:
  auth:
    username: admin
    token_var: GITEA_TOKEN
  rules:
    - match: github.com/microsoft/amplifier
      match_mode: boundary
      target: ${GITEA_URL}/admin/amplifier

passthrough:
  allow_external: true

provision:
  setup_cmds:
    - apt-get update && apt-get install -y git ca-certificates
"""
    )
    return str(profile_path)


@pytest.fixture(scope="module")
def dtu_env(amplifier_in_gitea, boundary_mode_profile):
    """Launch the DTU with the ``amplifier`` mirror configured via --var."""
    gitea = amplifier_in_gitea
    print(
        "[match-modes] Launching DTU with match_mode=boundary rule...",
        file=sys.stderr,
    )
    data, _ = run_cli_json(
        "launch",
        boundary_mode_profile,
        "--var",
        f"GITEA_URL={gitea['gitea_url']}",
        "--var",
        f"GITEA_TOKEN={gitea['token']}",
        timeout=900,
    )
    assert isinstance(data, dict)
    register_dtu_instance(data["id"])
    yield data
    run_cli("destroy", data["id"], timeout=60)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ls_remote_inside(instance_id: str, repo_url: str) -> dict:
    """Run ``git ls-remote <repo_url> HEAD`` inside the DTU."""
    data, _ = run_cli_json(
        "exec",
        instance_id,
        "--",
        "git",
        "ls-remote",
        repo_url,
        "HEAD",
        timeout=60,
    )
    assert isinstance(data, dict)
    return data


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_boundary_mode_rule_still_matches_intended_target(dtu_env):
    """Sanity check: with ``match_mode: boundary``, the rule MUST still match its
    intended target. ``ls-remote`` for ``amplifier`` should resolve through
    Gitea successfully. If this fails the new mode is too restrictive."""
    result = _ls_remote_inside(dtu_env["id"], AMPLIFIER_GITHUB_REPO)
    assert result["exit_code"] == 0, (
        "ls-remote for amplifier FAILED inside DTU. "
        "match_mode: boundary should still match the intended target.\n"
        f"  exit: {result['exit_code']}\n"
        f"  stdout: {result['stdout']}\n"
        f"  stderr: {result['stderr']}"
    )
    sha = result["stdout"].strip().split()[0]
    assert len(sha) == 40, f"expected a 40-char SHA, got {sha!r}"


@pytest.mark.e2e
def test_boundary_mode_does_not_capture_sibling_repos(dtu_env, upstream_sibling_sha):
    """The headline test: ``match_mode: boundary`` MUST NOT capture sibling
    repository URLs whose path merely shares a prefix.

    Today this fails because the loader silently drops ``match_mode: boundary``,
    leaving the rule in default ``prefix`` mode. The bare prefix then
    captures the sibling URL and routes it to a non-existent Gitea path,
    producing a non-zero exit from ``git ls-remote``.
    """
    result = _ls_remote_inside(dtu_env["id"], SIBLING_GITHUB_REPO)

    assert result["exit_code"] == 0, (
        "ls-remote for amplifier-foundation FAILED inside DTU. The rewrite "
        "rule for the bare 'amplifier' prefix incorrectly captured the "
        "sibling URL and routed it to a Gitea path that does not exist. "
        "match_mode: boundary is supposed to scope rules to a single repo path "
        "boundary -- when implemented, this assertion turns green.\n"
        f"  exit: {result['exit_code']}\n"
        f"  stdout: {result['stdout']}\n"
        f"  stderr: {result['stderr']}"
    )

    inside_sha = result["stdout"].strip().split()[0]
    assert inside_sha == upstream_sibling_sha, (
        "amplifier-foundation ls-remote inside DTU returned a SHA that does "
        "NOT match upstream GitHub HEAD. The request was probably rewritten "
        "to a Gitea repo. match_mode: boundary should pass it through unchanged.\n"
        f"  inside DTU: {inside_sha}\n"
        f"  upstream:   {upstream_sibling_sha}"
    )
