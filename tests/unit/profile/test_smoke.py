# Copyright (c) Microsoft. All rights reserved.

"""Smoke tests for shipped DTU profiles.

Every YAML profile under ``profiles/`` must:

1. Parse without raising.
2. Produce zero ``UnknownProfileFieldWarning``.

This is the regression net that catches drift between the parser's
allowed-keys sets, the dataclasses, the documented schema, and the
actual profiles we ship.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from amplifier_bundle_digital_twin_universe.profile import (
    UnknownProfileFieldWarning,
    load_profile_from_content,
)

# Locate the repo's profiles/ directory by walking up from this file.
# tests/unit/profile/test_smoke.py -> repo root is parents[3]
_PROFILES_DIR = Path(__file__).resolve().parents[3] / "profiles"

# Plausible defaults for any ${VAR} placeholders shipped profiles reference at
# launch time. We don't actually launch anything here; we just want each
# profile to *parse* so we can assert the structural validation passes.
# Add new entries when a new profile is added that uses a new variable.
_PROFILE_VARS: dict[str, str] = {
    "GH_TOKEN": "stub-gh-token",
    "GITEA_URL": "http://gitea.invalid:10110",
    "GITEA_TOKEN": "stub-gitea-token",
    "PORT": "8080",
}


def _discover_profiles() -> list[Path]:
    if not _PROFILES_DIR.exists():
        return []
    return sorted(_PROFILES_DIR.rglob("*.yaml"))


_SHIPPED_PROFILES = _discover_profiles()


def test_profiles_directory_exists():
    """Sanity: the profiles directory must exist where we expect it."""
    assert _PROFILES_DIR.is_dir(), f"Expected shipped profiles under {_PROFILES_DIR}"


def test_at_least_one_profile_shipped():
    """Sanity: there must be at least one shipped profile to smoke-test."""
    assert _SHIPPED_PROFILES, (
        f"No *.yaml profiles found under {_PROFILES_DIR}; "
        "the smoke test would silently pass with zero coverage."
    )


@pytest.mark.parametrize(
    "profile_path",
    _SHIPPED_PROFILES,
    ids=[str(p.relative_to(_PROFILES_DIR)) for p in _SHIPPED_PROFILES],
)
def test_shipped_profile_parses_without_unknown_field_warnings(profile_path: Path):
    """Every shipped profile must parse cleanly with zero unknown-field warnings.

    Profiles use ``${VAR}`` placeholders which the parser leaves unresolved
    by default, so we pass an empty variable map and only check that the
    structure validates against the parser's known-keys sets.
    """
    yaml_text = profile_path.read_text()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_profile_from_content(yaml_text, _PROFILE_VARS, path=profile_path)

    unknowns = [
        str(record.message)
        for record in caught
        if issubclass(record.category, UnknownProfileFieldWarning)
    ]
    assert unknowns == [], (
        f"{profile_path.relative_to(_PROFILES_DIR)} produced unknown-field warnings:\n  - "
        + "\n  - ".join(unknowns)
    )
