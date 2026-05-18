# Copyright (c) Microsoft. All rights reserved.

"""Unit tests for directory-recursive file_pull.

Bug context: ``amplifier-digital-twin file-pull <id> <remote-dir> <local-dir>``
calls ``incus file pull`` under the hood.  The underlying Incus endpoint
(``GET /1.0/instances/{id}/files?path=...``) is file-only; it errors with
``Can't pull a directory without --recursive`` when the remote source is a
directory, even on some versions with ``--recursive``.

The fix auto-detects directory sources and implements recursion at the Python
level:
  1. Detect remote directory via ``incus exec -- test -d <path>``.
  2. Create the local destination directory via ``os.makedirs``.
  3. Enumerate all files: ``incus exec -- find <remote_dir> -type f``.
  4. For each file: create local parent dirs, pull file individually
     via ``incus file pull``.

Symmetric to ``test_file_push_directory_recursion.py`` (PR #18).

No real Incus daemon required — subprocess calls are mocked throughout.

Run with: uv run pytest tests/unit/test_file_pull_directory_recursion.py -v
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from amplifier_bundle_digital_twin_universe import incus


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ok_run() -> MagicMock:
    """subprocess.CompletedProcess mock with returncode=0."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = ""
    m.stderr = ""
    return m


_DTU = "dtu-test"
_INCUS_MOD = "amplifier_bundle_digital_twin_universe.incus"


def _make_exec_side_effect(
    *,
    is_dir: bool = True,
    find_output: str = "",
) -> object:
    """Return an exec_command side_effect for a single-path scenario.

    - ``test -d`` calls return (0, "", "") when *is_dir* is True, else (1, "", "").
    - ``find`` calls return (0, find_output, "").
    - Any other call returns (0, "", "").
    """

    def side_effect(name, cmd, **kwargs):
        if cmd and cmd[0] == "test" and "-d" in cmd:
            return (0, "", "") if is_dir else (1, "", "")
        if cmd and cmd[0] == "find":
            return (0, find_output, "")
        return (0, "", "")

    return side_effect


def _make_exec_side_effect_multi(
    path_is_dir: dict[str, bool], find_map: dict[str, str]
) -> object:
    """Return an exec_command side_effect for multi-path scenarios.

    - ``test -d <path>`` → (0, ...) if path_is_dir[path] else (1, ...).
    - ``find <dir> -type f`` → (0, find_map[dir], "").
    """

    def side_effect(name, cmd, **kwargs):
        if cmd and cmd[0] == "test" and len(cmd) >= 3 and cmd[1] == "-d":
            path = cmd[2]
            return (0, "", "") if path_is_dir.get(path, False) else (1, "", "")
        if cmd and cmd[0] == "find" and len(cmd) >= 2:
            dirpath = cmd[1]
            return (0, find_map.get(dirpath, ""), "")
        return (0, "", "")

    return side_effect


# ===========================================================================
# Section A: directory source — recursive walk
# ===========================================================================


def test_file_pull_directory_creates_local_root_dir(tmp_path: Path) -> None:
    """A directory source creates the local root directory."""
    remote_dir = "/root/.amplifier/projects"
    local_parent = str(tmp_path / "output")

    exec_se = _make_exec_side_effect(
        is_dir=True,
        find_output="/root/.amplifier/projects/events.jsonl\n",
    )

    with (
        patch(f"{_INCUS_MOD}.exec_command", side_effect=exec_se),
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()),
    ):
        incus.file_pull(_DTU, [remote_dir], local_parent)

    assert (tmp_path / "output" / "projects").is_dir(), (
        "Expected local_parent/projects/ to be created"
    )


def test_file_pull_directory_pulls_each_file(tmp_path: Path) -> None:
    """Each file enumerated in the remote directory is pulled individually."""
    remote_dir = "/root/.amplifier/projects"
    local_parent = str(tmp_path / "output")
    os.makedirs(local_parent, exist_ok=True)

    find_output = (
        "/root/.amplifier/projects/events.jsonl\n"
        "/root/.amplifier/projects/mount_plan.json\n"
    )
    exec_se = _make_exec_side_effect(is_dir=True, find_output=find_output)

    with (
        patch(f"{_INCUS_MOD}.exec_command", side_effect=exec_se),
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()) as mock_run,
    ):
        incus.file_pull(_DTU, [remote_dir], local_parent)

    pull_calls = [
        c.args[0]
        for c in mock_run.call_args_list
        if c.args and c.args[0][:3] == ["incus", "file", "pull"]
    ]
    # cmd[3] = src ("dtu/path"), cmd[4] = local dest
    pulled_srcs = {cmd[3] for cmd in pull_calls}
    assert f"{_DTU}/root/.amplifier/projects/events.jsonl" in pulled_srcs, pulled_srcs
    assert f"{_DTU}/root/.amplifier/projects/mount_plan.json" in pulled_srcs, (
        pulled_srcs
    )


def test_file_pull_directory_nested_files_at_correct_relative_paths(
    tmp_path: Path,
) -> None:
    """Files in sub-directories of the remote tree are placed at the correct local paths."""
    remote_dir = "/root/.amplifier/projects"
    local_parent = str(tmp_path / "output")
    os.makedirs(local_parent, exist_ok=True)

    find_output = (
        "/root/.amplifier/projects/default/events.jsonl\n"
        "/root/.amplifier/projects/default/mount_plan.json\n"
    )
    exec_se = _make_exec_side_effect(is_dir=True, find_output=find_output)

    with (
        patch(f"{_INCUS_MOD}.exec_command", side_effect=exec_se),
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()) as mock_run,
    ):
        incus.file_pull(_DTU, [remote_dir], local_parent)

    pull_calls = [
        c.args[0]
        for c in mock_run.call_args_list
        if c.args and c.args[0][:3] == ["incus", "file", "pull"]
    ]
    # cmd[4] = local destination path
    local_dests = {cmd[4] for cmd in pull_calls}
    expected_events = str(tmp_path / "output" / "projects" / "default" / "events.jsonl")
    expected_mount = str(
        tmp_path / "output" / "projects" / "default" / "mount_plan.json"
    )
    assert expected_events in local_dests, (
        f"events.jsonl expected at {expected_events}; got {local_dests}"
    )
    assert expected_mount in local_dests, (
        f"mount_plan.json expected at {expected_mount}; got {local_dests}"
    )


def test_file_pull_directory_creates_nested_local_dirs(tmp_path: Path) -> None:
    """Parent directories for nested remote files are created locally."""
    remote_dir = "/root/.amplifier/projects"
    local_parent = str(tmp_path / "output")
    os.makedirs(local_parent, exist_ok=True)

    find_output = "/root/.amplifier/projects/a/b/deep.txt\n"
    exec_se = _make_exec_side_effect(is_dir=True, find_output=find_output)

    with (
        patch(f"{_INCUS_MOD}.exec_command", side_effect=exec_se),
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()),
    ):
        incus.file_pull(_DTU, [remote_dir], local_parent)

    # The parent directories a/b/ must be created even though the file pull is mocked.
    expected_parent = tmp_path / "output" / "projects" / "a" / "b"
    assert expected_parent.is_dir(), (
        f"Expected nested dirs {expected_parent} to be created"
    )


# ===========================================================================
# Section B: empty remote directory
# ===========================================================================


def test_file_pull_empty_directory_creates_local_dir(tmp_path: Path) -> None:
    """An empty remote directory still creates the local root directory."""
    remote_dir = "/root/.amplifier/projects"
    local_parent = str(tmp_path / "output")

    exec_se = _make_exec_side_effect(is_dir=True, find_output="")

    with (
        patch(f"{_INCUS_MOD}.exec_command", side_effect=exec_se),
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()) as mock_run,
    ):
        incus.file_pull(_DTU, [remote_dir], local_parent)

    assert (tmp_path / "output" / "projects").is_dir(), (
        "Expected empty remote dir to still create local root"
    )
    # No file pull calls.
    pull_calls = [
        c
        for c in mock_run.call_args_list
        if c.args and c.args[0][:3] == ["incus", "file", "pull"]
    ]
    assert len(pull_calls) == 0, (
        f"Expected no file pull calls for an empty directory; got {len(pull_calls)}"
    )


# ===========================================================================
# Section C: single-file pull regression — existing behavior unchanged
# ===========================================================================


def test_file_pull_single_file_uses_direct_incus_pull(tmp_path: Path) -> None:
    """A single file source uses the existing incus file pull without recursion."""
    local_dir = str(tmp_path / "output")
    os.makedirs(local_dir, exist_ok=True)

    exec_se = _make_exec_side_effect(is_dir=False)

    with (
        patch(f"{_INCUS_MOD}.exec_command", side_effect=exec_se) as mock_exec,
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()) as mock_run,
    ):
        incus.file_pull(_DTU, ["/workspace/config.yaml"], local_dir)

    # No find call — only test -d, no find
    exec_calls = [c.args[1] for c in mock_exec.call_args_list]
    find_calls = [c for c in exec_calls if c and c[0] == "find"]
    assert len(find_calls) == 0, (
        f"Expected no find calls for file source; got {find_calls}"
    )

    # Exactly one subprocess.run call.
    mock_run.assert_called_once()
    cmd = mock_run.call_args.args[0]
    assert cmd[:3] == ["incus", "file", "pull"], f"Expected incus file pull; got {cmd}"


def test_file_pull_multiple_files_uses_single_batch_call(tmp_path: Path) -> None:
    """Multiple file sources use a single incus file pull invocation (batch, no recursion)."""
    local_dir = str(tmp_path / "output")
    os.makedirs(local_dir, exist_ok=True)

    path_is_dir = {"/workspace/a.txt": False, "/workspace/b.txt": False}
    exec_se = _make_exec_side_effect_multi(path_is_dir=path_is_dir, find_map={})

    with (
        patch(f"{_INCUS_MOD}.exec_command", side_effect=exec_se),
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()) as mock_run,
    ):
        incus.file_pull(_DTU, ["/workspace/a.txt", "/workspace/b.txt"], local_dir)

    mock_run.assert_called_once()
    cmd = mock_run.call_args.args[0]
    assert cmd[:3] == ["incus", "file", "pull"], f"Expected incus file pull; got {cmd}"
    # Both sources in one call.
    assert f"{_DTU}/workspace/a.txt" in cmd, f"a.txt not in cmd: {cmd}"
    assert f"{_DTU}/workspace/b.txt" in cmd, f"b.txt not in cmd: {cmd}"


# ===========================================================================
# Section D: mixed sources — file + directory in same invocation
# ===========================================================================


def test_file_pull_mixed_sources_handles_directory(tmp_path: Path) -> None:
    """In a mixed pull (file + dir), the directory is recursed with name preserved."""
    local_dir = str(tmp_path / "output")
    os.makedirs(local_dir, exist_ok=True)

    path_is_dir = {"/workspace/README.md": False, "/root/.amplifier/projects": True}
    find_map = {"/root/.amplifier/projects": "/root/.amplifier/projects/events.jsonl\n"}
    exec_se = _make_exec_side_effect_multi(path_is_dir=path_is_dir, find_map=find_map)

    with (
        patch(f"{_INCUS_MOD}.exec_command", side_effect=exec_se),
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()) as mock_run,
    ):
        incus.file_pull(
            _DTU,
            ["/workspace/README.md", "/root/.amplifier/projects"],
            local_dir,
        )

    pull_calls = [
        c.args[0]
        for c in mock_run.call_args_list
        if c.args and c.args[0][:3] == ["incus", "file", "pull"]
    ]
    pulled_srcs = {cmd[3] for cmd in pull_calls}

    # Directory: events.jsonl pulled at correct src path.
    assert f"{_DTU}/root/.amplifier/projects/events.jsonl" in pulled_srcs, (
        f"Expected events.jsonl in srcs; got {pulled_srcs}"
    )
    # Local dir projects/ was created.
    assert (tmp_path / "output" / "projects").is_dir()


def test_file_pull_mixed_sources_handles_file(tmp_path: Path) -> None:
    """In a mixed pull (file + dir), the file is pulled at the correct local path."""
    local_dir = str(tmp_path / "output")
    os.makedirs(local_dir, exist_ok=True)

    path_is_dir = {"/workspace/README.md": False, "/root/.amplifier/projects": True}
    find_map = {"/root/.amplifier/projects": "/root/.amplifier/projects/events.jsonl\n"}
    exec_se = _make_exec_side_effect_multi(path_is_dir=path_is_dir, find_map=find_map)

    with (
        patch(f"{_INCUS_MOD}.exec_command", side_effect=exec_se),
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()) as mock_run,
    ):
        incus.file_pull(
            _DTU,
            ["/workspace/README.md", "/root/.amplifier/projects"],
            local_dir,
        )

    pull_calls = [
        c.args[0]
        for c in mock_run.call_args_list
        if c.args and c.args[0][:3] == ["incus", "file", "pull"]
    ]
    pulled_srcs = {cmd[3] for cmd in pull_calls}

    # File: README.md pulled from correct src.
    assert f"{_DTU}/workspace/README.md" in pulled_srcs, (
        f"Expected README.md in srcs; got {pulled_srcs}"
    )


# ===========================================================================
# Section E: error paths
# ===========================================================================


def test_file_pull_directory_raises_on_find_failure(tmp_path: Path) -> None:
    """IncusError is raised if the remote find command fails."""
    local_dir = str(tmp_path / "output")

    def exec_se(name, cmd, **kwargs):
        if cmd and cmd[0] == "test":
            return (0, "", "")  # is a directory
        if cmd and cmd[0] == "find":
            return (1, "", "permission denied")
        return (0, "", "")

    with (
        patch(f"{_INCUS_MOD}.exec_command", side_effect=exec_se),
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()),
    ):
        with pytest.raises(incus.IncusError, match="permission denied"):
            incus.file_pull(_DTU, ["/root/.amplifier/projects"], local_dir)


def test_file_pull_directory_raises_on_file_pull_failure(tmp_path: Path) -> None:
    """IncusError is raised if incus file pull fails for a file inside the dir."""
    local_dir = str(tmp_path / "output")

    exec_se = _make_exec_side_effect(
        is_dir=True, find_output="/root/.amplifier/projects/events.jsonl\n"
    )
    fail_run = MagicMock()
    fail_run.returncode = 1
    fail_run.stderr = "no such container"

    with (
        patch(f"{_INCUS_MOD}.exec_command", side_effect=exec_se),
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=fail_run),
    ):
        with pytest.raises(incus.IncusError, match="no such container"):
            incus.file_pull(_DTU, ["/root/.amplifier/projects"], local_dir)


def test_file_pull_single_file_raises_on_failure(tmp_path: Path) -> None:
    """IncusError is raised if the file pull fails for a plain file source."""
    local_dir = str(tmp_path / "output")
    exec_se = _make_exec_side_effect(is_dir=False)
    fail_run = MagicMock()
    fail_run.returncode = 1
    fail_run.stderr = "instance not found"

    with (
        patch(f"{_INCUS_MOD}.exec_command", side_effect=exec_se),
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=fail_run),
    ):
        with pytest.raises(incus.IncusError, match="instance not found"):
            incus.file_pull(_DTU, ["/workspace/config.yaml"], local_dir)
