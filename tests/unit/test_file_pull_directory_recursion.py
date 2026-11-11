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
  2. Enumerate directories and regular files using NUL-delimited ``find``.
  3. Validate the complete tree and existing destination paths before writing.
  4. Create all directories, including empty descendants; pull files individually.

Symmetric to ``test_file_push_directory_recursion.py`` (PR #18).

No real Incus daemon required — subprocess calls are mocked throughout.

Run with: uv run pytest tests/unit/test_file_pull_directory_recursion.py -v
"""

from __future__ import annotations

import os
import posixpath
import subprocess
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
    files: tuple[str, ...] = (),
) -> object:
    """Return an exec_command side_effect for a single-path scenario.

    - ``test -d`` calls return (0, "", "") when *is_dir* is True, else (1, "", "").
    - ``find`` reports files and their directory ancestors, in the requested
      framing. This models both the predecessor's plain find and ``-print0``.
    - Unexpected commands fail the test.
    """

    def side_effect(name, cmd, **kwargs):
        if cmd and cmd[0] == "test" and "-d" in cmd:
            return (0, "", "") if is_dir else (1, "", "")
        if cmd and cmd[0] == "find":
            return (0, _find_output(cmd, files), "")
        raise AssertionError(f"Unexpected exec: {cmd!r}")

    return side_effect


def _make_exec_side_effect_multi(
    path_is_dir: dict[str, bool], find_map: dict[str, tuple[str, ...]]
) -> object:
    """Return an exec_command side_effect for multi-path scenarios.

    - ``test -d <path>`` → (0, ...) if path_is_dir[path] else (1, ...).
    - ``find`` reports the requested file or directory listing and framing.
    """

    def side_effect(name, cmd, **kwargs):
        if cmd and cmd[0] == "test" and len(cmd) >= 3 and cmd[1] == "-d":
            path = cmd[2]
            return (0, "", "") if path_is_dir.get(path, False) else (1, "", "")
        if cmd and cmd[0] == "find" and len(cmd) >= 2:
            dirpath = cmd[1]
            return (0, _find_output(cmd, find_map.get(dirpath, ())), "")
        raise AssertionError(f"Unexpected exec: {cmd!r}")

    return side_effect


def _find_output(cmd: list[str], files: tuple[str, ...]) -> str:
    """Model find's default output as well as -print0, without losing names."""
    directories = {cmd[1]}
    for filename in files:
        parent = posixpath.dirname(filename)
        while parent != cmd[1]:
            assert parent and parent != "/", "Fixture file must be below its root"
            directories.add(parent)
            parent = posixpath.dirname(parent)
    entries = sorted(directories) if cmd[3] == "d" else files
    separator = "\0" if "-print0" in cmd else "\n"
    return "".join(entry + separator for entry in entries)


# ===========================================================================
# Section A: directory source — recursive walk
# ===========================================================================


def test_file_pull_directory_creates_local_root_dir(tmp_path: Path) -> None:
    """A directory source creates the local root directory."""
    remote_dir = "/root/.amplifier/projects"
    local_parent = str(tmp_path / "output")

    exec_se = _make_exec_side_effect(
        is_dir=True,
        files=("/root/.amplifier/projects/events.jsonl",),
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

    files = (
        "/root/.amplifier/projects/events.jsonl",
        "/root/.amplifier/projects/mount_plan.json",
    )
    exec_se = _make_exec_side_effect(is_dir=True, files=files)

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

    files = (
        "/root/.amplifier/projects/default/events.jsonl",
        "/root/.amplifier/projects/default/mount_plan.json",
    )
    exec_se = _make_exec_side_effect(is_dir=True, files=files)

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

    exec_se = _make_exec_side_effect(
        is_dir=True, files=("/root/.amplifier/projects/a/b/deep.txt",)
    )

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

    exec_se = _make_exec_side_effect(is_dir=True)

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
    find_map = {"/root/.amplifier/projects": ("/root/.amplifier/projects/events.jsonl",)}
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
    find_map = {"/root/.amplifier/projects": ("/root/.amplifier/projects/events.jsonl",)}
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
    assert [
        cmd[4] for cmd in pull_calls if cmd[3] == f"{_DTU}/workspace/README.md"
    ] == [str(Path(local_dir) / "README.md")]


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
        is_dir=True, files=("/root/.amplifier/projects/events.jsonl",)
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


# ===========================================================================
# Section F: complete trees, pathname framing and pre-write safety
# ===========================================================================


def _mock_remote_tree(
    monkeypatch,
    directories: list[str],
    files: dict[str, bytes],
    *,
    directory_sources: tuple[str, ...] = ("/tree",),
    failed_kind: str | None = None,
):
    """Mock only the child process; exercise real exec capture and local writes.

    os.write models subprocess output to the real temporary-file descriptors.
    StringIO or a mocked exec result would conceal universal-newline conversion.
    Both old find and -print0 are modelled so regressions can run on the base.
    """
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["incus", "exec", _DTU]:
            assert kwargs["stdin"] == subprocess.DEVNULL
            command = cmd[4:]
            if command[:2] == ["test", "-d"]:
                path = posixpath.normpath("/" + command[2].lstrip("/"))
                code = 0 if path in directory_sources else 1
                return subprocess.CompletedProcess(cmd, code)
            assert command[0] == "find"
            assert command[2:4] in (["-type", "d"], ["-type", "f"])
            assert command[4:] in ([], ["-print0"])
            root, kind = command[1], command[3]
            entries = directories if kind == "d" else list(files)
            entries = [
                entry for entry in entries
                if entry == root or entry.startswith(root.rstrip("/") + "/")
            ]
            separator = "\0" if "-print0" in command else "\n"
            output = "".join(entry + separator for entry in entries)
            os.write(kwargs["stdout"].fileno(), output.encode("utf-8"))
            if kind == failed_kind:
                os.write(kwargs["stderr"].fileno(), b"permission denied")
                return subprocess.CompletedProcess(cmd, 1)
            return subprocess.CompletedProcess(cmd, 0)
        assert cmd[:3] == ["incus", "file", "pull"]
        assert len(cmd) == 5
        remote = "/" + cmd[3].removeprefix(_DTU + "/")
        assert remote in files, f"Unexpected pull source: {remote!r}"
        Path(cmd[4]).write_bytes(files[remote])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(incus, "_run", run)
    return calls


@pytest.mark.parametrize("recursive", [False, True])
def test_pull_preserves_empty_descendants_and_file_bytes(monkeypatch, tmp_path, recursive):
    directories = ["/tree", "/tree/empty", "/tree/empty/deep", "/tree/populated"]
    payload = b"\x00binary\r\npayload\xff"
    calls = _mock_remote_tree(monkeypatch, directories, {"/tree/populated/data": payload})
    output = tmp_path / "output"

    incus.file_pull(_DTU, ["/tree/"], str(output), recursive=recursive, timeout=47)

    root = output / "tree"
    assert {p.relative_to(root).as_posix() for p in root.rglob("*")} == {
        "empty", "empty/deep", "populated", "populated/data",
    }
    assert (root / "empty/deep").is_dir()
    assert (root / "populated/data").read_bytes() == payload
    assert [cmd[4:] for cmd in calls if cmd[:2] == ["incus", "exec"]][1:] == [
        ["find", "/tree", "-type", "d", "-print0"],
        ["find", "/tree", "-type", "f", "-print0"],
    ]


@pytest.mark.parametrize(
    "component", [" space\t ", "line\nbreak", "carriage\rreturn", "crlf\r\nname", "café-漢"]
)
def test_pull_preserves_pathnames_through_real_capture(monkeypatch, tmp_path, component):
    directories = ["/tree", f"/tree/{component}"]
    remote_file = f"/tree/{component}/{component}"
    _mock_remote_tree(monkeypatch, directories, {remote_file: b"exact payload"})
    output = tmp_path / "output"

    incus.file_pull(_DTU, ["/tree"], str(output))

    root = output / "tree"
    assert {p.relative_to(root).as_posix() for p in root.rglob("*")} == {
        component, f"{component}/{component}",
    }
    assert (root / component / component).read_bytes() == b"exact payload"


def test_exec_command_default_newline_behavior_unchanged(monkeypatch):
    def run(cmd, **kwargs):
        os.write(kwargs["stdout"].fileno(), b"out\r\nnext\rtail\n")
        os.write(kwargs["stderr"].fileno(), b"err\r\nnext\rtail\n")
        return subprocess.CompletedProcess(cmd, 7)

    monkeypatch.setattr(incus, "_run", run)
    assert incus.exec_command(_DTU, ["fixture"]) == (
        7, "out\nnext\ntail\n", "err\nnext\ntail\n"
    )


@pytest.mark.parametrize(
    "source,root",
    [
        ("/tree", "/tree"), ("/tree///", "/tree"), ("tree", "/tree"),
        ("./tree", "/tree"), ("-tree", "/-tree"), ("/", "/"),
    ],
)
def test_pull_source_root_and_basename_semantics(monkeypatch, tmp_path, source, root):
    prefix = root.rstrip("/")
    directories = [root, prefix + "/empty", prefix + "/nested"]
    files = {prefix + "/nested/data": b"nested"}
    _mock_remote_tree(monkeypatch, directories, files, directory_sources=(root,))
    output = tmp_path / "output"

    incus.file_pull(_DTU, [source], str(output))

    local_root = output / posixpath.basename(root)
    assert (local_root / "empty").is_dir()
    assert (local_root / "nested/data").read_bytes() == b"nested"
    assert not (output / "data").exists(), "Pulling / must not flatten descendants"


@pytest.mark.parametrize("source", ["/tree", "/tree/"])
def test_pull_root_symlink_is_not_reported_as_empty_success(monkeypatch, tmp_path, source):
    # test -d follows a root symlink, but plain find does not traverse it.
    calls = _mock_remote_tree(monkeypatch, [], {})
    output = tmp_path / "output"

    with pytest.raises(incus.IncusError):
        incus.file_pull(_DTU, [source], str(output))

    assert not output.exists()
    assert not any(cmd[:3] == ["incus", "file", "pull"] for cmd in calls)


@pytest.mark.parametrize("kind", ["d", "f"])
def test_pull_failed_enumeration_has_no_local_effect(monkeypatch, tmp_path, kind):
    calls = _mock_remote_tree(
        monkeypatch, ["/tree", "/tree/empty"], {"/tree/data": b"data"}, failed_kind=kind
    )
    output = tmp_path / "output"

    with pytest.raises(incus.IncusError, match="permission denied"):
        incus.file_pull(_DTU, ["/tree"], str(output))

    assert not output.exists()
    assert not any(cmd[:3] == ["incus", "file", "pull"] for cmd in calls)


def test_pull_capture_error_has_no_local_effect(tmp_path):
    error = incus.OutputCaptureError(0)

    def exec_se(name, command, **kwargs):
        if command[0] == "find":
            raise error
        assert command == ["test", "-d", "/tree"]
        return 0, "", ""

    output = tmp_path / "output"
    with (
        patch(f"{_INCUS_MOD}.exec_command", side_effect=exec_se),
        patch(f"{_INCUS_MOD}._file_pull_single") as pull,
        pytest.raises(incus.OutputCaptureError) as raised,
    ):
        incus.file_pull(_DTU, ["/tree"], str(output))
    assert raised.value is error
    assert not output.exists()
    pull.assert_not_called()


@pytest.mark.parametrize(
    "directories,files",
    [
        (["/tree"], ["/tree/good", "/other/file"]),
        (["/tree"], ["/tree/good", "/tree-sibling/file"]),
        (["/tree"], ["/tree/good", "/tree/../escaped"]),
        (["/tree"], ["/tree/good", "/tree//absolute"]),
        (["/tree"], ["/tree/good", "/tree/./alias"]),
        (["/tree"], ["/tree/good", "relative/file"]),
        (["/tree", "/tree/../escaped"], []),
        (["/tree", "/tree//absolute"], []),
        (["/tree", "/tree", "/tree/empty"], []),
        (["/tree", "/tree/clash"], ["/tree/clash"]),
        (["/tree"], ["/tree/file", "/tree/file"]),
        (["/tree"], ["/tree"]),
        (["/tree"], ["/tree/missing-parent/file"]),
        (["/tree", "/tree/missing-parent/empty"], []),
    ],
)
def test_pull_rejects_entire_unsafe_manifest_before_writes(tmp_path, directories, files):
    def exec_se(name, command, **kwargs):
        if command[0] == "test":
            return 0, "", ""
        assert command[0] == "find"
        entries = directories if command[3] == "d" else files
        separator = "\0" if "-print0" in command else "\n"
        return 0, "".join(entry + separator for entry in entries), ""

    output = tmp_path / "output"
    with (
        patch(f"{_INCUS_MOD}.exec_command", side_effect=exec_se),
        patch(f"{_INCUS_MOD}._file_pull_single") as pull,
    ):
        with pytest.raises(incus.IncusError):
            incus.file_pull(_DTU, ["/tree"], str(output))
    assert not output.exists()
    pull.assert_not_called()


@pytest.mark.parametrize("output", ["/tree/data", "/tree/data\0\0", "\0"])
def test_pull_requires_complete_nonempty_nul_records(tmp_path, output):
    def exec_se(name, command, **kwargs):
        if command[0] == "test":
            return 0, "", ""
        return 0, "/tree\0" if command[3] == "d" else output, ""

    destination = tmp_path / "output"
    with (
        patch(f"{_INCUS_MOD}.exec_command", side_effect=exec_se),
        patch(f"{_INCUS_MOD}._file_pull_single") as pull,
    ):
        with pytest.raises(incus.IncusError):
            incus.file_pull(_DTU, ["/tree"], str(destination))
    assert not destination.exists()
    pull.assert_not_called()


@pytest.mark.parametrize(
    "conflict",
    ["parent-link", "root-link", "nested-link", "file-link", "dangling-link",
     "parent-file", "root-file", "nested-file", "file-directory"],
)
def test_pull_rejects_existing_destination_links_and_type_conflicts(
    monkeypatch, tmp_path, conflict
):
    output = tmp_path / "output"
    root = output / "tree"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"untouched")
    paths = {
        "parent-link": output, "root-link": root, "nested-link": root / "nested",
        "file-link": root / "nested/data", "dangling-link": root / "nested/data",
        "parent-file": output, "root-file": root, "nested-file": root / "nested",
        "file-directory": root / "nested/data",
    }
    conflict_path = paths[conflict]
    conflict_path.parent.mkdir(parents=True, exist_ok=True)
    if conflict.endswith("-link"):
        target = sentinel if conflict == "file-link" else outside
        if conflict == "dangling-link":
            target = outside / "missing"
        conflict_path.symlink_to(target)
    elif conflict == "file-directory":
        conflict_path.mkdir()
    else:
        conflict_path.write_bytes(b"existing file")
    calls = _mock_remote_tree(
        monkeypatch, ["/tree", "/tree/fresh", "/tree/nested"],
        {"/tree/nested/data": b"new data"},
    )

    with pytest.raises(incus.IncusError):
        incus.file_pull(_DTU, ["/tree"], str(output))

    assert sentinel.read_bytes() == b"untouched"
    assert not (root / "fresh").exists()
    assert not (outside / "missing").exists()
    assert not any(cmd[:3] == ["incus", "file", "pull"] for cmd in calls)


def test_pull_merges_existing_real_directories_and_overwrites_regular_files(monkeypatch, tmp_path):
    root = tmp_path / "tree"
    (root / "nested").mkdir(parents=True)
    (root / "nested/data").write_bytes(b"old")
    (root / "unrelated").write_bytes(b"keep")
    _mock_remote_tree(
        monkeypatch, ["/tree", "/tree/empty", "/tree/nested"],
        {"/tree/nested/data": b"new"},
    )

    incus.file_pull(_DTU, ["/tree"], str(tmp_path))

    assert (root / "nested/data").read_bytes() == b"new"
    assert (root / "unrelated").read_bytes() == b"keep"
    assert (root / "empty").is_dir()


@pytest.mark.parametrize("directory_first", [False, True])
def test_pull_mixed_sources_keeps_files_beside_complete_tree(monkeypatch, tmp_path, directory_first):
    _mock_remote_tree(
        monkeypatch, ["/tree", "/tree/empty"],
        {"/tree/data": b"tree data", "/readme": b"readme"},
    )
    sources = ["/tree", "/readme"] if directory_first else ["/readme", "/tree"]

    incus.file_pull(_DTU, sources, str(tmp_path))

    assert (tmp_path / "readme").read_bytes() == b"readme"
    assert (tmp_path / "tree/data").read_bytes() == b"tree data"
    assert (tmp_path / "tree/empty").is_dir()


def test_pull_mixed_file_does_not_follow_destination_symlink(monkeypatch, tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    output = tmp_path / "output"
    output.mkdir()
    (output / "readme").symlink_to(outside)
    calls = _mock_remote_tree(monkeypatch, ["/tree"], {"/readme": b"replace"})

    with pytest.raises(incus.IncusError):
        incus.file_pull(_DTU, ["/readme", "/tree"], str(output), create_dirs=True)

    assert outside.read_bytes() == b"keep"
    assert not (output / "tree").exists()
    assert not any(cmd[:3] == ["incus", "file", "pull"] for cmd in calls)


@pytest.mark.parametrize("recursive", [False, True])
@pytest.mark.parametrize("create_dirs", [False, True])
def test_all_file_batch_preserves_flags_timeout_and_exact_destination(
    tmp_path, recursive, create_dirs
):
    with (
        patch(f"{_INCUS_MOD}.exec_command", side_effect=_make_exec_side_effect(is_dir=False)) as execute,
        patch(f"{_INCUS_MOD}._run", return_value=_ok_run()) as run,
    ):
        incus.file_pull(
            _DTU, ["/a", "/b"], str(tmp_path), recursive=recursive,
            create_dirs=create_dirs, timeout=59,
        )
    flags = (["--recursive"] if recursive else []) + (["--create-dirs"] if create_dirs else [])
    run.assert_called_once_with(
        ["incus", "file", "pull", *flags, f"{_DTU}/a", f"{_DTU}/b", str(tmp_path)],
        capture_output=True, text=True, timeout=59,
    )
    assert [call.args[1] for call in execute.call_args_list] == [
        ["test", "-d", "/a"], ["test", "-d", "/b"],
    ]
