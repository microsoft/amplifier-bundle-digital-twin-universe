# Copyright (c) Microsoft. All rights reserved.

"""Unknown-field warning tests for the profile parser.

The parser is currently warn-only: unknown fields produce
``UnknownProfileFieldWarning`` and are silently dropped. A future release
will turn these into hard errors. These tests pin the warn-only contract
and the warning-message format.
"""

from __future__ import annotations

import textwrap
import warnings

import pytest

from amplifier_bundle_digital_twin_universe.profile import (
    UnknownProfileFieldWarning,
    _check_unknown,
    load_profile_from_content,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MIN_PROFILE = "name: sample\nbase:\n  image: ubuntu:24.04\n"


def _load(yaml_text: str, variables: dict[str, str] | None = None):
    """Convenience: load a profile from a YAML string with empty vars by default."""
    return load_profile_from_content(textwrap.dedent(yaml_text), variables or {})


def _unknown_warnings(records) -> list[str]:
    """Return only ``UnknownProfileFieldWarning`` messages from a recwarn list."""
    return [
        str(r.message)
        for r in records
        if issubclass(r.category, UnknownProfileFieldWarning)
    ]


# ---------------------------------------------------------------------------
# _check_unknown helper-level tests
# ---------------------------------------------------------------------------


def test_check_unknown_no_unknowns_no_warning():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _check_unknown({"a": 1, "b": 2}, {"a", "b"}, "where")
    assert _unknown_warnings(w) == []


def test_check_unknown_single_unknown_one_warning():
    with pytest.warns(UnknownProfileFieldWarning) as w:
        _check_unknown({"a": 1, "x": 2}, {"a", "b"}, "where")
    msgs = _unknown_warnings(w)
    assert len(msgs) == 1
    assert "where" in msgs[0]
    assert "'x'" in msgs[0]


def test_check_unknown_multiple_unknowns_each_warned():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _check_unknown({"a": 1, "x": 2, "y": 3, "z": 4}, {"a", "b"}, "where")
    msgs = _unknown_warnings(w)
    assert len(msgs) == 3
    # All three keys should each appear in their own warning.
    joined = "|".join(msgs)
    assert "'x'" in joined
    assert "'y'" in joined
    assert "'z'" in joined


def test_check_unknown_close_match_includes_suggestion():
    with pytest.warns(UnknownProfileFieldWarning, match=r"did you mean 'description'"):
        _check_unknown(
            {"descriptiom": "x"},
            {"name", "description", "base"},
            "profile",
        )


def test_check_unknown_no_close_match_omits_suggestion():
    with pytest.warns(UnknownProfileFieldWarning) as w:
        _check_unknown(
            {"xyzzy": "x"},
            {"name", "description", "base"},
            "profile",
        )
    msgs = _unknown_warnings(w)
    assert len(msgs) == 1
    assert "did you mean" not in msgs[0]


def test_check_unknown_empty_allowed_set_warns_every_key():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _check_unknown({"a": 1, "b": 2}, set(), "where")
    assert len(_unknown_warnings(w)) == 2


def test_check_unknown_none_data_is_noop():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _check_unknown(None, {"a"}, "where")
    assert _unknown_warnings(w) == []


def test_check_unknown_non_mapping_data_is_noop():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _check_unknown([1, 2, 3], {"a"}, "where")  # type: ignore[arg-type]
    assert _unknown_warnings(w) == []


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


def test_unknown_top_level_field_warns():
    body = """
        name: sample
        descriptiom: oops
        base:
          image: ubuntu:24.04
    """
    with pytest.warns(
        UnknownProfileFieldWarning, match=r"profile: unknown field 'descriptiom'"
    ):
        _load(body)


def test_unknown_top_level_section_warns_with_suggestion():
    """`mock_service` (singular) should suggest `mock_services`."""
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        mock_service:
          - source: x
    """
    with pytest.warns(
        UnknownProfileFieldWarning, match=r"did you mean 'mock_services'"
    ):
        _load(body)


def test_multiple_unknown_top_level_fields_all_warn():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        foo: 1
        bar: 2
    """
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _load(body)
    msgs = _unknown_warnings(w)
    joined = "|".join(msgs)
    assert "'foo'" in joined
    assert "'bar'" in joined


# ---------------------------------------------------------------------------
# Nested objects
# ---------------------------------------------------------------------------


def test_unknown_field_in_base_warns():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
          iamge: typo
    """
    with pytest.warns(UnknownProfileFieldWarning, match=r"base: unknown field 'iamge'"):
        _load(body)


def test_unknown_field_in_url_rewrites_warns():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        url_rewrites:
          rules: []
          allow_uv_giftubb_fast_path: true
    """
    with pytest.warns(UnknownProfileFieldWarning, match=r"url_rewrites: unknown field"):
        _load(body)


def test_unknown_field_in_url_rewrites_auth_warns():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        url_rewrites:
          auth:
            username: admin
            token_var: TOK
            extra: bad
          rules: []
    """
    with pytest.warns(
        UnknownProfileFieldWarning, match=r"url_rewrites\.auth: unknown field 'extra'"
    ):
        _load(body)


def test_unknown_field_in_passthrough_warns():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        passthrough:
          allow_external: true
          services: []
          unknown_thing: 1
    """
    with pytest.warns(
        UnknownProfileFieldWarning, match=r"passthrough: unknown field 'unknown_thing'"
    ):
        _load(body)


def test_unknown_field_in_provision_warns():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        provision:
          files: []
          setup_cmds: []
          run_after: ['echo hi']
    """
    with pytest.warns(
        UnknownProfileFieldWarning, match=r"provision: unknown field 'run_after'"
    ):
        _load(body)


def test_unknown_field_in_update_warns():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        update:
          cmds: []
          refresh_pypi: false
          parallelism: 4
    """
    with pytest.warns(
        UnknownProfileFieldWarning, match=r"update: unknown field 'parallelism'"
    ):
        _load(body)


def test_unknown_field_in_pypi_overrides_warns():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        pypi_overrides:
          packages: []
          mode: bogus
    """
    with pytest.warns(
        UnknownProfileFieldWarning, match=r"pypi_overrides: unknown field 'mode'"
    ):
        _load(body)


def test_unknown_field_in_access_warns():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        access:
          ports: []
          hostname: foo.local
          forwarding: enabled
    """
    with pytest.warns(
        UnknownProfileFieldWarning, match=r"access: unknown field 'forwarding'"
    ):
        _load(body)


# ---------------------------------------------------------------------------
# List items
# ---------------------------------------------------------------------------


def test_unknown_field_in_url_rewrite_rule_warns_with_index():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        url_rewrites:
          rules:
            - match: a
              target: b
            - match: c
              target: d
              priority: 1
    """
    with pytest.warns(
        UnknownProfileFieldWarning,
        match=r"url_rewrites\.rules\[1\]: unknown field 'priority'",
    ):
        _load(body)


def test_unknown_field_in_passthrough_service_warns():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        passthrough:
          services:
            - name: anthropic
              key_env: ANTHROPIC_API_KEY
              extra: 1
    """
    with pytest.warns(
        UnknownProfileFieldWarning,
        match=r"passthrough\.services\[0\]: unknown field 'extra'",
    ):
        _load(body)


def test_unknown_field_in_provision_file_warns_with_suggestion():
    """`recursiv` should suggest `recursive`."""
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        provision:
          files:
            - src: a
              dest: /b
              recursiv: true
    """
    with pytest.warns(UnknownProfileFieldWarning, match=r"did you mean 'recursive'"):
        _load(body)


def test_unknown_field_in_pypi_override_package_warns():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        pypi_overrides:
          packages:
            - name: mypkg
              wheel_path: /tmp/x.whl
              checksum: deadbeef
    """
    with pytest.warns(
        UnknownProfileFieldWarning,
        match=r"pypi_overrides\.packages\[0\]: unknown field 'checksum'",
    ):
        _load(body)


def test_unknown_field_in_access_port_warns():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        access:
          ports:
            - host: 8410
              container: 8410
              protocol: tcp
    """
    with pytest.warns(
        UnknownProfileFieldWarning,
        match=r"access\.ports\[0\]: unknown field 'protocol'",
    ):
        _load(body)


def test_unknown_field_in_readiness_check_warns():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        readiness:
          - name: web
            http:
              url: http://x/
            timeout: 10
    """
    with pytest.warns(
        UnknownProfileFieldWarning, match=r"readiness\[0\]: unknown field 'timeout'"
    ):
        _load(body)


def test_unknown_field_in_mock_service_warns():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        mock_services:
          - source: ./fake
            kind: slack
    """
    with pytest.warns(
        UnknownProfileFieldWarning, match=r"mock_services\[0\]: unknown field 'kind'"
    ):
        _load(body)


# ---------------------------------------------------------------------------
# Compound paths
# ---------------------------------------------------------------------------


def test_unknown_field_in_wheel_from_git_warns():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        pypi_overrides:
          packages:
            - name: mypkg
              wheel_from_git:
                repo: https://example.com/x.git
                branch: dev
    """
    with pytest.warns(
        UnknownProfileFieldWarning,
        match=r"pypi_overrides\.packages\[0\]\.wheel_from_git: unknown field 'branch'",
    ):
        _load(body)


def test_unknown_field_in_readiness_http_warns():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        readiness:
          - name: web
            http:
              url: http://x/
              method: POST
    """
    with pytest.warns(
        UnknownProfileFieldWarning,
        match=r"readiness\[0\]\.http: unknown field 'method'",
    ):
        _load(body)


def test_unknown_field_in_readiness_tcp_warns():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        readiness:
          - name: db
            tcp:
              port: 5432
              host: 127.0.0.1
    """
    with pytest.warns(
        UnknownProfileFieldWarning, match=r"readiness\[0\]\.tcp: unknown field 'host'"
    ):
        _load(body)


# ---------------------------------------------------------------------------
# Pass-through preservation -- inner config maps are NOT validated
# ---------------------------------------------------------------------------


def test_arbitrary_keys_in_base_config_no_warning():
    """`base.config` is a pass-through Incus config map -- inner keys must not warn."""
    body = """
        name: sample
        base:
          image: ubuntu:24.04
          config:
            security.privileged: 'true'
            security.nesting: 'true'
            limits.cpu: '4'
    """
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _load(body)
    assert _unknown_warnings(w) == []


def test_arbitrary_keys_in_mock_service_config_no_warning():
    """`mock_services[].config` is a pass-through env-var map -- inner keys must not warn."""
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        mock_services:
          - source: ./fake
            config:
              FAKE_DOMAIN: example.test
              FAKE_LATENCY_MS: '50'
              ARBITRARY_THING: 'whatever'
    """
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _load(body)
    assert _unknown_warnings(w) == []


# ---------------------------------------------------------------------------
# Behavior preservation -- warn != error
# ---------------------------------------------------------------------------


def test_profile_with_unknown_field_still_parses():
    """Warn-only contract: unknown fields produce a Profile, not an exception."""
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        descriptiom: typo
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = _load(body)
    assert result.name == "sample"
    assert result.base.image == "ubuntu:24.04"


def test_existing_required_field_error_unchanged():
    body = "name: sample\nbase: {}\n"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ValueError, match=r"base\.image"):
            _load(body)


def test_existing_pypi_overrides_exclusivity_error_unchanged():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        pypi_overrides:
          packages:
            - name: mypkg
              wheel_path: /tmp/x.whl
              wheel_var: WHEEL_VAR
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ValueError, match=r"exactly one of"):
            _load(body)


def test_existing_readiness_xor_error_unchanged():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        readiness:
          - name: web
            http:
              url: http://x/
            tcp:
              port: 80
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ValueError, match=r"exactly one of"):
            _load(body)


# ---------------------------------------------------------------------------
# Variable substitution interaction
# ---------------------------------------------------------------------------


def test_var_substitution_then_unknown_field_warns():
    body = """
        name: sample
        base:
          image: ${IMG}
        unknown_section: hi
    """
    with pytest.warns(
        UnknownProfileFieldWarning, match=r"profile: unknown field 'unknown_section'"
    ):
        _load(body, variables={"IMG": "ubuntu:24.04"})


def test_unknown_field_value_with_var_still_warns():
    """Substitution applies to values; unknown keys are still detected."""
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        descriptiom: ${NAME}
    """
    with pytest.warns(
        UnknownProfileFieldWarning, match=r"profile: unknown field 'descriptiom'"
    ):
        _load(body, variables={"NAME": "anything"})


# ---------------------------------------------------------------------------
# Empty / edge cases
# ---------------------------------------------------------------------------


def test_empty_section_no_warnings():
    """Optional sections present-but-empty should not warn."""
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        provision: {}
    """
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _load(body)
    # `provision: {}` is falsy in Python so the parser short-circuits past it,
    # but even if it walked in, an empty mapping has no unknown keys.
    assert _unknown_warnings(w) == []


def test_null_optional_section_no_warnings():
    """Optional sections present-but-null should not warn."""
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        update: ~
    """
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _load(body)
    assert _unknown_warnings(w) == []


def test_unknown_field_with_empty_string_value_still_warns():
    body = """
        name: sample
        base:
          image: ubuntu:24.04
        descriptiom: ''
    """
    with pytest.warns(
        UnknownProfileFieldWarning, match=r"profile: unknown field 'descriptiom'"
    ):
        _load(body)


# ---------------------------------------------------------------------------
# validate=True/False parameter
# ---------------------------------------------------------------------------
#
# The launch flow parses a profile twice (once before container creation to
# get the base image, once after host gateway IP detection to substitute
# rewritten vars). Snapshot replay during update/destroy is a third re-parse.
# Re-emitting unknown-field warnings on every re-parse produces duplicates
# and surfaces non-actionable noise. Callers re-parsing an already-validated
# profile pass `validate=False`.


# A profile with multiple unknown fields at different depths -- used by the
# parameter tests below.
_MULTI_TYPO_PROFILE = """
    name: noisy
    base:
      image: ubuntu:24.04
      iamge: typo                                # base
    descriptiom: typo at top-level               # profile
    url_rewrites:
      rules:
        - match: a
          target: b
          priority: 1                            # url_rewrites.rules[0]
"""


def test_load_profile_from_content_validate_true_is_default():
    """Default behavior of load_profile_from_content emits warnings."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_profile_from_content(textwrap.dedent(_MULTI_TYPO_PROFILE), {})
    # Three unknown fields seeded in _MULTI_TYPO_PROFILE.
    assert len(_unknown_warnings(caught)) == 3


def test_load_profile_from_content_validate_false_skips_all_warnings():
    """validate=False on load_profile_from_content suppresses every warning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_profile_from_content(
            textwrap.dedent(_MULTI_TYPO_PROFILE), {}, validate=False
        )
    assert _unknown_warnings(caught) == []


def test_load_profile_validate_false_skips_all_warnings(tmp_path):
    """validate=False on load_profile (path entry point) also suppresses."""
    from amplifier_bundle_digital_twin_universe.profile import load_profile

    profile_path = tmp_path / "noisy.yaml"
    profile_path.write_text(textwrap.dedent(_MULTI_TYPO_PROFILE))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_profile(str(profile_path), {}, validate=False)
    assert _unknown_warnings(caught) == []


def test_validate_false_does_not_swallow_required_field_errors():
    """validate=False only skips unknown-field warnings, not real validation."""
    body = "name: bad\nbase: {}\n"  # missing required base.image
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ValueError, match=r"base\.image"):
            load_profile_from_content(body, {}, validate=False)


def test_validate_false_does_not_swallow_exclusivity_errors():
    """validate=False only skips unknown-field warnings, not real validation."""
    body = """
        name: bad
        base:
          image: ubuntu:24.04
        pypi_overrides:
          packages:
            - name: x
              wheel_path: /tmp/x.whl
              wheel_var: WHEEL_VAR
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ValueError, match=r"exactly one of"):
            load_profile_from_content(textwrap.dedent(body), {}, validate=False)


def test_clean_profile_emits_no_warnings_with_either_validate_setting():
    """A profile with no unknown fields is identical under either setting."""
    body = textwrap.dedent("""
        name: clean
        base:
          image: ubuntu:24.04
    """)

    with warnings.catch_warnings(record=True) as caught_true:
        warnings.simplefilter("always")
        load_profile_from_content(body, {}, validate=True)
    assert _unknown_warnings(caught_true) == []

    with warnings.catch_warnings(record=True) as caught_false:
        warnings.simplefilter("always")
        load_profile_from_content(body, {}, validate=False)
    assert _unknown_warnings(caught_false) == []


def test_engine_double_parse_simulation_emits_warnings_once():
    """Mimic the engine.py launch flow: parse once with validate=True, then
    re-parse with validate=False (post-gateway-IP rewriting). Duplicates
    must NOT appear."""
    body = textwrap.dedent(_MULTI_TYPO_PROFILE)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # First parse: matches engine.py:949 (host_profile)
        load_profile_from_content(body, {})
        # Second parse: matches engine.py:987 (post-rewrite)
        load_profile_from_content(body, {"GATEWAY": "10.0.0.1"}, validate=False)

    msgs = _unknown_warnings(caught)
    # 3 unknowns x 1 (no duplication from second parse) = 3
    assert len(msgs) == 3, f"expected 3 warnings, got {len(msgs)}: {msgs}"


def test_snapshot_replay_simulation_emits_no_warnings():
    """Mimic update/destroy on an already-launched DTU: snapshot replay
    must not warn -- validation already ran at original launch."""
    body = textwrap.dedent(_MULTI_TYPO_PROFILE)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # Snapshot replay: matches engine.py:1239
        load_profile_from_content(body, {}, validate=False)

    assert _unknown_warnings(caught) == []
