# Copyright (c) Microsoft. All rights reserved.

"""E2E tests for the --visual-id interactive shell.

Launches a minimal container, then exercises the static
``/etc/profile.d/dtu-visual-id.sh`` script that the DTU engine writes at
launch time. The script picks up a ``DTU_VISUAL_ID`` env var and installs
a PROMPT_COMMAND that prepends a blue ``(dtu:<label>)`` marker to PS1.

The script's structure is:
- Gate on `$-` containing `i` (only interactive shells get the prefix)
- Gate on `DTU_VISUAL_ID` being non-empty
- Install PROMPT_COMMAND with an idempotency guard so the prefix doesn't
  stack across redraws

These tests use `bash -l -i -c ...` to get a login + interactive shell that
the script targets, then probe the resulting environment.

Run with: uv run pytest tests/e2e/features/test_visual_id.py --run-e2e -v -s
"""

import os

import pytest

from amplifier_bundle_digital_twin_universe import incus
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
description: Minimal profile for visual-id PROMPT_COMMAND injection tests

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


# ---------------------------------------------------------------------------
# PS1 prefix behavior
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_visual_id_installs_prompt_command(dtu_env):
    """With DTU_VISUAL_ID set, an interactive login shell should have
    PROMPT_COMMAND pointing at the _dtu_apply_prompt function.

    PROMPT_COMMAND only runs before each prompt redraw; in a `bash -i -c`
    invocation no prompt is drawn, so we can't observe PS1 directly. We
    instead verify that the installation hook took effect.
    """
    container_id = dtu_env["id"]
    exit_code, stdout, stderr = incus.exec_command(
        container_id,
        ["bash", "-l", "-i", "-c", "echo PC=$PROMPT_COMMAND"],
        env={"DTU_VISUAL_ID": "e2e-label"},
    )
    assert exit_code == 0, f"bash failed: {stderr}"
    assert "_dtu_apply_prompt" in stdout, (
        f"PROMPT_COMMAND missing _dtu_apply_prompt hook: {stdout!r}"
    )


@pytest.mark.e2e
def test_visual_id_prefix_applied_after_prompt_command_runs(dtu_env):
    """Manually invoking PROMPT_COMMAND should produce a PS1 with the
    blue (dtu:<label>) prefix.

    This is the closest we can get in a non-PTY test to observing what a
    real interactive shell sees after the first prompt redraw.
    """
    container_id = dtu_env["id"]
    exit_code, stdout, stderr = incus.exec_command(
        container_id,
        [
            "bash",
            "-l",
            "-i",
            "-c",
            'eval "$PROMPT_COMMAND"; echo "PROMPT=$PS1"',
        ],
        env={"DTU_VISUAL_ID": "e2e-label"},
    )
    assert exit_code == 0, f"bash failed: {stderr}"
    assert "PROMPT=" in stdout, f"probe missing from output: {stdout!r}"
    assert r"\[\e[1;34m\](dtu:e2e-label)\[\e[0m\]" in stdout, (
        f"visual-id prefix missing from PS1 after PROMPT_COMMAND: {stdout!r}"
    )


@pytest.mark.e2e
def test_visual_id_preserves_default_ps1(dtu_env):
    """PS1 after the prefix is applied must still include the container's
    default prompt tokens.
    """
    container_id = dtu_env["id"]
    exit_code, stdout, _stderr = incus.exec_command(
        container_id,
        [
            "bash",
            "-l",
            "-i",
            "-c",
            'eval "$PROMPT_COMMAND"; echo "PROMPT=$PS1"',
        ],
        env={"DTU_VISUAL_ID": "keep-default"},
    )
    assert exit_code == 0
    # Ubuntu's default PS1 for root contains "\h" (hostname) and "\w" (workdir).
    # After our injection, the default markers should still be present at the tail.
    assert "\\h" in stdout or "\\w" in stdout or "\\u" in stdout, (
        f"default PS1 tokens missing after injection: {stdout!r}"
    )


@pytest.mark.e2e
def test_visual_id_prefix_is_idempotent(dtu_env):
    """Running PROMPT_COMMAND multiple times must not stack prefixes.

    The idempotency check inside _dtu_apply_prompt looks for the marker
    already in PS1 and skips re-application.
    """
    container_id = dtu_env["id"]
    exit_code, stdout, _stderr = incus.exec_command(
        container_id,
        [
            "bash",
            "-l",
            "-i",
            "-c",
            'eval "$PROMPT_COMMAND"; eval "$PROMPT_COMMAND"; eval "$PROMPT_COMMAND"; '
            'echo "PROMPT=$PS1"',
        ],
        env={"DTU_VISUAL_ID": "idem"},
    )
    assert exit_code == 0
    # Count occurrences of the marker -- must be exactly one even after three runs.
    marker = "(dtu:idem)"
    assert stdout.count(marker) == 1, (
        f"prefix stacked {stdout.count(marker)} times instead of 1: {stdout!r}"
    )


# ---------------------------------------------------------------------------
# Inertness when DTU_VISUAL_ID is unset or non-interactive
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_no_prompt_command_when_visual_id_unset(dtu_env):
    """When DTU_VISUAL_ID is unset, the script must not install
    PROMPT_COMMAND -- bare `exec <id>` should be a normal login shell.
    """
    container_id = dtu_env["id"]
    exit_code, stdout, _stderr = incus.exec_command(
        container_id,
        ["bash", "-l", "-i", "-c", "echo PC=$PROMPT_COMMAND"],
    )
    assert exit_code == 0
    assert "_dtu_apply_prompt" not in stdout, (
        "PROMPT_COMMAND installed even though DTU_VISUAL_ID was unset -- "
        "the case-on-DTU_VISUAL_ID guard is not working. "
        f"stdout={stdout!r}"
    )


@pytest.mark.e2e
def test_no_prompt_command_in_noninteractive_shell(dtu_env):
    """When the shell is non-interactive (no -i flag, i.e. the JSON / --stream
    exec path), the script must skip PROMPT_COMMAND installation even with
    DTU_VISUAL_ID set. PS1 is meaningless in non-interactive shells and we
    don't want to pollute the env.
    """
    container_id = dtu_env["id"]
    exit_code, stdout, _stderr = incus.exec_command(
        container_id,
        ["bash", "-l", "-c", "echo PC=$PROMPT_COMMAND"],
        env={"DTU_VISUAL_ID": "should-not-apply"},
    )
    assert exit_code == 0
    assert "_dtu_apply_prompt" not in stdout, (
        "PROMPT_COMMAND installed in a non-interactive shell -- the case-on-$- "
        "interactive guard is not working. "
        f"stdout={stdout!r}"
    )


# ---------------------------------------------------------------------------
# PATH and passthrough env inheritance (now trivial: bash -l natively
# sources /etc/profile -> /etc/profile.d/*.sh)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_visual_id_shell_inherits_dtu_env_path(dtu_env):
    """A --visual-id login shell must inherit PATH from /etc/profile.d/dtu-env.sh.

    With the new mechanism this is trivial because the shell is `bash -l` --
    same as bare interactive -- and login shells source /etc/profile, which
    in turn sources /etc/profile.d/*.sh. The test is retained as a
    regression guard against any future change that breaks this contract.
    """
    container_id = dtu_env["id"]
    exit_code, stdout, stderr = incus.exec_command(
        container_id,
        ["bash", "-l", "-i", "-c", "echo PATH=$PATH"],
        env={"DTU_VISUAL_ID": "path-probe"},
    )
    assert exit_code == 0, f"bash failed: {stderr}"
    assert "/root/.local/bin" in stdout, (
        "visual-id shell missing /root/.local/bin from PATH -- "
        f"/etc/profile.d/dtu-env.sh was not sourced. stdout={stdout!r}"
    )
    assert "/root/.cargo/bin" in stdout, (
        "visual-id shell missing /root/.cargo/bin from PATH -- "
        f"/etc/profile.d/dtu-env.sh was not sourced. stdout={stdout!r}"
    )


@pytest.mark.e2e
def test_visual_id_shell_inherits_passthrough_env_var(dtu_env):
    """A --visual-id login shell must inherit passthrough env vars written
    to /etc/profile.d/dtu-env.sh.

    The fixture sets ``DTU_VISUAL_ID_PATH_SENTINEL`` on the host before
    launch, and the profile declares it under ``passthrough.services[].key_env``.
    engine._write_env forwards it into the container's
    ``/etc/profile.d/dtu-env.sh``. The bash -l shell sources that file
    automatically; the assertion here covers any future regression where
    that source ordering changes.
    """
    container_id = dtu_env["id"]
    # Sanity check the host actually has the sentinel set -- otherwise the test
    # would silently pass against the buggy code path (empty == empty).
    assert os.environ.get(_PATH_SENTINEL_ENV) == _PATH_SENTINEL_VALUE, (
        f"fixture failed to set {_PATH_SENTINEL_ENV} on the host"
    )

    exit_code, stdout, stderr = incus.exec_command(
        container_id,
        ["bash", "-l", "-i", "-c", f"echo SENTINEL=${_PATH_SENTINEL_ENV}"],
        env={"DTU_VISUAL_ID": "env-probe"},
    )
    assert exit_code == 0, f"bash failed: {stderr}"
    assert f"SENTINEL={_PATH_SENTINEL_VALUE}" in stdout, (
        f"visual-id shell missing passthrough env var {_PATH_SENTINEL_ENV} -- "
        "/etc/profile.d/dtu-env.sh was not sourced. "
        f"stdout={stdout!r}"
    )
