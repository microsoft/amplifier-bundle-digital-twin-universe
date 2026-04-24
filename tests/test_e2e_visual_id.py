# Copyright (c) Microsoft. All rights reserved.

"""E2E test for --visual-id PS1 injection.

Launches a minimal container, installs the visual-id rcfile via the engine
helper, then runs bash with ``--rcfile`` to verify that PS1 ends up with the
``(dtu:<label>)`` prefix.

Run with: uv run pytest tests/test_e2e_visual_id.py --run-e2e -v -s
"""

import pytest

from amplifier_bundle_digital_twin_universe import engine, incus
from conftest import register_dtu_instance
from helpers import run_cli, run_cli_json


@pytest.fixture(scope="module")
def visual_id_profile(tmp_path_factory):
    """Minimal ubuntu profile for visual-id tests."""
    profile_dir = tmp_path_factory.mktemp("visual-id-profile")
    profile_path = profile_dir / "visual-id-smoke.yaml"
    profile_path.write_text(
        """\
name: visual-id-smoke
description: Minimal profile for visual-id PS1 injection tests

base:
  image: ubuntu:24.04
"""
    )
    return str(profile_path)


@pytest.fixture(scope="module")
def dtu_env(visual_id_profile):
    """Create a minimal DTU environment for this module's tests."""
    data, _ = run_cli_json("launch", visual_id_profile, timeout=180)
    assert isinstance(data, dict)
    register_dtu_instance(data["id"])
    yield data
    run_cli("destroy", data["id"], timeout=30)


@pytest.mark.e2e
def test_install_rcfile_and_run_bash_with_prefix(dtu_env):
    """The rcfile written by the engine should produce a PS1 with the blue prefix."""
    container_id = dtu_env["id"]
    rcfile_path = engine.install_visual_id_rcfile(container_id, "e2e-label")

    # Run bash -i with --rcfile, echo PS1.  Interactive mode triggers rcfile
    # sourcing; -c runs our probe command after sourcing completes.
    exit_code, stdout, stderr = incus.exec_command(
        container_id,
        ["bash", "--rcfile", rcfile_path, "-i", "-c", 'echo "PROMPT=$PS1"'],
    )
    assert exit_code == 0, f"bash -i failed: {stderr}"
    assert "PROMPT=" in stdout, f"probe missing from output: {stdout!r}"
    # The literal escape-style marker we embedded in the rcfile should appear
    # in PS1 after sourcing.
    assert r"\[\e[1;34m\](dtu:e2e-label)\[\e[0m\]" in stdout, (
        f"visual-id prefix missing from PS1. stdout={stdout!r}"
    )


@pytest.mark.e2e
def test_rcfile_preserves_default_ps1(dtu_env):
    """PS1 after our prefix must still include the container's default prompt."""
    container_id = dtu_env["id"]
    rcfile_path = engine.install_visual_id_rcfile(container_id, "keep-default")
    exit_code, stdout, _stderr = incus.exec_command(
        container_id,
        ["bash", "--rcfile", rcfile_path, "-i", "-c", 'echo "PROMPT=$PS1"'],
    )
    assert exit_code == 0
    # Ubuntu's default PS1 for root contains "\h" (hostname) and "\w" (workdir).
    # After our injection, the default markers should still be present at the tail.
    assert "\\h" in stdout or "\\w" in stdout or "\\u" in stdout, (
        f"default PS1 tokens missing after injection: {stdout!r}"
    )
