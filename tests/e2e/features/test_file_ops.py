# Copyright (c) Microsoft. All rights reserved.

"""E2E tests for file-push, file-pull, and provision.files.

Launches a minimal container with provision.files, then exercises the
file-push and file-pull CLI commands.  No Docker, Gitea, or API keys
required -- only Incus.

Run with:
    uv run pytest tests/test_e2e_file_ops.py --run-e2e -v -s
"""

import sys
from pathlib import Path

import pytest

from conftest import register_dtu_instance
from helpers import run_cli, run_cli_json


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seed_files(tmp_path_factory):
    """Create temp files and a directory tree to push into the container."""
    root = tmp_path_factory.mktemp("dtu-file-ops-seed")

    # Single files
    (root / "single.txt").write_text("hello from host")
    (root / "a.txt").write_text("file-a")
    (root / "b.txt").write_text("file-b")

    # Directory tree
    tree = root / "tree"
    tree.mkdir()
    (tree / "top.txt").write_text("top-level")
    sub = tree / "nested"
    sub.mkdir()
    (sub / "deep.txt").write_text("nested-file")

    return root


@pytest.fixture(scope="module")
def file_ops_profile(tmp_path_factory, seed_files):
    """Write a minimal profile that exercises provision.files."""
    d = tmp_path_factory.mktemp("dtu-file-ops-profiles")
    profile = d / "file-ops-test.yaml"
    profile.write_text(
        f"""\
name: file-ops-test
description: E2E test for file-push, file-pull, and provision.files

base:
  image: ubuntu:24.04

provision:
  files:
    - src: {seed_files / "single.txt"}
      dest: /root/provisioned/single.txt
    - src: {seed_files / "tree"}
      dest: /root/provisioned/tree/
      recursive: true
  setup_cmds:
    - test -f /root/provisioned/single.txt
    - test -f /root/provisioned/tree/nested/deep.txt
"""
    )
    return str(profile)


@pytest.fixture(scope="module")
def dtu_env(file_ops_profile):
    """Launch a DTU from the file-ops profile, yield metadata, destroy on teardown."""
    print("[E2E-file-ops] Launching file-ops-test profile...", file=sys.stderr)
    data, _ = run_cli_json("launch", file_ops_profile, timeout=600)
    assert isinstance(data, dict), "Expected launch to return a JSON object"
    register_dtu_instance(data["id"])
    yield data
    run_cli("destroy", data["id"], timeout=60)


# ---------------------------------------------------------------------------
# provision.files
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestProvisionFiles:
    """Verify provision.files seeds data during launch."""

    def test_single_file_provisioned(self, dtu_env):
        """Single file is readable inside the container."""
        data, _ = run_cli_json(
            "exec", dtu_env["id"], "--", "cat", "/root/provisioned/single.txt"
        )
        assert data["exit_code"] == 0
        assert "hello from host" in data["stdout"]

    def test_recursive_dir_provisioned(self, dtu_env):
        """Recursive directory preserves tree structure."""
        data, _ = run_cli_json(
            "exec",
            dtu_env["id"],
            "--",
            "cat",
            "/root/provisioned/tree/nested/deep.txt",
        )
        assert data["exit_code"] == 0
        assert "nested-file" in data["stdout"]

    def test_setup_cmds_ran_after_files(self, dtu_env):
        """Launch succeeded, meaning setup_cmds (which assert files exist) passed."""
        assert dtu_env["status"] == "running"


# ---------------------------------------------------------------------------
# file-push
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestFilePush:
    """Verify the file-push CLI command."""

    def test_push_single_file(self, dtu_env, seed_files):
        data, _ = run_cli_json(
            "file-push",
            dtu_env["id"],
            str(seed_files / "a.txt"),
            "/root/pushed/a.txt",
        )
        assert data["dest"] == "/root/pushed/a.txt"
        out, _ = run_cli_json("exec", dtu_env["id"], "--", "cat", "/root/pushed/a.txt")
        assert "file-a" in out["stdout"]

    def test_push_multiple_files(self, dtu_env, seed_files):
        run_cli_json("exec", dtu_env["id"], "--", "mkdir", "-p", "/root/pushed/multi/")
        data, _ = run_cli_json(
            "file-push",
            dtu_env["id"],
            str(seed_files / "a.txt"),
            str(seed_files / "b.txt"),
            "/root/pushed/multi/",
        )
        assert len(data["sources"]) == 2
        out, _ = run_cli_json("exec", dtu_env["id"], "--", "ls", "/root/pushed/multi/")
        assert "a.txt" in out["stdout"]
        assert "b.txt" in out["stdout"]

    def test_push_recursive_directory(self, dtu_env, seed_files):
        run_cli_json(
            "file-push",
            dtu_env["id"],
            str(seed_files / "tree"),
            "/root/pushed/tree-copy/",
        )
        out, _ = run_cli_json(
            "exec",
            dtu_env["id"],
            "--",
            "cat",
            "/root/pushed/tree-copy/tree/nested/deep.txt",
        )
        assert "nested-file" in out["stdout"]

    def test_push_with_mode(self, dtu_env, seed_files):
        run_cli_json(
            "file-push",
            dtu_env["id"],
            str(seed_files / "a.txt"),
            "/root/pushed/moded.txt",
            "--mode",
            "0755",
        )
        out, _ = run_cli_json(
            "exec", dtu_env["id"], "--", "stat", "-c", "%a", "/root/pushed/moded.txt"
        )
        assert "755" in out["stdout"]


# ---------------------------------------------------------------------------
# file-pull
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestFilePull:
    """Verify the file-pull CLI command."""

    def test_pull_single_file(self, dtu_env, tmp_path_factory):
        dest = tmp_path_factory.mktemp("dtu-pull") / "single.txt"
        run_cli_json(
            "file-pull", dtu_env["id"], "/root/provisioned/single.txt", str(dest)
        )
        assert dest.read_text().strip() == "hello from host"

    def test_pull_recursive_directory(self, dtu_env, tmp_path_factory):
        dest_dir = str(tmp_path_factory.mktemp("dtu-pull-tree"))
        run_cli_json("file-pull", dtu_env["id"], "/root/provisioned/tree", dest_dir)
        pulled = Path(dest_dir) / "tree" / "nested" / "deep.txt"
        assert pulled.exists()
        assert "nested-file" in pulled.read_text()

    def test_pull_nonexistent_fails(self, dtu_env, tmp_path_factory):
        dest = str(tmp_path_factory.mktemp("dtu-pull-fail") / "nope.txt")
        result = run_cli("file-pull", dtu_env["id"], "/root/does-not-exist.txt", dest)
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestFilePushPullRoundtrip:
    """Push a file in, pull it back, verify content matches."""

    def test_roundtrip(self, dtu_env, tmp_path_factory, seed_files):
        run_cli_json(
            "file-push",
            dtu_env["id"],
            str(seed_files / "a.txt"),
            "/root/roundtrip/a.txt",
            "-p",
        )
        dest = tmp_path_factory.mktemp("dtu-roundtrip") / "a.txt"
        run_cli_json("file-pull", dtu_env["id"], "/root/roundtrip/a.txt", str(dest))
        assert dest.read_text() == (seed_files / "a.txt").read_text()
