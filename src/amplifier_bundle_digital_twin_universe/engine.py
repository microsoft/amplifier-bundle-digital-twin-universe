# Copyright (c) Microsoft. All rights reserved.

"""Launch / exec / destroy orchestration.

Each public function corresponds to a CLI command and returns a JSON-
serialisable ``dict`` (or an ``int`` exit code for the interactive case).
"""

from __future__ import annotations

import base64
import glob
import inspect
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from amplifier_bundle_digital_twin_universe import incus
from amplifier_bundle_digital_twin_universe.incus import IncusError
from dataclasses import dataclass

import yaml as _yaml

from amplifier_bundle_digital_twin_universe.profile import (
    Profile,
    _PATH_BOUNDARY_CHARS,
    _path_matches,
    has_unresolved_vars,
    load_profile,
    load_profile_from_content,
)

# Path inside each DTU container where the resolved profile snapshot is stored
# at launch time.  ``update`` reads from this path so it does not depend on
# the original on-host profile file still being present.
_PROFILE_SNAPSHOT_PATH = "/opt/dtu/profile.yaml"

# Port used by the local pypiserver inside the container.
_PYPI_SERVER_PORT = 8081

# Default Incus container config applied to every DTU launch. Profiles can
# override any key (including setting it to "false") via ``base.config``.
#
# ``security.nesting=true`` is required for running Docker (and other container
# runtimes) inside Incus. DTU environments routinely need it for mock service
# sidecars, dockerized apps, and nested test fixtures, so it is on by default.
DEFAULT_BASE_CONFIG: dict[str, str] = {
    "security.nesting": "true",
}


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------


def _resolve_image(image: str) -> str:
    """Translate Docker-style image refs to Incus format.

    ``ubuntu:24.04`` -> ``images:ubuntu/24.04``

    Already-qualified refs (e.g. ``images:...``, ``local:...``) pass through.
    """
    # Already prefixed with a known Incus remote
    if image.startswith(("images:", "local:")):
        return image
    # Docker-style  distro:version -> images:distro/version
    if ":" in image:
        distro, version = image.split(":", 1)
        return f"images:{distro}/{version}"
    return image


# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------


def _wait_for_gateway(container_name: str, timeout: int = 60) -> str:
    """Block until *container_name* has networking and return the gateway IP."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return incus.get_host_gateway_ip(container_name)
        except IncusError as exc:
            last_err = exc
            time.sleep(1)
    raise RuntimeError(
        f"Container {container_name} did not obtain networking "
        f"within {timeout}s: {last_err}"
    )


def _rewrite_localhost(variables: dict[str, str], host_ip: str) -> dict[str, str]:
    """Replace ``localhost`` / ``127.0.0.1`` in variable values with *host_ip*.

    Inside the container ``localhost`` means the container itself, not the
    host.  The bridge gateway IP is how the container reaches the host.
    """
    return {
        k: v.replace("localhost", host_ip).replace("127.0.0.1", host_ip)
        for k, v in variables.items()
    }


# ---------------------------------------------------------------------------
# Proxy (mitmproxy) setup
# ---------------------------------------------------------------------------


def _should_setup_proxy(
    profile: Profile,
    running_services: list[RunningService] | None = None,
) -> bool:
    """Return *True* if the proxy should be configured."""
    # Services with domains always need the proxy.
    if running_services:
        for svc in running_services:
            if svc.domains:
                return True

    if not profile.url_rewrites or not profile.url_rewrites.rules:
        return False
    # Skip proxy when required variables are still unresolved.
    for rule in profile.url_rewrites.rules:
        if has_unresolved_vars(rule.target):
            return False
    return True


_NETWORK_ERROR_HINTS = (
    "Unable to connect to",
    "Could not connect to",
    "Cannot initiate the connection",
    "Network is unreachable",
    "Connection timed out",
    "Temporary failure resolving",
    "Could not resolve host",
)


def _exec_checked(container_name: str, command: str, timeout: int = 600) -> str:
    """Run *command* via ``bash -c`` inside *container_name*; raise on failure."""
    exit_code, stdout, stderr = incus.exec_command(
        container_name, ["bash", "-c", command], timeout=timeout
    )
    if exit_code != 0:
        combined = stdout + stderr
        if any(hint in combined for hint in _NETWORK_ERROR_HINTS):
            diag = incus.diagnose_network_failure(container_name)
            raise RuntimeError(
                f"Command failed (exit {exit_code}): {command}\n\n"
                f"Network diagnostic:\n{diag}\n\n"
                f"stdout: {stdout}\nstderr: {stderr}"
            )
        raise RuntimeError(
            f"Command failed (exit {exit_code}): {command}\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )
    return stdout


_ADDON_TEMPLATE = """\
import json as _json
import sys
from mitmproxy import http
from urllib.parse import urlparse

RULES = {rules!r}
PYPI_OVERRIDES = {pypi_overrides!r}
PYPI_SERVER_PORT = {pypi_server_port}
SERVICE_DOMAINS = {service_domains!r}

# ---- canonical matcher source, injected from profile.py via inspect.getsource
# The host-side validator and the in-container proxy share this source so they
# cannot drift. See profile.match_url for the host-side caller.
{matcher_source}
# ---- end canonical matcher source

def _log(msg):
    print(f"[rewrite] {{msg}}", file=sys.stderr, flush=True)


class RewriteAddon:
    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host
        path = flow.request.path

        # Mock service domain rewrites (HTTP + WebSocket upgrade).
        if host in SERVICE_DOMAINS:
            target = SERVICE_DOMAINS[host]
            _log(f"SERVICE {{host}}{{path}} -> {{target['host']}}:{{target['port']}}")
            # Stash the original domain so the response hook can find it.
            flow.metadata["dtu_service_domain"] = host
            flow.request.scheme = "http"
            flow.request.host = target["host"]
            flow.request.port = target["port"]
            return

        # URL rewrite rules (GitHub -> Gitea etc.).
        # RULES is emitted longest-prefix-first so first-match-wins picks
        # the most specific rule. The match decision is delegated to
        # ``_path_matches`` (injected above), which is the single source of
        # truth shared with the host-side ``profile.match_url``.
        for rule in RULES:
            if host != rule["match_host"]:
                continue
            prefix = rule["match_path_prefix"]
            if not _path_matches(rule.get("match_mode", "prefix"), prefix, path):
                continue
            target = urlparse(rule["target_url"])
            flow.request.scheme = target.scheme
            flow.request.host = target.hostname
            flow.request.port = target.port or (443 if target.scheme == "https" else 80)
            flow.request.path = target.path + path[len(prefix):]
            if rule.get("auth_header"):
                flow.request.headers["Authorization"] = rule["auth_header"]
            return

        # PyPI interception -- redirect Simple API index requests and
        # wheel downloads for overridden packages to the local pypiserver.
        if host in ("pypi.org", "files.pythonhosted.org"):
            _log(f"PYPI REQUEST {{host}}{{path}}")
        if host in ("pypi.org", "files.pythonhosted.org") and PYPI_OVERRIDES:
            for pkg in PYPI_OVERRIDES:
                normalized = pkg.replace("-", "-").replace(".", "-").lower()
                # PEP 503 simple index page
                if host == "pypi.org" and (
                    path.rstrip("/").endswith(f"/simple/{{normalized}}")
                    or path.startswith(f"/simple/{{normalized}}/")
                ):
                    _log(f"PYPI INDEX {{host}}{{path}} -> localhost:{{PYPI_SERVER_PORT}}")
                    flow.request.scheme = "http"
                    flow.request.host = "localhost"
                    flow.request.port = PYPI_SERVER_PORT
                    return
                # Wheel download -- match on the wheel filename prefix
                # (e.g. amplifier_core- matches amplifier_core-1.3.3-*.whl)
                wheel_prefix = normalized.replace("-", "_")
                if f"/{{wheel_prefix}}-" in path or f"/{{wheel_prefix}}-" in path.lower():
                    filename = path.rsplit("/", 1)[-1]
                    _log(f"PYPI WHEEL {{host}}{{path}} -> localhost:{{PYPI_SERVER_PORT}}/packages/{{filename}}")
                    flow.request.scheme = "http"
                    flow.request.host = "localhost"
                    flow.request.port = PYPI_SERVER_PORT
                    flow.request.path = f"/packages/{{filename}}"
                    return
            _log(f"PYPI PASS-THROUGH {{host}}{{path}}")

    def response(self, flow: http.HTTPFlow) -> None:
        # Rewrite Socket Mode WebSocket URLs in apps.connections.open responses.
        # The mock returns ws://HOST:PORT/ws but inside the container that IP
        # is only reachable without TLS.  Rewrite the URL to use the original
        # service domain so the bot connects via wss:// through mitmproxy,
        # which routes it to the mock transparently.
        original_domain = flow.metadata.get("dtu_service_domain")
        if not original_domain:
            return
        path = flow.request.path
        if "/apps.connections.open" not in path:
            return
        if not flow.response or not flow.response.content:
            return
        try:
            body = _json.loads(flow.response.content)
            if body.get("ok") and "url" in body:
                old_url = body["url"]
                # Replace ws://HOST:PORT/path with wss://DOMAIN/path so the
                # bot connects via TLS through mitmproxy, which intercepts
                # the domain and routes it to the mock transparently.
                parsed = urlparse(old_url)
                new_url = f"wss://{{original_domain}}{{parsed.path}}"
                if parsed.query:
                    new_url += f"?{{parsed.query}}"
                body["url"] = new_url
                flow.response.content = _json.dumps(body).encode()
                _log(f"REWRITE apps.connections.open: {{old_url}} -> {{new_url}}")
        except Exception:
            pass


addons = [RewriteAddon()]
"""


def _generate_addon_script(
    profile: Profile,
    variables: dict[str, str],
    running_services: list[RunningService] | None = None,
    host_ip: str = "10.0.0.1",
) -> str:
    """Generate the mitmproxy rewrite addon.

    Supports three kinds of interception:

    1. **Mock service domains** -- redirect all traffic (HTTP + WebSocket) for
       domains declared by mock services to the Docker sidecar.
    2. **URL rewrites** -- redirect git/HTTPS requests matching a host+path
       prefix to a different target (e.g. GitHub -> Gitea).
    3. **PyPI overrides** -- intercept PyPI Simple API requests for specific
       packages and redirect them to a local pypiserver.
    """
    rules: list[dict[str, str]] = []

    if profile.url_rewrites and profile.url_rewrites.rules:
        auth_header = ""
        if profile.url_rewrites.auth:
            token = variables.get(profile.url_rewrites.auth.token_var, "")
            username = profile.url_rewrites.auth.username
            cred = base64.b64encode(f"{username}:{token}".encode()).decode()
            auth_header = f"Basic {cred}"

        # Emit longest-prefix-first so the in-container matcher's
        # first-match-wins iteration produces the same answer as the
        # host-side ``match_url`` (which does the same stable sort).
        sorted_rules = sorted(
            profile.url_rewrites.rules,
            key=lambda r: -len(r.match.split("/", 1)[1]) if "/" in r.match else 0,
        )
        for rule in sorted_rules:
            parts = rule.match.split("/", 1)
            match_host = parts[0]
            match_path_prefix = "/" + parts[1] if len(parts) > 1 else "/"
            rules.append(
                {
                    "match_host": match_host,
                    "match_path_prefix": match_path_prefix,
                    "target_url": rule.target,
                    "auth_header": auth_header,
                    "match_mode": rule.match_mode,
                }
            )

    # Build service domain -> (host, port) mapping.
    # The mock Docker container publishes a port on the host.  From inside
    # the Incus container we reach it via the bridge gateway IP.
    service_domains: dict[str, dict[str, object]] = {}
    if running_services:
        for svc in running_services:
            for domain in svc.domains:
                service_domains[domain] = {"host": host_ip, "port": svc.host_port}

    # Collect PyPI override package names (PEP 503 normalized).
    pypi_overrides: list[str] = []
    if profile.pypi_overrides:
        for pkg in profile.pypi_overrides.packages:
            pypi_overrides.append(pkg.name.lower().replace("-", "-"))

    # Inject the canonical matcher source so the in-container addon executes
    # the exact same Python as the host-side ``profile.match_url``. This is
    # the single source of truth for per-request match decisions; no logic
    # is duplicated in the template body.
    matcher_source = (
        f"_PATH_BOUNDARY_CHARS = {_PATH_BOUNDARY_CHARS!r}\n\n"
        + inspect.getsource(_path_matches)
    )

    return _ADDON_TEMPLATE.format(
        rules=rules,
        pypi_overrides=pypi_overrides,
        pypi_server_port=_PYPI_SERVER_PORT,
        service_domains=service_domains,
        matcher_source=matcher_source,
    )


def _setup_proxy(
    container_name: str,
    profile: Profile,
    variables: dict[str, str],
    running_services: list[RunningService] | None = None,
    host_ip: str = "10.0.0.1",
) -> None:
    """Install mitmproxy inside *container_name* and start the rewrite daemon."""
    # 1. Install mitmproxy, pypiserver, and dependencies
    _exec_checked(
        container_name,
        "apt-get update && apt-get install -y python3-pip ca-certificates",
    )
    _exec_checked(
        container_name,
        "pip3 install mitmproxy pypiserver "
        "--break-system-packages --ignore-installed typing-extensions",
    )

    # 2. Push the rewrite addon script
    _exec_checked(container_name, "mkdir -p /opt/dtu")
    addon_script = _generate_addon_script(
        profile, variables, running_services=running_services, host_ip=host_ip
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(addon_script)
        local_addon = f.name
    try:
        incus.file_push(container_name, [local_addon], "/opt/dtu/rewrite_addon.py")
    finally:
        os.unlink(local_addon)

    # 3. Bootstrap CA certificate
    _exec_checked(container_name, "timeout 2 mitmdump || true")
    _exec_checked(
        container_name,
        "cp /root/.mitmproxy/mitmproxy-ca-cert.pem "
        "/usr/local/share/ca-certificates/mitmproxy.crt",
    )
    _exec_checked(container_name, "update-ca-certificates")

    # 4. Start mitmdump as a background daemon
    #
    # --allow-hosts restricts TLS interception to only the hosts that have
    # rewrite rules.  All other traffic (LLM APIs, other GitHub repos)
    # passes through as a plain TCP tunnel -- no TLS termination, no crypto
    # overhead, much faster.  Since LLM API traffic (SSE/streaming) is
    # tunnelled rather than intercepted, it streams natively without needing
    # the stream_large_bodies setting.
    rewrite_hosts: set[str] = set()

    if profile.url_rewrites and profile.url_rewrites.rules:
        rewrite_hosts.update(
            rule.match.split("/", 1)[0] for rule in profile.url_rewrites.rules
        )

    # If pypi_overrides are configured, also intercept PyPI TLS traffic.
    if profile.pypi_overrides and profile.pypi_overrides.packages:
        rewrite_hosts.add("pypi.org")
        rewrite_hosts.add("files.pythonhosted.org")

    # Add domains from mock services.
    if running_services:
        for svc in running_services:
            rewrite_hosts.update(svc.domains)

    allow_hosts_re = "|".join(re.escape(h) for h in sorted(rewrite_hosts))

    _exec_checked(
        container_name,
        "nohup mitmdump -s /opt/dtu/rewrite_addon.py -p 8080 "
        "--set ssl_insecure=true --set upstream_cert=false "
        f"--allow-hosts '{allow_hosts_re}' "
        "> /var/log/mitmdump.log 2>&1 &",
    )

    # 5. Wait for it to come up
    time.sleep(2)
    for _ in range(5):
        ec, _, _ = incus.exec_command(
            container_name, ["bash", "-c", "pgrep -f mitmdump"], timeout=10
        )
        if ec == 0:
            return
        time.sleep(1)

    _, log, _ = incus.exec_command(
        container_name,
        ["bash", "-c", "cat /var/log/mitmdump.log 2>/dev/null || true"],
        timeout=10,
    )
    raise RuntimeError(f"mitmdump failed to start.  Log:\n{log}")


# ---------------------------------------------------------------------------
# PyPI overrides -- wheel injection + pypiserver
# ---------------------------------------------------------------------------


def _run_host_command(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 600,
) -> str:
    """Run a host-side command and return stdout, raising on failure."""
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=env,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Host command failed (exit {result.returncode}): {shlex.join(args)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout


def _with_basic_auth(url: str, username: str, token: str) -> str:
    """Return *url* with basic-auth credentials embedded."""
    if "://" not in url:
        raise RuntimeError(f"Unsupported authenticated URL (missing scheme): {url}")
    scheme, rest = url.split("://", 1)
    user = quote(username, safe="")
    secret = quote(token, safe="")
    return f"{scheme}://{user}:{secret}@{rest}"


def _select_wheel_file(pattern: str, base_dir: Path) -> Path:
    """Resolve *pattern* to a single wheel file relative to *base_dir*."""
    search_pattern = pattern
    if not Path(pattern).is_absolute():
        search_pattern = str(base_dir / pattern)

    matches = [Path(p).resolve() for p in glob.glob(search_pattern)]
    matches = [p for p in matches if p.is_file()]
    if not matches:
        raise RuntimeError(f"No wheel matched: {search_pattern}")

    # If multiple wheels match, prefer the newest build artifact.
    return max(matches, key=lambda p: p.stat().st_mtime)


def _resolve_host_wheel(
    profile: Profile,
    pkg,
    variables: dict[str, str],
) -> tuple[Path, bool]:
    """Return the host wheel path for *pkg* and whether it is temporary."""
    if pkg.wheel_path:
        return _select_wheel_file(pkg.wheel_path, profile.path.parent), False

    if pkg.wheel_var:
        wheel_path = variables.get(pkg.wheel_var, "")
        if not wheel_path:
            raise RuntimeError(
                f"PyPI override for {pkg.name!r} requires variable "
                f"{pkg.wheel_var!r} (pass via --var {pkg.wheel_var}=/path/to/wheel.whl)"
            )
        return _select_wheel_file(wheel_path, Path.cwd()), False

    assert pkg.wheel_from_git is not None
    source = pkg.wheel_from_git

    clone_url = source.repo
    if has_unresolved_vars(clone_url):
        raise RuntimeError(
            f"PyPI override for {pkg.name!r} has unresolved variables in "
            f"wheel_from_git.repo: {clone_url!r}"
        )
    if source.token_var:
        token = variables.get(source.token_var, "")
        if not token:
            raise RuntimeError(
                f"PyPI override for {pkg.name!r} requires variable "
                f"{source.token_var!r} for git authentication"
            )
        clone_url = _with_basic_auth(clone_url, source.username or "git", token)

    build_root = Path(tempfile.mkdtemp(prefix=f"dtu-wheel-build-{pkg.name}-"))
    repo_dir = build_root / "repo"
    artifact_dir = Path(tempfile.mkdtemp(prefix=f"dtu-wheel-artifact-{pkg.name}-"))
    build_env = os.environ.copy()
    build_env.setdefault("UV_CACHE_DIR", str(build_root / ".uv-cache"))

    try:
        print(f"  building wheel for {pkg.name} from git...", file=sys.stderr)
        _run_host_command(["git", "clone", clone_url, str(repo_dir)], timeout=300)
        if source.ref:
            _run_host_command(
                ["git", "checkout", source.ref],
                cwd=repo_dir,
                timeout=120,
            )
        _run_host_command(
            ["bash", "-lc", source.build_cmd],
            cwd=repo_dir,
            env=build_env,
            timeout=900,
        )
        built_wheel = _select_wheel_file(source.wheel_glob, repo_dir)
        materialized = artifact_dir / built_wheel.name
        shutil.copy2(built_wheel, materialized)
        return materialized, True
    finally:
        shutil.rmtree(build_root, ignore_errors=True)


def _setup_pypi_overrides(
    container_name: str, profile: Profile, variables: dict[str, str]
) -> None:
    """Resolve host-side wheels, push them into the container, and start pypiserver."""
    assert profile.pypi_overrides is not None  # caller checked

    _exec_checked(container_name, "mkdir -p /opt/dtu/wheels")

    for pkg in profile.pypi_overrides.packages:
        host_path, temporary = _resolve_host_wheel(profile, pkg, variables)
        try:
            print(
                f"  pushing wheel: {host_path.name} -> /opt/dtu/wheels/",
                file=sys.stderr,
            )
            incus.file_push(
                container_name, [str(host_path)], f"/opt/dtu/wheels/{host_path.name}"
            )
        finally:
            if temporary:
                try:
                    host_path.unlink()
                except FileNotFoundError:
                    pass
                try:
                    host_path.parent.rmdir()
                except OSError:
                    pass

    # Start pypiserver as a background daemon serving the wheels directory.
    _exec_checked(
        container_name,
        f"nohup pypi-server run -p {_PYPI_SERVER_PORT} /opt/dtu/wheels "
        "> /var/log/pypiserver.log 2>&1 &",
    )

    # Wait for pypiserver to come up.
    # curl is not yet installed (it's provisioned later), so use pgrep.
    time.sleep(1)
    for _ in range(5):
        ec, _, _ = incus.exec_command(
            container_name,
            ["bash", "-c", "pgrep -f pypi-server"],
            timeout=10,
        )
        if ec == 0:
            return
        time.sleep(1)

    _, log, _ = incus.exec_command(
        container_name,
        ["bash", "-c", "cat /var/log/pypiserver.log 2>/dev/null || true"],
        timeout=10,
    )
    raise RuntimeError(f"pypiserver failed to start.  Log:\n{log}")


# ---------------------------------------------------------------------------
# Mock services -- Docker sidecar lifecycle
# ---------------------------------------------------------------------------

_DOCKER_LABEL_MANAGED_BY = "dtu.managed-by"
_DOCKER_LABEL_ENV_ID = "dtu.env-id"
_DOCKER_LABEL_MOCK_NAME = "dtu.mock-name"


@dataclass
class MockManifest:
    """Parsed ``digital-twin-mock.yaml`` from a mock service repo."""

    name: str
    version: str
    description: str
    runtime_type: str
    runtime_build: str | None
    runtime_image: str | None
    runtime_port: int
    domains: list[str]


@dataclass
class RunningService:
    """Tracking info for a running mock service container."""

    name: str
    container_id: str
    host_port: int
    domains: list[str]


def _resolve_service_source(source: str) -> Path:
    """Clone from GitHub or resolve a local path.  Returns local directory."""
    if source.startswith(("http://", "https://", "github.com")):
        url = source if "://" in source else f"https://{source}"
        clone_dir = Path(tempfile.mkdtemp(prefix="dtu-mock-"))
        _run_host_command(
            ["git", "clone", "--depth", "1", url, str(clone_dir)],
            timeout=120,
        )
        return clone_dir
    # Local path (absolute or relative to CWD).
    local = Path(source).resolve()
    if not local.is_dir():
        raise RuntimeError(f"Mock service source is not a directory: {local}")
    return local


def _read_mock_manifest(service_dir: Path) -> MockManifest:
    """Parse ``digital-twin-mock.yaml`` from *service_dir*."""
    manifest_path = service_dir / "digital-twin-mock.yaml"
    if not manifest_path.exists():
        raise RuntimeError(
            f"Mock manifest not found: {manifest_path}\n"
            f"Each mock service must have a digital-twin-mock.yaml at its root."
        )
    raw = _yaml.safe_load(manifest_path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(
            f"Mock manifest must be a YAML mapping, got {type(raw).__name__}"
        )

    runtime = raw.get("runtime", {})
    return MockManifest(
        name=raw["name"],
        version=raw.get("version", "0.0.0"),
        description=raw.get("description", ""),
        runtime_type=runtime.get("type", "docker"),
        runtime_build=runtime.get("build"),
        runtime_image=runtime.get("image"),
        runtime_port=int(runtime.get("port", 3000)),
        domains=raw.get("domains", []),
    )


def _build_mock_image(manifest: MockManifest, service_dir: Path) -> str:
    """Build a Docker image for the mock.  Returns the image tag."""
    tag = f"dtu-mock-{manifest.name}:{manifest.version}"
    dockerfile = manifest.runtime_build or "Dockerfile"
    _run_host_command(
        ["docker", "build", "-t", tag, "-f", dockerfile, "."],
        cwd=service_dir,
        timeout=300,
    )
    return tag


def _start_mock_container(
    env_id: str,
    manifest: MockManifest,
    config: dict[str, str],
    image: str,
) -> tuple[str, int]:
    """Start a Docker container for the mock.

    Returns ``(container_id, host_port)``.
    """
    container_name = f"dtu-mock-{manifest.name}-{env_id}"
    cmd: list[str] = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--label",
        f"{_DOCKER_LABEL_MANAGED_BY}=amplifier-digital-twin",
        "--label",
        f"{_DOCKER_LABEL_ENV_ID}={env_id}",
        "--label",
        f"{_DOCKER_LABEL_MOCK_NAME}={manifest.name}",
        "-p",
        f"0:{manifest.runtime_port}",
    ]

    # Pass config as environment variables.
    for k, v in config.items():
        env_key = k.upper()
        cmd.extend(["-e", f"{env_key}={v}"])

    cmd.append(image)

    stdout = _run_host_command(cmd, timeout=60)
    container_id = stdout.strip()[:12]

    # Read the assigned host port.
    port_output = _run_host_command(
        ["docker", "port", container_name, str(manifest.runtime_port)],
        timeout=10,
    )
    # Output is like "0.0.0.0:38421" or "[::]:38421"
    host_port = int(port_output.strip().rsplit(":", 1)[-1])

    return container_id, host_port


def _stop_mock_containers(env_id: str) -> None:
    """Stop and remove all Docker mock containers for *env_id*."""
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label={_DOCKER_LABEL_ENV_ID}={env_id}",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return
    names = [n.strip() for n in result.stdout.strip().split("\n") if n.strip()]
    for name in names:
        subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True,
            timeout=30,
        )


def _setup_services(
    env_id: str,
    profile: Profile,
) -> list[RunningService]:
    """Resolve, build, and start all mock services declared in *profile*.

    Returns a list of :class:`RunningService` for wiring into mitmproxy.
    """
    running: list[RunningService] = []
    for svc in profile.mock_services:
        source = svc.source
        if has_unresolved_vars(source):
            raise RuntimeError(
                f"Service source has unresolved variables: {source!r}. "
                f"Pass the required --var at launch time."
            )

        print(f"  resolving mock service: {source}", file=sys.stderr)
        service_dir = _resolve_service_source(source)
        manifest = _read_mock_manifest(service_dir)

        print(f"  building Docker image: dtu-mock-{manifest.name}...", file=sys.stderr)
        image = _build_mock_image(manifest, service_dir)

        print(f"  starting mock container: {manifest.name}...", file=sys.stderr)
        container_id, host_port = _start_mock_container(
            env_id, manifest, svc.config, image
        )
        print(
            f"  mock {manifest.name} running on host port {host_port}",
            file=sys.stderr,
        )

        running.append(
            RunningService(
                name=manifest.name,
                container_id=container_id,
                host_port=host_port,
                domains=manifest.domains,
            )
        )

    return running


# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------


def _write_env(
    container_name: str,
    profile: Profile,
    variables: dict[str, str],
    proxy_enabled: bool,
) -> None:
    """Write ``/etc/profile.d/dtu-env.sh`` inside the container."""
    lines: list[str] = [
        "#!/bin/bash",
        'export PATH="/root/.cargo/bin:/root/.local/bin:$PATH"',
    ]

    if proxy_enabled:
        lines.extend(
            [
                'export HTTP_PROXY="http://localhost:8080"',
                'export HTTPS_PROXY="http://localhost:8080"',
                'export http_proxy="http://localhost:8080"',
                'export https_proxy="http://localhost:8080"',
                # Exempt loopback from the proxy.  Without this, an
                # in-container client talking to an in-container server on
                # localhost routes through mitmproxy, which buffers whole
                # response bodies and destroys SSE / token streaming.
                # Nothing needs loopback traffic proxied: the pypiserver
                # redirect happens proxy-side (the addon rewrites
                # flow.request.host) and mock services resolve to the host
                # gateway IP, not loopback.
                'export no_proxy="localhost,127.0.0.1,::1"',
                'export NO_PROXY="localhost,127.0.0.1,::1"',
                # uv bundles its own TLS certs and ignores the system store
                # by default.  UV_NATIVE_TLS makes it use OpenSSL / the
                # system cert bundle where we installed the mitmproxy CA.
                "export UV_NATIVE_TLS=true",
                # Belt-and-suspenders for pip, requests, and other tools.
                'export SSL_CERT_FILE="/etc/ssl/certs/ca-certificates.crt"',
                'export REQUESTS_CA_BUNDLE="/etc/ssl/certs/ca-certificates.crt"',
            ]
        )

        # Disable uv's GitHub fast path unless a profile explicitly opts back
        # in. The fast path calls api.github.com directly to resolve
        # @<branch> -> SHA and fetches pyproject.toml from
        # raw.githubusercontent.com; both bypass our url_rewrites and
        # silently install the upstream commit even when a Gitea mirror has
        # a different HEAD. With this set, uv falls back to git fetch, which
        # routes through the proxy and is correctly rewritten. Requires
        # uv >= 0.7.13. See `url_rewrites.allow_uv_github_fast_path` in
        # docs/profiles.md to opt back into uv's native behavior.
        if (
            profile.url_rewrites is None
            or not profile.url_rewrites.allow_uv_github_fast_path
        ):
            lines.append("export UV_NO_GITHUB_FAST_PATH=true")

    # When pypi_overrides are configured, tell uv/pip to check the local
    # pypiserver first.  The proxy intercepts pypi.org Simple API requests
    # for overridden packages, but uv's resolver can bypass the Simple API
    # in some flows (e.g. uv tool install from git sources).  Setting the
    # extra index URL ensures the local wheel is always found.
    if profile.pypi_overrides and profile.pypi_overrides.packages:
        lines.append(
            f'export UV_EXTRA_INDEX_URL="http://localhost:{_PYPI_SERVER_PORT}/simple/"'
        )
        lines.append(
            f'export PIP_EXTRA_INDEX_URL="http://localhost:{_PYPI_SERVER_PORT}/simple/"'
        )

    # Passthrough env vars from the host
    if profile.passthrough:
        for svc in profile.passthrough.services:
            if svc.key_env:
                value = os.environ.get(svc.key_env, "")
                if value:
                    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
                    lines.append(f'export {svc.key_env}="{escaped}"')

    env_script = "\n".join(lines) + "\n"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(env_script)
        local_env = f.name
    try:
        incus.file_push(container_name, [local_env], "/etc/profile.d/dtu-env.sh")
    finally:
        os.unlink(local_env)

    _exec_checked(container_name, "chmod +x /etc/profile.d/dtu-env.sh")


_VISUAL_ID_PROFILE_D_PATH = "/etc/profile.d/dtu-visual-id.sh"

# Static profile.d script that prepends a blue ``(dtu:<label>)`` marker to
# PS1 when the attaching shell has ``DTU_VISUAL_ID`` set. Written once at
# launch alongside ``dtu-env.sh``; inert when the env var is unset, so it
# costs nothing for any other exec path.
#
# Uses PROMPT_COMMAND rather than a direct PS1 assignment so the prefix
# survives a later ``~/.bashrc`` that resets PS1. PROMPT_COMMAND runs right
# before each prompt is drawn -- after /etc/profile, /etc/profile.d/*.sh,
# ~/.profile, and ~/.bashrc have all completed. The idempotency check
# prevents the prefix from stacking on every redraw.
_VISUAL_ID_PROFILE_D_SCRIPT = """\
#!/bin/bash
# amplifier-digital-twin visual id prompt injection
# Activates only when DTU_VISUAL_ID is set on the attaching shell's env.
# Bash-only; gated on interactive shells (PS1 only matters there).
case $- in
  *i*)
    if [ -n "${DTU_VISUAL_ID:-}" ]; then
      _dtu_apply_prompt() {
        case "$PS1" in
          *"(dtu:${DTU_VISUAL_ID})"*) ;;  # already applied this draw
          *) PS1="\\[\\e[1;34m\\](dtu:${DTU_VISUAL_ID})\\[\\e[0m\\] $PS1" ;;
        esac
      }
      PROMPT_COMMAND="_dtu_apply_prompt${PROMPT_COMMAND:+; $PROMPT_COMMAND}"
    fi
    ;;
esac
"""


def _write_visual_id_profile_d(container_name: str) -> None:
    """Write ``/etc/profile.d/dtu-visual-id.sh`` inside the container.

    The script is content-static: it does not embed the visual-id value.
    The per-attach label is supplied via the ``DTU_VISUAL_ID`` env var when
    ``exec_interactive`` runs ``bash -l``. This keeps multi-attach safe and
    means launch-time installation is a one-shot operation.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(_VISUAL_ID_PROFILE_D_SCRIPT)
        local_path = f.name
    try:
        incus.file_push(container_name, [local_path], _VISUAL_ID_PROFILE_D_PATH)
    finally:
        os.unlink(local_path)
    _exec_checked(container_name, f"chmod +x {_VISUAL_ID_PROFILE_D_PATH}")


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


def _push_provision_files(container_name: str, profile: Profile) -> None:
    """Push ``provision.files`` entries into the container."""
    assert profile.provision is not None
    for entry in profile.provision.files:
        src = (profile.path.parent / entry.src).resolve()
        if not src.exists():
            raise RuntimeError(f"Provision file not found: {src}")
        print(f"  provision file: {entry.src} -> {entry.dest}", file=sys.stderr)
        incus.file_push(
            container_name,
            [str(src)],
            entry.dest,
            recursive=entry.recursive,
            create_dirs=entry.create_dirs,
            mode=entry.mode,
            uid=entry.uid,
            gid=entry.gid,
        )


def _run_provisioning(container_name: str, commands: list[str]) -> None:
    """Execute each provisioning command with a login shell (env-aware)."""
    for cmd in commands:
        print(f"  provision: {cmd}", file=sys.stderr)
        exit_code, stdout, stderr = incus.exec_command(
            container_name, ["bash", "-lc", cmd]
        )
        if exit_code != 0:
            raise RuntimeError(
                f"Provisioning failed (exit {exit_code}): {cmd}\n"
                f"stdout: {stdout}\nstderr: {stderr}"
            )


# ===================================================================
# Instance cap enforcement
# ===================================================================
#
# There is no reaper or TTL anywhere in this system -- an instance launched
# and never destroyed sits there indefinitely.  Repeated or looped launches
# (e.g. from an automated eval/pipeline, or many stateless agent delegations
# each launching their own instance) can silently accumulate containers until
# the host runs out of disk, memory, or CPU.  This guard is a hard backstop:
# `launch` refuses outright once the number of live DTU instances meets a
# configurable cap.  It does not destroy anything and is not a substitute for
# callers actually tearing down instances they no longer need -- see
# docs/profiles.md#instance-lifecycle.

_DEFAULT_MAX_INSTANCES = 15
_MAX_INSTANCES_ENV_VAR = "AMPLIFIER_DTU_MAX_INSTANCES"


def resolve_max_instances(cli_value: int | None) -> int:
    """Resolve the effective ``--max-instances`` cap.

    Resolution order (first one set wins):
      1. ``cli_value`` -- the ``--max-instances`` CLI flag, if passed.
      2. ``AMPLIFIER_DTU_MAX_INSTANCES`` environment variable, if set.
      3. Default: ``15``.

    A resolved value of ``0`` means unlimited (the guard is disabled).
    """
    if cli_value is not None:
        return cli_value
    env_value = os.environ.get(_MAX_INSTANCES_ENV_VAR)
    if env_value is not None and env_value.strip() != "":
        try:
            return int(env_value.strip())
        except ValueError as exc:
            raise ValueError(
                f"Invalid {_MAX_INSTANCES_ENV_VAR} value: {env_value!r}. "
                "Must be an integer."
            ) from exc
    return _DEFAULT_MAX_INSTANCES


def count_live_instances() -> int:
    """Count live DTU-managed instances.

    Reuses the exact same discovery mechanism as ``list`` / ``list_environments``
    (``incus list <managed-by key>=<value> --format=json``), so this count always
    matches what ``amplifier-digital-twin list`` reports.
    """
    return len(incus.list_instances(_MANAGED_BY_KEY, _MANAGED_BY_VALUE))


def _enforce_max_instances(max_instances: int) -> None:
    """Raise if the live instance count already meets/exceeds *max_instances*.

    ``max_instances == 0`` means unlimited -- the check is skipped entirely.
    Must be called *before* any container is created so a refusal never
    leaves a partially-provisioned instance behind.
    """
    if max_instances == 0:
        return
    current = count_live_instances()
    if current >= max_instances:
        raise RuntimeError(
            f"Refusing to launch: {current} DTU instance(s) already running, "
            f"which meets or exceeds --max-instances={max_instances}. "
            "Nothing here auto-deletes instances -- destroy ones you no "
            "longer need (`amplifier-digital-twin list` / "
            "`amplifier-digital-twin destroy <id>`), or raise the cap "
            "explicitly with --max-instances N (0 = unlimited) or the "
            f"{_MAX_INSTANCES_ENV_VAR} environment variable."
        )


# ===================================================================
# Public API
# ===================================================================


def launch(
    profile_arg: str,
    variables: dict[str, str],
    name: str | None = None,
    hostname: str | None = None,
    max_instances: int | None = None,
) -> dict:
    """Launch a Digital Twin Universe.  Returns the JSON status dict."""
    incus.check_incus()

    # Enforce the instance cap before touching Incus at all -- a refusal here
    # must never leave a partially-created container behind.
    _enforce_max_instances(resolve_max_instances(max_instances))

    # Quick-load to get the base image.
    host_profile = load_profile(profile_arg, variables)

    container_name = name or f"dtu-{uuid.uuid4().hex[:8]}"
    image = _resolve_image(host_profile.base.image)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Creating container {container_name} ({image})...", file=sys.stderr)
    # Default-on with override: always inject security.nesting=true unless the
    # profile explicitly sets it (including setting it to "false"). Nesting is
    # required for running Docker (and other container runtimes) inside Incus,
    # and DTU environments routinely need it for mock service sidecars,
    # dockerized apps, and nested test fixtures.
    effective_config = {
        **DEFAULT_BASE_CONFIG,
        **(host_profile.base.config or {}),
    }
    incus.create_container(container_name, image, config=effective_config)

    try:
        # Store metadata so `status` and `list` can discover this instance.
        incus.set_config(
            container_name, "user.dtu.managed-by", "amplifier-digital-twin"
        )
        incus.set_config(container_name, "user.dtu.profile", host_profile.name)
        incus.set_config(container_name, "user.dtu.created-at", now)

        # Push a snapshot of the resolved profile source into the container so
        # `update` can re-read it without depending on the host filesystem
        # still having the original file at the same location.  We push the
        # raw pre-substitution YAML so `--var` values supplied at update time
        # are re-applied freshly (matching launch-time semantics).
        incus.file_push(
            container_name,
            [str(host_profile.path)],
            _PROFILE_SNAPSHOT_PATH,
            create_dirs=True,
        )

        # Detect host gateway IP (retries until networking is up).
        host_ip = _wait_for_gateway(container_name)
        print(f"  host gateway: {host_ip}", file=sys.stderr)

        # Rewrite localhost -> gateway IP and reload profile.
        # validate=False: validation already ran on the host_profile parse
        # above; re-emitting warnings here would produce duplicates.
        rewritten_vars = _rewrite_localhost(variables, host_ip)
        profile = load_profile(profile_arg, rewritten_vars, validate=False)

        # Mock services -- resolve, build, and start Docker sidecars.
        running_services: list[RunningService] = []
        if profile.mock_services:
            print("Setting up mock services...", file=sys.stderr)
            running_services = _setup_services(container_name, profile)

        # Proxy
        proxy_enabled = _should_setup_proxy(profile, running_services)
        if proxy_enabled:
            print("Setting up mitmproxy...", file=sys.stderr)
            _setup_proxy(
                container_name,
                profile,
                rewritten_vars,
                running_services=running_services,
                host_ip=host_ip,
            )
        else:
            print(
                "Skipping proxy (no url_rewrites, mock_services, or unresolved vars).",
                file=sys.stderr,
            )

        # PyPI overrides -- push wheels and start pypiserver
        if host_profile.pypi_overrides and host_profile.pypi_overrides.packages:
            print("Setting up PyPI overrides...", file=sys.stderr)
            _setup_pypi_overrides(container_name, host_profile, variables)

        # Environment variables
        _write_env(container_name, profile, rewritten_vars, proxy_enabled)

        # Visual-id prompt-prefix profile.d script (inert unless an
        # `exec --visual-id LABEL` session passes DTU_VISUAL_ID in the env).
        _write_visual_id_profile_d(container_name)

        # Provision files (pushed before setup_cmds so commands can use them)
        if profile.provision and profile.provision.files:
            print("Pushing provision files...", file=sys.stderr)
            _push_provision_files(container_name, profile)

        # Provisioning
        if profile.provision and profile.provision.setup_cmds:
            print("Running provisioning...", file=sys.stderr)
            _run_provisioning(container_name, profile.provision.setup_cmds)

        # Hostname registration (must happen before URL construction).
        resolved_hostname: str | None = None
        raw_hostname = hostname  # CLI --hostname takes priority
        if not raw_hostname and profile.access and profile.access.hostname:
            raw_hostname = profile.access.hostname
        if not raw_hostname:
            raw_hostname = container_name

        from amplifier_bundle_digital_twin_universe.hostname import HostnameManager

        hostname_mgr = HostnameManager(raw_hostname, container_name)
        hostname_result = hostname_mgr.register()
        if hostname_result:
            resolved_hostname = hostname_result["hostname"]
            incus.set_config(container_name, "user.dtu.hostname", resolved_hostname)

        # Port forwarding via Incus proxy devices
        access_urls: list[dict[str, str]] = []
        if profile.access and profile.access.ports:
            container_ip = incus.get_container_ip(container_name)
            for pm in profile.access.ports:
                device = f"proxy-{pm.host}"
                incus.add_proxy_device(
                    container_name,
                    device,
                    pm.host,
                    pm.container,
                )
                url = f"http://localhost:{pm.host}{pm.path}"
                label = pm.label or f"port {pm.host}"
                entry: dict[str, str] = {"label": label, "url": url}
                if resolved_hostname:
                    entry["mdns_url"] = f"http://{resolved_hostname}:{pm.host}{pm.path}"
                access_urls.append(entry)
                print(
                    f"  forwarding :{pm.host} -> :{pm.container} ({label})",
                    file=sys.stderr,
                )

            # Nested-Incus support: if the launch is being driven from inside
            # another Incus instance (e.g. a reality-check runner sibling), the
            # new DTU's host-side proxy device is bound on the host's loopback —
            # but the calling instance has its own (empty) loopback.  Without a
            # self-proxy, `localhost:<port>` from inside the calling instance
            # can't reach the SUT.
            #
            # Add a proxy device on the calling instance that listens on its own
            # 127.0.0.1 and connects to the new DTU's container IP.  This makes
            # `localhost:<port>` work from inside the caller, matching the
            # bundle's documented contract.
            caller_name = incus.running_inside_incus_instance()
            if caller_name and container_ip:
                for pm in profile.access.ports:
                    try:
                        incus.add_proxy_device(
                            caller_name,
                            f"sut-proxy-{pm.host}",
                            pm.host,
                            pm.container,
                            connect_host=container_ip,
                        )
                        print(
                            f"  nested-Incus self-proxy: {caller_name}:"
                            f"{pm.host} -> {container_ip}:{pm.container}",
                            file=sys.stderr,
                        )
                    except Exception as exc:
                        # Best-effort: if we can't self-proxy, the caller can
                        # still reach the SUT via its container IP.  Don't fail
                        # the launch.
                        print(
                            f"WARNING: nested-Incus self-proxy failed for "
                            f"{caller_name}:{pm.host} -> "
                            f"{container_ip}:{pm.container} ({exc})",
                            file=sys.stderr,
                        )
        else:
            container_ip = None

        # Store access.ports config as metadata for check-readiness.
        if profile.access and profile.access.ports:
            import json as _json_access

            access_config = [
                {
                    "host": pm.host,
                    "container": pm.container,
                    "label": pm.label,
                    "path": pm.path,
                    "verify": pm.verify,
                    "verify_timeout": pm.verify_timeout,
                    "verify_interval": pm.verify_interval,
                }
                for pm in profile.access.ports
            ]
            incus.set_config(
                container_name,
                "user.dtu.access-ports",
                _json_access.dumps(access_config),
            )

        # Store readiness config as metadata for check-readiness.
        info: list[str] = []
        if profile.readiness:
            readiness_config = [
                {
                    "name": c.name,
                    **(
                        {
                            "http": {
                                "url": c.http.url,
                                **(
                                    {"expect_json": c.http.expect_json}
                                    if c.http.expect_json
                                    else {}
                                ),
                            }
                        }
                        if c.http
                        else {}
                    ),
                    **({"tcp": {"port": c.tcp.port}} if c.tcp else {}),
                    **({"command": c.command} if c.command else {}),
                }
                for c in profile.readiness
            ]
            import json as _json

            incus.set_config(
                container_name,
                "user.dtu.readiness",
                _json.dumps(readiness_config),
            )
            info.append(
                f"Readiness checks configured. Poll with: "
                f"amplifier-digital-twin check-readiness {container_name}"
            )

        print(f"DTU {container_name} ready.", file=sys.stderr)
        result: dict = {
            "id": container_name,
            "name": container_name,
            "profile": profile.name,
            "status": "running",
            "created_at": now,
            "info": info,
        }
        if resolved_hostname:
            result["hostname"] = resolved_hostname
        if container_ip:
            result["container_ip"] = container_ip
        if access_urls:
            result["access"] = access_urls
        if running_services:
            result["mock_services"] = [
                {
                    "name": svc.name,
                    "container_id": svc.container_id,
                    "host_port": svc.host_port,
                    "domains": svc.domains,
                }
                for svc in running_services
            ]
        return result
    except Exception:
        # Best-effort cleanup on failure.
        try:
            from amplifier_bundle_digital_twin_universe.hostname import (
                HostnameManager as _HM,
            )

            _hm_hostname = incus.get_config(container_name, "user.dtu.hostname")
            if _hm_hostname:
                _HM(_hm_hostname.removesuffix(".local"), container_name).unregister()
        except Exception:
            pass
        try:
            _stop_mock_containers(container_name)
        except Exception:
            pass
        try:
            incus.delete_container(container_name, force=True)
        except Exception:
            pass
        raise


def _read_profile_snapshot(container_id: str) -> str | None:
    """Return the raw YAML text stored at launch time, or None if absent.

    Containers launched before the snapshot feature landed will not have the
    file at ``/opt/dtu/profile.yaml``; callers should fall back to
    re-resolving the profile from host search paths in that case.
    """
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w+", suffix=".yaml", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            incus.file_pull(container_id, [_PROFILE_SNAPSHOT_PATH], tmp_path)
        except IncusError:
            return None
        return Path(tmp_path).read_text()
    finally:
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)


def update(
    container_id: str,
    variables: dict[str, str],
    skip_readiness: bool = False,
) -> dict:
    """Update provisioned software in a running environment.

    Re-runs the ``update`` section of the profile: optionally refreshes PyPI
    overrides (rebuild wheels, re-push), then executes the update commands.
    If the profile defines readiness checks they are re-run unless
    *skip_readiness* is True.

    The profile is read from a snapshot stored inside the container at
    ``/opt/dtu/profile.yaml`` (written at launch time).  For containers
    launched before this snapshot feature existed, falls back to resolving
    the profile by name from host-side search paths.
    """
    incus.check_incus()

    if not incus.container_exists(container_id):
        raise RuntimeError(f"Environment not found: {container_id}")

    state = incus.get_instance_state(container_id)
    if state != "Running":
        raise RuntimeError(
            f"Environment {container_id} is not running (state: {state})"
        )

    # Detect host gateway and rewrite localhost references in user-supplied vars.
    host_ip = _wait_for_gateway(container_id)
    rewritten_vars = _rewrite_localhost(variables, host_ip)

    # Prefer the in-container snapshot written at launch time.  Fall back to
    # re-resolving by name from host search paths for pre-upgrade containers.
    snapshot_yaml = _read_profile_snapshot(container_id)
    if snapshot_yaml is not None:
        profile_name = (
            incus.get_config(container_id, "user.dtu.profile") or "<snapshot>"
        )
        # validate=False: this is a re-parse during update/destroy of an
        # already-launched DTU. Validation ran at original launch; re-running
        # it here would produce duplicate warnings and would also fail loudly
        # if a future bundle release tightens the schema beyond what the
        # snapshot was authored against.
        profile = load_profile_from_content(
            snapshot_yaml, rewritten_vars, validate=False
        )
    else:
        profile_name = incus.get_config(container_id, "user.dtu.profile")
        if not profile_name:
            raise RuntimeError(
                f"Environment {container_id} has no stored profile name or snapshot"
            )
        print(
            "Warning: container has no embedded profile snapshot at "
            f"{_PROFILE_SNAPSHOT_PATH}; falling back to host-side resolution "
            f"of profile {profile_name!r}. This container was likely launched "
            "before the snapshot feature was added.",
            file=sys.stderr,
        )
        # validate=False: same justification as the snapshot path above --
        # this is a re-parse for an already-launched DTU.
        profile = load_profile(profile_name, rewritten_vars, validate=False)

    if not profile.update:
        raise RuntimeError(
            f"Profile {profile_name!r} does not define an 'update' section"
        )

    # Refresh PyPI overrides if requested and available.
    pypi_refreshed = False
    if (
        profile.update.refresh_pypi
        and profile.pypi_overrides
        and profile.pypi_overrides.packages
    ):
        print("Refreshing PyPI overrides...", file=sys.stderr)
        # Kill existing pypiserver and clear old wheels.
        incus.exec_command(
            container_id,
            ["bash", "-c", "pkill -f pypi-server || true"],
            timeout=10,
        )
        incus.exec_command(
            container_id,
            ["bash", "-c", "rm -f /opt/dtu/wheels/*.whl"],
            timeout=10,
        )
        _setup_pypi_overrides(container_id, profile, variables)
        pypi_refreshed = True

    # Run update commands.
    print("Running update commands...", file=sys.stderr)
    _run_provisioning(container_id, profile.update.cmds)

    result: dict = {
        "id": container_id,
        "profile": profile_name,
        "status": "updated",
        "pypi_refreshed": pypi_refreshed,
        "cmds_run": len(profile.update.cmds),
    }

    # Re-run readiness checks unless skipped.
    if not skip_readiness:
        readiness_result = check_readiness(container_id)
        result["readiness"] = readiness_result

    print(f"DTU {container_id} updated.", file=sys.stderr)
    return result


def exec_command(
    container_id: str,
    command: list[str],
    *,
    timeout: int | None = 600,
) -> dict:
    """Run *command* inside the environment.  Returns JSON status dict.

    ``timeout`` is the maximum number of seconds to wait for the command
    to complete.  Pass ``None`` to disable the timeout entirely.  Default
    is 600 seconds.
    """
    if not incus.container_exists(container_id):
        raise RuntimeError(f"Environment not found: {container_id}")

    cmd_str = shlex.join(command)
    exit_code, stdout, stderr = incus.exec_command(
        container_id, ["bash", "-lc", cmd_str], timeout=timeout
    )
    return {
        "id": container_id,
        "command": cmd_str,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }


def exec_stream(
    container_id: str,
    command: list[str],
    *,
    timeout: int | None = 600,
) -> int:
    """Run *command* inside the environment with real-time output.

    stdout and stderr stream to the terminal as produced.  Returns
    the command's exit code (no JSON envelope).

    ``timeout`` is the maximum number of seconds to wait for the command
    to complete.  Pass ``None`` to disable the timeout entirely.  Default
    is 600 seconds.
    """
    if not incus.container_exists(container_id):
        raise RuntimeError(f"Environment not found: {container_id}")

    cmd_str = shlex.join(command)
    return incus.exec_stream(container_id, ["bash", "-lc", cmd_str], timeout=timeout)


_VISUAL_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,40}$")


def sanitize_visual_id(value: str) -> str:
    """Validate *value* is safe to embed in a shell env var.

    Only allows alphanumerics, dot, underscore, colon, slash, and hyphen.
    Caps at 40 characters to keep prompts readable. The character set
    excludes ``$``, backticks, quotes, backslashes, and whitespace so the
    value is safe to expand inside the profile.d script that builds PS1.

    Raises ValueError on invalid input.
    """
    if not _VISUAL_ID_RE.match(value):
        raise ValueError(
            f"Invalid --visual-id {value!r}: must match {_VISUAL_ID_RE.pattern}"
        )
    return value


def exec_interactive(container_id: str, *, visual_id: str | None = None) -> int:
    """Attach an interactive shell to the environment.

    Always launches ``bash -l`` (login shell, sources ``/etc/profile.d/*.sh``
    natively -- same as the ``exec -- <cmd>`` and ``exec --stream`` paths).

    When *visual_id* is set, the value is forwarded as the ``DTU_VISUAL_ID``
    env var; the static ``/etc/profile.d/dtu-visual-id.sh`` script written
    at launch picks it up and prepends a blue ``(dtu:<visual_id>)`` marker
    to PS1 via PROMPT_COMMAND. The raw value is validated via
    :func:`sanitize_visual_id` before being passed.
    """
    if not incus.container_exists(container_id):
        print(f"Error: Environment not found: {container_id}", file=sys.stderr)
        return 1
    env: dict[str, str] | None = None
    if visual_id is not None:
        env = {"DTU_VISUAL_ID": sanitize_visual_id(visual_id)}
    return incus.exec_interactive(container_id, env=env)


def file_push(
    container_id: str,
    local_paths: list[str],
    container_path: str,
    *,
    recursive: bool = False,
    create_dirs: bool = False,
    mode: str | None = None,
    uid: int | None = None,
    gid: int | None = None,
    timeout: int = 120,
) -> None:
    """Push files from the host into the environment."""
    if not incus.container_exists(container_id):
        raise RuntimeError(f"Environment not found: {container_id}")
    incus.file_push(
        container_id,
        local_paths,
        container_path,
        recursive=recursive,
        create_dirs=create_dirs,
        mode=mode,
        uid=uid,
        gid=gid,
        timeout=timeout,
    )


def file_pull(
    container_id: str,
    container_paths: list[str],
    local_path: str,
    *,
    recursive: bool = False,
    create_dirs: bool = False,
    timeout: int = 120,
) -> None:
    """Pull files from the environment to the host."""
    if not incus.container_exists(container_id):
        raise RuntimeError(f"Environment not found: {container_id}")
    incus.file_pull(
        container_id,
        container_paths,
        local_path,
        recursive=recursive,
        create_dirs=create_dirs,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Metadata label used to discover DTU-managed instances.
# ---------------------------------------------------------------------------

_MANAGED_BY_KEY = "user.dtu.managed-by"
_MANAGED_BY_VALUE = "amplifier-digital-twin"


def _instance_info(name: str) -> dict:
    """Build the status dict for a single managed instance.

    Shared by ``status()`` and ``list_environments()`` so both return the
    same shape.
    """
    info: dict = {
        "id": name,
        "profile": incus.get_config(name, "user.dtu.profile"),
        "status": incus.get_instance_state(name),
        "created_at": incus.get_config(name, "user.dtu.created-at"),
    }
    _hostname = incus.get_config(name, "user.dtu.hostname")
    if _hostname:
        info["hostname"] = _hostname
    raw_access = incus.get_config(name, "user.dtu.access-ports")
    if raw_access:
        import json as _json_info

        access_config = _json_info.loads(raw_access)
        access_urls: list[dict[str, str]] = []
        for p in access_config:
            host_port = p["host"]
            path = p.get("path", "/")
            label = p.get("label", "") or f"port {host_port}"
            entry: dict[str, str] = {
                "label": label,
                "url": f"http://localhost:{host_port}{path}",
            }
            if _hostname:
                entry["mdns_url"] = f"http://{_hostname}:{host_port}{path}"
            access_urls.append(entry)
        info["access"] = access_urls
    return info


def status(container_id: str) -> dict:
    """Return status info for a single environment."""
    if not incus.container_exists(container_id):
        raise RuntimeError(f"Environment not found: {container_id}")
    return _instance_info(container_id)


def list_environments() -> list[dict]:
    """Return status info for all DTU-managed environments."""
    instances = incus.list_instances(_MANAGED_BY_KEY, _MANAGED_BY_VALUE)
    return [_instance_info(inst["name"]) for inst in instances]


def check_readiness(
    container_id: str,
    skip_access_check: bool = False,
) -> dict:
    """Run readiness checks for *container_id*.  Returns a JSON-serialisable dict.

    When *skip_access_check* is False (the default), host-side access
    verification is included for any ``access.ports`` entries stored at
    launch time.
    """
    if not incus.container_exists(container_id):
        raise RuntimeError(f"Environment not found: {container_id}")

    import json as _json

    from amplifier_bundle_digital_twin_universe.profile import (
        PortMapping,
        ReadinessCheck,
        ReadinessCheckHttp,
        ReadinessCheckTcp,
    )
    from amplifier_bundle_digital_twin_universe.readiness import (
        check_readiness as _do_checks,
        verify_access_ports,
    )

    # -- In-container readiness checks --
    raw = incus.get_config(container_id, "user.dtu.readiness")
    has_readiness = bool(raw)

    readiness_result: dict | None = None
    if has_readiness:
        config = _json.loads(raw)
        checks: list[ReadinessCheck] = []
        for item in config:
            http_check = None
            tcp_check = None
            command_check = None

            if "http" in item:
                http_check = ReadinessCheckHttp(
                    url=item["http"]["url"],
                    expect_json=item["http"].get("expect_json"),
                )
            if "tcp" in item:
                tcp_check = ReadinessCheckTcp(port=int(item["tcp"]["port"]))
            if "command" in item:
                command_check = item["command"]

            checks.append(
                ReadinessCheck(
                    name=item["name"],
                    http=http_check,
                    tcp=tcp_check,
                    command=command_check,
                )
            )
        readiness_result = _do_checks(container_id, checks)

    # -- Host-side access verification --
    access_result: dict | None = None
    if not skip_access_check:
        raw_access = incus.get_config(container_id, "user.dtu.access-ports")
        if raw_access:
            access_config = _json.loads(raw_access)
            port_mappings = [
                PortMapping(
                    host=int(p["host"]),
                    container=int(p["container"]),
                    label=p.get("label", ""),
                    path=p.get("path", "/"),
                    verify=bool(p.get("verify", True)),
                    verify_timeout=int(p.get("verify_timeout", 30)),
                    verify_interval=int(p.get("verify_interval", 2)),
                )
                for p in access_config
            ]
            access_result = verify_access_ports(port_mappings)

    # -- Aggregate --
    if not has_readiness and access_result is None:
        return {"ready": None, "message": "profile has no readiness checks"}

    # Determine overall readiness.
    readiness_ok = readiness_result is None or readiness_result.get("ready", False)
    access_ok = access_result is None or access_result.get("verified", False)
    overall_ready = readiness_ok and access_ok

    result: dict = {"ready": overall_ready}

    if readiness_result is not None:
        result["message"] = readiness_result.get("message", "")
        if "checks" in readiness_result:
            result["checks"] = readiness_result["checks"]
    else:
        result["message"] = (
            "all checks passed" if overall_ready else "access verification failed"
        )

    if access_result is not None:
        result["access"] = access_result

    return result


def destroy(container_id: str) -> dict:
    """Destroy the environment.  Returns ``{id, destroyed}``."""
    if not incus.container_exists(container_id):
        raise RuntimeError(f"Environment not found: {container_id}")

    # Unregister hostname before destroying the container.
    try:
        _hostname = incus.get_config(container_id, "user.dtu.hostname")
        if _hostname:
            from amplifier_bundle_digital_twin_universe.hostname import (
                HostnameManager,
            )

            HostnameManager(_hostname.removesuffix(".local"), container_id).unregister()
    except Exception:
        pass  # best-effort

    # Stop mock service Docker containers before destroying the Incus container.
    _stop_mock_containers(container_id)

    incus.stop_container(container_id)
    incus.delete_container(container_id)
    return {"id": container_id, "destroyed": True}
