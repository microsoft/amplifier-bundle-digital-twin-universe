# Copyright (c) Microsoft. All rights reserved.

"""Schema/dataclass field-sync tests for the profile parser.

Several schema nodes correspond exactly to a dataclass's fields. If a
field is added to one but not the other, validation will silently drift.
These tests pin the synchronization between ``_PROFILE_SCHEMA`` and the
backing dataclasses.
"""

from __future__ import annotations

from typing import Any

import pytest

from amplifier_bundle_digital_twin_universe import profile as profile_mod


# ---------------------------------------------------------------------------
# Sync between schema and dataclass fields
# ---------------------------------------------------------------------------
#
# `schema_path` is a tuple of keys to walk through `_PROFILE_SCHEMA`. The
# sentinel `"[]"` steps into a list-of-mappings's per-item schema (i.e.
# `node[0]`).


def _walk_schema(path: tuple[str, ...]) -> dict:
    """Walk *path* through `_PROFILE_SCHEMA` and return the mapping at the end."""
    node: Any = profile_mod._PROFILE_SCHEMA
    for step in path:
        if step == "[]":
            assert isinstance(node, list), (
                f"schema step '[]' applied to non-list at path {path}"
            )
            node = node[0]
        else:
            assert isinstance(node, dict), (
                f"schema step {step!r} applied to non-dict at path {path}"
            )
            node = node[step]
    assert isinstance(node, dict), f"schema path {path} did not land on a dict node"
    return node


@pytest.mark.parametrize(
    ("schema_path", "dataclass_name"),
    [
        (("base",), "Base"),
        (("url_rewrites", "auth"), "UrlRewriteAuth"),
        (("url_rewrites", "rules", "[]"), "UrlRewriteRule"),
        (("passthrough", "services", "[]"), "PassthroughService"),
        (("provision", "files", "[]"), "FileEntry"),
        (("update",), "Update"),
        (
            ("pypi_overrides", "packages", "[]", "wheel_from_git"),
            "PypiOverrideGitSource",
        ),
        (("access", "ports", "[]"), "PortMapping"),
        (("readiness", "[]", "http"), "ReadinessCheckHttp"),
        (("readiness", "[]", "tcp"), "ReadinessCheckTcp"),
        (("mock_services", "[]"), "ServiceEntry"),
    ],
)
def test_schema_keys_match_dataclass_fields(schema_path, dataclass_name):
    from dataclasses import fields

    node = _walk_schema(schema_path)
    cls = getattr(profile_mod, dataclass_name)
    schema_keys = set(node.keys())
    field_names = {f.name for f in fields(cls)}
    assert schema_keys == field_names, (
        f"_PROFILE_SCHEMA at {schema_path} drifted from {dataclass_name} fields:\n"
        f"  schema-only: {schema_keys - field_names}\n"
        f"  fields-only: {field_names - schema_keys}"
    )
