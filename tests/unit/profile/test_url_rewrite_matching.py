# Copyright (c) Microsoft. All rights reserved.

"""url_rewrites matcher contract.

Pins the matcher's current and proposed semantics:

* Section A -- current ``prefix`` behaviour (host==exact, path startswith,
  first-match-wins). Passes today; locks down backward compat.
* Section B -- longest-match-first ordering. Currently FAILS; turns green
  when the loader sorts rules by descending path-prefix length before
  emitting them.
* Section C -- new ``match_mode: boundary`` field with slash/.git/?/#
  boundary semantics. Currently FAILS; turns green when the dataclass gains
  the field, the loader validates it, and ``match_url`` honours it.
* Section D -- ``match_mode`` is recognised by the unknown-field validator.
* Section E -- ``OverlappingRewriteRulesWarning`` fires when two rules'
  path prefixes collide and neither uses ``match_mode: boundary``.
"""

from __future__ import annotations

import textwrap
import warnings

import pytest

from amplifier_bundle_digital_twin_universe.profile import (
    UrlRewriteRule,
    load_profile_from_content,
    match_url,
)


def _rules(*pairs: tuple[str, str]) -> list[UrlRewriteRule]:
    """Construct a rule list from (match, target) pairs."""
    return [UrlRewriteRule(match=m, target=t) for m, t in pairs]


# ---------------------------------------------------------------------------
# A. Current ``prefix`` behaviour -- backward compat, must stay green
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/microsoft/foo",
        "/microsoft/foo/",
        "/microsoft/foo.git",
        "/microsoft/foo.git/info/refs",
        "/microsoft/foo/info/refs?service=git-upload-pack",
    ],
)
def test_prefix_mode_matches_paths_with_prefix(path: str) -> None:
    """Default mode: host==exact + path.startswith. All these paths match."""
    rules = _rules(("github.com/microsoft/foo", "https://gitea.example/foo"))
    matched = match_url(rules, host="github.com", path=path)
    assert matched is not None
    assert matched.match == "github.com/microsoft/foo"


@pytest.mark.parametrize(
    "host,path",
    [
        ("gitlab.com", "/microsoft/foo"),  # different host
        ("github.com", "/other/foo"),  # different path root
        ("github.com", "/microsoft"),  # path is shorter than prefix
    ],
)
def test_prefix_mode_returns_none_for_non_matches(host: str, path: str) -> None:
    rules = _rules(("github.com/microsoft/foo", "https://gitea.example/foo"))
    assert match_url(rules, host=host, path=path) is None


def test_legacy_prefix_substring_collision_pinned() -> None:
    """The known prefix-collision footgun, pinned as the documented
    behaviour of ``match_mode: prefix``.

    A bare ``amplifier`` rule under ``match_mode: prefix`` (the default)
    captures ``amplifier-module-foo`` URLs. This is intentional: ``prefix``
    is a pure ``str.startswith`` match. Users who do not want this should
    set ``match_mode: boundary`` per rule or ``default_match_mode: boundary``
    on the block, both of which are covered in adjacent tests.
    """
    rules = _rules(
        ("github.com/microsoft/amplifier", "https://gitea.example/amplifier")
    )
    matched = match_url(
        rules, host="github.com", path="/microsoft/amplifier-module-foo.git"
    )
    assert matched is not None
    assert matched.match == "github.com/microsoft/amplifier"


# ---------------------------------------------------------------------------
# B. Longest-match-first ordering -- new, FAILS today
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule_order",
    [
        # Specific listed first -- already works under current first-match-wins
        [
            ("github.com/microsoft/amplifier-module-foo", "TARGET_SPECIFIC"),
            ("github.com/microsoft/amplifier", "TARGET_BARE"),
        ],
        # Specific listed LAST -- this is the headline new behaviour
        [
            ("github.com/microsoft/amplifier", "TARGET_BARE"),
            ("github.com/microsoft/amplifier-module-foo", "TARGET_SPECIFIC"),
        ],
        # Three rules with mixed ordering
        [
            ("github.com/microsoft/amplifier", "TARGET_BARE"),
            ("github.com/microsoft/amplifier-module-foo", "TARGET_SPECIFIC"),
            ("github.com/microsoft/amp", "TARGET_AMP"),
        ],
    ],
)
def test_longer_prefix_wins_regardless_of_declared_order(
    rule_order: list[tuple[str, str]],
) -> None:
    """A request to the more-specific repo URL must hit the more-specific
    rule, even when the bare/shorter rule is listed earlier."""
    rules = _rules(*rule_order)
    matched = match_url(
        rules, host="github.com", path="/microsoft/amplifier-module-foo.git"
    )
    assert matched is not None
    assert matched.target == "TARGET_SPECIFIC", (
        f"Expected the more-specific rule to win regardless of declared order. "
        f"Got rule={matched.match} target={matched.target}"
    )


def test_equal_length_prefixes_preserve_declared_order() -> None:
    """When two rules have the same prefix length, the first declared wins
    (stable sort)."""
    rules = _rules(
        ("github.com/microsoft/aaa", "FIRST"),
        ("github.com/microsoft/bbb", "SECOND"),
    )
    # Both are the same length; ordering should be preserved.
    matched = match_url(rules, host="github.com", path="/microsoft/aaa.git")
    assert matched is not None and matched.target == "FIRST"
    matched = match_url(rules, host="github.com", path="/microsoft/bbb.git")
    assert matched is not None and matched.target == "SECOND"


def test_longest_match_does_not_mutate_input_list() -> None:
    """The matcher must not reorder the user's ``rules`` list in place --
    callers may inspect it after a match."""
    rules = _rules(
        ("github.com/microsoft/amplifier", "BARE"),
        ("github.com/microsoft/amplifier-module-foo", "SPECIFIC"),
    )
    snapshot = [r.match for r in rules]
    match_url(rules, host="github.com", path="/microsoft/amplifier-module-foo.git")
    assert [r.match for r in rules] == snapshot


# ---------------------------------------------------------------------------
# C. ``match_mode: boundary`` -- new, FAILS today
# ---------------------------------------------------------------------------


def test_url_rewrite_rule_match_mode_field_defaults_to_prefix() -> None:
    """The dataclass gains an optional ``match_mode`` field. Default keeps
    today's behaviour."""
    rule = UrlRewriteRule(match="github.com/microsoft/foo", target="https://x")
    assert rule.match_mode == "prefix"


@pytest.mark.parametrize(
    "path",
    [
        "/microsoft/amplifier",  # exact
        "/microsoft/amplifier/",  # trailing slash
        "/microsoft/amplifier.git",  # .git suffix
        "/microsoft/amplifier.git/info/refs",  # smart-http
        "/microsoft/amplifier/info/refs",  # subpath
        "/microsoft/amplifier?service=git-upload-pack",  # query string
        "/microsoft/amplifier#anchor",  # fragment-like
    ],
)
def test_boundary_mode_matches_repo_url_variants(path: str) -> None:
    """``match_mode: boundary`` matches the repo at any of the boundary chars."""
    rules = [
        UrlRewriteRule(
            match="github.com/microsoft/amplifier",
            target="https://gitea.example/amplifier",
            match_mode="boundary",
        )
    ]
    matched = match_url(rules, host="github.com", path=path)
    assert matched is not None, f"boundary mode should match path={path!r}"


@pytest.mark.parametrize(
    "path",
    [
        "/microsoft/amplifier-module-foo",  # the bug we're fixing
        "/microsoft/amplifier-module-foo.git",
        "/microsoft/amplifier-foundation",
        "/microsoft/amplifier-foundation/info/refs",
        "/microsoft/amplifier_old",
        "/microsoft/amplifierx",
    ],
)
def test_boundary_mode_does_not_match_sibling_repos(path: str) -> None:
    """The headline bug fix: ``match_mode: boundary`` must NOT capture URLs that
    merely share a path prefix."""
    rules = [
        UrlRewriteRule(
            match="github.com/microsoft/amplifier",
            target="https://gitea.example/amplifier",
            match_mode="boundary",
        )
    ]
    matched = match_url(rules, host="github.com", path=path)
    assert matched is None, f"boundary mode must not match sibling path={path!r}"


def test_invalid_match_mode_raises_value_error() -> None:
    """Unknown ``match_mode`` values are rejected at load time."""
    body = textwrap.dedent("""
        name: bad
        base:
          image: ubuntu:24.04
        url_rewrites:
          rules:
            - match: github.com/microsoft/foo
              target: https://gitea.example/foo
              match_mode: bogus
    """)
    with pytest.raises(ValueError, match=r"match_mode"):
        load_profile_from_content(body, {})


# ---------------------------------------------------------------------------
# D. Schema -- ``match_mode`` is a recognised field (no unknown-field warning)
# ---------------------------------------------------------------------------


def test_match_mode_recognised_by_loader_no_unknown_field_warning() -> None:
    body = textwrap.dedent("""
        name: ok
        base:
          image: ubuntu:24.04
        url_rewrites:
          rules:
            - match: github.com/microsoft/foo
              target: https://gitea.example/foo
              match_mode: boundary
    """)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        profile = load_profile_from_content(body, {})

    from amplifier_bundle_digital_twin_universe.profile import (
        UnknownProfileFieldWarning,
    )

    unknown = [
        str(r.message)
        for r in caught
        if issubclass(r.category, UnknownProfileFieldWarning)
    ]
    assert unknown == [], f"unexpected unknown-field warnings: {unknown}"

    assert profile.url_rewrites is not None
    assert profile.url_rewrites.rules[0].match_mode == "boundary"


# ---------------------------------------------------------------------------
# E. Overlapping-prefix warning -- new, FAILS today
# ---------------------------------------------------------------------------


def _overlap_warnings(records) -> list[str]:
    from amplifier_bundle_digital_twin_universe.profile import (
        OverlappingRewriteRulesWarning,
    )

    return [
        str(r.message)
        for r in records
        if issubclass(r.category, OverlappingRewriteRulesWarning)
    ]


def test_overlapping_prefix_rules_emit_warning_naming_both_rules() -> None:
    """Two ``prefix``-mode rules where one is a path-prefix of the other
    should produce a warning that names both match strings."""
    body = textwrap.dedent("""
        name: overlap
        base:
          image: ubuntu:24.04
        url_rewrites:
          rules:
            - match: github.com/microsoft/amplifier
              target: https://gitea.example/amplifier
            - match: github.com/microsoft/amplifier-foundation
              target: https://gitea.example/amplifier-foundation
    """)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_profile_from_content(body, {})

    msgs = _overlap_warnings(caught)
    assert len(msgs) >= 1, "expected at least one overlap warning"
    joined = "|".join(msgs)
    assert "github.com/microsoft/amplifier" in joined
    assert "github.com/microsoft/amplifier-foundation" in joined


@pytest.mark.parametrize("boundary_rule_index", [0, 1])
def test_overlap_warning_suppressed_by_boundary_mode_on_either_rule(
    boundary_rule_index: int,
) -> None:
    """If either of the colliding rules opts in to ``match_mode: boundary``, the
    overlap is no longer ambiguous and the warning must NOT fire."""
    rules_yaml = [
        "    - match: github.com/microsoft/amplifier\n"
        "      target: https://gitea.example/amplifier",
        "    - match: github.com/microsoft/amplifier-foundation\n"
        "      target: https://gitea.example/amplifier-foundation",
    ]
    rules_yaml[boundary_rule_index] += "\n      match_mode: boundary"
    body = (
        textwrap.dedent("""\
        name: ok
        base:
          image: ubuntu:24.04
        url_rewrites:
          rules:
    """)
        + "\n".join(rules_yaml)
        + "\n"
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_profile_from_content(body, {})

    msgs = _overlap_warnings(caught)
    assert msgs == [], (
        f"overlap warning should be suppressed when rule[{boundary_rule_index}] "
        f"uses match_mode: boundary, got: {msgs}"
    )


def test_no_overlap_no_warning() -> None:
    """Disjoint prefixes must not warn."""
    body = textwrap.dedent("""
        name: disjoint
        base:
          image: ubuntu:24.04
        url_rewrites:
          rules:
            - match: github.com/microsoft/foo
              target: https://gitea.example/foo
            - match: github.com/microsoft/bar
              target: https://gitea.example/bar
    """)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_profile_from_content(body, {})
    assert _overlap_warnings(caught) == []


def test_overlap_warning_skipped_when_validate_false() -> None:
    """Snapshot replay (validate=False) must not re-emit overlap warnings."""
    body = textwrap.dedent("""
        name: overlap
        base:
          image: ubuntu:24.04
        url_rewrites:
          rules:
            - match: github.com/microsoft/amplifier
              target: https://gitea.example/amplifier
            - match: github.com/microsoft/amplifier-foundation
              target: https://gitea.example/amplifier-foundation
    """)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_profile_from_content(body, {}, validate=False)
    assert _overlap_warnings(caught) == []


# ---------------------------------------------------------------------------
# F. Block-level ``default_match_mode`` -- new in v0.2
# ---------------------------------------------------------------------------


def test_default_match_mode_boundary_inherited_by_unannotated_rules() -> None:
    """``default_match_mode: boundary`` makes every unannotated rule
    effectively boundary-mode."""
    body = textwrap.dedent("""
        name: defaults
        base:
          image: ubuntu:24.04
        url_rewrites:
          default_match_mode: boundary
          rules:
            - match: github.com/microsoft/foo
              target: https://gitea.example/foo
            - match: github.com/microsoft/bar
              target: https://gitea.example/bar
    """)
    profile = load_profile_from_content(body, {})
    assert profile.url_rewrites is not None
    assert profile.url_rewrites.default_match_mode == "boundary"
    for rule in profile.url_rewrites.rules:
        assert rule.match_mode == "boundary", (
            f"unannotated rule {rule.match!r} should inherit boundary mode"
        )


def test_per_rule_match_mode_overrides_default() -> None:
    """A rule's explicit ``match_mode`` always wins over the block default."""
    body = textwrap.dedent("""
        name: mixed
        base:
          image: ubuntu:24.04
        url_rewrites:
          default_match_mode: boundary
          rules:
            - match: github.com/microsoft/foo
              target: https://gitea.example/foo
            - match: github.com/microsoft/bar
              target: https://gitea.example/bar
              match_mode: prefix
    """)
    profile = load_profile_from_content(body, {})
    assert profile.url_rewrites is not None
    by_match = {r.match: r for r in profile.url_rewrites.rules}
    assert by_match["github.com/microsoft/foo"].match_mode == "boundary"
    assert by_match["github.com/microsoft/bar"].match_mode == "prefix"


def test_default_match_mode_omitted_preserves_legacy_prefix_default() -> None:
    """Backwards compat: omitting ``default_match_mode`` keeps the historic
    ``prefix`` default for unannotated rules."""
    body = textwrap.dedent("""
        name: legacy
        base:
          image: ubuntu:24.04
        url_rewrites:
          rules:
            - match: github.com/microsoft/foo
              target: https://gitea.example/foo
    """)
    profile = load_profile_from_content(body, {})
    assert profile.url_rewrites is not None
    assert profile.url_rewrites.default_match_mode == "prefix"
    assert profile.url_rewrites.rules[0].match_mode == "prefix"


def test_invalid_default_match_mode_raises_value_error() -> None:
    """Unknown ``default_match_mode`` values are rejected at load time."""
    body = textwrap.dedent("""
        name: bad-default
        base:
          image: ubuntu:24.04
        url_rewrites:
          default_match_mode: bogus
          rules:
            - match: github.com/microsoft/foo
              target: https://gitea.example/foo
    """)
    with pytest.raises(ValueError, match=r"default_match_mode"):
        load_profile_from_content(body, {})


def test_default_match_mode_recognised_no_unknown_field_warning() -> None:
    """Schema must recognise ``default_match_mode`` so unknown-field
    validation does not produce noise."""
    from amplifier_bundle_digital_twin_universe.profile import (
        UnknownProfileFieldWarning,
    )

    body = textwrap.dedent("""
        name: ok
        base:
          image: ubuntu:24.04
        url_rewrites:
          default_match_mode: boundary
          rules:
            - match: github.com/microsoft/foo
              target: https://gitea.example/foo
    """)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_profile_from_content(body, {})
    unknown = [
        str(r.message)
        for r in caught
        if issubclass(r.category, UnknownProfileFieldWarning)
    ]
    assert unknown == [], f"unexpected unknown-field warnings: {unknown}"


def test_default_match_mode_boundary_blocks_sibling_overmatch_end_to_end() -> None:
    """Round-trip: a profile with ``default_match_mode: boundary`` and a
    bare-prefix-shaped rule must NOT capture sibling repos via ``match_url``."""
    body = textwrap.dedent("""
        name: e2e-default
        base:
          image: ubuntu:24.04
        url_rewrites:
          default_match_mode: boundary
          rules:
            - match: github.com/microsoft/amplifier
              target: https://gitea.example/amplifier
    """)
    profile = load_profile_from_content(body, {})
    assert profile.url_rewrites is not None
    matched = match_url(
        profile.url_rewrites.rules,
        host="github.com",
        path="/microsoft/amplifier-foundation.git",
    )
    assert matched is None, (
        "default_match_mode: boundary must scope rules; sibling repo URLs "
        "must NOT match a bare-prefix rule"
    )


# ---------------------------------------------------------------------------
# G. Single-rule SuspiciousPrefixRuleWarning -- new in v0.2
# ---------------------------------------------------------------------------


def _suspicious_warnings(records) -> list[str]:
    from amplifier_bundle_digital_twin_universe.profile import (
        SuspiciousPrefixRuleWarning,
    )

    return [
        str(r.message)
        for r in records
        if issubclass(r.category, SuspiciousPrefixRuleWarning)
    ]


def test_suspicious_prefix_warning_fires_for_org_repo_shape() -> None:
    """A single ``prefix``-mode rule with the ``/org/repo`` shape must
    produce a warning that names the rule."""
    body = textwrap.dedent("""
        name: footgun
        base:
          image: ubuntu:24.04
        url_rewrites:
          rules:
            - match: github.com/microsoft/amplifier
              target: https://gitea.example/amplifier
    """)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_profile_from_content(body, {})
    msgs = _suspicious_warnings(caught)
    assert len(msgs) == 1, f"expected exactly one warning, got: {msgs}"
    assert "github.com/microsoft/amplifier" in msgs[0]
    assert "match_mode: boundary" in msgs[0]


def test_suspicious_prefix_warning_suppressed_by_per_rule_boundary() -> None:
    """Per-rule ``match_mode: boundary`` suppresses the warning."""
    body = textwrap.dedent("""
        name: ok
        base:
          image: ubuntu:24.04
        url_rewrites:
          rules:
            - match: github.com/microsoft/amplifier
              target: https://gitea.example/amplifier
              match_mode: boundary
    """)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_profile_from_content(body, {})
    assert _suspicious_warnings(caught) == []


def test_suspicious_prefix_warning_suppressed_by_default_match_mode_boundary() -> None:
    """Block-level ``default_match_mode: boundary`` propagates to unannotated
    rules and suppresses the warning."""
    body = textwrap.dedent("""
        name: ok
        base:
          image: ubuntu:24.04
        url_rewrites:
          default_match_mode: boundary
          rules:
            - match: github.com/microsoft/amplifier
              target: https://gitea.example/amplifier
    """)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_profile_from_content(body, {})
    assert _suspicious_warnings(caught) == []


@pytest.mark.parametrize(
    "match_string",
    [
        "github.com/microsoft",  # one segment -- whole org legitimately
        "github.com/microsoft/foo/bar",  # three segments -- subpath
        "github.com/microsoft/foo/",  # trailing slash already
        "github.com",  # bare host -- root prefix
        "github.com/",  # bare host with slash
    ],
)
def test_suspicious_prefix_warning_does_not_fire_for_non_repo_shapes(
    match_string: str,
) -> None:
    """Shapes that are not ``/org/repo`` must NOT trigger the warning."""
    body = textwrap.dedent(f"""
        name: ok
        base:
          image: ubuntu:24.04
        url_rewrites:
          rules:
            - match: {match_string}
              target: https://gitea.example/x
    """)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_profile_from_content(body, {})
    msgs = _suspicious_warnings(caught)
    assert msgs == [], f"non-repo shape {match_string!r} should not warn: {msgs}"


def test_suspicious_prefix_warning_skipped_when_validate_false() -> None:
    """Snapshot replay (validate=False) must not re-emit the warning."""
    body = textwrap.dedent("""
        name: footgun
        base:
          image: ubuntu:24.04
        url_rewrites:
          rules:
            - match: github.com/microsoft/amplifier
              target: https://gitea.example/amplifier
    """)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_profile_from_content(body, {}, validate=False)
    assert _suspicious_warnings(caught) == []


def test_suspicious_prefix_warning_fires_per_rule_not_once() -> None:
    """Two suspicious rules → two warnings (one per rule, distinguishing
    which rule needs attention)."""
    body = textwrap.dedent("""
        name: two-footguns
        base:
          image: ubuntu:24.04
        url_rewrites:
          rules:
            - match: github.com/microsoft/foo
              target: https://gitea.example/foo
            - match: github.com/microsoft/bar
              target: https://gitea.example/bar
    """)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_profile_from_content(body, {})
    msgs = _suspicious_warnings(caught)
    assert len(msgs) == 2
    assert any("microsoft/foo" in m for m in msgs)
    assert any("microsoft/bar" in m for m in msgs)
