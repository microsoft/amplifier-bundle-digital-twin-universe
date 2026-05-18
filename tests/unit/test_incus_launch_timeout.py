# Copyright (c) Microsoft. All rights reserved.

"""Unit tests for configurable incus launch timeout.

Verifies that create_container() reads AMPLIFIER_DTU_INCUS_LAUNCH_TIMEOUT_SECONDS
from the environment and passes the correct timeout to subprocess.run().

Bug context: on a loaded host (36+ containers), ``incus launch`` takes 130-170s
in practice.  The hardcoded timeout=120 in create_container() caused TimeoutExpired
during smoke tests.  This test suite locks in the configurable-timeout behaviour.

No real Incus daemon required -- all subprocess calls are mocked.

Run with: uv run pytest tests/unit/test_incus_launch_timeout.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from amplifier_bundle_digital_twin_universe import incus
from amplifier_bundle_digital_twin_universe.incus import _get_launch_timeout_seconds


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _ok_result() -> MagicMock:
    """Return a mock subprocess.CompletedProcess with returncode=0."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = ""
    m.stderr = ""
    return m


_ENV_VAR = "AMPLIFIER_DTU_INCUS_LAUNCH_TIMEOUT_SECONDS"
_DEFAULT = 120


# ===========================================================================
# Section A: _get_launch_timeout_seconds() helper -- direct unit tests
# ===========================================================================


def test_helper_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns the 120s default when the env var is not set at all."""
    monkeypatch.delenv(_ENV_VAR, raising=False)
    assert _get_launch_timeout_seconds() == _DEFAULT


def test_helper_default_when_env_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns the 120s default when the env var is set to an empty string."""
    monkeypatch.setenv(_ENV_VAR, "")
    assert _get_launch_timeout_seconds() == _DEFAULT


def test_helper_valid_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns the integer value when the env var is a valid positive integer."""
    monkeypatch.setenv(_ENV_VAR, "500")
    assert _get_launch_timeout_seconds() == 500


def test_helper_valid_override_boundary_low(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimum valid value: 1 second."""
    monkeypatch.setenv(_ENV_VAR, "1")
    assert _get_launch_timeout_seconds() == 1


def test_helper_valid_override_boundary_high(monkeypatch: pytest.MonkeyPatch) -> None:
    """Maximum valid value: 3600 seconds (1 hour)."""
    monkeypatch.setenv(_ENV_VAR, "3600")
    assert _get_launch_timeout_seconds() == 3600


def test_helper_non_integer_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Non-integer string: falls back to default and emits a warning to stderr."""
    monkeypatch.setenv(_ENV_VAR, "abc")
    result = _get_launch_timeout_seconds()
    assert result == _DEFAULT, f"Expected {_DEFAULT}, got {result}"
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower() or _ENV_VAR in captured.err, (
        f"Expected a warning referencing {_ENV_VAR!r} in stderr; got: {captured.err!r}"
    )


def test_helper_negative_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Negative integer: falls back to default and emits a warning to stderr."""
    monkeypatch.setenv(_ENV_VAR, "-5")
    result = _get_launch_timeout_seconds()
    assert result == _DEFAULT, f"Expected {_DEFAULT}, got {result}"
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower() or _ENV_VAR in captured.err, (
        f"Expected a warning referencing {_ENV_VAR!r} in stderr; got: {captured.err!r}"
    )


def test_helper_zero_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Zero: falls back to default and emits a warning to stderr."""
    monkeypatch.setenv(_ENV_VAR, "0")
    result = _get_launch_timeout_seconds()
    assert result == _DEFAULT, f"Expected {_DEFAULT}, got {result}"
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower() or _ENV_VAR in captured.err, (
        f"Expected a warning referencing {_ENV_VAR!r} in stderr; got: {captured.err!r}"
    )


def test_helper_too_large_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Value exceeding the 3600s cap: falls back to default and warns."""
    monkeypatch.setenv(_ENV_VAR, "9999")
    result = _get_launch_timeout_seconds()
    assert result == _DEFAULT, f"Expected {_DEFAULT}, got {result}"
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower() or _ENV_VAR in captured.err, (
        f"Expected a warning referencing {_ENV_VAR!r} in stderr; got: {captured.err!r}"
    )


# ===========================================================================
# Section B: create_container() integration -- subprocess.run kwargs
# ===========================================================================


def test_create_container_default_timeout_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess.run receives timeout=120 when env var is not set."""
    monkeypatch.delenv(_ENV_VAR, raising=False)
    with patch(
        "amplifier_bundle_digital_twin_universe.incus.subprocess.run",
        return_value=_ok_result(),
    ) as mock_run:
        incus.create_container("my-dtu", "images:ubuntu/24.04")

    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    assert kwargs.get("timeout") == _DEFAULT, (
        f"Expected timeout={_DEFAULT}, got timeout={kwargs.get('timeout')}"
    )


def test_create_container_timeout_overridden_by_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess.run receives the env-var timeout when AMPLIFIER_DTU_INCUS_LAUNCH_TIMEOUT_SECONDS=500."""
    monkeypatch.setenv(_ENV_VAR, "500")
    with patch(
        "amplifier_bundle_digital_twin_universe.incus.subprocess.run",
        return_value=_ok_result(),
    ) as mock_run:
        incus.create_container("my-dtu", "images:ubuntu/24.04")

    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    assert kwargs.get("timeout") == 500, (
        f"Expected timeout=500, got timeout={kwargs.get('timeout')}"
    )


def test_create_container_invalid_env_var_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Invalid env var: create_container falls back to 120s and warns to stderr."""
    monkeypatch.setenv(_ENV_VAR, "abc")
    with patch(
        "amplifier_bundle_digital_twin_universe.incus.subprocess.run",
        return_value=_ok_result(),
    ) as mock_run:
        incus.create_container("my-dtu", "images:ubuntu/24.04")

    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    assert kwargs.get("timeout") == _DEFAULT, (
        f"Expected fallback timeout={_DEFAULT}, got timeout={kwargs.get('timeout')}"
    )
    captured = capsys.readouterr()
    assert captured.err.strip() != "", (
        "Expected a warning to stderr for an invalid env var value"
    )


def test_create_container_negative_env_var_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Negative env var: create_container falls back to 120s and warns."""
    monkeypatch.setenv(_ENV_VAR, "-5")
    with patch(
        "amplifier_bundle_digital_twin_universe.incus.subprocess.run",
        return_value=_ok_result(),
    ) as mock_run:
        incus.create_container("my-dtu", "images:ubuntu/24.04")

    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    assert kwargs.get("timeout") == _DEFAULT, (
        f"Expected fallback timeout={_DEFAULT}, got timeout={kwargs.get('timeout')}"
    )
    captured = capsys.readouterr()
    assert captured.err.strip() != "", (
        "Expected a warning to stderr for a negative env var value"
    )


def test_create_container_empty_env_var_falls_back_silently(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Empty string env var: falls back to 120s without any warning (treated as unset)."""
    monkeypatch.setenv(_ENV_VAR, "")
    with patch(
        "amplifier_bundle_digital_twin_universe.incus.subprocess.run",
        return_value=_ok_result(),
    ) as mock_run:
        incus.create_container("my-dtu", "images:ubuntu/24.04")

    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    assert kwargs.get("timeout") == _DEFAULT, (
        f"Expected default timeout={_DEFAULT}, got timeout={kwargs.get('timeout')}"
    )
    # Empty string is treated as unset -- no warning expected
    captured = capsys.readouterr()
    assert captured.err.strip() == "", (
        f"Expected no warning for empty env var; got: {captured.err!r}"
    )
