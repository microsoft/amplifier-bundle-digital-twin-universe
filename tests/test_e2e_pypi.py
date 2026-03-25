# Copyright (c) Microsoft. All rights reserved.

"""Fast E2E tests for PyPI overrides using wheel_from_git.

This suite exercises the same host-side git clone -> wheel build -> wheel
injection flow used by amplifier-user-sim, but with a tiny pure-Python package
to keep runtime low.

Run with:
    uv run pytest tests/test_e2e_pypi.py --run-e2e -v -s
"""

import sys
from pathlib import Path

import pytest

from helpers import git_checked, run_cli, run_cli_json

_DUMMY_PKG_NAME = "dtu-test-pkg"
_DUMMY_PKG_MODULE = "dtu_test_pkg"
_DUMMY_PKG_VERSION = "99.0.0"


def _init_git_repo(repo_dir: Path) -> None:
    git_checked("init", "-b", "main", cwd=repo_dir)
    git_checked("add", "-A", cwd=repo_dir)
    git_checked(
        "-c",
        "user.name=Amplifier DTU Tests",
        "-c",
        "user.email=dtu-tests@example.com",
        "commit",
        "-m",
        "init test package",
        cwd=repo_dir,
    )


@pytest.fixture(scope="module")
def dummy_package_repo(tmp_path_factory):
    """Create a tiny git repo that can be built with uv build --wheel."""
    repo_dir = tmp_path_factory.mktemp("dtu-test-pkg-repo")
    pkg_dir = repo_dir / _DUMMY_PKG_MODULE
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text(f'__version__ = "{_DUMMY_PKG_VERSION}"\n')
    (repo_dir / "pyproject.toml").write_text(
        f"""\
[project]
name = "{_DUMMY_PKG_NAME}"
version = "{_DUMMY_PKG_VERSION}"
description = "Dummy package for amplifier-bundle-digital-twin-universe tests"
requires-python = ">=3.11"

[build-system]
requires = ["setuptools>=70"]
build-backend = "setuptools.build_meta"
"""
    )
    _init_git_repo(repo_dir)
    return repo_dir


@pytest.fixture(scope="module")
def pypi_test_profile(tmp_path_factory, dummy_package_repo):
    """Write a minimal profile that exercises wheel_from_git."""
    profile_dir = tmp_path_factory.mktemp("pypi-profiles")
    profile_path = profile_dir / "pypi-wheel-from-git.yaml"
    profile_path.write_text(
        f"""\
name: pypi-wheel-from-git
description: Fast wheel_from_git test profile

base:
  image: ubuntu:24.04

url_rewrites:
  rules:
    - match: dtu-noop.invalid/never-matches
      target: http://localhost:1/never-matches

pypi_overrides:
  packages:
    - name: {_DUMMY_PKG_NAME}
      wheel_from_git:
        repo: {dummy_package_repo}
        ref: main
        build_cmd: uv build --wheel
        wheel_glob: dist/{_DUMMY_PKG_MODULE}-*.whl

provision:
  setup_cmds:
    - apt-get update && apt-get install -y python3-pip curl git
    - curl -LsSf https://astral.sh/uv/install.sh | sh
    - |
      export PATH="/root/.local/bin:$PATH"
      pip3 install {_DUMMY_PKG_NAME} --break-system-packages
      uv pip install {_DUMMY_PKG_NAME} --system --break-system-packages
"""
    )
    return str(profile_path)


@pytest.fixture(scope="module")
def dtu_env(pypi_test_profile):
    print("[E2E-pypi] Launching wheel_from_git profile...", file=sys.stderr)
    data, _ = run_cli_json("launch", pypi_test_profile, timeout=600)
    assert isinstance(data, dict), "Expected launch to return a JSON object"
    yield data
    run_cli("destroy", data["id"], timeout=60)


@pytest.mark.e2e
def test_wheel_from_git_installs_expected_version(dtu_env):
    """Verify both pip and uv consumed the git-built wheel."""
    data, _ = run_cli_json(
        "exec",
        dtu_env["id"],
        "--",
        "python3",
        "-c",
        (
            f"import {_DUMMY_PKG_MODULE}; "
            f"print({_DUMMY_PKG_MODULE}.__version__)"
        ),
    )
    assert data["exit_code"] == 0, (
        f"Import failed (exit {data['exit_code']}):\n"
        f"stdout: {data['stdout']}\nstderr: {data['stderr']}"
    )
    assert _DUMMY_PKG_VERSION in data["stdout"]


@pytest.mark.e2e
def test_pypiserver_running(dtu_env):
    """Verify pypiserver is still running inside the container."""
    data, _ = run_cli_json(
        "exec", dtu_env["id"], "--", "bash", "-lc", "pgrep -f pypi-server"
    )
    assert data["exit_code"] == 0, "pypiserver is not running"


@pytest.mark.e2e
def test_mitmproxy_log_contains_intercept_evidence(dtu_env):
    """Verify mitmproxy recorded a rewritten PyPI request."""
    data, _ = run_cli_json(
        "exec",
        dtu_env["id"],
        "--",
        "bash",
        "-lc",
        (
            "grep -E 'PYPI INDEX|PYPI WHEEL' /var/log/mitmdump.log "
            "2>/dev/null | tail -20"
        ),
    )
    assert data["exit_code"] == 0, (
        f"Expected PyPI interception evidence in mitmdump.log:\n{data['stdout']}"
    )
    assert "PYPI " in data["stdout"]


@pytest.mark.e2e
def test_pypiserver_serves_package_index(dtu_env):
    """Verify the local pypiserver exposes the overridden package index."""
    data, _ = run_cli_json(
        "exec",
        dtu_env["id"],
        "--",
        "bash",
        "-lc",
        (
            f"curl -fsS http://localhost:8081/simple/{_DUMMY_PKG_NAME}/ "
            "| tail -20"
        ),
    )
    assert data["exit_code"] == 0, (
        "Expected package-specific response from local pypiserver:\n"
        f"{data['stdout']}\n{data['stderr']}"
    )
    assert _DUMMY_PKG_MODULE in data["stdout"].lower()
