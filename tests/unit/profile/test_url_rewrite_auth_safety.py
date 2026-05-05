# Copyright (c) Microsoft. All rights reserved.

"""Auth-header leakage contract for ``url_rewrites``.

When ``url_rewrites.auth`` is configured, every matched request gets an
``Authorization: Basic <token>`` header injected by the in-container
mitmproxy addon. A ``prefix``-mode rule that over-matches a sibling repo
URL therefore leaks the rewrite credential to whatever target the over-match
selected.

This test pins both halves of the bug + fix:

* Section A pins the legacy behaviour (``prefix`` mode + auth on a bare
  ``org/repo`` rule WILL match a sibling URL, and the rule WILL have an
  auth header attached on the wire). This is the leakage path.
* Section B verifies that ``match_mode: boundary`` removes the match for
  the sibling URL, so the auth header is never sent to the wrong target.

The matcher contract is enforced in ``test_url_rewrite_matching.py`` and
``test_addon_matcher_parity.py``. This file exists to make the security
argument visible: ``match_mode: boundary`` matters even more when ``auth``
is configured. Why ``boundary`` is the right default for any rule with
``auth`` is documented in ``docs/profiles.md``.
"""

from __future__ import annotations

import ast
import textwrap

from amplifier_bundle_digital_twin_universe.engine import _generate_addon_script
from amplifier_bundle_digital_twin_universe.profile import (
    load_profile_from_content,
    match_url,
)


_GITHUB_TOKEN_VAR = "GITEA_TOKEN"
_PREFIX_PROFILE_BODY = textwrap.dedent("""
    name: leak-prefix
    base:
      image: ubuntu:24.04
    url_rewrites:
      auth:
        username: admin
        token_var: GITEA_TOKEN
      rules:
        - match: github.com/microsoft/amplifier
          target: ${GITEA_URL}/admin/amplifier
""")
_BOUNDARY_PROFILE_BODY = textwrap.dedent("""
    name: safe-boundary
    base:
      image: ubuntu:24.04
    url_rewrites:
      auth:
        username: admin
        token_var: GITEA_TOKEN
      rules:
        - match: github.com/microsoft/amplifier
          target: ${GITEA_URL}/admin/amplifier
          match_mode: boundary
""")
_VARIABLES = {
    "GITEA_URL": "https://gitea.example",
    "GITEA_TOKEN": "secret-token-do-not-leak",
}


def _extract_addon_rules(addon_source: str) -> list[dict]:
    """Parse ``RULES = [...]`` from the generated addon source."""
    tree = ast.parse(addon_source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "RULES"
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("addon source has no top-level RULES assignment")


# ---------------------------------------------------------------------------
# A. Legacy ``prefix`` + auth -- pinned leakage path
# ---------------------------------------------------------------------------


def test_prefix_mode_with_auth_matches_sibling_repo_url() -> None:
    """Pinned legacy bug: a ``prefix``-mode rule for ``org/repo`` with auth
    DOES match a sibling repo's URL via the host-side matcher. This is the
    decision that the in-container addon would make on the wire."""
    profile = load_profile_from_content(_PREFIX_PROFILE_BODY, _VARIABLES)
    assert profile.url_rewrites is not None
    matched = match_url(
        profile.url_rewrites.rules,
        host="github.com",
        path="/microsoft/amplifier-foundation.git",
    )
    assert matched is not None, (
        "prefix mode legacy bug: bare-prefix rule must match sibling repo "
        "URL (this is exactly why match_mode: boundary exists)"
    )
    assert matched.match == "github.com/microsoft/amplifier"


def test_prefix_mode_addon_rule_carries_auth_header() -> None:
    """The matched rule on the wire carries an ``auth_header`` value, so
    when the matcher fires on a sibling URL, the rewrite credential is
    attached to the request bound for the rewritten target. This is the
    leakage."""
    profile = load_profile_from_content(_PREFIX_PROFILE_BODY, _VARIABLES)
    addon = _generate_addon_script(profile, _VARIABLES)
    rules = _extract_addon_rules(addon)
    assert len(rules) == 1
    rule = rules[0]
    assert rule["match_mode"] == "prefix"
    assert rule["auth_header"], (
        "auth_header must be present so the addon attaches it on match. "
        "Combined with prefix-mode over-match, this is the credential leak."
    )
    # Sanity: the secret is base64-encoded into Basic auth.
    assert rule["auth_header"].startswith("Basic ")


# ---------------------------------------------------------------------------
# B. ``boundary`` mode + auth -- the fix
# ---------------------------------------------------------------------------


def test_boundary_mode_with_auth_does_not_match_sibling_repo_url() -> None:
    """``match_mode: boundary`` rejects sibling repo URLs. The addon will
    not call the matcher's rewrite branch, so no Authorization header is
    attached -- the credential cannot leak to a sibling target."""
    profile = load_profile_from_content(_BOUNDARY_PROFILE_BODY, _VARIABLES)
    assert profile.url_rewrites is not None
    matched = match_url(
        profile.url_rewrites.rules,
        host="github.com",
        path="/microsoft/amplifier-foundation.git",
    )
    assert matched is None, (
        "boundary mode must NOT match a sibling repo URL. If this fails, "
        "the auth header will be sent to the wrong target."
    )


def test_boundary_mode_still_matches_intended_repo_url() -> None:
    """Sanity: boundary mode does not break the legitimate match path.
    A request to the actual configured repo must still match and would
    receive the auth header (which is correct)."""
    profile = load_profile_from_content(_BOUNDARY_PROFILE_BODY, _VARIABLES)
    assert profile.url_rewrites is not None
    matched = match_url(
        profile.url_rewrites.rules,
        host="github.com",
        path="/microsoft/amplifier.git",
    )
    assert matched is not None
    assert matched.match == "github.com/microsoft/amplifier"


def test_boundary_mode_addon_rule_still_carries_auth_header() -> None:
    """The auth header is still emitted into the addon RULES under boundary
    mode (the fix is in the matcher, not in suppressing auth)."""
    profile = load_profile_from_content(_BOUNDARY_PROFILE_BODY, _VARIABLES)
    addon = _generate_addon_script(profile, _VARIABLES)
    rules = _extract_addon_rules(addon)
    assert len(rules) == 1
    rule = rules[0]
    assert rule["match_mode"] == "boundary"
    assert rule["auth_header"], "auth header must still be wired to legitimate matches"
