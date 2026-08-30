# Copyright (c) Microsoft. All rights reserved.

"""Unit tests for the launch --max-instances guard.

Incident context: an eval batch launched 126 DTU instances over 3 days with
no cap anywhere in the stack, and the host hit 100% disk and stalled. This
test suite locks in the enforcement mechanism that prevents that from
happening silently again:

  - ``resolve_max_instances()`` -- cap resolution precedence (CLI flag >
    ``AMPLIFIER_DTU_MAX_INSTANCES`` env var > default 15).
  - ``count_live_instances()`` -- reuses the same discovery mechanism as
    ``list``/``list_environments`` (``incus.list_instances``).
  - ``_enforce_max_instances()`` -- raises before any container is created
    when the live count already meets/exceeds the cap; ``0`` disables it.
  - ``engine.launch()`` -- wires the above together and refuses *before*
    calling ``incus.create_container``.

No real Incus daemon required -- all subprocess/Incus calls are mocked.

Run with: uv run pytest tests/unit/test_max_instances_guard.py -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from amplifier_bundle_digital_twin_universe import engine as engine_mod

# ---------------------------------------------------------------------------
# Section A: resolve_max_instances() -- cap resolution precedence
# ---------------------------------------------------------------------------


def test_resolve_default_when_flag_and_env_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(engine_mod._MAX_INSTANCES_ENV_VAR, raising=False)
    assert engine_mod.resolve_max_instances(None) == 15


def test_resolve_env_var_used_when_flag_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(engine_mod._MAX_INSTANCES_ENV_VAR, "42")
    assert engine_mod.resolve_max_instances(None) == 42


def test_resolve_flag_wins_over_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine_mod._MAX_INSTANCES_ENV_VAR, "42")
    assert engine_mod.resolve_max_instances(7) == 7


def test_resolve_flag_zero_means_unlimited_and_wins_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(engine_mod._MAX_INSTANCES_ENV_VAR, "5")
    assert engine_mod.resolve_max_instances(0) == 0


def test_resolve_empty_env_var_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(engine_mod._MAX_INSTANCES_ENV_VAR, "")
    assert engine_mod.resolve_max_instances(None) == 15


def test_resolve_invalid_env_var_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine_mod._MAX_INSTANCES_ENV_VAR, "not-a-number")
    with pytest.raises(ValueError):
        engine_mod.resolve_max_instances(None)


# ---------------------------------------------------------------------------
# Section B: count_live_instances() -- reuses the `list` discovery mechanism
# ---------------------------------------------------------------------------


def test_count_live_instances_reuses_list_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import amplifier_bundle_digital_twin_universe.incus as incus_mod

    list_instances_mock = MagicMock(
        return_value=[{"name": "dtu-a"}, {"name": "dtu-b"}, {"name": "dtu-c"}]
    )
    monkeypatch.setattr(incus_mod, "list_instances", list_instances_mock)

    assert engine_mod.count_live_instances() == 3
    list_instances_mock.assert_called_once_with(
        engine_mod._MANAGED_BY_KEY, engine_mod._MANAGED_BY_VALUE
    )


def test_count_live_instances_zero_when_none_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import amplifier_bundle_digital_twin_universe.incus as incus_mod

    monkeypatch.setattr(incus_mod, "list_instances", MagicMock(return_value=[]))
    assert engine_mod.count_live_instances() == 0


# ---------------------------------------------------------------------------
# Section C: _enforce_max_instances() -- the guard itself
# ---------------------------------------------------------------------------


def test_enforce_raises_when_count_meets_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_mod, "count_live_instances", MagicMock(return_value=15))
    with pytest.raises(RuntimeError, match="15"):
        engine_mod._enforce_max_instances(15)


def test_enforce_raises_when_count_exceeds_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_mod, "count_live_instances", MagicMock(return_value=20))
    with pytest.raises(RuntimeError, match="20"):
        engine_mod._enforce_max_instances(15)


def test_enforce_error_names_current_count_cap_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_mod, "count_live_instances", MagicMock(return_value=16))
    with pytest.raises(RuntimeError) as exc_info:
        engine_mod._enforce_max_instances(15)
    message = str(exc_info.value)
    assert "16" in message
    assert "15" in message
    assert "--max-instances" in message
    assert engine_mod._MAX_INSTANCES_ENV_VAR in message


def test_enforce_does_not_raise_when_below_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_mod, "count_live_instances", MagicMock(return_value=3))
    engine_mod._enforce_max_instances(15)  # must not raise


def test_enforce_zero_disables_guard_regardless_of_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        engine_mod, "count_live_instances", MagicMock(return_value=999)
    )
    engine_mod._enforce_max_instances(0)  # must not raise


# ---------------------------------------------------------------------------
# Section D: engine.launch() -- end-to-end wiring, no container on refusal
# ---------------------------------------------------------------------------


def _write_profile(tmp_path: Path) -> str:
    profile_path = tmp_path / "test.yaml"
    profile_path.write_text(
        "name: minimal\n"
        "description: Minimal profile for guard tests\n"
        "base:\n"
        "  image: ubuntu:24.04\n"
    )
    return str(profile_path)


@pytest.fixture()
def _patch_launch_infra(monkeypatch: pytest.MonkeyPatch):
    """Patch all infrastructure calls in launch() so it can run without Incus."""
    import amplifier_bundle_digital_twin_universe.incus as incus_mod

    create_container_mock = MagicMock()

    monkeypatch.setattr(incus_mod, "check_incus", MagicMock())
    monkeypatch.setattr(incus_mod, "create_container", create_container_mock)
    monkeypatch.setattr(incus_mod, "set_config", MagicMock())
    monkeypatch.setattr(incus_mod, "file_push", MagicMock())
    monkeypatch.setattr(incus_mod, "exec_command", MagicMock(return_value=(0, "", "")))
    monkeypatch.setattr(
        incus_mod, "get_container_ip", MagicMock(return_value="10.0.0.42")
    )
    monkeypatch.setattr(incus_mod, "add_proxy_device", MagicMock())
    monkeypatch.setattr(
        incus_mod, "running_inside_incus_instance", MagicMock(return_value=None)
    )
    monkeypatch.setattr(
        engine_mod, "_wait_for_gateway", MagicMock(return_value="10.0.0.1")
    )
    return create_container_mock


def test_launch_refuses_before_creating_container_when_cap_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _patch_launch_infra: MagicMock,
) -> None:
    """launch() must raise -- and never call create_container -- when the
    live count already meets the cap."""
    monkeypatch.setattr(
        engine_mod, "count_live_instances", MagicMock(return_value=15)
    )
    profile_path = _write_profile(tmp_path)

    with pytest.raises(RuntimeError, match="Refusing to launch"):
        engine_mod.launch(profile_path, {}, name="dtu-cap-test", max_instances=15)

    _patch_launch_infra.assert_not_called()


def test_launch_succeeds_when_below_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _patch_launch_infra: MagicMock,
) -> None:
    monkeypatch.setattr(engine_mod, "count_live_instances", MagicMock(return_value=1))
    profile_path = _write_profile(tmp_path)

    result = engine_mod.launch(
        profile_path, {}, name="dtu-cap-test-2", max_instances=15
    )

    _patch_launch_infra.assert_called_once()
    assert result["status"] == "running"


def test_launch_max_instances_zero_bypasses_guard_even_with_many_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _patch_launch_infra: MagicMock,
) -> None:
    monkeypatch.setattr(
        engine_mod, "count_live_instances", MagicMock(return_value=500)
    )
    profile_path = _write_profile(tmp_path)

    result = engine_mod.launch(
        profile_path, {}, name="dtu-cap-test-3", max_instances=0
    )

    _patch_launch_infra.assert_called_once()
    assert result["status"] == "running"


def test_launch_default_cap_applies_when_flag_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _patch_launch_infra: MagicMock,
) -> None:
    """When the CLI flag is not passed (None), the default cap of 15 applies."""
    monkeypatch.delenv(engine_mod._MAX_INSTANCES_ENV_VAR, raising=False)
    monkeypatch.setattr(
        engine_mod, "count_live_instances", MagicMock(return_value=15)
    )
    profile_path = _write_profile(tmp_path)

    with pytest.raises(RuntimeError, match="Refusing to launch"):
        engine_mod.launch(profile_path, {}, name="dtu-cap-test-4")

    _patch_launch_infra.assert_not_called()
