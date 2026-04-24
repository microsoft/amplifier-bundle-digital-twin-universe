# Copyright (c) Microsoft. All rights reserved.

"""E2E test for the in-container profile snapshot used by ``update``.

Writes a minimal profile to a tempdir, launches a DTU from it, *moves* the
profile file off the host filesystem (to a location no longer in the profile
search paths), then runs ``amplifier-digital-twin update``. The update must
succeed because the container carries its own snapshot at ``/opt/dtu/profile.yaml``
-- the host file is no longer needed.

Prerequisites: Incus only. No Docker, Gitea, or API keys.

Run with:
    uv run pytest tests/test_e2e_update_profile_snapshot.py --run-e2e -v -s
"""

import shutil
import sys

import pytest

from conftest import register_dtu_instance
from helpers import run_cli, run_cli_json


MINIMAL_PROFILE = """\
name: update-snapshot-test
description: Minimal profile with an update section for snapshot E2E test

base:
  image: ubuntu:24.04

update:
  cmds:
    - echo "update-marker"
"""


@pytest.fixture
def profile_file(tmp_path_factory):
    """Write a minimal profile file for this test."""
    d = tmp_path_factory.mktemp("dtu-update-snapshot-profiles")
    profile = d / "update-snapshot-test.yaml"
    profile.write_text(MINIMAL_PROFILE)
    return profile


@pytest.mark.e2e
def test_update_survives_host_profile_move(profile_file, tmp_path_factory):
    """``update`` must succeed even when the original on-host profile file is
    moved out from under it. The container snapshot at /opt/dtu/profile.yaml
    is authoritative.
    """
    # -- Phase 1: launch from the host-side profile file --
    print(f"[E2E-update-snapshot] Launching from {profile_file}...", file=sys.stderr)
    launch_data, _ = run_cli_json("launch", str(profile_file), timeout=600)
    env_id = launch_data["id"]
    register_dtu_instance(env_id)

    try:
        # -- Phase 2: verify the snapshot made it into the container --
        snapshot_data, _ = run_cli_json(
            "exec",
            env_id,
            "--",
            "cat",
            "/opt/dtu/profile.yaml",
            timeout=30,
        )
        assert snapshot_data["exit_code"] == 0, (
            "Expected /opt/dtu/profile.yaml to exist inside the container "
            f"(stderr: {snapshot_data['stderr']})"
        )
        assert "name: update-snapshot-test" in snapshot_data["stdout"]
        assert 'echo "update-marker"' in snapshot_data["stdout"]

        # -- Phase 3: move the host profile out of the search paths --
        quarantine = tmp_path_factory.mktemp("dtu-profile-quarantine")
        moved_to = quarantine / profile_file.name
        shutil.move(str(profile_file), str(moved_to))
        assert not profile_file.exists()

        # Sanity: a fresh `launch` from the original path now fails.  We use
        # this to prove the profile truly isn't resolvable from the host.
        launch_fail = run_cli("launch", str(profile_file), timeout=30)
        assert launch_fail.returncode != 0, (
            "Expected launch from the moved-away path to fail; it somehow "
            "succeeded, which breaks this test's assumptions."
        )
        assert "Profile not found" in launch_fail.stderr

        # -- Phase 4: update must still succeed via the container snapshot --
        print(
            "[E2E-update-snapshot] Running update after moving host profile...",
            file=sys.stderr,
        )
        update_data, _ = run_cli_json("update", env_id, timeout=120)
        assert update_data["status"] == "updated", update_data
        assert update_data["cmds_run"] == 1
        # --skip-readiness was not passed; profile has no readiness checks so
        # the key may be absent or empty -- either is fine.  What matters is
        # that `update` did not fail with "Profile not found".

    finally:
        # Restore the file so pytest's tmp cleanup is tidy (not strictly
        # required; tmp_path_factory handles deletion either way).
        run_cli("destroy", env_id, timeout=60)


@pytest.mark.e2e
def test_update_uses_snapshot_not_host_edits(profile_file):
    """Editing the host profile after launch does NOT affect update.

    This documents the snapshot semantics: the container owns its own profile.
    If/when a ``--refresh-profile`` flag is added, that path can opt back in
    to re-reading the host file.
    """
    print(f"[E2E-update-snapshot] Launching from {profile_file}...", file=sys.stderr)
    launch_data, _ = run_cli_json("launch", str(profile_file), timeout=600)
    env_id = launch_data["id"]
    register_dtu_instance(env_id)

    try:
        # Mutate the host file *after* launch with a different marker.
        profile_file.write_text(
            MINIMAL_PROFILE.replace('echo "update-marker"', 'echo "host-edited-marker"')
        )

        # Run update; it should use the original snapshot, not the edited file.
        update_data, _ = run_cli_json("update", env_id, timeout=120)
        assert update_data["status"] == "updated", update_data

        # Confirm by reading the snapshot back -- still has the original marker.
        snapshot_data, _ = run_cli_json(
            "exec",
            env_id,
            "--",
            "cat",
            "/opt/dtu/profile.yaml",
            timeout=30,
        )
        assert snapshot_data["exit_code"] == 0
        assert 'echo "update-marker"' in snapshot_data["stdout"]
        assert "host-edited-marker" not in snapshot_data["stdout"]
    finally:
        run_cli("destroy", env_id, timeout=60)
