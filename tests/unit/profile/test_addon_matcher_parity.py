# Copyright (c) Microsoft. All rights reserved.

"""Parity contract: host-side ``match_url`` and the in-container mitmproxy
addon must agree on every rule decision.

The two callers used to inline their own copies of the matching logic. They
have been collapsed: ``engine._generate_addon_script`` injects the source of
``profile._path_matches`` (and ``_PATH_BOUNDARY_CHARS``) into the addon
template via ``inspect.getsource``. This test verifies that:

1. The injected source survives ``.format()`` substitution and parses as
   valid Python (no curly-brace collisions, no indentation drift).
2. For a comprehensive parameter table, the addon's extracted matcher
   returns the same boolean as the host's ``_path_matches``.
3. The boundary char set in the addon equals the host module's value
   (catches a subtle drift where the constant could be hand-edited in one
   place but not the other).

If this test goes red, host-side and in-container matching have drifted.
That is the bug that motivated the collapse.
"""

from __future__ import annotations

import textwrap

import pytest

from amplifier_bundle_digital_twin_universe.engine import _generate_addon_script
from amplifier_bundle_digital_twin_universe.profile import (
    _PATH_BOUNDARY_CHARS,
    _path_matches,
    load_profile_from_content,
)


_MATCHER_BEGIN = "# ---- canonical matcher source"
_MATCHER_END = "# ---- end canonical matcher source"


def _extract_addon_matcher(addon_source: str) -> dict:
    """Extract the matcher namespace from a generated addon source.

    Locates the canonical matcher block between the BEGIN/END markers and
    executes only that block in a fresh namespace. The rest of the addon
    (which imports mitmproxy and would fail on the host) is ignored.

    Returns the namespace dict containing ``_PATH_BOUNDARY_CHARS`` and
    ``_path_matches``.
    """
    begin = addon_source.find(_MATCHER_BEGIN)
    end = addon_source.find(_MATCHER_END)
    assert begin != -1, "addon source missing matcher BEGIN marker"
    assert end != -1, "addon source missing matcher END marker"
    block = addon_source[begin:end]
    ns: dict = {}
    exec(compile(block, "<addon-matcher>", "exec"), ns)
    assert "_PATH_BOUNDARY_CHARS" in ns, "matcher block did not define constant"
    assert "_path_matches" in ns, "matcher block did not define function"
    return ns


def _addon_for_minimal_profile() -> str:
    """Generate an addon source for a minimal profile (one rule, no auth).

    The exact rule shape doesn't matter here -- this test cares only about
    the matcher block, which is the same for every profile.
    """
    profile = load_profile_from_content(
        textwrap.dedent("""
            name: parity
            base:
              image: ubuntu:24.04
            url_rewrites:
              rules:
                - match: github.com/microsoft/foo
                  target: https://gitea.example/foo
        """),
        {},
    )
    return _generate_addon_script(profile, {})


# ---------------------------------------------------------------------------
# 1. Structural integrity
# ---------------------------------------------------------------------------


def test_matcher_block_present_and_parseable() -> None:
    """The injected matcher block must be syntactically valid Python."""
    addon = _addon_for_minimal_profile()
    ns = _extract_addon_matcher(addon)
    # Constant and function present and callable
    assert ns["_PATH_BOUNDARY_CHARS"] == _PATH_BOUNDARY_CHARS
    assert callable(ns["_path_matches"])


def test_addon_boundary_chars_match_host() -> None:
    """The boundary char set in the addon must equal the host's. If a
    future maintainer edits one without the other, this catches the drift."""
    addon = _addon_for_minimal_profile()
    ns = _extract_addon_matcher(addon)
    assert ns["_PATH_BOUNDARY_CHARS"] == _PATH_BOUNDARY_CHARS, (
        f"addon boundary chars {ns['_PATH_BOUNDARY_CHARS']!r} drifted from "
        f"host {_PATH_BOUNDARY_CHARS!r}"
    )


# ---------------------------------------------------------------------------
# 2. Behavioural parity over a comprehensive parameter table
# ---------------------------------------------------------------------------


_PARITY_CASES = [
    # (mode, prefix, path)
    # --- prefix mode ---
    ("prefix", "/microsoft/foo", "/microsoft/foo"),
    ("prefix", "/microsoft/foo", "/microsoft/foo.git"),
    ("prefix", "/microsoft/foo", "/microsoft/foo/info/refs"),
    ("prefix", "/microsoft/foo", "/microsoft/foo-extra"),  # the over-match case
    ("prefix", "/microsoft/foo", "/microsoft/foobar"),  # over-match
    ("prefix", "/microsoft/foo", "/other/path"),  # non-match
    ("prefix", "/microsoft/foo", "/microsoft"),  # path shorter than prefix
    ("prefix", "/", "/anything"),  # bare-host prefix
    # --- boundary mode ---
    ("boundary", "/microsoft/foo", "/microsoft/foo"),  # exact
    ("boundary", "/microsoft/foo", "/microsoft/foo/"),  # / boundary
    ("boundary", "/microsoft/foo", "/microsoft/foo.git"),  # . boundary
    ("boundary", "/microsoft/foo", "/microsoft/foo.git/info/refs"),
    ("boundary", "/microsoft/foo", "/microsoft/foo?service=git-upload-pack"),  # ?
    ("boundary", "/microsoft/foo", "/microsoft/foo#frag"),  # #
    ("boundary", "/microsoft/foo", "/microsoft/foo-extra"),  # NOT a match
    ("boundary", "/microsoft/foo", "/microsoft/foobar"),  # NOT a match
    ("boundary", "/microsoft/foo", "/microsoft/foo_old"),  # NOT a match
    ("boundary", "/microsoft/foo", "/other/path"),  # NOT a match
    # --- edge: empty path, root prefix ---
    ("boundary", "/", ""),
    ("boundary", "/", "/anything"),
]


@pytest.mark.parametrize("mode,prefix,path", _PARITY_CASES)
def test_addon_matcher_agrees_with_host_matcher(
    mode: str, prefix: str, path: str
) -> None:
    """For every (mode, prefix, path) tuple, addon and host must agree."""
    addon = _addon_for_minimal_profile()
    ns = _extract_addon_matcher(addon)
    addon_match = ns["_path_matches"](mode, prefix, path)
    host_match = _path_matches(mode, prefix, path)
    assert addon_match == host_match, (
        f"matcher drift for mode={mode!r} prefix={prefix!r} path={path!r}: "
        f"addon={addon_match} host={host_match}"
    )


# ---------------------------------------------------------------------------
# 3. Source-level identity (strongest guarantee, lowest cost)
# ---------------------------------------------------------------------------


def test_addon_function_source_is_identical_to_host() -> None:
    """The function body in the addon must be byte-identical to the host's
    ``inspect.getsource(_path_matches)``. This catches not just behavioural
    drift but any textual edit that bypasses the inspect-injection path."""
    import inspect

    addon = _addon_for_minimal_profile()
    host_source = inspect.getsource(_path_matches)
    assert host_source in addon, (
        "addon does not contain the host's _path_matches source verbatim. "
        "The collapse-via-inspect contract has been violated."
    )
