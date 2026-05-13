# Copyright (c) Microsoft. All rights reserved.

"""Unit tests for the default base.config behavior.

Verifies that ``security.nesting=true`` is injected by default into every DTU
launch, and that profiles can override it (including setting it to ``"false"``)
via ``base.config``.

No Incus, Docker, or any real container runtime is required. All subprocess
calls are mocked.

Run with: uv run pytest tests/unit/test_default_base_config.py -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from amplifier_bundle_digital_twin_universe import engine as engine_mod


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_default_base_config_contains_security_nesting() -> None:
    """The module-level default declares security.nesting=true."""
    assert engine_mod.DEFAULT_BASE_CONFIG.get("security.nesting") == "true"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_profile(tmp_path: Path, body: str) -> str:
    profile_path = tmp_path / "test.yaml"
    profile_path.write_text(body)
    return str(profile_path)


@pytest.fixture()
def _patch_launch_infra(monkeypatch: pytest.MonkeyPatch):
    """Patch all infrastructure calls in launch() so we can call it without Incus.

    Yields the create_container MagicMock so tests can inspect its call args.
    """
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

    with patch(
        "amplifier_bundle_digital_twin_universe.hostname.HostnameManager"
    ) as mock_hm_cls:
        mock_hm_instance = MagicMock()
        mock_hm_instance.register.return_value = None
        mock_hm_cls.return_value = mock_hm_instance
        yield create_container_mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_launch_injects_default_security_nesting(
    tmp_path: Path,
    _patch_launch_infra,
) -> None:
    """A profile with no base.config still gets security.nesting=true."""
    profile_path = _write_profile(
        tmp_path,
        """\
name: minimal
description: Minimal profile with no base.config
base:
  image: ubuntu:24.04
""",
    )

    from amplifier_bundle_digital_twin_universe import engine

    result = engine.launch(profile_path, {}, name="dtu-default-001")

    _patch_launch_infra.assert_called_once()
    call_kwargs = _patch_launch_infra.call_args.kwargs
    config = call_kwargs.get("config")
    assert config is not None, "config should not be None when defaults apply"
    assert config.get("security.nesting") == "true"

    assert result["status"] == "running"


def test_launch_merges_profile_config_over_default(
    tmp_path: Path,
    _patch_launch_infra,
) -> None:
    """Profile-specified base.config keys are merged on top of the defaults.

    Default keys not overridden are preserved; profile-only keys are added.
    """
    profile_path = _write_profile(
        tmp_path,
        """\
name: merged
description: Profile adds a custom key without touching security.nesting
base:
  image: ubuntu:24.04
  config:
    security.privileged: "true"
""",
    )

    from amplifier_bundle_digital_twin_universe import engine

    engine.launch(profile_path, {}, name="dtu-merge-001")

    config = _patch_launch_infra.call_args.kwargs.get("config")
    assert config is not None
    # Default preserved
    assert config.get("security.nesting") == "true"
    # Profile-only key passed through
    assert config.get("security.privileged") == "true"


def test_launch_profile_can_override_security_nesting_to_false(
    tmp_path: Path,
    _patch_launch_infra,
) -> None:
    """A profile that explicitly sets security.nesting=false wins over the default."""
    profile_path = _write_profile(
        tmp_path,
        """\
name: opt-out
description: Profile explicitly opts out of nesting
base:
  image: ubuntu:24.04
  config:
    security.nesting: "false"
""",
    )

    from amplifier_bundle_digital_twin_universe import engine

    engine.launch(profile_path, {}, name="dtu-override-001")

    config = _patch_launch_infra.call_args.kwargs.get("config")
    assert config is not None
    assert config.get("security.nesting") == "false"


def test_launch_default_does_not_mutate_module_level_constant(
    tmp_path: Path,
    _patch_launch_infra,
) -> None:
    """Repeated launches with profile overrides must not mutate DEFAULT_BASE_CONFIG."""
    profile_path = _write_profile(
        tmp_path,
        """\
name: opt-out-2
description: Profile explicitly opts out of nesting
base:
  image: ubuntu:24.04
  config:
    security.nesting: "false"
    extra.key: "value"
""",
    )

    from amplifier_bundle_digital_twin_universe import engine

    engine.launch(profile_path, {}, name="dtu-noleak-001")

    # The module-level constant must still reflect only the defaults.
    assert engine_mod.DEFAULT_BASE_CONFIG == {"security.nesting": "true"}
