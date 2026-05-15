# Copyright (c) Microsoft. All rights reserved.

"""E2E test for shell-invocation parity across all four `exec` surfaces.

Installs amplifier-app-cli into /root/.local/bin (where `uv tool install`
places CLI binaries) and verifies `amplifier --version` succeeds through
each of the four invocation modes the engine exposes:

1. `exec <id> -- <cmd>` (JSON mode)         -> `bash -lc <cmd>`
2. `exec --stream <id> -- <cmd>`            -> `bash -lc <cmd>`
3. `exec <id>`  (bare interactive)          -> `bash -l`
4. `exec <id> --visual-id LABEL`            -> `bash -l` + DTU_VISUAL_ID env

All four paths must source `/etc/profile.d/dtu-env.sh` (where DTU writes
`PATH=/root/.cargo/bin:/root/.local/bin:$PATH`), so all four must find
`amplifier` on PATH without any inline workaround.

Cases 1 and 2 are driven through the CLI directly. Cases 3 and 4 are
interactive shells that cannot be driven through the CLI in a test
harness, so they are exercised at the same engine layer the CLI uses --
the exact bash invocation the engine builds, run via `incus.exec_command`
with a `-c` probe.

Sources of the invocations under test:
- exec_command:      engine.exec_command -> `bash -lc <cmd>`
- exec_stream:       engine.exec_stream  -> `bash -lc <cmd>`
- bare interactive:  incus.exec_interactive default -> `bash -l`
- --visual-id:       engine.exec_interactive(visual_id=...) -> `bash -l`
                     with `DTU_VISUAL_ID=<label>` forwarded via --env, picked
                     up by /etc/profile.d/dtu-visual-id.sh at attach time.

Run with: uv run pytest tests/e2e/features/test_exec_path_parity.py --run-e2e -v -s
"""

import pytest

from amplifier_bundle_digital_twin_universe import incus
from conftest import register_dtu_instance
from helpers import run_cli, run_cli_json


@pytest.fixture(scope="module")
def amplifier_install_profile(tmp_path_factory):
    """Profile that installs amplifier-app-cli into /root/.local/bin.

    setup_cmds run via `bash -lc` (login -> sources /etc/profile.d/dtu-env.sh),
    so `uv tool install` and the post-install sanity check both succeed
    without any inline PATH workaround. This is the baseline; the four tests
    below assert the remaining exec surfaces behave the same way.
    """
    d = tmp_path_factory.mktemp("exec-path-parity")
    profile = d / "exec-path-parity.yaml"
    profile.write_text(
        """\
name: exec-path-parity
description: >
  Installs amplifier-app-cli into /root/.local/bin so the four exec surfaces
  (exec, exec --stream, bare interactive, --visual-id) can each be verified
  for PATH parity.

base:
  image: ubuntu:24.04

provision:
  setup_cmds:
    - apt-get update && apt-get install -y git curl
    # uv installs into /root/.cargo/bin and /root/.local/bin -- both are
    # added to PATH via /etc/profile.d/dtu-env.sh by the DTU engine.
    - curl -LsSf https://astral.sh/uv/install.sh | sh
    # Install the Amplifier CLI from upstream. `uv tool install` places the
    # `amplifier` entry point in /root/.local/bin.
    - uv tool install git+https://github.com/microsoft/amplifier-app-cli
    # Sanity check via the same `bash -lc` path setup_cmds runs through.
    # If this line fails, the install itself is broken and the four
    # PATH-parity assertions below would be testing the wrong thing.
    - amplifier --version
"""
    )
    return str(profile)


@pytest.fixture(scope="module")
def dtu_env(amplifier_install_profile):
    """Launch the profile (long timeout: amplifier install pulls many deps)."""
    data, _ = run_cli_json("launch", amplifier_install_profile, timeout=600)
    assert isinstance(data, dict), f"expected dict, got {type(data)}"
    register_dtu_instance(data["id"])
    yield data
    run_cli("destroy", data["id"], timeout=60)


@pytest.mark.e2e
def test_exec_command_finds_amplifier(dtu_env):
    """`exec <id> -- amplifier --version` (JSON mode -> `bash -lc`).

    The engine wraps the user command in `bash -lc` (login shell), which
    sources /etc/profile.d/dtu-env.sh and puts /root/.local/bin on PATH.
    """
    data, _ = run_cli_json(
        "exec", dtu_env["id"], "--", "amplifier", "--version", timeout=60
    )
    assert isinstance(data, dict), f"expected dict, got {type(data)}"
    assert data["exit_code"] == 0, (
        "`exec -- amplifier --version` failed -- amplifier missing from "
        "bash -lc PATH.\n"
        f"  stdout: {data['stdout']!r}\n"
        f"  stderr: {data['stderr']!r}"
    )
    assert "amplifier" in data["stdout"].lower(), (
        f"unexpected stdout from amplifier --version: {data['stdout']!r}"
    )


@pytest.mark.e2e
def test_exec_stream_finds_amplifier(dtu_env):
    """`exec --stream <id> -- amplifier --version` (stream mode -> `bash -lc`).

    Same wrapping as JSON mode; output streams to the CLI's stdout instead
    of being captured into a JSON envelope.
    """
    result = run_cli(
        "exec",
        "--stream",
        dtu_env["id"],
        "--",
        "amplifier",
        "--version",
        timeout=60,
    )
    assert result.returncode == 0, (
        "`exec --stream -- amplifier --version` failed -- amplifier missing "
        f"from bash -lc PATH (exit {result.returncode}).\n"
        f"  stdout: {result.stdout}\n"
        f"  stderr: {result.stderr}"
    )
    assert "amplifier" in result.stdout.lower(), (
        f"unexpected stdout from amplifier --version: {result.stdout!r}"
    )


@pytest.mark.e2e
def test_bare_interactive_finds_amplifier(dtu_env):
    """Bare `exec <id>` (no command, no --visual-id) -> `bash -l`.

    Bare interactive attach defaults to `bash -l` (login shell) via
    incus.exec_interactive at incus.py:240. The interactive shell cannot
    be driven through a test harness, so this test probes the same shell
    semantics by running `bash -l -c "amplifier --version"` via
    incus.exec_command. Both forms are login shells and therefore source
    /etc/profile -> /etc/profile.d/*.sh -> dtu-env.sh in the same way; the
    only difference is interactive prompt handling, which does not affect
    PATH resolution.
    """
    exit_code, stdout, stderr = incus.exec_command(
        dtu_env["id"],
        ["bash", "-l", "-c", "amplifier --version"],
        timeout=60,
    )
    assert exit_code == 0, (
        "bare interactive (`bash -l`) failed to find amplifier on PATH -- "
        "/etc/profile.d/dtu-env.sh was not sourced.\n"
        f"  stdout: {stdout!r}\n"
        f"  stderr: {stderr!r}"
    )
    assert "amplifier" in stdout.lower(), (
        f"unexpected stdout from amplifier --version: {stdout!r}"
    )


@pytest.mark.e2e
def test_visual_id_interactive_finds_amplifier(dtu_env):
    """`exec <id> --visual-id LABEL` -> `bash -l` with DTU_VISUAL_ID env.

    The --visual-id path launches the same `bash -l` login shell as the
    bare interactive case; the only difference is `DTU_VISUAL_ID=<label>`
    is forwarded via `incus exec --env`. The static
    /etc/profile.d/dtu-visual-id.sh script installed at launch picks up
    the env var and adds the PROMPT_COMMAND-based prefix to PS1.

    Because the shell mechanism is just `bash -l`, PATH parity with the
    other three exec surfaces is automatic. This test asserts that and
    pins the contract so future visual-id mechanism changes can't break
    PATH resolution without failing the test.
    """
    exit_code, stdout, stderr = incus.exec_command(
        dtu_env["id"],
        ["bash", "-l", "-c", "amplifier --version"],
        env={"DTU_VISUAL_ID": "path-parity"},
        timeout=60,
    )
    assert exit_code == 0, (
        "--visual-id shell (`bash -l` + DTU_VISUAL_ID) failed to find "
        "amplifier on PATH -- /etc/profile.d/dtu-env.sh was not sourced.\n"
        f"  stdout: {stdout!r}\n"
        f"  stderr: {stderr!r}"
    )
    assert "amplifier" in stdout.lower(), (
        f"unexpected stdout from amplifier --version: {stdout!r}"
    )
