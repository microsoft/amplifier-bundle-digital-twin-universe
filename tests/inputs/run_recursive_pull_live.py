"""Installed CLI directory roundtrip in the maintained disposable Incus lab.

Compare directory paths and file bytes, not just a populated descendant. Refusal
controls use only owned fixture trees; no hostile concurrent-writer guarantee.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import run_ordinary_capture_live as lab


def cli(*args, check=True):
    executable = Path(sys.executable).parent / "amplifier-digital-twin"
    result = subprocess.run([str(executable), *args], cwd=lab.PRIVATE,
                            capture_output=True, text=True, timeout=180, check=False)
    path = lab.OUT / "cli.json"
    rows = json.loads(path.read_text()) if path.exists() else []
    rows.append({"args": args, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
    lab.save("cli.json", rows)
    if check:
        assert result.returncode == 0, rows[-1]
    return result


def tree(root):
    """Small owned fixture snapshot; never read through symlinks."""
    if not root.exists() and not root.is_symlink():
        return None
    if root.is_symlink():
        return {".": {"link": os.readlink(root)}}
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        mode = path.lstat().st_mode
        key = path.relative_to(root).as_posix()
        if stat.S_ISLNK(mode):
            result[key] = {"link": os.readlink(path)}
        elif stat.S_ISDIR(mode):
            result[key] = {"directory": True}
        else:
            assert stat.S_ISREG(mode)
            raw = path.read_bytes()
            result[key] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    return result


def run():
    lab.verify_source()
    image = lab.setup()
    name, request = f"{lab.PROJECT}-pull", f"{lab.OWNER}-pull"
    ownership = json.loads((lab.PRIVATE / "ownership.json").read_text())
    ownership["instances"].append({"name": name, "request": request})
    (lab.PRIVATE / "ownership.json").write_text(json.dumps(ownership))
    profile = lab.PRIVATE / "pull-profile.json"
    profile.write_text(json.dumps({"name": "pull-tree", "base": {"image": image, "config": {
        "security.nesting": "false", "security.privileged": "false", "limits.cpu": "1", "limits.memory": "256MiB"}}}))
    launched = json.loads(cli("launch", str(profile), "--name", name, "--owner-root", lab.OWNER,
                             "--request-id", request, "--offline").stdout)
    assert launched["id"] == name
    found = next(r for r in lab.inventory() if r["name"] == name)
    assert found["config"]["user.dtu.request-id"] == request and found["config"]["user.dtu.owner-root"] == lab.OWNER
    lab.save("launched-instance.json", found)

    source = lab.PRIVATE / "tree"
    source.mkdir()
    (source / "empty" / "nested").mkdir(parents=True)
    (source / "full").mkdir()
    (source / "full" / "binary.bin").write_bytes(bytes(range(256)))
    (source / "zero").write_bytes(b"")
    names = [" leading", "trailing ", " ", "line\nbreak", "line\rbreak", "line\r\nbreak", "雪-é", "\u2028", "-option;$name"]
    for index, component in enumerate(names):
        (source / component).mkdir()
        (source / component / "empty child").mkdir()
        (source / component / f"{component}.dat").write_bytes(f"payload-{index}".encode())
    expected = tree(source)
    lab.save("expected-tree.json", expected)
    cli("file-push", "-r", name, str(source), "/root/")
    # Independent native byte framing retains CR/LF; no JSON/text pathname split.
    dirs = lab.command(["exec", name, "--", "find", "/root/tree", "-type", "d", "-print0"]).stdout
    files = lab.command(["exec", name, "--", "find", "/root/tree", "-type", "f", "-print0"]).stdout
    remote_dirs = {v.decode().removeprefix("/root/tree").lstrip("/") or "." for v in dirs.split(b"\0") if v}
    remote_files = {v.decode().removeprefix("/root/tree/") for v in files.split(b"\0") if v}
    assert remote_dirs == {n for n, v in expected.items() if "directory" in v}
    assert remote_files == {n for n, v in expected.items() if "sha256" in v}
    lab.save("native-remote-inventory.json", {"directories": sorted(remote_dirs), "files": sorted(remote_files)})
    # Existing semantics skip descendant links/special files; never follow them.
    lab.command(["exec", name, "--", "ln", "-s", "/etc/passwd", "/root/tree/ignored-link"])
    lab.command(["exec", name, "--", "mkfifo", "/root/tree/ignored-fifo"])
    outputs = {}
    for key, remote in (("absolute", "/root/tree/"), ("root-relative", "root/tree")):
        dest = lab.PRIVATE / key
        cli("file-pull", "-r", name, remote, str(dest))
        outputs[key] = tree(dest / "tree")
        assert outputs[key] == expected
    # An alias above the selected parent is legitimate caller ancestry; the
    # selected parent and remotely derived components must still be protected.
    real = lab.PRIVATE / "real-ancestry"
    real.mkdir()
    alias = lab.PRIVATE / "ancestry-alias"
    alias.symlink_to(real, target_is_directory=True)
    cli("file-pull", "-r", name, "/root/tree", str(alias / "selected"))
    outputs["ancestry-alias"] = tree(real / "selected" / "tree")
    assert outputs["ancestry-alias"] == expected
    loose = lab.PRIVATE / "loose.txt"
    loose.write_bytes(b"standalone\x00data\n")
    cli("file-push", name, str(loose), "/root/loose.txt")
    for index, sources in enumerate((("/root/tree", "/root/loose.txt"), ("/root/loose.txt", "/root/tree"))):
        dest = lab.PRIVATE / f"mixed-{index}"
        cli("file-pull", name, *sources, str(dest))
        outputs[f"mixed-{index}"] = tree(dest / "tree")
        assert outputs[f"mixed-{index}"] == expected
        assert (dest / "loose.txt").read_bytes() == loose.read_bytes()
    # Ordinary all-file batching remains the native client path.
    batch = lab.PRIVATE / "batch"
    batch.mkdir()
    cli("file-pull", name, "/root/loose.txt", "/root/tree/zero", str(batch))
    assert (batch / "loose.txt").read_bytes() == loose.read_bytes() and (batch / "zero").read_bytes() == b""
    lab.save("actual-trees.json", outputs)

    refusals = []
    outside = lab.PRIVATE / "outside"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"UNCHANGED")
    for case in ("selected-parent", "copied-root", "intermediate", "leaf", "type-conflict"):
        dest = lab.PRIVATE / f"refuse-{case}"
        if case == "selected-parent":
            dest.symlink_to(outside, target_is_directory=True)
        elif case == "copied-root":
            dest.mkdir()
            (dest / "tree").symlink_to(outside, target_is_directory=True)
        elif case == "intermediate":
            (dest / "tree").mkdir(parents=True)
            (dest / "tree" / "empty").symlink_to(outside, target_is_directory=True)
        elif case == "leaf":
            (dest / "tree").mkdir(parents=True)
            (dest / "tree" / "zero").symlink_to(outside / "sentinel")
        else:
            (dest / "tree" / "zero").mkdir(parents=True)
        before, outside_before = tree(dest), tree(outside)
        result = cli("file-pull", "-r", name, "/root/tree", str(dest), check=False)
        assert result.returncode != 0 and "Unsafe pull destination" in result.stderr
        assert tree(dest) == before and tree(outside) == outside_before
        refusals.append({"case": case, "refused": True, "unchanged": True})
    lab.command(["exec", name, "--", "ln", "-s", "/root/tree", "/root/tree-link"])
    dest = lab.PRIVATE / "source-link"
    result = cli("file-pull", "-r", name, "/root/tree-link", str(dest), check=False)
    assert result.returncode != 0 and "symlink roots are unsupported" in result.stderr and not dest.exists()
    refusals.append({"case": "source-root-symlink", "refused": True, "unchanged": True})
    lab.save("refusals.json", refusals)
    lab.save("qualification.json", {"passed": True, "directories": len(remote_dirs), "files": len(remote_files),
        "successful_tree_copies": 5, "full_inventory_and_content_match": True, "refusals": refusals,
        "scope": "Actual installed CLI/real Incus; LF/CR/CRLF and decodable Unicode preserved; existing symlinks/type conflicts rejected. No concurrent-tree or physical macOS claim.",
        "cleanup": "Separate explicit owned lab teardown after saved evidence; not a destroy-product test"})


if __name__ == "__main__":
    lab.run = run
    lab.main()
