# Copyright (c) Microsoft. All rights reserved.

"""Shared fixtures and test configuration."""

import os
import shutil
import socket

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests that require Incus",
    )
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="Run E2E tests (requires Docker, Incus, amplifier-gitea, API keys)",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-integration"):
        skip = pytest.mark.skip(reason="needs --run-integration")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip)

    if not config.getoption("--run-e2e"):
        skip = pytest.mark.skip(reason="needs --run-e2e")
        for item in items:
            if "e2e" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def check_uv():
    if not shutil.which("uv"):
        pytest.fail("uv is required to run tests: https://docs.astral.sh/uv/")


@pytest.fixture(scope="module")
def require_anthropic_key():
    """Skip the test module if ANTHROPIC_API_KEY is not set."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY required for E2E tests")
    return key


@pytest.fixture(scope="module")
def require_github_token():
    """Skip the test module if no GitHub token is available."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    if shutil.which("gh"):
        import subprocess

        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    pytest.skip("GitHub token required for mirror-from-github E2E tests")


@pytest.fixture(scope="module")
def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


# Session-wide registry of DTU instance IDs created by tests.
# Only these instances are cleaned up at session end -- instances from other
# sessions or manual use are left untouched.
_test_created_dtu_ids: set[str] = set()


def register_dtu_instance(instance_id: str) -> None:
    """Register a DTU instance ID for cleanup at session end.

    Call this immediately after ``launch`` in every test fixture or standalone
    test that creates a DTU environment.  The session-scoped cleanup fixture
    will force-delete any registered IDs that still exist when the test suite
    finishes (e.g. because a test was interrupted before its own teardown ran).
    """
    _test_created_dtu_ids.add(instance_id)


@pytest.fixture(autouse=True, scope="session")
def cleanup_test_containers():
    yield
    # Clean up DTU containers created by THIS test session only.
    # Instances from other sessions or manual use are NOT touched.
    import subprocess

    for instance_id in _test_created_dtu_ids:
        subprocess.run(
            ["incus", "delete", instance_id, "--force"],
            capture_output=True,
            timeout=30,
        )
