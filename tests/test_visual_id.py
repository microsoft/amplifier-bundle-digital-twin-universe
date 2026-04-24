# Copyright (c) Microsoft. All rights reserved.

"""Unit tests for the --visual-id flag and engine helpers.

These tests do not require Incus or Docker. They exercise:
- sanitize_visual_id validation
- build_visual_id_rcfile output shape
- CLI flag parsing and forwarding via a mocked engine

The --visual-id flag uses an empty-string sentinel for "use profile name":
  not passed             -> visual_id=None         -> no prefix
  --visual-id ""         -> visual_id=""           -> resolve to profile name
  --visual-id LABEL      -> visual_id="LABEL"      -> use LABEL literally

Run with: uv run pytest tests/test_visual_id.py -v
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
# build_visual_id_rcfile
# ---------------------------------------------------------------------------


def test_build_visual_id_rcfile_contains_blue_escape() -> None:
    content = engine.build_visual_id_rcfile("my-profile")
    # Literal escape sequence for bold blue + reset, with \[ \] markers.
    assert r"\[\e[1;34m\](dtu:my-profile)\[\e[0m\]" in content


def test_build_visual_id_rcfile_sources_default_bashrc_first() -> None:
    content = engine.build_visual_id_rcfile("foo")
    lines = content.splitlines()
    # The PS1 assignment must come AFTER the bashrc sources so the container's
    # default bashrc doesn't clobber our PS1.
    bashrc_idx = next(i for i, line in enumerate(lines) if "bash.bashrc" in line)
    ps1_idx = next(i for i, line in enumerate(lines) if line.startswith("PS1="))
    assert ps1_idx > bashrc_idx


def test_build_visual_id_rcfile_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        engine.build_visual_id_rcfile('has"quote')


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
    """--visual-id \"\" (empty sentinel) resolves the profile name via engine.status()."""
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
    """--visual-id \"\" with no profile in status should warn and fall back."""
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
