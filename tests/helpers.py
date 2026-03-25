# Copyright (c) Microsoft. All rights reserved.

"""Test helpers for subprocess-level CLI invocation."""

import json
import os
import subprocess
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# amplifier-digital-twin CLI helpers (run from local source via uv)
# ---------------------------------------------------------------------------


def run_cli(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run amplifier-digital-twin via uv, exactly as a user would.

    Uses ``uv run --project`` so the invocation works from any working
    directory without requiring PATH or venv activation.
    """
    return subprocess.run(
        [
            "uv",
            "run",
            "--no-sync",
            "--project",
            str(PROJECT_DIR),
            "amplifier-digital-twin",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_cli_json(
    *args: str, **kwargs
) -> tuple[dict | list, subprocess.CompletedProcess[str]]:
    """Run amplifier-digital-twin and parse JSON from stdout."""
    result = run_cli(*args, **kwargs)
    assert result.returncode == 0, (
        f"Command failed (exit {result.returncode}):\n"
        f"  stdout: {result.stdout}\n"
        f"  stderr: {result.stderr}"
    )
    return json.loads(result.stdout), result


# ---------------------------------------------------------------------------
# amplifier-gitea CLI helpers (must be installed on PATH)
# ---------------------------------------------------------------------------


def run_gitea_cli(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run amplifier-gitea CLI. Must be installed on PATH."""
    return subprocess.run(
        ["amplifier-gitea", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_gitea_cli_json(
    *args: str, **kwargs
) -> tuple[dict | list, subprocess.CompletedProcess[str]]:
    """Run amplifier-gitea and parse JSON from stdout."""
    result = run_gitea_cli(*args, **kwargs)
    assert result.returncode == 0, (
        f"amplifier-gitea failed (exit {result.returncode}):\n"
        f"  stdout: {result.stdout}\n"
        f"  stderr: {result.stderr}"
    )
    return json.loads(result.stdout), result


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def resolve_github_token() -> str | None:
    """Resolve a GitHub token from environment or gh CLI."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    return None


def git(*args: str, cwd: Path | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run a git command."""
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
    )


def git_checked(
    *args: str, cwd: Path | None = None, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    """Run git and assert success."""
    result = git(*args, cwd=cwd, timeout=timeout)
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed (exit {result.returncode}):\n"
        f"  stdout: {result.stdout}\n"
        f"  stderr: {result.stderr}"
    )
    return result


def github_repo_name(github_repo: str) -> str:
    """Return the repository name from a GitHub URL."""
    name = github_repo.rstrip("/").rsplit("/", 1)[-1]
    return name.removesuffix(".git")


def gitea_clone_url(port: int, token: str, repo_name: str) -> str:
    """Return the authenticated clone URL for an admin-owned Gitea repo."""
    return f"http://admin:{token}@localhost:{port}/admin/{repo_name}.git"


def mirror_repo_to_gitea(
    gitea_id: str,
    github_repo: str,
    github_token: str,
    *,
    timeout: int = 180,
) -> None:
    """Mirror a GitHub repo into Gitea via the amplifier-gitea CLI."""
    run_gitea_cli_json(
        "mirror-from-github",
        gitea_id,
        "--github-repo",
        github_repo,
        "--github-token",
        github_token,
        timeout=timeout,
    )


def clone_local_repo(src_repo: Path, dest_dir: Path) -> Path:
    """Clone a local repo into *dest_dir* and return the clone path."""
    git_checked("clone", str(src_repo), str(dest_dir), timeout=180)
    return dest_dir


def commit_all(repo_dir: Path, message: str) -> None:
    """Create a deterministic test commit in *repo_dir*."""
    git_checked("add", "-A", cwd=repo_dir)
    git_checked(
        "-c",
        "user.name=Amplifier DTU Tests",
        "-c",
        "user.email=dtu-tests@example.com",
        "commit",
        "-m",
        message,
        cwd=repo_dir,
    )


def push_repo_to_gitea(repo_dir: Path, clone_url: str, refspec: str = "HEAD:main") -> None:
    """Push *repo_dir* to Gitea using *clone_url*."""
    git_checked("remote", "set-url", "origin", clone_url, cwd=repo_dir)
    git_checked("push", "origin", refspec, "--force", cwd=repo_dir, timeout=180)
