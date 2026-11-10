"""Finite installed-wheel acceptance in one disposable GitHub-hosted Incus VM.

Not a general runner. No collector, fake backend, daemon exposure, credentials,
retries or historical resources. Cleanup is explicit operator action after export.
"""
from __future__ import annotations

import hashlib
from contextlib import redirect_stderr, redirect_stdout
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import traceback


PRIVATE: Path
OUT: Path
SOURCE: Path
OWNER: str
PROJECT: str
POOL: str
LOG: list[dict] = []
PRODUCT: list[dict] = []


def save(name, value):
    path = OUT / name
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(0o644)


def command(args, *, check=True, timeout=120, project=True):
    argv = ["incus", "--project", PROJECT, *args] if project else ["incus", *args]
    row = {"argv": argv, "started": True}
    LOG.append(row)
    save("operator-commands.json", LOG)
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
        row.update(returncode=result.returncode, stdout=result.stdout.decode("utf-8", "backslashreplace"),
                   stderr=result.stderr.decode("utf-8", "backslashreplace"))
        save("operator-commands.json", LOG)
        if check and result.returncode:
            raise RuntimeError(f"Operator command failed: {args[:2]}, exit {result.returncode}")
        return result
    except BaseException as exc:
        row["exception"] = type(exc).__name__
        save("operator-commands.json", LOG)
        raise


def query(args, *, project=True):
    return json.loads(command([*args, "--format=json"], project=project).stdout)


def profile():
    # Incus query rejects --project; profile show has no --format in Incus 6.0.
    return json.loads(command(["query", f"/1.0/profiles/default?project={PROJECT}"], project=False).stdout)


def inventory():
    return query(["list"])


def receipt_snapshot(name, admission):
    directory = admission.state_directory(admission.context())
    values = {p.name: p.read_bytes().hex() for p in directory.glob("*.json")}
    save(name, values)
    return values


def verify_source():
    # Git is read only. Explicit safe.directory is limited to this checkout.
    head = subprocess.check_output(["git", "-c", f"safe.directory={SOURCE}", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True).strip()
    assert head == os.environ["CANDIDATE_SHA"]
    git = ["git", "-c", f"safe.directory={SOURCE}", "-C", str(SOURCE)]
    paths = subprocess.check_output([*git, "ls-tree", "-r", "--name-only", "-z", head]).decode().split("\0")
    source = {}
    for p in filter(None, paths):
        committed = subprocess.check_output([*git, "show", f"{head}:{p}"])
        assert (SOURCE / p).read_bytes() == committed, f"Changed candidate file: {p}"
        source[p] = hashlib.sha256(committed).hexdigest()
    dist = importlib.metadata.distribution("amplifier-bundle-digital-twin-universe")
    direct = json.loads(dist.read_text("direct_url.json"))
    assert not direct.get("dir_info", {}).get("editable", False)
    assert direct["url"].endswith(".whl")
    installed = {}
    for p, digest in source.items():
        if p.startswith("src/") and p.endswith(".py"):
            path = Path(dist.locate_file(p.removeprefix("src/"))).resolve()
            assert not path.is_relative_to(SOURCE)
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
            installed[p] = digest
    assert len(installed) == 9
    save("source-installed.json", {"head": head, "source_sha256": source,
                                   "installed_sha256": installed, "direct_url": direct,
                                   "python": sys.version, "executable": sys.executable})


def setup():
    assert not (PRIVATE / "ownership.json").exists(), "Never replay a prior operation"
    assert all(p["name"] != PROJECT for p in query(["project", "list"], project=False))
    assert all(p["name"] != POOL for p in query(["storage", "list"], project=False))
    # Names are unique to this hosted run. Record intent before first creation.
    ownership = {"owner": OWNER, "project": PROJECT, "pool": POOL, "instances": [],
                 "candidate": os.environ["CANDIDATE_SHA"], "stage": "preparing"}
    (PRIVATE / "ownership.json").write_text(json.dumps(ownership))
    command(["project", "create", PROJECT, "-c", "features.images=true", "-c", "features.profiles=true",
             "-c", f"user.capture-owner={OWNER}"], project=False)
    command(["storage", "create", POOL, "dir", f"user.capture-owner={OWNER}"], project=False)
    command(["profile", "device", "add", "default", "root", "disk", "path=/", f"pool={POOL}"])
    command(["image", "copy", "images:ubuntu/24.04", "local:", "--alias", "capture-image"], timeout=300)
    images = query(["image", "list"])
    assert len(images) == 1
    image = images[0]
    fingerprint = image["fingerprint"]
    assert len(fingerprint) == 64 and all(c in "0123456789abcdef" for c in fingerprint)
    save("image.json", image)
    save("daemon.json", json.loads(command(["query", "/1.0"], project=False).stdout))
    assert not image.get("auto_update"), "The copied exact image must not auto-update"
    save("profile.json", profile())
    ownership.update(image=fingerprint, stage="ready")
    (PRIVATE / "ownership.json").write_text(json.dumps(ownership))
    return fingerprint


def run():
    verify_source()
    image = setup()
    from amplifier_bundle_digital_twin_universe import admission, engine, incus
    # Keep the real subprocess implementation. Observe returned client evidence
    # without substituting output, return codes or daemon behavior.
    native = incus._run
    def observed(command_args, *args, **kwargs):
        row = {"argv": command_args, "entered": True}
        PRODUCT.append(row)
        save("product-calls.json", PRODUCT)
        try:
            result = native(command_args, *args, **kwargs)
            row["returncode"] = result.returncode
            if command_args[1] == "exec":
                # pread does not alter spool position/decoder; output is finite.
                row["stdout_hex"] = os.pread(kwargs["stdout"].fileno(), 4096, 0).hex()
                row["stderr_hex"] = os.pread(kwargs["stderr"].fileno(), 4096, 0).hex()
            else:
                for key in ("stdout", "stderr"):
                    value = getattr(result, key)
                    row[key] = value.decode("utf-8", "backslashreplace") if isinstance(value, bytes) else value
            return result
        except BaseException as exc:
            row["exception"] = type(exc).__name__
            raise
        finally:
            save("product-calls.json", PRODUCT)
    incus._run = observed
    results = {}
    try:
        for case in ("invalid", "success", "nonzero"):
            # Each case uses one fresh stable request/name, never reused after failure.
            name = f"{PROJECT}-{case}"
            request = f"{OWNER}-{case}"
            ownership = json.loads((PRIVATE / "ownership.json").read_text())
            ownership["instances"].append({"name": name, "request": request})
            (PRIVATE / "ownership.json").write_text(json.dumps(ownership))
            first = {"invalid": "printf first >> /root/capture-first; printf '\\377'",
                     "success": "printf first >> /root/capture-first; printf complete-output",
                     "nonzero": "printf original-failure; printf complete-error >&2; exit 37"}[case]
            profile = PRIVATE / f"{case}.yaml"
            # Raw exact cached fingerprint selects the ordinary Incus CLI route;
            # no mutable image alias and no direct-operation collector path.
            profile.write_text(json.dumps({"name": "capture-safety", "base": {"image": image, "config": {
                "security.privileged": "false", "security.nesting": "false", "limits.cpu": "1", "limits.memory": "256MiB"}},
                "provision": {"setup_cmds": [first, "printf later > /root/capture-later"]}}))
            shutil.copyfile(profile, OUT / f"{case}-profile.json")
            args = dict(name=name, request_id=request, owner_root=OWNER, offline=True)
            start = len(PRODUCT)
            try:
                with (OUT / f"{case}-launch.stdout").open("w") as stdout, (OUT / f"{case}-launch.stderr").open("w") as stderr:
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        result = engine.launch(str(profile), {}, **args)
                assert case == "success", "Expected launch failure"
                results[case] = {"result": result}
            except incus.OutputCaptureError as exc:
                assert case == "invalid"
                assert exc.returncode == 0 and isinstance(exc.__cause__, UnicodeDecodeError)
                results[case] = {"error": str(exc), "returncode": exc.returncode, "cause_type": type(exc.__cause__).__name__}
            except RuntimeError as exc:
                assert case == "nonzero"
                assert "exit 37" in str(exc) and "original-failure" in str(exc) and "complete-error" in str(exc)
                results[case] = {"error": str(exc)}
            calls = PRODUCT[start:]
            creates = [c for c in calls if c["argv"][1] == "launch"]
            deletes = [c for c in calls if c["argv"][1] == "delete"]
            stops = [c for c in calls if c["argv"][1] == "stop"]
            provisions = [c for c in calls if c["argv"][1] == "exec" and c["argv"][-1] in (first, "printf later > /root/capture-later")]
            assert len(creates) == 1 and creates[0]["returncode"] == 0
            assert not stops and len(deletes) == (1 if case == "nonzero" else 0)
            assert len(provisions) == (2 if case == "success" else 1)
            if case == "invalid":
                assert provisions[0]["returncode"] == 0 and provisions[0]["stdout_hex"] == "ff"
            before = receipt_snapshot(f"{case}-receipts-before.json", admission)
            status = engine.launch_status(request)
            save(f"{case}-status.json", status)
            rows = inventory()
            found = [r for r in rows if r["name"] == name]
            if case == "nonzero":
                assert not found and status["state"] == "failed"
            else:
                assert len(found) == 1 and found[0]["status"] == "Running"
                assert found[0]["config"]["user.dtu.owner-root"] == OWNER
                assert found[0]["config"]["user.dtu.request-id"] == request
                assert found[0]["config"]["user.dtu.launch-state"] == ("ready" if case == "success" else "provisioning")
                marker = command(["exec", name, "--", "cat", "/root/capture-first"])
                assert marker.stdout == b"first"
                later = command(["exec", name, "--", "test", "-f", "/root/capture-later"], check=False)
                assert later.returncode == (0 if case == "success" else 1)
                save(f"{case}-instance.json", found[0])
            if case == "invalid":
                assert status["state"] == "unknown" and "result" not in status
                retry_start = len(PRODUCT)
                try:
                    engine.launch(str(profile), {}, **args)
                except admission.AdmissionError as exc:
                    assert exc.code == "launch_unknown"
                    results[case]["retry"] = exc.as_dict()
                else:
                    raise AssertionError("Unknown request replayed")
                assert not any(c["argv"][1] in {"launch", "start", "exec", "stop", "delete"} for c in PRODUCT[retry_start:])
                assert not any(c["argv"][1:3] in (["config", "set"], ["config", "unset"], ["config", "edit"])
                               for c in PRODUCT[retry_start:])
                assert command(["exec", name, "--", "cat", "/root/capture-first"]).stdout == b"first"
                after_retry_status = engine.launch_status(request)
                after_retry_instance = next(r for r in inventory() if r["name"] == name)
                assert after_retry_status["state"] == "unknown" and "result" not in after_retry_status
                assert after_retry_instance["status"] == "Running"
                assert after_retry_instance["config"]["user.dtu.launch-state"] == "provisioning"
                assert after_retry_instance["config"]["user.dtu.owner-root"] == OWNER
                assert after_retry_instance["config"]["user.dtu.request-id"] == request
                save("invalid-status-after-retry.json", after_retry_status)
                save("invalid-instance-after-retry.json", after_retry_instance)
            if case == "success":
                # Complete strings and nonzero return survive through the installed library.
                for code in (0, 37):
                    assert incus.exec_command(name, ["bash", "-c", f"printf 'out\\n'; printf 'err\\n' >&2; exit {code}"]) == (code, "out\n", "err\n")
            assert receipt_snapshot(f"{case}-receipts-after.json", admission) == before
            results[case].update(create_calls=len(creates), delete_calls=len(deletes), stop_calls=len(stops), provisioning_calls=len(provisions), passed=True)
            save("results.json", results)
            # Evidence is saved before explicit operator teardown. Keep only one
            # case guest at a time, and never count this as product cleanup.
            if case != "nonzero":
                owned = next(r for r in inventory() if r["name"] == name)
                assert owned["config"]["user.dtu.owner-root"] == OWNER
                assert owned["config"]["user.dtu.request-id"] == request
                command(["delete", name, "--force"])
                assert not any(r["name"] == name for r in inventory())
                results[case]["operator_teardown"] = "removed_after_evidence"
                save("results.json", results)
        save("qualification.json", {"passed": True, "results": results,
             "scope": "Real Incus ordinary CLI launch via installed library; call counts cover this library's observed CLI invocations, not a daemon-wide lifecycle event census",
             "operator_cleanup": "Separate always-step after retained observations; not product automatic cleanup"})
    finally:
        incus._run = native


def cleanup():
    ownership = json.loads((PRIVATE / "ownership.json").read_text())
    assert ownership["owner"] == OWNER and ownership["project"] == PROJECT and ownership["pool"] == POOL
    prior_log = OUT / "operator-commands.json"
    if prior_log.exists():
        LOG.extend(json.loads(prior_log.read_text()))
    projects = query(["project", "list"], project=False)
    present = next((p for p in projects if p["name"] == PROJECT), None)
    result = {"owner": OWNER, "instances_removed": [], "project_absent": False, "pool_absent": False}
    if present:
        assert present["config"].get("user.capture-owner") == OWNER
        rows = inventory()
        save("pre-cleanup-instances.json", rows)
        allowed = {row["name"]: row["request"] for row in ownership["instances"]}
        # Refuse foreign resources, even in our otherwise private project.
        for row in rows:
            assert row["name"] in allowed
            assert row["config"].get("user.dtu.owner-root") == OWNER
            assert row["config"].get("user.dtu.request-id") == allowed[row["name"]]
        for row in rows:
            command(["delete", row["name"], "--force"])
            result["instances_removed"].append(row["name"])
        assert not inventory()
        # Images were copied only into the fresh project; project deletion owns no peer data.
        for image in query(["image", "list"]):
            command(["image", "delete", image["fingerprint"]])
        current_profile = profile()
        if "root" in current_profile.get("devices", {}):
            assert current_profile["devices"]["root"]["pool"] == POOL
            command(["profile", "device", "remove", "default", "root"])
        command(["project", "delete", PROJECT], project=False)
    assert all(p["name"] != PROJECT for p in query(["project", "list"], project=False))
    result["project_absent"] = True
    pools = query(["storage", "list"], project=False)
    pool = next((p for p in pools if p["name"] == POOL), None)
    if pool:
        assert pool["config"].get("user.capture-owner") == OWNER
        command(["storage", "delete", POOL], project=False)
    assert all(p["name"] != POOL for p in query(["storage", "list"], project=False))
    result["pool_absent"] = True
    save("cleanup.json", result)


def main():
    global PRIVATE, OUT, SOURCE, OWNER, PROJECT, POOL
    assert os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("HOSTED_RUNNER") == "github-hosted"
    assert os.getuid() == 0, "Job-VM setup requires its designated root; never run on a live host"
    PRIVATE, OUT, SOURCE = (Path(os.environ[key]).resolve() for key in ("QUALIFICATION", "ARTIFACTS", "GITHUB_WORKSPACE"))
    run_id, attempt = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
    assert run_id.isdecimal() and attempt.isdecimal()
    OWNER = f"ordinary-capture-{run_id}-{attempt}"
    PROJECT = POOL = f"capture-{run_id}-{attempt}"
    os.environ["INCUS_CONF"] = str(PRIVATE / "client")
    os.environ["AMPLIFIER_DTU_INCUS_PROJECT"] = PROJECT
    os.environ["AMPLIFIER_DTU_ADMISSION_DIR"] = str(PRIVATE / "admission")
    os.environ["PATH"] = "/usr/bin:/bin"
    for key in ("AMPLIFIER_DTU_MAX_RUNNING", "AMPLIFIER_DTU_MAX_INSTANCES", "AMPLIFIER_DTU_MIN_FREE_BYTES"):
        os.environ.pop(key, None)
    (PRIVATE / "client").mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryFile("w+") as stream:
        assert stream.encoding.lower().replace("-", "") == "utf8"
    try:
        {"run": run, "cleanup": cleanup}[sys.argv[1]]()
    except BaseException:
        (OUT / f"{sys.argv[1]}-failure.txt").write_text(traceback.format_exc())
        raise
    finally:
        # No key or private client config is exported. Receipts contain synthetic identities only.
        save("artifact-hashes.json", {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in OUT.iterdir() if p.is_file() and p.name != "artifact-hashes.json"})


if __name__ == "__main__":
    main()
