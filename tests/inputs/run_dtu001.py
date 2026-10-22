"""Run the frozen mocked selection only inside a prepared managed Python guest.

This input runner does not change tests or convert expected failures into passes.
It records attempted external effects even when the tested code catches them.
"""
from __future__ import annotations

import collections
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time
import tomllib


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path("/artifacts/dtu001-mocked")
MANIFEST = ROOT / "tests/inputs/dtu001-selection.json"
FORBIDDEN = {
    "subprocess.Popen", "os.system", "os.posix_spawn", "os.fork",
    "os.forkpty", "os.exec", "socket.__new__", "socket.connect",
    "socket.connect_ex", "socket.bind", "socket.getaddrinfo",
    "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo",
    "os.kill", "os.killpg", "signal.pthread_kill",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def encoded(value):
    if isinstance(value, bytes):
        return {"bytesHex": value.hex()}
    if isinstance(value, dict):
        return {str(key): encoded(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encoded(item) for item in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise TypeError("Unexpected test parameter type")


def case_key(function, parameters):
    return json.dumps([function, encoded(parameters)], sort_keys=True)


def main():
    # A mounted path alone is not isolation; the caller must also verify the
    # managed resource receipt. This check prevents accidental host execution.
    if ROOT != Path("/workspace") or Path.cwd().resolve() != ROOT:
        raise RuntimeError("Run only from the exact managed /workspace snapshot")
    plan_path = Path("/opt/unified-prepared/plan.json")
    inventory_path = Path("/opt/unified-prepared/inventory.json")
    plan = json.loads(plan_path.read_text())
    inventory = json.loads(inventory_path.read_text())
    manifest = json.loads(MANIFEST.read_text())
    if plan["profileId"] != "python-project" or inventory["profileId"] != "python-project":
        raise RuntimeError("The prepared Python project profile is required")
    if plan["sourceSha256"] != inventory["sourceSha256"]:
        raise RuntimeError("Prepared dependency/source identity mismatch")
    for relative, expected in manifest["sourceFiles"].items():
        if digest(ROOT / relative) != expected:
            raise RuntimeError("Frozen source/input hash mismatch: " + relative)
    locks = {row["path"]: row["sha256"] for row in inventory["lockfiles"]}
    if any(locks.get(name) != digest(ROOT / name) for name in ("uv.lock", "pyproject.toml")):
        raise RuntimeError("Prepared lockfiles do not match the source")

    OUTPUT.mkdir(mode=0o700, parents=True, exist_ok=False)
    (OUTPUT / "tmp").mkdir(mode=0o700)
    (OUTPUT / "home").mkdir(mode=0o700)
    (OUTPUT / "cache").mkdir(mode=0o700)
    # Do not inherit pytest plugins/options, admission directories or credentials.
    os.environ.clear()
    os.environ.update(
        PATH="/opt/unified-prepared/venv/bin:/opt/unified-prepared/bootstrap/bin:/usr/local/bin:/usr/bin:/bin",
        HOME=str(OUTPUT / "home"), TMPDIR=str(OUTPUT / "tmp"),
        XDG_CACHE_HOME=str(OUTPUT / "cache"), LANG="C.UTF-8",
        PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        UV_OFFLINE="1", PIP_NO_INDEX="1",
    )
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(1, str(ROOT / "tests"))
    blocked = []

    def audit(event, args):
        if event in FORBIDDEN:
            # Record event name only, never command arguments or payloads.
            blocked.append(event)
            raise RuntimeError("Mocked selection attempted an external effect: " + event)

    sys.addaudithook(audit)
    def deny_signal(*args, **kwargs):
        blocked.append("signal.delivery")
        raise RuntimeError("Mocked selection attempted signal delivery")

    for name in ("raise_signal", "pthread_kill", "pidfd_send_signal"):
        if hasattr(signal, name):
            setattr(signal, name, deny_signal)
    result = {
        "status": "readiness", "sourceBase": manifest["sourceBase"],
        "sourceFiles": manifest["sourceFiles"], "selectionSha256": digest(MANIFEST),
        "preparedSourceSha256": plan["sourceSha256"],
        "preparedInventorySha256": digest(inventory_path),
        "python": sys.version, "executable": sys.executable,
        "collected": [], "reports": [], "blockedEvents": blocked,
        "startedAt": time.time(), "testExecutionStarted": False,
        "faultAcceptance": False,
    }
    code = 2
    try:
        lock = tomllib.loads((ROOT / "uv.lock").read_text())
        expected_versions = {p["name"]: p["version"] for p in lock["package"]}
        versions = {}
        for package in ("pytest", "click", "pyyaml", "iniconfig", "packaging", "pluggy", "pygments"):
            versions[package] = importlib.metadata.version(package)
            if versions[package] != expected_versions[package]:
                raise RuntimeError("Prepared package version mismatch: " + package)
        result["packageVersions"] = versions
        if not shutil.which("uv"):
            raise RuntimeError("The existing check_uv fixture requires a real uv executable")
        result["uvExecutable"] = shutil.which("uv")
        import pytest
        import click
        import yaml
        from amplifier_bundle_digital_twin_universe import admission, engine, incus, operations
        modules = (admission, engine, incus, operations)
        if any(not Path(module.__file__).resolve().is_relative_to(ROOT / "src") for module in modules):
            raise RuntimeError("DTU imports did not come from the exact candidate")
        result["imports"] = {
            "pytest": pytest.__file__, "click": click.__file__, "yaml": yaml.__file__,
            **{module.__name__: module.__file__ for module in modules},
        }
        expected_cases = collections.Counter(
            case_key(case["function"], case["parameters"]) for case in manifest["cases"]
        )

        class Evidence:
            def pytest_collection_finish(self, session):
                actual = []
                for item in session.items:
                    params = encoded(getattr(getattr(item, "callspec", None), "params", {}))
                    actual.append({
                        "nodeid": item.nodeid, "function": item.originalname,
                        "parameters": params,
                    })
                result["collected"] = actual
                if collections.Counter(
                    case_key(item["function"], item["parameters"]) for item in actual
                ) != expected_cases:
                    pytest.exit("Exact selected case set changed", returncode=4)

            def pytest_runtest_logstart(self, nodeid, location):
                result["testExecutionStarted"] = True

            def pytest_runtest_logreport(self, report):
                result["reports"].append({
                    "nodeid": report.nodeid, "when": report.when,
                    "outcome": report.outcome, "duration": report.duration,
                    "longrepr": str(report.longrepr)[:16000] if report.failed or report.skipped else None,
                })

        selectors = ["tests/unit/test_admission.py::" + name for name in manifest["functions"]]
        code = int(pytest.main([
            "-q", "--tb=short", "-ra", "-p", "no:cacheprovider",
            "--basetemp", str(OUTPUT / "pytest-tmp"),
            "--junitxml", str(OUTPUT / "junit.xml"), *selectors,
        ], plugins=[Evidence()]))
        result["pytestExitCode"] = code
        result["status"] = "completed"
    except BaseException as exc:
        result["status"] = "runner_failed"
        result["errorType"] = type(exc).__name__
        result["error"] = str(exc)[:2000]
        code = 2
    finally:
        changed = []
        for name, expected in manifest["sourceFiles"].items():
            try:
                if digest(ROOT / name) != expected:
                    changed.append(name)
            except OSError:
                changed.append(name)
        result["changedSourceFiles"] = changed
        result["finishedAt"] = time.time()
        result["counts"] = {
            outcome: sum(r["when"] == "call" and r["outcome"] == outcome for r in result["reports"])
            for outcome in ("passed", "failed", "skipped")
        }
        result["setupOrTeardownFailures"] = sum(
            r["when"] != "call" and r["outcome"] == "failed" for r in result["reports"]
        )
        result["skippedNodeIds"] = sorted({
            r["nodeid"] for r in result["reports"] if r["outcome"] == "skipped"
        })
        called = [r["nodeid"] for r in result["reports"] if r["when"] == "call"]
        collected = [r["nodeid"] for r in result["collected"]]
        result["missingCallNodeIds"] = sorted(set(collected) - set(called))
        result["selectionComplete"] = (
            len(collected) == len(manifest["cases"])
            and collections.Counter(called) == collections.Counter(collected)
            and not result["skippedNodeIds"]
            and not result["setupOrTeardownFailures"]
        )
        if not result["selectionComplete"]:
            result["status"] = "incomplete"
            if code == 0:
                code = 2
        if blocked or changed:
            code = 3
        result["exitCode"] = code
        (OUTPUT / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({key: result[key] for key in (
            "status", "counts", "setupOrTeardownFailures", "blockedEvents",
            "changedSourceFiles", "exitCode",
        )}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())