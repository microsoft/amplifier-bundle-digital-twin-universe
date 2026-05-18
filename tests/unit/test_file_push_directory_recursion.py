# Copyright (c) Microsoft. All rights reserved.

"""Unit tests for directory-recursive file_push.

Bug context: ``amplifier-digital-twin file-push <id> <local-dir> <remote-dir>``
calls ``incus file push`` under the hood.  The underlying HTTP endpoint
(``POST /1.0/instances/{id}/files``) is file-only; it errors with
``is a directory`` when the source is a directory, even with ``--recursive``.

The fix auto-detects directory sources and implements recursion at the Python
level:
  1. Create the remote destination directory via ``incus exec -- mkdir -p``.
  2. Walk the local tree.
  3. Create each sub-directory via ``incus exec -- mkdir -p``.
  4. Push each file individually via a single ``incus file push`` invocation.

No real Incus daemon required — subprocess calls are mocked throughout.

Run with: uv run pytest tests/unit/test_file_push_directory_recursion.py -v
"""

from __future__ import annotations

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


# ===========================================================================
# Section A: directory source — recursive walk
# ===========================================================================


def test_file_push_directory_creates_remote_root_dir(tmp_path: Path) -> None:
    """A directory source creates the remote root via mkdir -p."""
    src = tmp_path / "mydir"
    src.mkdir()
    (src / "file.txt").write_text("hello")

    with (
        patch(f"{_INCUS_MOD}.exec_command", return_value=(0, "", "")) as mock_exec,
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()),
    ):
        incus.file_push(_DTU, [str(src)], "/workspace/")

    # Root directory must be created first.
    mock_exec.assert_any_call(_DTU, ["mkdir", "-p", "/workspace/mydir"], timeout=120)


def test_file_push_directory_pushes_each_file(tmp_path: Path) -> None:
    """Each file inside the directory is pushed individually via incus file push."""
    src = tmp_path / "mydir"
    src.mkdir()
    f1 = src / "alpha.txt"
    f2 = src / "beta.txt"
    f1.write_text("a")
    f2.write_text("b")

    with (
        patch(f"{_INCUS_MOD}.exec_command", return_value=(0, "", "")),
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()) as mock_run,
    ):
        incus.file_push(_DTU, [str(src)], "/workspace/")

    # Collect all incus-file-push commands.
    push_calls = [
        c.args[0]
        for c in mock_run.call_args_list
        if c.args and c.args[0][:3] == ["incus", "file", "push"]
    ]
    pushed_dests = {cmd[-1] for cmd in push_calls}  # last arg = dest

    assert f"{_DTU}/workspace/mydir/alpha.txt" in pushed_dests, pushed_dests
    assert f"{_DTU}/workspace/mydir/beta.txt" in pushed_dests, pushed_dests


def test_file_push_directory_creates_subdirectory(tmp_path: Path) -> None:
    """Sub-directories inside the source are created via mkdir -p."""
    src = tmp_path / "mydir"
    src.mkdir()
    sub = src / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested")

    with (
        patch(f"{_INCUS_MOD}.exec_command", return_value=(0, "", "")) as mock_exec,
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()),
    ):
        incus.file_push(_DTU, [str(src)], "/workspace/")

    mkdir_commands = [c.args[1] for c in mock_exec.call_args_list]
    assert ["mkdir", "-p", "/workspace/mydir/sub"] in mkdir_commands, (
        f"Expected mkdir -p /workspace/mydir/sub; got: {mkdir_commands}"
    )


def test_file_push_directory_pushes_nested_file(tmp_path: Path) -> None:
    """Files inside sub-directories are pushed at the correct remote path."""
    src = tmp_path / "mydir"
    src.mkdir()
    sub = src / "sub"
    sub.mkdir()
    nested = sub / "nested.txt"
    nested.write_text("deep")

    with (
        patch(f"{_INCUS_MOD}.exec_command", return_value=(0, "", "")),
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()) as mock_run,
    ):
        incus.file_push(_DTU, [str(src)], "/workspace/")

    push_calls = [
        c.args[0]
        for c in mock_run.call_args_list
        if c.args and c.args[0][:3] == ["incus", "file", "push"]
    ]
    pushed_dests = {cmd[-1] for cmd in push_calls}
    assert f"{_DTU}/workspace/mydir/sub/nested.txt" in pushed_dests, pushed_dests


# ===========================================================================
# Section B: empty directory
# ===========================================================================


def test_file_push_empty_directory_creates_remote_dir(tmp_path: Path) -> None:
    """An empty directory source still creates the remote directory."""
    empty = tmp_path / "emptydir"
    empty.mkdir()

    with (
        patch(f"{_INCUS_MOD}.exec_command", return_value=(0, "", "")) as mock_exec,
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()) as mock_run,
    ):
        incus.file_push(_DTU, [str(empty)], "/workspace/")

    # mkdir called for the empty dir.
    mock_exec.assert_any_call(_DTU, ["mkdir", "-p", "/workspace/emptydir"], timeout=120)
    # No file pushes.
    push_calls = [
        c
        for c in mock_run.call_args_list
        if c.args and c.args[0][:3] == ["incus", "file", "push"]
    ]
    assert len(push_calls) == 0, (
        f"Expected no file pushes for an empty directory; got {len(push_calls)}"
    )


# ===========================================================================
# Section C: remote directory permissions
# ===========================================================================


def test_file_push_directory_mkdir_uses_only_p_flag(tmp_path: Path) -> None:
    """mkdir is called with ONLY -p (system default 0755, no extra mode flags).

    Directories should be created with the sensible default permissions (0755
    modified by umask), not with explicit --mode or unusual flags.
    """
    src = tmp_path / "mydir"
    src.mkdir()
    sub = src / "sub"
    sub.mkdir()
    (sub / "file.txt").write_text("x")

    with (
        patch(f"{_INCUS_MOD}.exec_command", return_value=(0, "", "")) as mock_exec,
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()),
    ):
        incus.file_push(_DTU, [str(src)], "/workspace/")

    # exec_command must have been called at least once (root dir + sub dir).
    assert mock_exec.call_args_list, "Expected exec_command to be called for mkdir -p"

    for c in mock_exec.call_args_list:
        cmd = c.args[1]
        assert cmd[0] == "mkdir", f"Expected mkdir; got {cmd}"
        assert cmd[1] == "-p", f"Expected -p flag; got {cmd}"
        assert len(cmd) == 3, (
            f"mkdir should be exactly ['mkdir', '-p', <path>]; got {cmd}"
        )


# ===========================================================================
# Section D: single-file regression — existing behavior unchanged
# ===========================================================================


def test_file_push_single_file_uses_batch_incus_push(tmp_path: Path) -> None:
    """A single file source uses the existing incus file push without recursion."""
    f = tmp_path / "config.yaml"
    f.write_text("key: value")

    with (
        patch(f"{_INCUS_MOD}.exec_command") as mock_exec,
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()) as mock_run,
    ):
        incus.file_push(_DTU, [str(f)], "/workspace/")

    # No mkdir calls — file push only.
    mock_exec.assert_not_called()

    # Exactly one subprocess.run call.
    mock_run.assert_called_once()
    cmd = mock_run.call_args.args[0]
    assert cmd[:3] == ["incus", "file", "push"], f"Expected incus file push; got {cmd}"


def test_file_push_multiple_files_uses_single_batch_call(tmp_path: Path) -> None:
    """Multiple file sources use a single incus file push invocation (batch, no recursion)."""
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("a")
    f2.write_text("b")

    with (
        patch(f"{_INCUS_MOD}.exec_command") as mock_exec,
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()) as mock_run,
    ):
        incus.file_push(_DTU, [str(f1), str(f2)], "/workspace/")

    mock_exec.assert_not_called()
    mock_run.assert_called_once()
    cmd = mock_run.call_args.args[0]
    # Both files in one call.
    assert str(f1) in cmd, f"{f1} not in cmd: {cmd}"
    assert str(f2) in cmd, f"{f2} not in cmd: {cmd}"


# ===========================================================================
# Section E: mixed sources — file + directory in one call
# ===========================================================================


def test_file_push_mixed_sources_handles_file_correctly(tmp_path: Path) -> None:
    """In a mixed push (file + dir), the file is pushed at the correct remote path."""
    readme = tmp_path / "README.md"
    readme.write_text("# Readme")
    greeter = tmp_path / "greeter"
    greeter.mkdir()
    (greeter / "hello.py").write_text("print('hello')")

    with (
        patch(f"{_INCUS_MOD}.exec_command", return_value=(0, "", "")),
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()) as mock_run,
    ):
        incus.file_push(_DTU, [str(readme), str(greeter)], "/workspace/")

    push_calls = [
        c.args[0]
        for c in mock_run.call_args_list
        if c.args and c.args[0][:3] == ["incus", "file", "push"]
    ]
    pushed_dests = {cmd[-1] for cmd in push_calls}

    # README must land at /workspace/README.md
    assert f"{_DTU}/workspace/README.md" in pushed_dests, (
        f"Expected README.md at /workspace/README.md; pushed_dests={pushed_dests}"
    )


def test_file_push_mixed_sources_handles_directory_correctly(tmp_path: Path) -> None:
    """In a mixed push (file + dir), the directory is recursed with name preserved."""
    readme = tmp_path / "README.md"
    readme.write_text("# Readme")
    greeter = tmp_path / "greeter"
    greeter.mkdir()
    (greeter / "hello.py").write_text("print('hello')")

    with (
        patch(f"{_INCUS_MOD}.exec_command", return_value=(0, "", "")) as mock_exec,
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()) as mock_run,
    ):
        incus.file_push(_DTU, [str(readme), str(greeter)], "/workspace/")

    # greeter/ should be created as /workspace/greeter
    mock_exec.assert_any_call(_DTU, ["mkdir", "-p", "/workspace/greeter"], timeout=120)

    push_calls = [
        c.args[0]
        for c in mock_run.call_args_list
        if c.args and c.args[0][:3] == ["incus", "file", "push"]
    ]
    pushed_dests = {cmd[-1] for cmd in push_calls}

    # hello.py must land at /workspace/greeter/hello.py
    assert f"{_DTU}/workspace/greeter/hello.py" in pushed_dests, (
        f"Expected hello.py at /workspace/greeter/hello.py; pushed_dests={pushed_dests}"
    )


# ===========================================================================
# Section F: mode/uid/gid propagation to file pushes
# ===========================================================================


def test_file_push_directory_passes_mode_to_file_push(tmp_path: Path) -> None:
    """The --mode flag is passed to incus file push for each file."""
    src = tmp_path / "mydir"
    src.mkdir()
    (src / "script.sh").write_text("#!/bin/bash")

    with (
        patch(f"{_INCUS_MOD}.exec_command", return_value=(0, "", "")),
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()) as mock_run,
    ):
        incus.file_push(_DTU, [str(src)], "/workspace/", mode="0755")

    push_calls = [
        c.args[0]
        for c in mock_run.call_args_list
        if c.args and c.args[0][:3] == ["incus", "file", "push"]
    ]
    assert len(push_calls) == 1, f"Expected 1 file push; got {len(push_calls)}"
    cmd = push_calls[0]
    assert "--mode" in cmd, f"Expected --mode in cmd: {cmd}"
    mode_idx = cmd.index("--mode")
    assert cmd[mode_idx + 1] == "0755", f"Expected mode 0755; got {cmd[mode_idx + 1]}"


# ===========================================================================
# Section G: error propagation
# ===========================================================================


def test_file_push_directory_raises_on_mkdir_failure(tmp_path: Path) -> None:
    """IncusError is raised if mkdir -p fails for the root directory."""
    src = tmp_path / "mydir"
    src.mkdir()
    (src / "file.txt").write_text("x")

    with (
        patch(f"{_INCUS_MOD}.exec_command", return_value=(1, "", "permission denied")),
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=_ok_run()),
    ):
        with pytest.raises(incus.IncusError, match="permission denied"):
            incus.file_push(_DTU, [str(src)], "/workspace/")


def test_file_push_directory_raises_on_file_push_failure(tmp_path: Path) -> None:
    """IncusError is raised if incus file push fails for a file inside the dir."""
    src = tmp_path / "mydir"
    src.mkdir()
    (src / "file.txt").write_text("x")

    fail_run = MagicMock()
    fail_run.returncode = 1
    fail_run.stderr = "no such container"

    with (
        patch(f"{_INCUS_MOD}.exec_command", return_value=(0, "", "")),
        patch(f"{_INCUS_MOD}.subprocess.run", return_value=fail_run),
    ):
        with pytest.raises(incus.IncusError, match="no such container"):
            incus.file_push(_DTU, [str(src)], "/workspace/")
