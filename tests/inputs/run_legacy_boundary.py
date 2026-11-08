"""Root fixture driver INSIDE an owned guest. No live daemon or host access.

Qualifies real operator/agent OS separation, not a human approval gesture.
All backend replies are explicitly synthetic. No candidate monkeypatch in the
positive/negative CLI cases; storage fault injection lives in focused unit tests.
"""
import argparse
import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

OPERATOR = 2101
AGENT = 2102
PACKAGE = "amplifier_bundle_digital_twin_universe"
CLI = PACKAGE + ".legacy_cli"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def owned(path, uid, mode=0o700):
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, uid, uid)
    path.chmod(mode)
    return path


def write(path, text, uid=OPERATOR, mode=0o600):
    path.write_text(text)
    os.chown(path, uid, uid)
    path.chmod(mode)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    assert os.getuid() == 0 and Path("/opt/fixture").is_dir()
    args.output.mkdir(parents=True, exist_ok=False)
    run_id = uuid.uuid4().hex
    report = {"run_id": run_id, "scope": "synthetic backend / real POSIX accounts",
              "human_gesture_tested": False, "cases": [], "wheel_sha256": digest(args.wheel)}
    records = []

    def run(uid, argv, expect=0, env=None):
        command = ["/usr/bin/setpriv", "--reuid", str(uid), "--regid", str(uid),
                   "--clear-groups", "--no-new-privs", *map(str, argv)]
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=60,
                                env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", **(env or {})})
        records.append({"uid": uid, "argv": list(map(str, argv)), "returncode": result.returncode,
                        "stdout": result.stdout, "stderr": result.stderr})
        assert result.returncode == expect, records[-1]
        return result.stdout

    try:
        op_python = Path("/opt/protected-executor/venv/bin/python")
        agent_python = Path("/work/protected-executor/venv/bin/python")
        for python in (op_python, agent_python):
            if not python.exists():
                subprocess.run(["/usr/bin/python3", "-m", "venv", str(python.parent.parent)], check=True)
            subprocess.run([str(python), "-m", "pip", "install", "--force-reinstall", str(args.wheel)], check=True)
        # Installation is fixture-owned. The adversary gets only its own copy.
        for path in Path("/work/protected-executor").rglob("*"):
            if not path.is_symlink():
                os.chown(path, AGENT, AGENT)
        base = owned(Path("/var/lib/protected-executor") / run_id, OPERATOR)
        conf = owned(base / "config", OPERATOR)
        global_conf = owned(base / "global", OPERATOR)
        binary = base / "incus"
        trace = base / "trace.jsonl"
        inventory = base / "inventory.json"
        write(inventory, "[]")
        write(conf / "config.yml", "default-remote: local\nremotes:\n  local:\n    addr: unix://" + str(base / "synthetic.socket") + "\n    project: default\n")
        fake = '''#!/usr/bin/python3
import json, pathlib, sys, time
from urllib.parse import urlsplit, parse_qs
base=pathlib.Path(__file__).parent
args=sys.argv[1:]
if args[:2]==['--project','default']: args=args[2:]
kind='refused'
value=None
if args[:1]==['query'] and len(args)==2:
 p=urlsplit(args[1]); q=parse_qs(p.query)
 if q.get('project')==['default']:
  if p.path=='/1.0': kind='identity'; value={'environment':{'certificate_fingerprint':'a'*64,'project':'default'}}
  elif p.path=='/1.0/profiles/default': kind='profile'; value={'devices':{'root':{'type':'disk','path':'/','pool':'pool'}}}
  elif p.path=='/1.0/instances' and q.get('recursion')==['1']: kind='inventory'; value=json.loads((base/'inventory.json').read_text())
  elif p.path=='/1.0/storage-pools/pool/resources': kind='storage'; value={'space':{'total':100000000000,'used':0}}
elif args==['list','user.dtu.managed-by=amplifier-digital-twin','--format=json']:
 kind='list'; value=json.loads((base/'inventory.json').read_text())
with (base/'trace.jsonl').open('a') as f: f.write(json.dumps({'kind':kind})+'\\n')
if kind=='inventory' and (base/'gate.json').exists():
 gate=json.loads((base/'gate.json').read_text())
 if pathlib.Path(gate['intent']).exists():
  (base/'entered').write_text('entered')
  deadline=time.monotonic()+20
  while not (base/'release').exists():
   if time.monotonic()>deadline: sys.exit(93)
   time.sleep(0.01)
if value is None: sys.exit(92)
print(json.dumps(value))
'''
        write(binary, fake, mode=0o700)
        capture = "from " + PACKAGE + " import bound_backend; import json; print(json.dumps(bound_backend.capture().as_dict()))"
        backend = json.loads(run(OPERATOR, [op_python, "-I", "-c", capture], env={
            "PATH": str(base) + ":/usr/bin:/bin", "INCUS_CONF": str(conf), "INCUS_GLOBAL_CONF": str(global_conf)}))
        scope = backend["scope"]
        root = owned(base / hashlib.sha256((scope["daemon"] + "\n" + scope["project"]).encode()).hexdigest(), OPERATOR)
        prefix = op_python.parent.parent
        package_path = next((prefix / "lib").glob("python*/site-packages/" + PACKAGE))
        config = {"version": 1, "operator_uid": OPERATOR, "admission_base": str(base),
                  "backend": backend, "generation": run_id * 2, "enabled": True,
                  "python_prefix": str(prefix), "package_path": str(package_path),
                  "writer_boundary": "exclusive_operator"}
        config_dir = Path("/etc/amplifier-dtu")
        config_dir.mkdir(mode=0o755, exist_ok=True)
        config_path = config_dir / "legacy-disposition.json"
        write(config_path, json.dumps(config), uid=0, mode=0o644)
        payloads = {str(p.relative_to(package_path)): digest(p) for p in package_path.rglob("*.py")}
        sources = {str(p.relative_to(args.source / "src" / PACKAGE)): digest(p)
                   for p in (args.source / "src" / PACKAGE).rglob("*.py")}
        assert payloads == sources
        report["installed_modules"] = payloads
        report["source_match"] = True
        report["backend"] = {"scope": scope, "synthetic": True}
        credentials = "import os,json,pathlib; print(json.dumps({'uid':os.getresuid(),'gid':os.getresgid(),'kernel':[l for l in pathlib.Path('/proc/self/status').read_text().splitlines() if l.startswith(('CapEff:','NoNewPrivs:'))]}))"
        report["identities"] = {str(uid): json.loads(run(uid, [python, "-I", "-c", credentials]))
                                for uid, python in ((OPERATOR, op_python), (AGENT, agent_python))}

        def seed(label):
            request = run_id + "-" + label
            receipt = {"request_id": request, "name": "legacy-" + label,
                       "owner_root": "fixture-owner", "intent_digest": "b" * 64,
                       "created_at": "synthetic-initial", "state": "unknown"}
            path = root / (hashlib.sha256(request.encode()).hexdigest() + ".json")
            write(path, json.dumps(receipt, indent=2) + "\n")
            return request, path, digest(path)

        def cli(operation, request, *extra, uid=OPERATOR, expect=0):
            python = op_python if uid == OPERATOR else agent_python
            return json.loads(run(uid, [python, "-I", "-m", CLI, operation, request, *extra], expect))

        def prep(request, effect):
            return cli("prepare", request, "--owner-root", "fixture-owner", "--effect", effect)["digest"]

        def apply(request, effect, d, identity="decision", expect=0):
            return cli("apply", request, "--owner-root", "fixture-owner", "--effect", effect,
                       "--digest", d, "--operation-id", identity, expect=expect)

        keep = "record_unknown_keep_reservation"
        release = "accept_unknown_risk_release_reservation"
        request, path, original = seed("release")
        d = prep(request, release)
        denied = cli("apply", request, "--owner-root", "fixture-owner", "--effect", release,
                     "--digest", d, "--operation-id", "agent", uid=AGENT, expect=1)
        assert denied["error"] == "authority_unavailable"
        attack = "import pathlib,json,os; paths=" + repr([str(path), str(config_path), str(package_path / "legacy_disposition.py")]) + "; out=[]\nfor name in paths:\n try:\n  fd=os.open(name,os.O_WRONLY); os.close(fd); out.append('WRITABLE')\n except PermissionError: out.append('denied')\nprint(json.dumps(out)); assert out==['denied']*len(paths)"
        run(AGENT, [agent_python, "-I", "-c", attack])
        report["cases"].append("agent direct apply and protected state/config/code writes denied")
        assert apply(request, release, "0" * 64, expect=1)["error"] == "disposition_conflict"
        assert apply(request, keep, d, expect=1)["error"] == "disposition_conflict"
        assert apply(request, release, d)["outcome"] == "applied"
        assert apply(request, release, d)["outcome"] == "already_applied"
        assert apply(request, release, d, "other", expect=1)["error"] == "disposition_conflict"
        assert digest(path) == original
        report["cases"].append("exact apply/idempotent duplicate; wrong digest/effect/operation refused; original unchanged")
        status = cli("reconcile", request)
        assert status["original_outcome"] == "unknown" and status["replay_permitted"] is False
        counts_code = "from " + PACKAGE + " import legacy_disposition as l, admission; import json; e=l.Executor.from_system()\nwith e.scope(), admission.locked(e.backend.scope) as root: print(json.dumps(admission.snapshot(e.backend.scope,admission.policy(0,0,0),root)['counts']))"
        assert json.loads(run(OPERATOR, [op_python, "-I", "-c", counts_code]))["pending_unobserved"] == 0
        rkeep, pkeep, hkeep = seed("keep")
        assert apply(rkeep, keep, prep(rkeep, keep))["outcome"] == "applied"
        assert json.loads(run(OPERATOR, [op_python, "-I", "-c", counts_code]))["pending_unobserved"] == 1
        assert digest(pkeep) == hkeep
        report["cases"].append("canonical snapshot release0 vs keep1; no receipt rewrite")
        # Any future same-name resource still counts; no historical outcome is promoted.
        write(inventory, json.dumps([{"name": "legacy-release", "status": "Running", "config": {}}]))
        counts = json.loads(run(OPERATOR, [op_python, "-I", "-c", counts_code]))
        assert counts["active"] == 2 and counts["total"] == 2
        rlate, plate, hlate = seed("late")
        write(inventory, "[]")
        dlate = prep(rlate, release)
        write(inventory, json.dumps([{"name": "legacy-late", "status": "Stopped", "config": {}}]))
        assert apply(rlate, release, dlate, expect=1)["error"] == "residual_conflict"
        assert digest(plate) == hlate
        write(inventory, "[]")
        report["cases"].append("late untagged resource counted; residual drift blocks apply")
        # Disable/re-enable is a root-owned deployment revocation, not agent input.
        config["enabled"] = False
        write(config_path, json.dumps(config), uid=0, mode=0o644)
        assert cli("prepare", rlate, "--owner-root", "fixture-owner", "--effect", release, expect=1)["error"] == "authority_unavailable"
        config["enabled"] = True
        write(config_path, json.dumps(config), uid=0, mode=0o644)
        report["cases"].append("root-owned deployment revocation refuses")
        # Real EACCES after committed intent, with a concurrent exact apply.
        # The gate is in the synthetic backend, not in the installed product.
        rp, pp, hp = seed("pending")
        dp = prep(rp, release)
        ip = root / "legacy" / (hashlib.sha256(rp.encode()).hexdigest() + ".intent")
        mp = ip.with_suffix(".applied")
        write(base / "gate.json", json.dumps({"intent": str(ip)}))
        argv = ["/usr/bin/setpriv", "--reuid", str(OPERATOR), "--regid", str(OPERATOR),
                "--clear-groups", "--no-new-privs", str(op_python), "-I", "-m", CLI,
                "apply", rp, "--owner-root", "fixture-owner", "--effect", release,
                "--digest", dp, "--operation-id", "decision"]
        child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"})
        try:
            deadline = time.monotonic() + 15
            while not (base / "entered").exists():
                assert child.poll() is None and time.monotonic() < deadline, "Commit gate not reached"
                time.sleep(0.01)
            assert ip.exists() and not mp.exists()
            busy = apply(rp, release, dp, expect=1)
            assert busy["error"] == "admission_busy"
            ip.parent.chmod(0o500)
            write(base / "release", "release")
            out, err = child.communicate(timeout=30)
            records.append({"uid": OPERATOR, "argv": argv, "returncode": child.returncode,
                            "stdout": out, "stderr": err})
            assert child.returncode == 1 and json.loads(out)["error"] == "projection_unknown"
        finally:
            ip.parent.chmod(0o700)
            write(base / "release", "release")
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        (base / "gate.json").unlink()
        assert ip.exists() and not mp.exists() and digest(pp) == hp
        before = {str(p): digest(p) for p in (ip, pp)}
        assert cli("reconcile", rp)["outcome"] == "committed_pending"
        assert apply(rp, release, dp, expect=1)["error"] == "projection_pending"
        assert before == {str(p): digest(p) for p in (ip, pp)}
        recovered = cli("recover", rp, "--digest", dp, "--operation-id", "decision")
        assert recovered["outcome"] == "applied"
        assert cli("recover", rp, "--digest", dp, "--operation-id", "decision")["outcome"] == "already_applied"
        assert before == {str(p): digest(p) for p in (ip, pp)}
        report["cases"].append("concurrent apply refused; real postcommit EACCES retains pending; readonly/retry do not repair; exact recovery once")
        report["interrupted_process"] = {"pid": child.pid, "reaped": child.returncode is not None,
                                         "failure": "real filesystem EACCES before marker",
                                         "hard_kill_tested": False}
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        assert not any(e["kind"] == "refused" for e in events)
        report["backend_events"] = {kind: sum(e["kind"] == kind for e in events)
                                    for kind in {e["kind"] for e in events}}
        report["backend_mutations"] = 0
        report["status"] = "passed"
    except BaseException as exc:
        report.update(status="failed", failure_type=type(exc).__name__, failure=str(exc))
        raise
    finally:
        (args.output / "commands.json").write_text(json.dumps(records, indent=2) + "\n")
        (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))


if __name__ == "__main__":
    main()
