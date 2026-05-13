# Copyright (c) Microsoft. All rights reserved.

"""E2E tests for the --visual-id interactive shell.

Launches a minimal container, installs the visual-id rcfile via the engine
helper, then runs bash with ``--rcfile`` against it to verify three contracts
of the interactive non-login shell that ``exec --visual-id`` produces:

1. PS1 carries the ``(dtu:<label>)`` blue prefix.
2. PS1 still includes the container's default prompt tokens.
3. PATH and passthrough env vars from ``/etc/profile.d/dtu-env.sh`` are
   inherited (regression test for the bug where the rcfile only sourced
   ``/etc/bash.bashrc`` and ``~/.bashrc`` and missed ``/etc/profile.d/*.sh``).

Run with: uv run pytest tests/e2e/features/test_visual_id.py --run-e2e -v -s
"""

import os

import pytest

from amplifier_bundle_digital_twin_universe import engine, incus
from conftest import register_dtu_instance
from helpers import run_cli, run_cli_json

# Sentinel env var forwarded into the container via passthrough.services so we
# can assert that --visual-id shells see passthrough exports. Distinct from any
# real provider key so it can't be confused with a configured API key.
_PATH_SENTINEL_ENV = "DTU_VISUAL_ID_PATH_SENTINEL"
_PATH_SENTINEL_VALUE = "visual-id-path-sentinel-ok"


@pytest.fixture(scope="module")
def visual_id_profile(tmp_path_factory):
    """Minimal ubuntu profile for visual-id tests.

    Declares a passthrough.services entry for a sentinel env var so the
    generated ``/etc/profile.d/dtu-env.sh`` exports both the baseline PATH
    additions and the forwarded sentinel -- giving the env-inheritance tests
    something deterministic to probe.
    """
    profile_dir = tmp_path_factory.mktemp("visual-id-profile")
    profile_path = profile_dir / "visual-id-smoke.yaml"
    profile_path.write_text(
        f"""\
name: visual-id-smoke
description: Minimal profile for visual-id PS1 injection tests

base:
  image: ubuntu:24.04

passthrough:
  allow_external: true
  services:
    - name: path-sentinel
      key_env: {_PATH_SENTINEL_ENV}
"""
    )
    return str(profile_path)


@pytest.fixture(scope="module")
def dtu_env(visual_id_profile, monkeypatch_module):
    """Create a minimal DTU environment for this module's tests.

    Sets the sentinel env var before launch so engine._write_env forwards it
    into /etc/profile.d/dtu-env.sh; restored on teardown.
    """
    monkeypatch_module.setenv(_PATH_SENTINEL_ENV, _PATH_SENTINEL_VALUE)
    data, _ = run_cli_json("launch", visual_id_profile, timeout=180)
    assert isinstance(data, dict)
    register_dtu_instance(data["id"])
    yield data
    run_cli("destroy", data["id"], timeout=30)


@pytest.fixture(scope="module")
def monkeypatch_module():
    """Module-scoped monkeypatch -- pytest's built-in monkeypatch is function-scoped."""
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    yield mp
    mp.undo()


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


@pytest.mark.e2e
def test_visual_id_shell_inherits_dtu_env_path(dtu_env):
    """A --visual-id interactive shell must inherit PATH from /etc/profile.d/dtu-env.sh.

    Regression test for the bug where ``bash --rcfile <X> -i`` -- the shell
    that ``exec --visual-id`` launches -- sources ``/etc/bash.bashrc`` and
    ``~/.bashrc`` but never ``/etc/profile.d/dtu-env.sh``, leaving
    ``/root/.cargo/bin`` and ``/root/.local/bin`` (where DTU installs
    ``amplifier`` and ``uv``) missing from PATH.

    Asserts against the engine's baseline PATH export at
    ``_write_env`` -> ``/etc/profile.d/dtu-env.sh``.
    """
    container_id = dtu_env["id"]
    rcfile_path = engine.install_visual_id_rcfile(container_id, "path-probe")

    exit_code, stdout, stderr = incus.exec_command(
        container_id,
        ["bash", "--rcfile", rcfile_path, "-i", "-c", 'echo "PATH=$PATH"'],
    )
    assert exit_code == 0, f"bash -i failed: {stderr}"
    # Both paths are written to /etc/profile.d/dtu-env.sh by engine._write_env.
    # If the rcfile doesn't source profile.d, neither will appear here.
    assert "/root/.local/bin" in stdout, (
        "visual-id shell missing /root/.local/bin from PATH -- "
        "rcfile likely failed to source /etc/profile.d/dtu-env.sh. "
        f"stdout={stdout!r}"
    )
    assert "/root/.cargo/bin" in stdout, (
        "visual-id shell missing /root/.cargo/bin from PATH -- "
        "rcfile likely failed to source /etc/profile.d/dtu-env.sh. "
        f"stdout={stdout!r}"
    )


@pytest.mark.e2e
def test_visual_id_shell_inherits_passthrough_env_var(dtu_env):
    """A --visual-id interactive shell must inherit passthrough env vars
    written to /etc/profile.d/dtu-env.sh.

    The fixture sets ``DTU_VISUAL_ID_PATH_SENTINEL`` on the host before launch,
    and the profile declares it under ``passthrough.services[].key_env``.
    engine._write_env forwards it into the container's
    ``/etc/profile.d/dtu-env.sh``. The --visual-id shell must source that file
    or the value will be missing -- which is the exact symptom users hit when
    a forwarded API key like ``ANTHROPIC_API_KEY`` is needed inside the shell.
    """
    container_id = dtu_env["id"]
    # Sanity check the host actually has the sentinel set -- otherwise the test
    # would silently pass against the buggy code path (empty == empty).
    assert os.environ.get(_PATH_SENTINEL_ENV) == _PATH_SENTINEL_VALUE, (
        f"fixture failed to set {_PATH_SENTINEL_ENV} on the host"
    )

    rcfile_path = engine.install_visual_id_rcfile(container_id, "env-probe")
    exit_code, stdout, stderr = incus.exec_command(
        container_id,
        [
            "bash",
            "--rcfile",
            rcfile_path,
            "-i",
            "-c",
            f'echo "SENTINEL=${_PATH_SENTINEL_ENV}"',
        ],
    )
    assert exit_code == 0, f"bash -i failed: {stderr}"
    assert f"SENTINEL={_PATH_SENTINEL_VALUE}" in stdout, (
        f"visual-id shell missing passthrough env var {_PATH_SENTINEL_ENV} -- "
        "rcfile likely failed to source /etc/profile.d/dtu-env.sh. "
        f"stdout={stdout!r}"
    )
