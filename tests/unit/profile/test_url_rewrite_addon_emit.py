# Copyright (c) Microsoft. All rights reserved.

"""Wire-format contract for the mitmproxy addon emitted by ``_generate_addon_script``.

The matcher logic is unit-tested directly via ``match_url`` in the sibling
``test_url_rewrite_matching.py``. These tests cover the *other half* of the
contract: that the addon script ``_generate_addon_script`` writes into the
container reflects the expected ordering and per-rule fields. They protect
against drift between the host-side matcher and the in-container template.
"""

from __future__ import annotations

import ast
from pathlib import Path

from amplifier_bundle_digital_twin_universe.engine import _generate_addon_script
from amplifier_bundle_digital_twin_universe.profile import load_profile_from_content


def _extract_rules_assignment(addon_source: str) -> list[dict]:
    """Parse the addon source and return the literal value assigned to RULES."""
    tree = ast.parse(addon_source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "RULES"
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("addon source has no top-level `RULES = [...]` assignment")


def _profile(yaml_text: str):
    """Load a profile from inline YAML, returning the Profile."""
    return load_profile_from_content(yaml_text, {}, path=Path("<test>"))


def test_generated_addon_rules_sorted_longest_first() -> None:
    """When the loader sorts rules by descending prefix length, the
    serialised RULES list in the addon must reflect that order."""
    body = """\
name: emit
base:
  image: ubuntu:24.04
url_rewrites:
  rules:
    - match: github.com/microsoft/amplifier
      target: https://gitea.example/amplifier
    - match: github.com/microsoft/amplifier-module-foo
      target: https://gitea.example/amplifier-module-foo
"""
    profile = _profile(body)
    addon = _generate_addon_script(profile, variables={})
    rules = _extract_rules_assignment(addon)

    matches = [r["match_path_prefix"] for r in rules]
    # Longer prefix must precede shorter prefix.
    assert matches.index("/microsoft/amplifier-module-foo") < matches.index(
        "/microsoft/amplifier"
    ), f"rules not sorted longest-first: {matches}"


def test_generated_addon_rules_include_match_mode_field() -> None:
    """Every rule emitted into the addon must carry a ``match_mode`` key so
    the in-container matcher can honour boundary semantics."""
    body = """\
name: emit
base:
  image: ubuntu:24.04
url_rewrites:
  rules:
    - match: github.com/microsoft/foo
      target: https://gitea.example/foo
    - match: github.com/microsoft/bar
      target: https://gitea.example/bar
      match_mode: boundary
"""
    profile = _profile(body)
    addon = _generate_addon_script(profile, variables={})
    rules = _extract_rules_assignment(addon)

    assert all("match_mode" in r for r in rules), (
        f"every emitted rule must include a match_mode field; got rules={rules}"
    )

    by_match = {r["match_path_prefix"]: r for r in rules}
    assert by_match["/microsoft/foo"]["match_mode"] == "prefix", (
        "rule without explicit match_mode must default to 'prefix' in emitted addon"
    )
    assert by_match["/microsoft/bar"]["match_mode"] == "boundary", (
        "rule with explicit match_mode: boundary must be emitted as 'boundary'"
    )
