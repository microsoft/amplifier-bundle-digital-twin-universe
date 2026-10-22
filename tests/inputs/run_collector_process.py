"""Guest-only controlled observer loss. No Incus or production dispatcher proof.

Exactly two supervisor-owned children, no descendants, sockets or broad signal
permission. Fresh interpreters install their own guard before candidate imports.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import time

ROOT = Path("/workspace")
SCRIPT = ROOT / "tests/inputs/run_collector_process.py"
MANIFEST = ROOT / "tests/inputs/dtu001-selection.json"
OUTPUT = Path("/artifacts/dtu001-collector-process")
DENY = {"subprocess.Popen", "os.system", "os.posix_spawn", "os.fork", "os.forkpty", "os.exec",
        "socket.__new__", "socket.connect", "socket.connect_ex", "socket.bind", "socket.getaddrinfo",
        "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo", "socket.sendto",
        "socket.sendmsg", "os.kill", "os.killpg", "signal.pthread_kill"}
BINDING = dict(request_id="synthetic-collector-process", owner_root="synthetic-owner",
               name="synthetic-instance", intent_digest="a" * 64, daemon="b" * 64,
               project="synthetic", storage_pool="synthetic", destination_digest="c" * 64)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message="Fixture invariant failed"):
    if not condition:
        raise RuntimeError(message)


def inputs():
    if sys.flags.optimize:
        raise RuntimeError("Optimized fixture execution is unsupported")
    sys.dont_write_bytecode = True
    if Path.cwd().resolve() != ROOT or Path(__file__).resolve() != SCRIPT:
        raise RuntimeError("Only the managed frozen workspace is supported")
    manifest = json.loads(MANIFEST.read_text())
    plan = json.loads(Path("/opt/unified-prepared/plan.json").read_text())
    inventory = json.loads(Path("/opt/unified-prepared/inventory.json").read_text())
    require(plan["profileId"] == inventory["profileId"] == "python-project")
    require(plan["sourceSha256"] == inventory["sourceSha256"])
    for name, expected in manifest["sourceFiles"].items():
        require(digest(ROOT / name) == expected, "source input changed")
    locks = {row["path"]: row["sha256"] for row in inventory["lockfiles"]}
    require(all(locks.get(n) == digest(ROOT / n) for n in ("uv.lock", "pyproject.toml")))
    require("tests/inputs/run_collector_process.py" in manifest["sourceFiles"])
    return manifest, plan


class Policy:
    def __init__(self, emit=None):
        self.permit = None
        self.allowed = []
        self.blocked = []
        self.controls = []
        self.checking = False
        self.emit = emit
        sys.addaudithook(self.audit)
        for name in ("raise_signal", "pthread_kill", "pidfd_send_signal"):
            if hasattr(signal, name):
                setattr(signal, name, self.signal_denied)

    def signal_denied(self, *args, **kwargs):
        self.audit("signal.delivery", ())

    def deny(self, event):
        if self.checking:
            self.controls.append(event)
            raise RuntimeError("Expected synthetic denial")
        if len(self.blocked) < 128:
            self.blocked.append(event)
        if self.emit:
            self.emit("denied")
        raise RuntimeError("Forbidden fixture effect: " + event)

    def own_path(self, path):
        if not isinstance(path, (str, bytes, os.PathLike)):
            return False
        return Path(os.fsdecode(path)).resolve().is_relative_to(OUTPUT)

    def probe_denials(self):
        self.checking = True
        try:
            for event in ("os.kill", "subprocess.Popen", "socket.__new__"):
                try:
                    sys.audit(event)
                except RuntimeError:
                    pass
                else:
                    raise RuntimeError("Missing audit denial")
        finally:
            self.checking = False
        require(self.controls == ["os.kill", "subprocess.Popen", "socket.__new__"])

    def audit(self, event, args):
        if event in DENY or event == "signal.delivery":
            if self.permit is not None and (event, args) == self.permit:
                self.permit = None
                self.allowed.append(event)
                return
            self.deny(event)
        # Frozen input cannot be rewritten by reviewed fixture/candidate code.
        if event == "open":
            path, mode, flags = args
            writing = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            if writing and isinstance(path, (str, bytes, os.PathLike)):
                if not self.own_path(path):
                    self.deny("filesystem.open")
        if event in {"os.rename", "os.link", "os.symlink"}:
            if event != "os.rename" or not all(self.own_path(p) for p in args[:2]):
                self.deny("filesystem." + event)
            if any(fd not in (-1, None) for fd in args[2:]):
                self.deny("filesystem.relative_fd")
        if event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.truncate", "os.utime"}:
            if not self.own_path(args[0]):
                self.deny("filesystem." + event)


def receive(fd, expected, deadline):
    ready, _, _ = select.select([fd], [], [], max(0, deadline - time.monotonic()))
    if not ready or os.read(fd, 64) != expected:
        raise RuntimeError("Missing or invalid bounded control message")


def child(role, control_fd):
    sequence = 0

    def emit(event):
        nonlocal sequence
        sequence += 1
        print(json.dumps({"seq": sequence, "event": event}), flush=True)

    policy = Policy(emit)
    policy.probe_denials()
    require(stat.S_ISFIFO(os.fstat(control_fd).st_mode), "Control FD is not an owned pipe")
    sys.path.insert(0, str(ROOT / "src"))
    from amplifier_bundle_digital_twin_universe import admission, collector
    proof = {"role": role, "executable": sys.executable, "python": sys.version,
             "interpreterSha256": digest(Path(sys.executable)),
             "isolated": sys.flags.isolated, "noSite": sys.flags.no_site,
             "controls": policy.controls, "blocked": policy.blocked,
             "moduleOrigins": [admission.__file__, collector.__file__]}
    (OUTPUT / (role + "-proof.json")).write_text(json.dumps(proof))
    emit("guard_ready")
    store = collector.AttemptStore(OUTPUT / "admission", BINDING)
    ref = (OUTPUT / "generation").read_text()
    deadline = time.monotonic() + 20
    if role == "foreground":
        require(store.observe()["phase"] == "submitting")
        emit("observing")
        receive(control_fd, b"finish\n", deadline)
    else:
        def reserve():
            with (OUTPUT / "reserve-count").open("a") as stream:
                stream.write("1\n")
            receipt = {k: BINDING[k] for k in ("request_id", "owner_root", "name", "intent_digest")}
            receipt.update(state="launching", created_at="synthetic-time")
            admission.save_receipt(store.root, receipt)

        def execute():
            # Count entries independently of any later effect/file replacement.
            with (OUTPUT / "effect-count").open("a") as stream:
                stream.write("1\n")
            emit("submitted")
            receive(control_fd, b"finish\n", deadline)
            receipt = admission.read_receipt(store.root, BINDING["request_id"])
            result = {k: receipt[k] for k in ("request_id", "name", "owner_root", "created_at")}
            result.update(id=receipt["name"], status="running", info=[])
            receipt.update(state="ready", result=result)
            admission.save_receipt(store.root, receipt)
            return result

        store.run_once(ref, reserve, execute)
        emit("completed")
    os.close(control_fd)
    require(not policy.blocked)
    (OUTPUT / (role + "-proof.json")).write_text(json.dumps(proof))
    emit("clean_exit")


def process_identity(pid):
    text = Path(f"/proc/{pid}/stat").read_text()
    fields = text[text.rfind(")") + 2:].split()
    return {"pid": pid, "start": fields[19], "session": int(fields[3]),
            "uid": Path(f"/proc/{pid}").stat().st_uid}


class Children:
    def __init__(self, policy):
        self.policy = policy
        self.rows = {}
        self.attempted = set()

    def start(self, role):
        if role not in {"foreground", "collector"} or role in self.attempted:
            raise RuntimeError("No replacement child is permitted")
        self.attempted.add(role)
        read_fd = write_fd = null_fd = None
        try:
            read_fd, write_fd = os.pipe()
            null_fd = os.open("/dev/null", os.O_RDONLY)
            argv = [sys.executable, "-I", "-S", "-B", str(SCRIPT), role, str(read_fd)]
            env = {"LANG": "C.UTF-8", "HOME": str(OUTPUT), "PATH": "/usr/bin:/bin"}
            self.policy.permit = ("subprocess.Popen", (sys.executable, argv, str(ROOT), env))
            process = subprocess.Popen(argv, cwd=str(ROOT), env=env, stdin=null_fd,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       shell=False, close_fds=True, start_new_session=True,
                                       preexec_fn=None, pass_fds=(read_fd,))
            # Register before closing even a local descriptor can fail.
            self.rows[role] = dict(process=process, control=write_fd, identity=None,
                                   buffers={}, events=[], bytes=0)
            write_fd = None  # Ownership transferred to the registered row.
        finally:
            self.policy.permit = None
            errors = []
            for fd in (read_fd, write_fd, null_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError as exc:
                        errors.append(type(exc).__name__)
            if errors:
                raise RuntimeError("Owned launch descriptor cleanup failed")
        row = self.rows[role]
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            row["buffers"][stream.fileno()] = b""
        row["identity"] = process_identity(process.pid)
        require(row["identity"]["session"] == process.pid)
        require(row["identity"]["uid"] == os.getuid())

    def pump(self, deadline):
        streams = []
        for row in self.rows.values():
            for stream in (row["process"].stdout, row["process"].stderr):
                if not stream.closed:
                    streams.append(stream.fileno())
        if not streams:
            return
        ready, _, _ = select.select(streams, [], [], max(0, min(0.1, deadline - time.monotonic())))
        for fd in ready:
            for role, row in self.rows.items():
                if fd not in row["buffers"]:
                    continue
                chunk = os.read(fd, 4096)
                if not chunk:
                    require(not row["buffers"][fd], "Truncated child frame")
                    for stream in (row["process"].stdout, row["process"].stderr):
                        if not stream.closed and stream.fileno() == fd:
                            stream.close()
                    continue
                row["bytes"] += len(chunk)
                if row["bytes"] > 65536:
                    raise RuntimeError("Child output limit")
                if not row["process"].stderr.closed and row["process"].stderr.fileno() == fd:
                    raise RuntimeError("Unexpected child stderr")
                row["buffers"][fd] += chunk
                while b"\n" in row["buffers"][fd]:
                    line, row["buffers"][fd] = row["buffers"][fd].split(b"\n", 1)
                    if len(line) > 4096 or len(row["events"]) >= 16:
                        raise RuntimeError("Child frame limit")
                    event = json.loads(line)
                    require(isinstance(event, dict) and set(event) == {"seq", "event"})
                    require(type(event["seq"]) is int and event["seq"] == len(row["events"]) + 1)
                    if event["event"] == "denied":
                        row["events"].append("denied")
                        raise RuntimeError("Child audit denial")
                    expected = (["guard_ready", "observing", "clean_exit"] if role == "foreground"
                                else ["guard_ready", "submitted", "completed", "clean_exit"])
                    require(len(row["events"]) < len(expected))
                    require(event["event"] == expected[len(row["events"])], "Unexpected frame order")
                    row["events"].append(event["event"])
                if len(row["buffers"][fd]) > 4096:
                    raise RuntimeError("Child partial frame limit")

    def until(self, role, event, deadline):
        while event not in self.rows[role]["events"]:
            if time.monotonic() >= deadline:
                raise TimeoutError("Fixture deadline")
            self.pump(deadline)
            if self.rows[role]["process"].poll() is not None and event not in self.rows[role]["events"]:
                self.pump(deadline)
                if event not in self.rows[role]["events"]:
                    raise RuntimeError("Child exited before required event")

    def drain(self, deadline):
        while any(not stream.closed for row in self.rows.values()
                  for stream in (row["process"].stdout, row["process"].stderr)):
            if time.monotonic() >= deadline:
                raise TimeoutError("Child output did not reach EOF")
            self.pump(deadline)

    def terminate(self, role, sig):
        row = self.rows[role]
        process = row["process"]
        if process.poll() is not None:
            return
        if sig not in {signal.SIGTERM, signal.SIGKILL} or process.pid <= 0:
            raise RuntimeError("Invalid exact-child signal")
        if row["identity"] is not None and process_identity(process.pid) != row["identity"]:
            raise RuntimeError("Child identity changed")
        self.policy.permit = ("os.kill", (process.pid, sig))
        try:
            os.kill(process.pid, sig)
        finally:
            self.policy.permit = None

    def cleanup(self):
        deadline = time.monotonic() + 5
        errors = []
        for role, row in self.rows.items():
            try:
                os.close(row["control"])
            except OSError as exc:
                errors.append(type(exc).__name__)
            try:
                self.terminate(role, signal.SIGTERM)
            except (OSError, RuntimeError) as exc:
                errors.append(type(exc).__name__)
        result = []
        for role, row in self.rows.items():
            process = row["process"]
            try:
                try:
                    process.wait(timeout=max(0.01, min(1, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    self.terminate(role, signal.SIGKILL)
                    process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                errors.append(type(exc).__name__)
            for stream in (process.stdout, process.stderr):
                try:
                    if not stream.closed:
                        stream.close()
                except OSError as exc:
                    errors.append(type(exc).__name__)
            original_absent = False
            try:
                original_absent = not Path(f"/proc/{process.pid}").exists()
                if not original_absent and row["identity"] is not None:
                    original_absent = process_identity(process.pid) != row["identity"]
            except OSError as exc:
                errors.append(type(exc).__name__)
            result.append(dict(role=role, identity=row["identity"], returncode=process.returncode,
                               reaped=process.returncode is not None, originalAbsent=original_absent,
                               events=row["events"], bytes=row["bytes"]))
        return result, errors


def supervisor(manifest, plan):
    OUTPUT.mkdir(mode=0o700, exist_ok=False)
    (OUTPUT / "admission").mkdir(mode=0o700)
    policy = Policy()
    policy.probe_denials()
    children = Children(policy)
    report = {"status": "incomplete", "scope": "synthetic_sibling_process_observer_loss_only",
              "realIncusAcceptance": False, "productionDispatcherQualified": False,
              "preparedSourceSha256": plan["sourceSha256"], "sourceFiles": manifest["sourceFiles"],
              "interpreter": sys.executable, "interpreterSha256": digest(Path(sys.executable)),
              "manifestSha256": digest(MANIFEST),
              "inventorySha256": digest(Path("/opt/unified-prepared/inventory.json")),
              "allowed": policy.allowed, "blocked": policy.blocked, "cleanup": [],
              "denialControls": policy.controls}
    code = 1
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from amplifier_bundle_digital_twin_universe import admission, collector
        store = collector.AttemptStore(OUTPUT / "admission", BINDING)
        generation = store.prepare()
        (OUTPUT / "generation").write_text(generation)
        store.dispatch_once(generation, lambda _: None)
        forbidden = []
        try:
            store.run_once("0" * 32, lambda: forbidden.append("reserve"),
                           lambda: forbidden.append("execute"))
        except admission.AdmissionError as exc:
            require(exc.code == "collector_conflict")
        else:
            raise RuntimeError("Stale dispatched generation accepted")
        require(not forbidden)
        deadline = time.monotonic() + 20
        children.start("collector")
        children.until("collector", "submitted", deadline)
        children.start("foreground")
        children.until("foreground", "observing", deadline)
        try:
            with store.mutation_guard(generation):
                raise AssertionError("Mutation incorrectly allowed")
        except admission.AdmissionError as exc:
            require(exc.code == "admission_busy", "Wrong custody refusal")
            report["busyMutationRefused"] = True
        before = store.path.read_bytes()
        children.terminate("foreground", signal.SIGTERM)
        foreground = children.rows["foreground"]["process"]
        foreground_exit = foreground.wait(timeout=max(0.01, deadline - time.monotonic()))
        require(foreground_exit == -signal.SIGTERM)
        require(store.path.read_bytes() == before)
        os.write(children.rows["collector"]["control"], b"finish\n")
        children.until("collector", "clean_exit", deadline)
        children.drain(deadline)
        collector_exit = children.rows["collector"]["process"].wait(timeout=max(0.01, deadline - time.monotonic()))
        require(collector_exit == 0)
        observed = collector.AttemptStore(OUTPUT / "admission", BINDING).observe()
        require(observed["phase"] == "completed" and not observed["evidence_gap"])
        expected = {k: BINDING[k] for k in ("request_id", "owner_root", "name")}
        expected.update(created_at="synthetic-time", id=BINDING["name"], status="running", info=[])
        require(observed["result"] == expected)
        require(admission.read_receipt(store.root, BINDING["request_id"])["result"] == expected)
        for operation in (lambda: store.dispatch_once(generation, forbidden.append),
                          lambda: store.run_once(generation, lambda: forbidden.append("reserve"),
                                                 lambda: forbidden.append("execute")),
                          lambda: store.run_once("0" * 32, lambda: forbidden.append("reserve"),
                                                 lambda: forbidden.append("execute"))):
            try:
                operation()
            except admission.AdmissionError:
                pass
            else:
                raise AssertionError("Replay was accepted")
        require(not forbidden)
        require((OUTPUT / "effect-count").read_text() == "1\n")
        require((OUTPUT / "reserve-count").read_text() == "1\n")
        require(time.monotonic() <= deadline, "Scenario exceeded its acceptance budget")
        report["childProofs"] = [json.loads((OUTPUT / (role + "-proof.json")).read_text())
                                for role in ("foreground", "collector")]
        require(all(not p["blocked"] and p["isolated"] == 1 and p["noSite"] == 1
                    for p in report["childProofs"]))
        report.update(status="passed", effectCount=1, zeroReplay=True, result=observed["result"])
        code = 0
    except BaseException as exc:
        report["errorType"] = type(exc).__name__
    finally:
        try:
            report["cleanup"], report["cleanupErrors"] = children.cleanup()
        except BaseException as exc:
            report["cleanupError"] = type(exc).__name__
            code = 1
        if (len(report["cleanup"]) != 2 or any(not r["originalAbsent"] or not r["reaped"] for r in report["cleanup"])
                or policy.blocked or report.get("cleanupErrors")):
            code = 1
        changed = []
        for name, expected in manifest["sourceFiles"].items():
            try:
                if digest(ROOT / name) != expected:
                    changed.append(name)
            except OSError:
                changed.append(name)
        report["changedSourceFiles"] = changed
        if changed:
            code = 1
        if code:
            report["status"] = "failed"
        report["exitCode"] = code
        (OUTPUT / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
    return code


if __name__ == "__main__":
    manifest, plan = inputs()
    if len(sys.argv) == 1:
        raise SystemExit(supervisor(manifest, plan))
    if len(sys.argv) != 3 or sys.argv[1] not in {"foreground", "collector"} or not sys.argv[2].isdigit():
        raise RuntimeError("Unsupported fixture invocation")
    child(sys.argv[1], int(sys.argv[2]))
