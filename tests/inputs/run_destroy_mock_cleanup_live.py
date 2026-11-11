"""Docker-free destroy acceptance; uses the maintained private hosted Incus lab.

Only a job-local executable search path hides Docker. No daemon, installed binary,
shared host or credentials are changed. The tested destroy is the installed CLI.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import run_ordinary_capture_live as lab


def cli(*args, path=None, check=True):
    env = dict(os.environ)
    if path is not None:
        env["PATH"] = str(path)
    executable = Path(sys.executable).parent / "amplifier-digital-twin"
    result = subprocess.run([str(executable), *args], env=env, cwd=lab.PRIVATE,
                            capture_output=True, text=True, timeout=180, check=False)
    rows = json.loads((lab.OUT / "cli.json").read_text()) if (lab.OUT / "cli.json").exists() else []
    rows.append({"args": args, "path_hides_docker": path is not None, "returncode": result.returncode,
                 "stdout": result.stdout, "stderr": result.stderr})
    lab.save("cli.json", rows)
    if check:
        assert result.returncode == 0, rows[-1]
    return result


def run():
    lab.verify_source()
    image = lab.setup()
    no_docker = lab.PRIVATE / "no-docker-bin"
    no_docker.mkdir()
    incus = shutil.which("incus")
    assert incus
    (no_docker / "incus").symlink_to(incus)
    assert shutil.which("docker", path=str(no_docker)) is None
    try:
        subprocess.run(["docker", "version"], env={**os.environ, "PATH": str(no_docker)}, check=False)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("Docker is not absent from the tested process path")
    lab.save("docker-absence.json", {"mechanism": "private PATH contains only the real Incus executable",
                                    "actual_FileNotFoundError": True, "host_installation_modified": False})
    from amplifier_bundle_digital_twin_universe import admission
    results = {}
    for case in ("running", "stopped"):
        name, request = f"{lab.PROJECT}-{case}", f"{lab.OWNER}-{case}"
        ownership = json.loads((lab.PRIVATE / "ownership.json").read_text())
        ownership["instances"].append({"name": name, "request": request})
        (lab.PRIVATE / "ownership.json").write_text(json.dumps(ownership))
        source = lab.PRIVATE / f"{case}.json"
        source.write_text(json.dumps({"name": "docker-free", "base": {"image": image, "config": {
            "security.nesting": "false", "security.privileged": "false", "limits.cpu": "1", "limits.memory": "256MiB"}}}))
        launched = json.loads(cli("launch", str(source), "--name", name, "--owner-root", lab.OWNER,
                                 "--request-id", request, "--offline", path=no_docker).stdout)
        assert launched["id"] == name
        rows = lab.inventory()
        found = next(r for r in rows if r["name"] == name)
        assert found["config"]["user.dtu.mock-cleanup"] == "none"
        assert found["config"]["user.dtu.request-id"] == request and found["config"]["user.dtu.owner-root"] == lab.OWNER
        if case == "stopped":
            lab.command(["stop", name])
        expected_state = "Stopped" if case == "stopped" else "Running"
        before = lab.receipt_snapshot(f"{case}-receipt-before.json", admission)
        mismatch = cli("destroy", name, "--owner-root", "foreign", "--request-id", request, path=no_docker, check=False)
        assert mismatch.returncode != 0 and json.loads(mismatch.stdout)["error"] == "ownership_conflict"
        assert lab.receipt_snapshot(f"{case}-receipt-after-mismatch.json", admission) == before
        # Missing, unknown and required metadata never infer no mocks from an
        # empty/no-mock profile. These are this newly owned synthetic instance.
        for index, marker in enumerate(("", "unknown", "required", " none ", "none\n")):
            if marker:
                lab.command(["config", "set", name, "user.dtu.mock-cleanup", marker])
            else:
                lab.command(["config", "unset", name, "user.dtu.mock-cleanup"])
            refused = cli("destroy", name, "--owner-root", lab.OWNER, "--request-id", request, path=no_docker, check=False)
            assert refused.returncode != 0 and "destroyed" not in refused.stdout
            found = next(r for r in lab.inventory() if r["name"] == name)
            assert found["status"] == expected_state
            assert lab.receipt_snapshot(f"{case}-refused-{index}.json", admission) == before
        # Restore the exact original synthetic no-mocks fact, not historical data.
        lab.command(["config", "set", name, "user.dtu.mock-cleanup", "none"])
        destroyed = json.loads(cli("destroy", name, "--owner-root", lab.OWNER, "--request-id", request, path=no_docker).stdout)
        assert destroyed == {"id": name, "destroyed": True}
        assert not any(r["name"] == name for r in lab.inventory())
        after = lab.receipt_snapshot(f"{case}-receipt-after-destroy.json", admission)
        records = [json.loads(bytes.fromhex(raw)) for raw in after.values()]
        persisted = next(r for r in records if r["request_id"] == request)
        original = next(json.loads(bytes.fromhex(raw)) for raw in before.values()
                        if json.loads(bytes.fromhex(raw))["request_id"] == request)
        assert persisted == {**original, "state": "removed"}
        status = json.loads(cli("launch-status", request, path=no_docker).stdout)
        assert status["state"] == "removed"
        results[case] = {"passed": True, "destroyed": destroyed, "status": status,
                         "ownership_refused": True, "uncertain_mock_markers_refused": True,
                         "absence_independently_observed": True}
        lab.save("destroy-results.json", results)
    lab.save("qualification.json", {"passed": True, "results": results,
        "scope": "Noneditable installed CLI, actual Incus and actual missing Docker executable via privatePATH; no fake Docker, runtime or product monkeypatch. Mock-runtime/removal failures have separate focused unit coverage."})


if __name__ == "__main__":
    lab.run = run
    lab.main()
