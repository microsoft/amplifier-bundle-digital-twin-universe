"""Filesystem-only Incus; parent stages bin/incus with an owned Python shebang.
Fresh private ROOT is the staged script's grandparent. state.json contains one
instance/push receipts; guests/ contains inert bytes (single-file, <=1 MiB).
launch-count/provision-count append lines; watch entered, create release in 10s.
trace.jsonl is classifications + PID/PPID only (64 KiB cap, fails closed).
Mode/uid/gid/chmod are inert. Parent owns source/interpreter receipts and audit.
Python guard is not an OS sandbox against hostile concurrent writers.
"""
import sys

sys.dont_write_bytecode = True
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import time
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "a" * 64
MANAGED = "user.dtu.managed-by=amplifier-digital-twin"
CAP = 262144
CHMOD = {"chmod +x /etc/profile.d/dtu-env.sh",
         "chmod +x /etc/profile.d/dtu-visual-id.sh"}

def require(value):
    if not value:
        raise RuntimeError("fixture rejected operation or state")

def denied(*args, **kwargs):
    raise RuntimeError("fixture denied effect")

def owned(value):
    require(isinstance(value, (str, bytes, os.PathLike)))
    path = Path(os.fsdecode(value))
    require(path.is_absolute() and path.resolve().is_relative_to(ROOT))
    require(not path.is_symlink())
    if path.exists():
        info = path.stat()
        require(stat.S_ISDIR(info.st_mode) or
                (stat.S_ISREG(info.st_mode) and info.st_nlink == 1))

def audit(event, args):
    if event.startswith(("subprocess.", "socket.", "signal.", "ctypes.")):
        denied()
    if event == "open":
        path, mode, flags = args
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            owned(path)
    elif event == "os.mkdir":
        owned(args[0])
        require(args[2] in (-1, None))
    elif event == "os.rename":
        owned(args[0])
        owned(args[1])
        require(all(fd in (-1, None) for fd in args[2:]))
    elif event.startswith("os.") and event not in {"os.listdir", "os.scandir"}:
        denied()  # Includes fork/exec/spawn/system/kill, links and other mutations.

def append(name, text):
    path = ROOT / name
    require((path.stat().st_size if path.exists() else 0) + len(text.encode()) <= 65536)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())

def trace(kind):
    append("trace.jsonl", json.dumps({"kind": kind, "pid": os.getpid(),
                                      "ppid": os.getppid()}) + "\n")

@contextmanager
def locked():
    with (ROOT / "state.lock").open("a") as stream:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(time.monotonic() < deadline)
                time.sleep(0.01)
        yield

def instance(state, name):
    require(re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,62}", name))
    row = state["instance"]
    require(row is not None and row["name"] == name)
    return row

def pair(value):
    key, sep, text = value.partition("=")
    require(sep and re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", key))
    return key, text

def push(args, state):
    options = {}
    while args and args[0].startswith("--"):
        flag = args.pop(0)
        require(flag in {"--create-dirs", "--mode", "--uid", "--gid"} and flag not in options)
        if flag == "--create-dirs":
            options[flag] = True
        else:
            require(args and re.fullmatch(r"[0-7]{3,4}" if flag == "--mode" else r"[0-9]{1,10}", args[0]))
            options[flag] = args.pop(0)
    require(len(args) == 2 and not args[0].startswith("-") and len(state["pushes"]) < 64)
    name, sep, guest = args[1].partition("/")
    require(sep and all(p not in {"", ".", ".."} for p in guest.split("/")))
    require(instance(state, name)["status"] == "Running")
    target = ROOT / "guests" / name / guest
    owned(target)
    require(stat.S_ISREG(os.stat(args[0], follow_symlinks=False).st_mode))
    fd = os.open(args[0], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        require(stat.S_ISREG(os.fstat(source.fileno()).st_mode))
        data = source.read(1048577)
    require(len(data) <= 1048576)
    if options.get("--create-dirs"):
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    state["pushes"].append({"guest": guest, "sha256": hashlib.sha256(data).hexdigest(),
                            "bytes": len(data), "options": options})

def dispatch(a, state):
    if a == ["version"]:
        return "Client version: fixture\nServer version: fixture", False
    if len(a) == 2 and a[0] == "query":
        replies = {
            "/1.0": {"environment": {"certificate_fingerprint": "b" * 64,
                                     "project": "default"}, "api_extensions": []},
            "/1.0/profiles/default": {"devices": {"root": {"type": "disk", "path": "/", "pool": "pool"}}},
            "/1.0/storage-pools/pool/resources": {"space": {"total": 2**40, "used": 0}},
        }
        return replies[a[1].removesuffix("?project=default")], False
    if a == ["image", "list", "local:", IMAGE, "--format=json"]:
        return [{"fingerprint": IMAGE}], False
    if len(a) == 2 and a[0] == "info":
        instance(state, a[1])
        return "", False
    if len(a) == 3 and a[0] == "list" and a[2] == "--format=json":
        require(a[1] == MANAGED or re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,62}", a[1]))
        row = state["instance"]
        matches = row and (row["config"].get("user.dtu.managed-by") == "amplifier-digital-twin"
                          if a[1] == MANAGED else row["name"] == a[1])
        return [row] if matches else [], False
    if len(a) >= 6 and a[:2] == ["launch", "local:" + IMAGE]:
        require(a[3:6] == ["--no-profiles", "--storage", "pool"] and (len(a) - 6) % 2 == 0)
        require(state["instance"] is None and re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,62}", a[2]))
        config = {}
        for i in range(6, len(a), 2):
            require(a[i] == "--config")
            key, value = pair(a[i + 1])
            require(key not in config)
            config[key] = value
        (ROOT / "guests" / a[2] / "etc/profile.d").mkdir(parents=True, exist_ok=True)
        state["instance"] = {"name": a[2], "status": "Running", "config": config}
        append("launch-count", "1\n")
        return "", False
    if len(a) == 4 and a[:2] in (["config", "set"], ["config", "get"]):
        row = instance(state, a[2])
        if a[1] == "set":
            key, value = pair(a[3])
            row["config"][key] = value
            return "", False
        require(re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", a[3]))
        return row["config"].get(a[3], ""), False
    if a[:2] == ["file", "push"]:
        push(a[2:], state)
        return "", False
    if len(a) == 6 and a[0] == "exec" and a[2:5] == ["--", "bash", "-lc"]:
        require(instance(state, a[1])["status"] == "Running")
        if a[5] in CHMOD:
            require((ROOT / "guests" / a[1] / a[5].split()[-1].lstrip("/")).is_file())
            return "", False  # Never invoke chmod or execute the pushed bytes.
        require(a[5] == "collector-fixture-provision")
        append("provision-count", "1\n")
        (ROOT / "entered").write_text("1\n", encoding="utf-8")
        return "", True
    if (len(a) == 2 and a[0] in {"stop", "delete"}) or (
            len(a) == 3 and a[0] == "delete" and a[2] == "--force"):
        row = instance(state, a[1])
        if a[0] == "stop":
            row["status"] = "Stopped"
        else:
            state["instance"] = None  # Retain guest files and receipts as evidence.
        return "", False
    denied()

def main():
    require(Path(__file__).resolve() == ROOT / "bin/incus")
    os.umask(0o077)
    sys.addaudithook(audit)
    for name in ("raise_signal", "pthread_kill", "pidfd_send_signal", "alarm",
                 "setitimer", "signal", "siginterrupt", "pthread_sigmask"):
        if hasattr(signal, name):
            setattr(signal, name, denied)
    with locked():
        try:
            a = sys.argv[1:]
            if a[:2] == ["--project", "default"]:
                a = a[2:]
            require(a and len(a) <= 256 and sum(len(s.encode()) for s in a) <= 65536)
            kind = ".".join(a[:2]) if a[:2] in (
                ["file", "push"], ["config", "set"], ["config", "get"], ["image", "list"]) else a[0]
            trace(kind if kind in {"query", "version", "info", "list", "launch", "stop",
                                  "delete", "exec", "file.push", "config.set", "config.get", "image.list"} else "unlisted")
            path = ROOT / "state.json"
            owned(path)
            require(not path.exists() or path.stat().st_size <= CAP)
            before = path.read_text(encoding="utf-8") if path.exists() else None
            state = json.loads(before) if before is not None else {"instance": None, "pushes": []}
            require(isinstance(state, dict) and set(state) == {"instance", "pushes"})
            result, wait = dispatch(a, state)
            after = json.dumps(state, sort_keys=True)
            require(len(after.encode()) <= CAP)
            if after != before:
                temporary = ROOT / "state.tmp"
                with temporary.open("w", encoding="utf-8") as stream:
                    stream.write(after)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(path)
        except Exception:
            trace("rejected")
            raise
    if wait:
        deadline = time.monotonic() + 10
        release = ROOT / "release"
        while time.monotonic() < deadline:
            owned(release)
            if release.is_file():
                break
            time.sleep(0.02)
        else:
            with locked():
                trace("provision.timeout")
            return 124
        with locked():
            trace("provision.released")
    print(result if isinstance(result, str) else json.dumps(result))
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("fake-incus: rejected operation, state, or effect", file=sys.stderr)
        sys.exit(2)
