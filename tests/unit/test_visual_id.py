# Copyright (c) Microsoft. All rights reserved.

"""Unit tests for the --visual-id flag and engine helpers.

These tests do not require Incus or Docker. They exercise:
- sanitize_visual_id validation
- _VISUAL_ID_PROFILE_D_SCRIPT content shape
- CLI flag parsing and forwarding via a mocked engine

The --visual-id flag uses an empty-string sentinel for "use profile name":
  not passed             -> visual_id=None         -> no prefix
  --visual-id ""         -> visual_id=""           -> resolve to profile name
  --visual-id LABEL      -> visual_id="LABEL"      -> use LABEL literally

The engine forwards the label as the ``DTU_VISUAL_ID`` env var to the
``bash -l`` shell; the static ``/etc/profile.d/dtu-visual-id.sh`` script
written at launch picks it up via PROMPT_COMMAND.

Run with: uv run pytest tests/unit/test_visual_id.py -v
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from amplifier_bundle_digital_twin_universe import engine
from amplifier_bundle_digital_twin_universe.cli import main


# ---------------------------------------------------------------------------
# sanitize_visual_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "amplifier-user-sim",
        "testing-pr-42",
        "a",
        "A1_b.c:d/e-f",
        "x" * 40,
    ],
)
def test_sanitize_visual_id_accepts_valid(value: str) -> None:
    assert engine.sanitize_visual_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",  # empty
        "has space",
        "has\nnewline",
        'has"quote',
        "has`backtick",
        "has$dollar",
        "has\\backslash",
        "has;semi",
        "x" * 41,  # too long
    ],
)
def test_sanitize_visual_id_rejects_invalid(value: str) -> None:
    with pytest.raises(ValueError):
        engine.sanitize_visual_id(value)


# ---------------------------------------------------------------------------
# /etc/profile.d/dtu-visual-id.sh script content
# ---------------------------------------------------------------------------


def test_profile_d_script_has_interactive_guard() -> None:
    """The script must only act on interactive shells, gated by `case $- in *i*)`.

    Non-interactive shells (bash -lc invoked by exec_command / exec_stream)
    must not have PROMPT_COMMAND modified -- PS1 is meaningless there and
    we don't want side effects in those code paths.
    """
    assert "case $- in" in engine._VISUAL_ID_PROFILE_D_SCRIPT
    assert "*i*)" in engine._VISUAL_ID_PROFILE_D_SCRIPT


def test_profile_d_script_checks_dtu_visual_id_set() -> None:
    """The script must be inert unless DTU_VISUAL_ID is set on the shell env."""
    assert 'if [ -n "${DTU_VISUAL_ID:-}" ]' in engine._VISUAL_ID_PROFILE_D_SCRIPT


def test_profile_d_script_installs_prompt_command() -> None:
    """The script must chain a _dtu_apply_prompt entry into PROMPT_COMMAND
    (preserving any existing user-set PROMPT_COMMAND on the right).
    """
    script = engine._VISUAL_ID_PROFILE_D_SCRIPT
    assert "_dtu_apply_prompt()" in script
    assert "PROMPT_COMMAND=" in script
    # Preserve any pre-existing PROMPT_COMMAND on the tail.
    assert "${PROMPT_COMMAND:+; $PROMPT_COMMAND}" in script


def test_profile_d_script_idempotency_guard() -> None:
    """_dtu_apply_prompt must skip re-application if the marker is already in PS1.

    Without this guard, each prompt redraw would stack a new copy of the
    prefix on top of the previous one.
    """
    script = engine._VISUAL_ID_PROFILE_D_SCRIPT
    assert '*"(dtu:${DTU_VISUAL_ID})"*' in script


def test_profile_d_script_contains_blue_escape_marker() -> None:
    """PS1 assignment must use the bold-blue ANSI escape pair wrapped in
    \\[ \\] readline non-printing markers so line-edit behaves correctly.
    """
    # The script is a Python string with single backslash escapes (\\[ -> \[).
    # We check for the literal bash form that bash will see.
    script = engine._VISUAL_ID_PROFILE_D_SCRIPT
    assert r"\[\e[1;34m\]" in script
    assert r"\[\e[0m\]" in script


def test_profile_d_script_starts_with_shebang() -> None:
    """The script needs a shebang so chmod +x and standalone invocation work
    (profile.d sourcing only needs read+exec, but the shebang is the
    convention used by dtu-env.sh as well).
    """
    assert engine._VISUAL_ID_PROFILE_D_SCRIPT.startswith("#!/bin/bash\n")


def test_profile_d_path_matches_engine_constant() -> None:
    """The container path the engine writes the script to must be in /etc/profile.d
    so login shells source it automatically via /etc/profile.
    """
    assert engine._VISUAL_ID_PROFILE_D_PATH == "/etc/profile.d/dtu-visual-id.sh"


# ---------------------------------------------------------------------------
# CLI flag parsing and forwarding
# ---------------------------------------------------------------------------


def test_exec_without_flag_passes_none() -> None:
    """Default interactive exec: --visual-id not passed, no prefix injected."""
    runner = CliRunner()
    with (
        patch.object(engine, "exec_interactive", return_value=0) as mock_exec,
        patch.object(engine, "status") as mock_status,
    ):
        result = runner.invoke(main, ["exec", "dtu-abc"])
        assert result.exit_code == 0
        mock_exec.assert_called_once_with("dtu-abc", visual_id=None)
        mock_status.assert_not_called()


def test_exec_with_empty_visual_id_resolves_to_profile() -> None:
    """--visual-id "" (empty sentinel) resolves the profile name via engine.status()."""
    runner = CliRunner()
    with (
        patch.object(engine, "exec_interactive", return_value=0) as mock_exec,
        patch.object(
            engine,
            "status",
            return_value={"id": "dtu-abc", "profile": "amplifier-user-sim"},
        ) as mock_status,
    ):
        result = runner.invoke(main, ["exec", "--visual-id", "", "dtu-abc"])
        assert result.exit_code == 0, result.output
        mock_status.assert_called_once_with("dtu-abc")
        mock_exec.assert_called_once_with("dtu-abc", visual_id="amplifier-user-sim")


def test_exec_with_explicit_visual_id_uses_value() -> None:
    """--visual-id LABEL uses the provided string; no status lookup needed."""
    runner = CliRunner()
    with (
        patch.object(engine, "exec_interactive", return_value=0) as mock_exec,
        patch.object(engine, "status") as mock_status,
    ):
        result = runner.invoke(
            main, ["exec", "--visual-id", "testing-pr-42", "dtu-abc"]
        )
        assert result.exit_code == 0, result.output
        mock_status.assert_not_called()
        mock_exec.assert_called_once_with("dtu-abc", visual_id="testing-pr-42")


def test_exec_with_equals_syntax() -> None:
    """--visual-id=LABEL syntax works identically to space-separated."""
    runner = CliRunner()
    with (
        patch.object(engine, "exec_interactive", return_value=0) as mock_exec,
        patch.object(engine, "status") as mock_status,
    ):
        result = runner.invoke(main, ["exec", "--visual-id=testing-pr-42", "dtu-abc"])
        assert result.exit_code == 0, result.output
        mock_status.assert_not_called()
        mock_exec.assert_called_once_with("dtu-abc", visual_id="testing-pr-42")


def test_exec_with_invalid_label_exits_with_error() -> None:
    """Invalid --visual-id values fail fast before touching the engine."""
    runner = CliRunner()
    with patch.object(engine, "exec_interactive", return_value=0) as mock_exec:
        result = runner.invoke(main, ["exec", "--visual-id", "has space", "dtu-abc"])
        assert result.exit_code == 1
        assert "Invalid --visual-id" in result.output or "Error" in result.output
        mock_exec.assert_not_called()


def test_exec_with_command_ignores_visual_id_in_stream_mode() -> None:
    """--visual-id is prompt-only; --stream mode has no prompt."""
    runner = CliRunner()
    with (
        patch.object(engine, "exec_stream", return_value=0) as mock_stream,
        patch.object(engine, "exec_command") as mock_command,
    ):
        result = runner.invoke(
            main,
            [
                "exec",
                "--stream",
                "--visual-id",
                "",
                "dtu-abc",
                "--",
                "echo",
                "hi",
            ],
        )
        assert result.exit_code == 0, result.output
        mock_stream.assert_called_once()
        mock_command.assert_not_called()


def test_exec_with_command_ignores_visual_id_in_json_mode() -> None:
    """--visual-id is prompt-only; JSON mode has no prompt."""
    runner = CliRunner()
    with (
        patch.object(
            engine,
            "exec_command",
            return_value={"id": "dtu-abc", "exit_code": 0, "stdout": "", "stderr": ""},
        ) as mock_command,
        patch.object(engine, "exec_stream") as mock_stream,
    ):
        result = runner.invoke(
            main,
            [
                "exec",
                "--visual-id",
                "",
                "dtu-abc",
                "--",
                "echo",
                "hi",
            ],
        )
        assert result.exit_code == 0, result.output
        mock_command.assert_called_once()
        mock_stream.assert_not_called()


def test_exec_warns_when_profile_missing() -> None:
    """--visual-id "" with no profile in status should warn and fall back."""
    runner = CliRunner()
    with (
        patch.object(engine, "exec_interactive", return_value=0) as mock_exec,
        patch.object(
            engine,
            "status",
            return_value={"id": "dtu-abc"},  # no 'profile' key
        ),
    ):
        result = runner.invoke(main, ["exec", "--visual-id", "", "dtu-abc"])
        assert result.exit_code == 0
        assert "could not resolve profile name" in result.output
        # Engine called with None since profile resolution failed.
        mock_exec.assert_called_once_with("dtu-abc", visual_id=None)
