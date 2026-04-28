# Copyright (c) Microsoft. All rights reserved.

"""Unit tests for profile resolution in profile.find_profile_path()."""

from pathlib import Path

import pytest

from amplifier_bundle_digital_twin_universe import profile as profile_mod


def _seed_profile(
    root: Path,
    rel: str,
    body: str = "name: sample\nbase:\n  image: ubuntu:24.04\n",
) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    return target


def test_find_profile_recurses_into_buckets(tmp_path, monkeypatch):
    """Built-in name lookup finds a profile nested inside a bucket directory."""
    fake_builtin = tmp_path / "profiles"
    _seed_profile(fake_builtin, "amplifier/amplifier-chat.yaml")
    monkeypatch.setattr(profile_mod, "_BUILTIN_PROFILES_DIR", fake_builtin)

    resolved = profile_mod.find_profile_path("amplifier-chat")

    assert resolved == (fake_builtin / "amplifier" / "amplifier-chat.yaml").resolve()


def test_find_profile_recurses_multiple_buckets(tmp_path, monkeypatch):
    """Recursion works regardless of which bucket holds the profile."""
    fake_builtin = tmp_path / "profiles"
    _seed_profile(fake_builtin, "community/openai-codex-cli.yaml")
    monkeypatch.setattr(profile_mod, "_BUILTIN_PROFILES_DIR", fake_builtin)

    resolved = profile_mod.find_profile_path("openai-codex-cli")

    assert resolved.name == "openai-codex-cli.yaml"
    assert resolved.parent.name == "community"


def test_find_profile_collision_raises(tmp_path, monkeypatch):
    """Two built-in profiles with the same stem raise ValueError listing both paths."""
    fake_builtin = tmp_path / "profiles"
    _seed_profile(fake_builtin, "amplifier/demo.yaml")
    _seed_profile(fake_builtin, "community/demo.yaml")
    monkeypatch.setattr(profile_mod, "_BUILTIN_PROFILES_DIR", fake_builtin)

    with pytest.raises(ValueError) as excinfo:
        profile_mod.find_profile_path("demo")

    msg = str(excinfo.value)
    assert "Ambiguous" in msg
    assert "amplifier/demo.yaml" in msg
    assert "community/demo.yaml" in msg


def test_find_profile_ignores_cwd_profiles(tmp_path, monkeypatch):
    """A 'profiles/<name>.yaml' in CWD must NOT be auto-resolved.

    The undocumented CWD/profiles/<name>.yaml fallback was removed. A stray
    'profiles/' subdirectory in the user's CWD must not shadow the built-in
    name lookup.
    """
    # Point the built-in dir at an empty directory so lookup can't find anything.
    empty_builtin = tmp_path / "empty"
    empty_builtin.mkdir()
    monkeypatch.setattr(profile_mod, "_BUILTIN_PROFILES_DIR", empty_builtin)

    # Create ./profiles/sneaky.yaml in a working directory and chdir into it.
    cwd = tmp_path / "cwd"
    _seed_profile(cwd, "profiles/sneaky.yaml")
    monkeypatch.chdir(cwd)

    with pytest.raises(FileNotFoundError) as excinfo:
        profile_mod.find_profile_path("sneaky")

    # Error must not reference a CWD-derived profiles path.
    msg = str(excinfo.value)
    assert "sneaky" in msg
    assert str(cwd / "profiles") not in msg


def test_find_profile_accepts_relative_path(tmp_path, monkeypatch):
    """An explicit relative path to a YAML file still resolves."""
    empty_builtin = tmp_path / "empty"
    empty_builtin.mkdir()
    monkeypatch.setattr(profile_mod, "_BUILTIN_PROFILES_DIR", empty_builtin)

    cwd = tmp_path / "cwd"
    target = _seed_profile(cwd, "some/dir/explicit.yaml")
    monkeypatch.chdir(cwd)

    resolved = profile_mod.find_profile_path("some/dir/explicit.yaml")

    assert resolved == target.resolve()


def test_find_profile_accepts_absolute_path(tmp_path, monkeypatch):
    """An explicit absolute path to a YAML file still resolves."""
    empty_builtin = tmp_path / "empty"
    empty_builtin.mkdir()
    monkeypatch.setattr(profile_mod, "_BUILTIN_PROFILES_DIR", empty_builtin)

    target = _seed_profile(tmp_path, "abs.yaml")

    resolved = profile_mod.find_profile_path(str(target))

    assert resolved == target.resolve()
