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
import socket
import socketserver
import sys
import threading
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
    "socket.sendto", "socket.sendmsg",
    "os.kill", "os.killpg", "signal.pthread_kill",
}

HTTP_CASES = {
    "tests/unit/test_readiness_access.py::test_poll_port_retries_until_available",
    "tests/unit/test_readiness_access.py::test_verify_access_ports_skips_verify_false",
}
UNIX_CASE = "tests/unit/test_nested_incus.py::test_running_inside_incus_instance_real_socket"


class LocalFixtures:
    """Narrow guest-only exceptions, never permissions for a daemon or peer."""

    def __init__(self, enabled):
        self.enabled = enabled
        self.active = None
        self.unix_path = None
        self.ports = set()
        self.sockets = {}
        self.threads = []
        self.servers = {}
        self.allowed = collections.Counter()
        self.cleanup = []
        self.errors = []
        self.controls = []
        self.control = False
        self.closing = False
        self.originals = {}

    def owns_port(self, port):
        return (
            type(port) is int and port in self.ports
            or isinstance(port, str) and port in {str(p) for p in self.ports}
        )

    def permits(self, event, args):
        if self.closing or not self.enabled or self.active not in HTTP_CASES | {UNIX_CASE}:
            return False
        http = self.active in HTTP_CASES
        if event == "socket.__new__":
            sock, family, kind, protocol = args
            valid = (
                kind == socket.SOCK_STREAM and protocol in ((0, socket.IPPROTO_TCP) if http else (0,))
                and family == (socket.AF_INET if http else socket.AF_UNIX)
            )
            if valid:
                self.sockets[sock] = {"reserve": False, "bound": None}
            return valid
        if event in {"socket.bind", "socket.connect"}:
            sock, address = args
            if sock not in self.sockets:
                return False
            if not http:
                return (
                    event == "socket.bind" and self.unix_path is not None
                    and address == str(self.unix_path)
                )
            if event == "socket.bind" and address == ("", 0):
                return self.sockets[sock].get("reserving", False)
            return (
                isinstance(address, tuple) and len(address) == 2
                and address[0] == "127.0.0.1" and address[1] in self.ports
            )
        if event == "socket.getaddrinfo":
            host, port, family, kind, protocol = args
            return (
                http and host == "127.0.0.1" and self.owns_port(port)
                and family == socket.AF_INET and kind == socket.SOCK_STREAM
                and protocol in (0, socket.IPPROTO_TCP)
            )
        return False

    def install(self, audit):
        """Wrap non-audited listen/cleanup and numeric-only local resolution."""
        self.originals = {
            "bind": socket.socket.bind, "listen": socket.socket.listen,
            "connect": socket.socket.connect,
            "getaddrinfo": socket.getaddrinfo, "getfqdn": socket.getfqdn,
            "start": threading.Thread.start, "serve": socketserver.BaseServer.serve_forever,
        }

        def bind(sock, address):
            row = self.sockets.get(sock, {})
            if address == ("", 0):
                caller = sys._getframe(1)
                row["reserving"] = (
                    caller.f_code.co_name == "find_free_port"
                    and Path(caller.f_code.co_filename).resolve() == ROOT / "tests/helpers.py"
                )
            try:
                # Check raw address before native conversion can resolve a hostname.
                audit("socket.bind", (sock, address))
                value = self.originals["bind"](sock, address)
                row["bound"] = address
                if address == ("", 0):
                    row["reserve"] = True
                    self.ports.add(sock.getsockname()[1])
                return value
            finally:
                row["reserving"] = False

        def connect(sock, address):
            audit("socket.connect", (sock, address))
            return self.originals["connect"](sock, address)

        def deny_address_operation(*args, **kwargs):
            audit("fixture.address-operation.denied", ())

        def listen(sock, *args, **kwargs):
            row = self.sockets.get(sock, {})
            address = row.get("bound")
            if not (
                not self.closing and self.active in HTTP_CASES and not row.get("reserve")
                and isinstance(address, tuple) and address[0] == "127.0.0.1"
                and address[1] in self.ports
            ):
                audit("fixture.listen.denied", ())
            self.allowed[(self.active, "socket.listen")] += 1
            return self.originals["listen"](sock, *args, **kwargs)

        def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            if not (
                not self.closing and self.active in HTTP_CASES and host in {"localhost", "127.0.0.1"}
                and self.owns_port(port) and family in (0, socket.AF_INET)
                and type == socket.SOCK_STREAM and proto in (0, socket.IPPROTO_TCP)
            ):
                audit("fixture.resolution.denied", ())
            # Numeric host means no external DNS or host-dependent NSS answer.
            return self.originals["getaddrinfo"](
                "127.0.0.1", port, socket.AF_INET, type, proto,
                flags | socket.AI_NUMERICHOST,
            )

        def getfqdn(name=""):
            # HTTPServer uses this only for its display name, not a connection.
            if self.active in HTTP_CASES and name == "127.0.0.1":
                self.allowed[(self.active, "local-server-name")] += 1
                return "localhost"
            return self.originals["getfqdn"](name)

        def start(thread, *args, **kwargs):
            if self.active in HTTP_CASES:
                if self.closing:
                    audit("fixture.late-thread.denied", ())
                self.threads.append(thread)
            return self.originals["start"](thread, *args, **kwargs)

        def serve(server, *args, **kwargs):
            if self.active not in HTTP_CASES:
                return self.originals["serve"](server, *args, **kwargs)
            self.servers[server] = threading.current_thread()
            try:
                return self.originals["serve"](server, *args, **kwargs)
            finally:
                self.servers.pop(server, None)

        socket.socket.bind = bind
        socket.socket.connect = connect
        socket.socket.connect_ex = deny_address_operation
        socket.socket.sendto = deny_address_operation
        socket.socket.sendmsg = deny_address_operation
        socket.socket.listen = listen
        socket.getaddrinfo = getaddrinfo
        socket.getfqdn = getfqdn
        threading.Thread.start = start
        socketserver.BaseServer.serve_forever = serve

    def begin(self, nodeid):
        if self.errors:
            raise RuntimeError("Previous local fixture did not finish safely")
        self.closing = False
        self.active = nodeid if self.enabled and nodeid in HTTP_CASES | {UNIX_CASE} else None

    def finish(self):
        if self.active is None:
            return
        self.closing = True
        deadline = time.monotonic() + 5
        try:
            for server, thread in list(self.servers.items()):
                if thread.is_alive():
                    closer = threading.Thread(target=server.shutdown, daemon=True)
                    self.threads.append(closer)
                    self.originals["start"](closer)
            for thread in self.threads:
                thread.join(timeout=max(0, deadline - time.monotonic()))
            for sock in list(self.sockets):
                sock.close()
            alive = sum(thread.is_alive() for thread in self.threads)
            opened = sum(sock.fileno() != -1 for sock in self.sockets)
            self.cleanup.append({
                "nodeid": self.active, "sockets": len(self.sockets),
                "threads": len(self.threads), "liveThreads": alive, "openSockets": opened,
            })
            if alive or opened:
                self.errors.append("Fixture resources survived teardown")
        except Exception as exc:
            self.errors.append(type(exc).__name__ + " during fixture cleanup")
        finally:
            self.active = self.unix_path = None
            self.ports.clear()
            if not self.errors:
                self.sockets.clear()
                self.threads.clear()
                self.servers.clear()

    def denial_controls(self):
        """Exercise the installed audit hook without dispatching any OS effect."""
        marker = object()
        self.control = True
        self.unix_path = OUTPUT / "pytest-tmp" / "fixture" / "incus.sock"
        self.ports.add(42123)
        try:
            for node, event, args in [
                (None, "socket.__new__", (marker, socket.AF_INET, socket.SOCK_STREAM, 0)),
                (next(iter(HTTP_CASES)), "socket.connect", (marker, ("203.0.113.1", 443))),
                (next(iter(HTTP_CASES)), "socket.connect", (marker, ("127.0.0.1", 1))),
                (next(iter(HTTP_CASES)), "socket.getaddrinfo", ("example.invalid", 443, 2, 1, 0)),
                (UNIX_CASE, "socket.bind", (marker, "/var/lib/incus/unix.socket")),
                (UNIX_CASE, "socket.connect", (marker, "/artifacts/fixture.sock")),
                (None, "subprocess.Popen", ()),
                (None, "os.kill", ()),
            ]:
                self.active = node
                self.sockets[marker] = {"reserve": False, "bound": None}
                try:
                    sys.audit(event, *args)
                except RuntimeError:
                    continue
                raise AssertionError("Denial control unexpectedly permitted " + event)
        finally:
            self.active = None
            self.unix_path = None
            self.ports.clear()
            self.sockets.clear()
            self.control = False


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
    if sys.argv[1:] not in ([], ["--unit-suite"], ["--identity-capture"], ["--publication-repair"],
                           ["--operational-capture-contract"], ["--capture-fault"]):
        raise RuntimeError("Unsupported exact test selection")
    full_suite = sys.argv[1:] == ["--unit-suite"]
    capture_suite = sys.argv[1:] in (["--identity-capture"], ["--publication-repair"],
                                    ["--operational-capture-contract"], ["--capture-fault"])
    capture_files = (["tests/unit/test_bound_backend.py", "tests/unit/test_identity_query_capture.py"]
                     if sys.argv[1:] == ["--identity-capture"] else [
                         "tests/unit/test_collector_projection.py", "tests/unit/test_launch_result_checkpoint.py",
                         "tests/unit/test_collector_custody.py", "tests/unit/test_collector_observations.py",
                         "tests/unit/test_collector_evidence.py", "tests/unit/test_admission.py"])
    fault_functions = [
        "test_collected_capture_fault_after_completion_never_authorizes_delete",
        "test_native_capture_error_preserves_returncode_and_cause",
        "test_complete_nonzero_provisioning_still_cleans_up_once",
        "test_capture_primary_survives_failure_evidence_write_fault",
        "test_gateway_capture_error_is_not_retried",
        "test_snapshot_capture_error_is_not_missing_profile",
        "test_optional_capture_diagnostic_does_not_erase_known_failure",
        "test_complete_string_exec_contract_is_preserved",
        "test_nonzero_provisioning_retains_original_complete_diagnostics",
        "test_execution_timeout_is_not_replaced_by_capture_read",
    ] if sys.argv[1:] == ["--capture-fault"] else []
    if sys.argv[1:] in (["--operational-capture-contract"], ["--capture-fault"]):
        capture_files = ["tests/unit/test_operational_capture_contract.py"]
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
    unit_files = {
        str(path.relative_to(ROOT)): digest(path)
        for path in sorted((ROOT / "tests/unit").rglob("*.py"))
    }
    unit_tree = hashlib.sha256(json.dumps(unit_files, sort_keys=True).encode()).hexdigest()
    if (full_suite or capture_suite) and unit_tree != manifest["unitSuite"]["sourceSha256"]:
        raise RuntimeError("Unit-suite source tree does not match the frozen input")
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
    fixtures = LocalFixtures(full_suite)

    def audit(event, args):
        if event in FORBIDDEN or event.startswith("fixture."):
            if fixtures.permits(event, args):
                fixtures.allowed[(fixtures.active, event)] += 1
                return
            # Record event name only, never command arguments or payloads.
            if fixtures.control:
                fixtures.controls.append(event)
                raise RuntimeError("Expected denial control")
            if len(blocked) < 128:
                blocked.append(event)
            raise RuntimeError("Mocked selection attempted an external effect: " + event)

    sys.addaudithook(audit)
    if full_suite:
        fixtures.install(audit)
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
        "faultAcceptance": False, "fullUnitSuite": full_suite,
        "identityCaptureSuite": sys.argv[1:] == ["--identity-capture"],
        "publicationRepairSuite": sys.argv[1:] == ["--publication-repair"],
        "operationalCaptureContractSuite": sys.argv[1:] == ["--operational-capture-contract"],
        "captureFaultSuite": bool(fault_functions),
        "deferredUnexecutedCases": ([
            "tests/unit/test_operational_capture_contract.py::test_successful_provisioning_avoids_unsized_output_reads"
        ] if fault_functions else []),
        "unitSuiteSourceSha256": unit_tree, "collectionVerified": False,
        "collectionErrors": [], "deselectedNodeIds": [],
        "fixtureCleanup": fixtures.cleanup, "fixtureErrors": fixtures.errors,
        "denialControls": fixtures.controls,
    }
    code = 2
    try:
        fixtures.denial_controls()
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
            @pytest.hookimpl(hookwrapper=True)
            def pytest_runtest_protocol(self, item, nextitem):
                fixtures.begin(item.nodeid)
                try:
                    yield
                finally:
                    fixtures.finish()
                    if fixtures.errors:
                        pytest.exit("Local fixture cleanup incomplete", returncode=3)

            @pytest.hookimpl(hookwrapper=True)
            def pytest_fixture_setup(self, fixturedef, request):
                if fixtures.active == UNIX_CASE and fixturedef.argname == "real_unix_socket":
                    path = request.getfixturevalue("tmp_path").resolve() / "incus.sock"
                    if not path.is_relative_to(OUTPUT / "pytest-tmp"):
                        raise RuntimeError("Unix fixture path is outside owned temporary storage")
                    fixtures.unix_path = path
                yield

            def pytest_collection_finish(self, session):
                actual = []
                for item in session.items:
                    required = (
                        item.nodeid.split("::")[0] == "tests/unit/test_admission.py"
                        and item.originalname in manifest["functions"]
                    )
                    params = (
                        encoded(getattr(getattr(item, "callspec", None), "params", {}))
                        if required else None
                    )
                    actual.append({
                        "nodeid": item.nodeid, "function": item.originalname,
                        "parameters": params,
                    })
                result["collected"] = actual
                selected_actual = [
                    item for item in actual
                    if item["nodeid"].split("::")[0] == "tests/unit/test_admission.py"
                    and item["function"] in manifest["functions"]
                ]
                if collections.Counter(
                    case_key(item["function"], item["parameters"]) for item in actual
                ) != expected_cases and not full_suite and not capture_suite:
                    pytest.exit("Exact selected case set changed", returncode=4)
                if full_suite:
                    if collections.Counter(
                        case_key(item["function"], item["parameters"]) for item in selected_actual
                    ) != expected_cases:
                        pytest.exit("Required regression selection changed", returncode=4)
                    collected_files = {item["nodeid"].split("::")[0] for item in actual}
                    if collected_files != set(manifest["unitSuite"]["testFiles"]):
                        pytest.exit("Default unit-suite file selection changed", returncode=4)
                if capture_suite:
                    if {item["nodeid"].split("::")[0] for item in actual} != set(capture_files):
                        pytest.exit("Identity capture file selection changed", returncode=4)
                    if fault_functions and {item["function"] for item in actual} != set(fault_functions):
                        pytest.exit("Capture fault safety selection changed", returncode=4)
                result["collectionVerified"] = True

            def pytest_collectreport(self, report):
                if report.failed:
                    result["collectionErrors"].append({
                        "nodeid": report.nodeid, "longrepr": str(report.longrepr)[:16000],
                    })

            def pytest_deselected(self, items):
                result["deselectedNodeIds"].extend(item.nodeid for item in items)

            def pytest_runtest_logstart(self, nodeid, location):
                result["testExecutionStarted"] = True

            def pytest_runtest_logreport(self, report):
                result["reports"].append({
                    "nodeid": report.nodeid, "when": report.when,
                    "outcome": report.outcome, "duration": report.duration,
                    "longrepr": str(report.longrepr)[:16000] if report.failed or report.skipped else None,
                })

        selectors = (
            [capture_files[0] + "::" + name for name in fault_functions] if fault_functions else
            ["tests/unit/"] if full_suite else capture_files if capture_suite else
            ["tests/unit/test_admission.py::" + name for name in manifest["functions"]]
        )
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
        if full_suite or capture_suite:
            for name, expected in unit_files.items():
                try:
                    if digest(ROOT / name) != expected:
                        changed.append(name)
                except OSError:
                    changed.append(name)
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
            result["collectionVerified"]
            and bool(collected)
            and (full_suite or capture_suite or len(collected) == len(manifest["cases"]))
            and collections.Counter(called) == collections.Counter(collected)
            and not result["skippedNodeIds"]
            and not result["setupOrTeardownFailures"]
            and not result["collectionErrors"]
            and not result["deselectedNodeIds"]
        )
        if not result["selectionComplete"]:
            result["status"] = "incomplete"
            if code == 0:
                code = 2
        result["allowedFixtureEvents"] = [
            {"nodeid": node, "event": event, "count": count}
            for (node, event), count in sorted(fixtures.allowed.items())
        ]
        if blocked or changed or fixtures.errors:
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