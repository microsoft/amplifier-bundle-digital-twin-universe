# Copyright (c) Microsoft. All rights reserved.

"""Launch / exec / destroy orchestration.

Each public function corresponds to a CLI command and returns a JSON-
serialisable ``dict`` (or an ``int`` exit code for the interactive case).
"""

from __future__ import annotations

import base64
import glob
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
from amplifier_bundle_digital_twin_universe.profile import (
    Profile,
    has_unresolved_vars,
    load_profile,
)

# Port used by the local pypiserver inside the container.
_PYPI_SERVER_PORT = 8081


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


def _should_setup_proxy(profile: Profile) -> bool:
    """Return *True* if the proxy should be configured."""
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
import sys
from mitmproxy import http
from urllib.parse import urlparse

RULES = {rules!r}
PYPI_OVERRIDES = {pypi_overrides!r}
PYPI_SERVER_PORT = {pypi_server_port}

def _log(msg):
    print(f"[rewrite] {{msg}}", file=sys.stderr, flush=True)


class RewriteAddon:
    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host
        path = flow.request.path

        # URL rewrite rules (GitHub -> Gitea etc.)
        for rule in RULES:
            if host == rule["match_host"] and path.startswith(rule["match_path_prefix"]):
                target = urlparse(rule["target_url"])
                flow.request.scheme = target.scheme
                flow.request.host = target.hostname
                flow.request.port = target.port or (443 if target.scheme == "https" else 80)
                flow.request.path = target.path + path[len(rule["match_path_prefix"]):]
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


addons = [RewriteAddon()]
"""


def _generate_addon_script(profile: Profile, variables: dict[str, str]) -> str:
    """Generate the mitmproxy rewrite addon from *profile*'s url_rewrites.

    Supports two kinds of interception:

    1. **URL rewrites** -- redirect git/HTTPS requests matching a host+path
       prefix to a different target (e.g. GitHub -> Gitea).
    2. **PyPI overrides** -- intercept PyPI Simple API requests for specific
       packages and redirect them to a local pypiserver.
    """
    rules: list[dict[str, str]] = []
    assert profile.url_rewrites is not None  # caller checked

    auth_header = ""
    if profile.url_rewrites.auth:
        token = variables.get(profile.url_rewrites.auth.token_var, "")
        username = profile.url_rewrites.auth.username
        cred = base64.b64encode(f"{username}:{token}".encode()).decode()
        auth_header = f"Basic {cred}"

    for rule in profile.url_rewrites.rules:
        parts = rule.match.split("/", 1)
        match_host = parts[0]
        match_path_prefix = "/" + parts[1] if len(parts) > 1 else "/"
        rules.append(
            {
                "match_host": match_host,
                "match_path_prefix": match_path_prefix,
                "target_url": rule.target,
                "auth_header": auth_header,
            }
        )

    # Collect PyPI override package names (PEP 503 normalized).
    pypi_overrides: list[str] = []
    if profile.pypi_overrides:
        for pkg in profile.pypi_overrides.packages:
            pypi_overrides.append(pkg.name.lower().replace("-", "-"))

    return _ADDON_TEMPLATE.format(
        rules=rules,
        pypi_overrides=pypi_overrides,
        pypi_server_port=_PYPI_SERVER_PORT,
    )


def _setup_proxy(
    container_name: str, profile: Profile, variables: dict[str, str]
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
    addon_script = _generate_addon_script(profile, variables)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(addon_script)
        local_addon = f.name
    try:
        incus.push_file(container_name, local_addon, "/opt/dtu/rewrite_addon.py")
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
    assert profile.url_rewrites is not None  # caller checked
    rewrite_hosts = {rule.match.split("/", 1)[0] for rule in profile.url_rewrites.rules}

    # If pypi_overrides are configured, also intercept PyPI TLS traffic.
    # Both pypi.org (Simple API index) and files.pythonhosted.org (wheel
    # downloads) need TLS interception so mitmproxy can rewrite requests
    # for overridden packages to the local pypiserver.
    if profile.pypi_overrides and profile.pypi_overrides.packages:
        rewrite_hosts.add("pypi.org")
        rewrite_hosts.add("files.pythonhosted.org")

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
            incus.push_file(
                container_name, str(host_path), f"/opt/dtu/wheels/{host_path.name}"
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
                # uv bundles its own TLS certs and ignores the system store
                # by default.  UV_NATIVE_TLS makes it use OpenSSL / the
                # system cert bundle where we installed the mitmproxy CA.
                "export UV_NATIVE_TLS=true",
                # Belt-and-suspenders for pip, requests, and other tools.
                'export SSL_CERT_FILE="/etc/ssl/certs/ca-certificates.crt"',
                'export REQUESTS_CA_BUNDLE="/etc/ssl/certs/ca-certificates.crt"',
            ]
        )

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
        incus.push_file(container_name, local_env, "/etc/profile.d/dtu-env.sh")
    finally:
        os.unlink(local_env)

    _exec_checked(container_name, "chmod +x /etc/profile.d/dtu-env.sh")


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


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
# Public API
# ===================================================================


def launch(
    profile_arg: str,
    variables: dict[str, str],
    name: str | None = None,
) -> dict:
    """Launch a Digital Twin Universe.  Returns the JSON status dict."""
    incus.check_incus()

    # Quick-load to get the base image.
    host_profile = load_profile(profile_arg, variables)

    container_name = name or f"dtu-{uuid.uuid4().hex[:8]}"
    image = _resolve_image(host_profile.base.image)

    print(f"Creating container {container_name} ({image})...", file=sys.stderr)
    incus.create_container(container_name, image)

    try:
        # Detect host gateway IP (retries until networking is up).
        host_ip = _wait_for_gateway(container_name)
        print(f"  host gateway: {host_ip}", file=sys.stderr)

        # Rewrite localhost -> gateway IP and reload profile.
        rewritten_vars = _rewrite_localhost(variables, host_ip)
        profile = load_profile(profile_arg, rewritten_vars)

        # Proxy
        proxy_enabled = _should_setup_proxy(profile)
        if proxy_enabled:
            print("Setting up mitmproxy...", file=sys.stderr)
            _setup_proxy(container_name, profile, rewritten_vars)
        else:
            print(
                "Skipping proxy (no url_rewrites or unresolved vars).",
                file=sys.stderr,
            )

        # PyPI overrides -- push wheels and start pypiserver
        if host_profile.pypi_overrides and host_profile.pypi_overrides.packages:
            print("Setting up PyPI overrides...", file=sys.stderr)
            _setup_pypi_overrides(container_name, host_profile, variables)

        # Environment variables
        _write_env(container_name, profile, rewritten_vars, proxy_enabled)

        # Provisioning
        if profile.provision and profile.provision.setup_cmds:
            print("Running provisioning...", file=sys.stderr)
            _run_provisioning(container_name, profile.provision.setup_cmds)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"DTU {container_name} ready.", file=sys.stderr)
        return {
            "id": container_name,
            "name": container_name,
            "profile": profile.name,
            "status": "running",
            "created_at": now,
        }
    except Exception:
        # Best-effort cleanup on failure.
        try:
            incus.delete_container(container_name, force=True)
        except Exception:
            pass
        raise


def exec_command(container_id: str, command: list[str]) -> dict:
    """Run *command* inside the environment.  Returns JSON status dict."""
    if not incus.container_exists(container_id):
        raise RuntimeError(f"Environment not found: {container_id}")

    cmd_str = shlex.join(command)
    exit_code, stdout, stderr = incus.exec_command(
        container_id, ["bash", "-lc", cmd_str]
    )
    return {
        "id": container_id,
        "command": cmd_str,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }


def exec_interactive(container_id: str) -> int:
    """Attach an interactive shell to the environment."""
    if not incus.container_exists(container_id):
        print(f"Error: Environment not found: {container_id}", file=sys.stderr)
        return 1
    return incus.exec_interactive(container_id)


def destroy(container_id: str) -> dict:
    """Destroy the environment.  Returns ``{id, destroyed}``."""
    if not incus.container_exists(container_id):
        raise RuntimeError(f"Environment not found: {container_id}")

    incus.stop_container(container_id)
    incus.delete_container(container_id)
    return {"id": container_id, "destroyed": True}
