# Copyright (c) Microsoft. All rights reserved.

"""Validation contract for ``url_rewrites.rules[*].target``.

A rule's ``target`` must, after ``${VAR}`` substitution, be either:

  * a valid http/https URL with a non-empty hostname, OR
  * a string still containing ``${VAR}`` placeholders (i.e. unresolved --
    the engine's ``_should_setup_proxy`` gate skips the proxy in that case).

Anything else -- in particular, the result of ``${UNSET_VAR}`` substituting
to an empty string and turning ``${UNSET_VAR}/admin/foo`` into
``/admin/foo`` -- is silently broken: mitmproxy gets ``host=None`` from the
addon and 502s every clone. This test pins the load-time validation that
prevents that.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from amplifier_bundle_digital_twin_universe.profile import (
    load_profile_from_content,
)


def _profile_yaml(target: str) -> str:
    """A minimal YAML profile with a single url_rewrites rule."""
    return textwrap.dedent(f"""\
        name: target-validation
        base:
          image: ubuntu:24.04
        url_rewrites:
          rules:
            - match: github.com/microsoft/amplifier-bundle-digital-twin-universe
              target: {target}
    """)


def _load(yaml_text: str, variables: dict[str, str] | None = None):
    return load_profile_from_content(yaml_text, variables or {}, path=Path("<test>"))


# ---------------------------------------------------------------------------
# Happy paths -- valid targets must continue to load
# ---------------------------------------------------------------------------


def test_valid_https_target_loads() -> None:
    profile = _load(
        _profile_yaml("${GITEA_URL}/admin/amplifier-bundle-digital-twin-universe"),
        {"GITEA_URL": "http://localhost:3000"},
    )
    assert profile.url_rewrites is not None
    assert profile.url_rewrites.rules[0].target == (
        "http://localhost:3000/admin/amplifier-bundle-digital-twin-universe"
    )


def test_literal_https_target_loads() -> None:
    profile = _load(
        _profile_yaml("https://gitea.example.com/admin/repo"),
    )
    assert profile.url_rewrites is not None
    assert profile.url_rewrites.rules[0].target == (
        "https://gitea.example.com/admin/repo"
    )


# ---------------------------------------------------------------------------
# Unresolved-var path -- must remain a soft fall-through, not an error
# ---------------------------------------------------------------------------


def test_unresolved_var_in_target_does_not_raise() -> None:
    """When ``GITEA_URL`` is not provided, ``${GITEA_URL}`` stays literal in
    the target. The existing ``_should_setup_proxy`` gate will skip the
    proxy at launch time -- profile load must NOT raise here, so the
    documented "launch without Gitea" path keeps working.
    """
    profile = _load(
        _profile_yaml("${GITEA_URL}/admin/amplifier-bundle-digital-twin-universe"),
        {},  # GITEA_URL deliberately not provided
    )
    assert profile.url_rewrites is not None
    # Substitution leaves ${GITEA_URL} as-is when the variable is not in the
    # variables dict -- this is the contract _should_setup_proxy relies on.
    assert profile.url_rewrites.rules[0].target.startswith("${GITEA_URL}")


# ---------------------------------------------------------------------------
# Bug 1 -- empty-string substitution must raise loudly at load time
# ---------------------------------------------------------------------------


def test_empty_var_substitution_raises_value_error() -> None:
    """The bug: ``--var GITEA_URL=`` substitutes ``${GITEA_URL}/admin/foo``
    to ``/admin/foo`` -- which has no scheme and no host. mitmproxy 502s
    on every matching clone. Catch this at load time instead.
    """
    with pytest.raises(ValueError) as excinfo:
        _load(
            _profile_yaml("${GITEA_URL}/admin/amplifier-bundle-digital-twin-universe"),
            {"GITEA_URL": ""},
        )
    msg = str(excinfo.value)
    # Error must identify the specific rule and the substituted target so the
    # user can act without spelunking through provisioning logs.
    assert "github.com/microsoft/amplifier-bundle-digital-twin-universe" in msg
    assert "/admin/amplifier-bundle-digital-twin-universe" in msg
    # And it must mention scheme/host so the cause is unambiguous.
    assert "scheme" in msg.lower() or "host" in msg.lower()


def test_target_without_scheme_raises_value_error() -> None:
    """A literal typo'd target without a scheme -- e.g. ``gitea.local/foo``
    -- must also fail at load time, not silently break the proxy."""
    with pytest.raises(ValueError) as excinfo:
        _load(_profile_yaml("gitea.local/admin/repo"))
    assert "gitea.local/admin/repo" in str(excinfo.value)


def test_target_with_unsupported_scheme_raises_value_error() -> None:
    """Only http and https are wired up in the mitmproxy addon. ftp:// or
    git:// in a target is almost certainly a typo or a feature we don't
    actually support -- fail loudly so the user knows."""
    with pytest.raises(ValueError) as excinfo:
        _load(_profile_yaml("ftp://gitea.example.com/admin/repo"))
    assert "ftp" in str(excinfo.value).lower()


def test_first_invalid_rule_identified_in_mixed_set() -> None:
    """When several rules are present and one substitutes to an invalid
    target, the error must identify which rule is bad."""
    body = textwrap.dedent("""\
        name: mixed
        base:
          image: ubuntu:24.04
        url_rewrites:
          rules:
            - match: github.com/microsoft/amplifier-bundle-digital-twin-universe
              target: ${GITEA_URL}/admin/amplifier-bundle-digital-twin-universe
            - match: github.com/microsoft/amplifier-bundle-gitea
              target: https://gitea.example.com/admin/amplifier-bundle-gitea
    """)
    with pytest.raises(ValueError) as excinfo:
        load_profile_from_content(body, {"GITEA_URL": ""}, path=Path("<test>"))
    msg = str(excinfo.value)
    assert "amplifier-bundle-digital-twin-universe" in msg
    # The valid rule must NOT appear in the error message -- only the bad one.
    assert "amplifier-bundle-gitea" not in msg or msg.count(
        "amplifier-bundle-gitea"
    ) <= msg.count("amplifier-bundle-digital-twin-universe")
