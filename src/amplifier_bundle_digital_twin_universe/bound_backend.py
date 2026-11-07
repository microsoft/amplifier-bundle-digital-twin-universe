# Copyright (c) Microsoft. All rights reserved.

"""Opt-in binding to one existing, private, local Incus client configuration.

This module does not wire the legacy backend or authorize a launch. Its caller
must enforce the explicit-collect subset (offline, exact local image, nonblank
owner, no external files, mocks or passthrough) before admission.

Both INCUS_CONF and INCUS_GLOBAL_CONF must name existing private directories.
The former must contain config.yml; the latter may omit it or contain an
equivalent configuration. Only a single local Unix remote and inert client
defaults are supported. Nothing creates/copies configuration or credentials.
Project precedence is AMPLIFIER_DTU_INCUS_PROJECT, INCUS_PROJECT, then the
configured local project (defaulting to "default"). Project and pool names are
limited to 63 ASCII letters/digits/dots/underscores/hyphens, starting alphanumeric.
Identity query timeout is captured from
AMPLIFIER_DTU_BOUND_BACKEND_QUERY_TIMEOUT_SECONDS (1..3600; default 30).

The private serialized record contains bootstrap paths and identities, never
configuration contents, credentials, arbitrary argv or the caller's environment.
It is evidence to verify, not authority. Checks detect observed drift, not an
administrator replacing a file/socket/daemon between verification and execution.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, quote, urlsplit

import yaml

from . import incus

_CONFIG_LIMIT = 64 * 1024
_RECORD_LIMIT = 32 * 1024
_QUERY_LIMIT = 1024 * 1024
_EXECUTABLE_LIMIT = 256 * 1024 * 1024
_TIMEOUT_ENV = "AMPLIFIER_DTU_BOUND_BACKEND_QUERY_TIMEOUT_SECONDS"
_HASH = re.compile(r"[a-f0-9]{64}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
_STAT_KEYS = {"device", "inode", "uid", "gid", "mode"}
_FILE_KEYS = _STAT_KEYS | {"size", "mtime_ns", "ctime_ns", "sha256"}


class BackendFence(incus.IncusError):
    """Sanitized refusal; no raw paths, backend output or command arguments."""


class BackendExecutionTimeout(BackendFence):
    """The operational subprocess timed out, not an earlier identity query."""


def _require(condition, message="Invalid bound backend record") -> None:
    if not condition:
        raise BackendFence(message)


def _name(value) -> bool:
    return isinstance(value, str) and _NAME.fullmatch(value) is not None


def _absolute(value) -> bool:
    return (
        isinstance(value, str)
        and 1 < len(value) <= 4096
        and value.startswith("/")
        and not value.startswith("//")
        and all(ord(c) >= 32 and ord(c) != 127 for c in value)
        and ".." not in PurePosixPath(value).parts
        and str(PurePosixPath(value)) == value
    )


def _identity(info, *, file=False) -> dict:
    result = dict(
        device=info.st_dev, inode=info.st_ino, uid=info.st_uid,
        gid=info.st_gid, mode=info.st_mode,
    )
    if file:
        result.update(
            size=info.st_size, mtime_ns=info.st_mtime_ns,
            ctime_ns=info.st_ctime_ns,
        )
    return result


def _read_file(path, *, private, dir_fd=None):
    """Bounded reads; do not block on FIFOs or follow the final symlink."""
    maximum = _CONFIG_LIMIT if private else _EXECUTABLE_LIMIT
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        _require(stat.S_ISREG(before.st_mode), "Backend file is not regular")
        if private:
            _require(
                before.st_uid == os.getuid() and not before.st_mode & 0o077
                and before.st_nlink == 1,
                "Backend configuration is not private",
            )
        else:
            _require(
                before.st_uid in {0, os.getuid()}
                and not before.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID)
                and before.st_mode & 0o111,
                "Backend executable is not trusted",
            )
        _require(0 < before.st_size <= maximum, "Backend file exceeds its bound")
        digest = hashlib.sha256()
        chunks = []
        count = 0
        while chunk := stream.read(min(65536, maximum + 1 - count)):
            count += len(chunk)
            _require(count <= maximum, "Backend file exceeds its bound")
            digest.update(chunk)
            if private:
                chunks.append(chunk)
        after = os.fstat(stream.fileno())
        _require(
            _identity(before, file=True) == _identity(after, file=True)
            and count == before.st_size,
            "Backend file changed while reading",
        )
    return {**_identity(before, file=True), "sha256": digest.hexdigest()}, b"".join(chunks)


class _ConfigLoader(yaml.SafeLoader):
    """Reject ambiguous YAML instead of guessing how the Go client parses it."""

    def compose_node(self, parent, index):
        event = self.peek_event()
        _require(
            not isinstance(event, yaml.AliasEvent) and not getattr(event, "anchor", None),
            "Backend YAML aliases and anchors are unsupported",
        )
        self._nodes = getattr(self, "_nodes", 0) + 1
        self._depth = getattr(self, "_depth", 0) + 1
        _require(self._nodes <= 1024 and self._depth <= 24, "Backend YAML exceeds its bound")
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            _require(key_node.tag == "tag:yaml.org,2002:str", "Invalid backend YAML key")
            key = self.construct_object(key_node, deep=deep)
            _require(key not in result, "Duplicate backend YAML key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _configuration(raw: bytes) -> dict:
    value = yaml.load(raw, Loader=_ConfigLoader)
    _require(
        isinstance(value, dict)
        and set(value) <= {"default-remote", "remotes", "aliases", "defaults"}
        and value.get("default-remote") == "local"
        and value.get("aliases", {}) == {},
        "Unsupported backend client configuration",
    )
    defaults = value.get("defaults", {})
    inert = {
        "list_format": "", "console_type": "",
        "console_spice_command": "", "no_color": False,
    }
    _require(
        isinstance(defaults, dict) and set(defaults) <= set(inert)
        and all(type(v) is type(inert[k]) and v == inert[k] for k, v in defaults.items()),
        "Backend client defaults are unsupported",
    )
    remotes = value.get("remotes")
    _require(
        isinstance(remotes, dict) and set(remotes) == {"local"},
        "Backend must have only the local remote",
    )
    remote = remotes["local"]
    _require(
        isinstance(remote, dict)
        and set(remote) <= {"addr", "protocol", "public", "project", "auth_type"},
        "Unsupported backend remote configuration",
    )
    _require(
        remote.get("protocol", "incus") == "incus"
        and remote.get("public", False) is False
        and remote.get("auth_type", "tls") == "tls",
        "Backend remote must be private Incus",
    )
    address = remote.get("addr")
    _require(
        isinstance(address, str) and address.startswith("unix://"),
        "Backend remote must use one explicit Unix socket",
    )
    socket_path = address[len("unix://"):]
    _require(
        _absolute(socket_path)
        and not any(c in socket_path for c in ",?#%:")
        and not any(c.isspace() for c in socket_path),
        "Backend remote must use one explicit Unix socket",
    )
    project = remote.get("project", "default")
    _require(_name(project), "Invalid backend project")
    return {"socket": socket_path, "project": project}


def _directory(path: str, *, required: bool):
    _require(_absolute(path), "Backend configuration paths must be explicit and absolute")
    _require(str(Path(path).resolve(strict=True)) == path, "Backend configuration path is aliased")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        _require(
            before.st_uid == os.getuid() and not before.st_mode & 0o077,
            "Backend configuration directory is not private",
        )
        try:
            identity, raw = _read_file("config.yml", private=True, dir_fd=fd)
        except FileNotFoundError:
            _require(not required, "Explicit backend client configuration is required")
            identity, parsed = None, None
        else:
            parsed = _configuration(raw)
        _require(
            _identity(before) == _identity(os.fstat(fd)),
            "Backend configuration directory changed",
        )
        return {"path": path, "identity": _identity(before), "file": identity}, parsed
    finally:
        os.close(fd)


def _local_evidence(executable, config_path, global_path):
    try:
        _require(
            _absolute(executable) and str(Path(executable).resolve(strict=True)) == executable,
            "Backend executable path changed",
        )
        executable_id, _ = _read_file(executable, private=False)
        config, parsed = _directory(config_path, required=True)
        global_config, global_parsed = _directory(global_path, required=False)
        _require(
            global_parsed is None or global_parsed == parsed,
            "Global backend configuration conflicts with client configuration",
        )
        return {
            "executable": {"path": executable, "identity": executable_id},
            "config": config, "global_config": global_config,
        }, parsed
    except (OSError, ValueError, RuntimeError, yaml.YAMLError):
        raise BackendFence("Backend bootstrap files are unavailable or invalid") from None


def _environment(record: dict) -> dict[str, str]:
    # No inherited HOME, loader variables, INCUS_SOCKET/DIR, proxy, secrets,
    # editor commands or current defaults. Absolute executable avoids PATH lookup.
    return {
        "PATH": "/usr/bin:/bin", "LC_ALL": "C",
        "HOME": record["config"]["path"],
        "INCUS_CONF": record["config"]["path"],
        "INCUS_GLOBAL_CONF": record["global_config"]["path"],
        "INCUS_REMOTE": "local", "INCUS_PROJECT": record["scope"]["project"],
    }


def _check_local(record: dict) -> None:
    evidence, parsed = _local_evidence(
        record["executable"]["path"], record["config"]["path"],
        record["global_config"]["path"],
    )
    _require(
        all(evidence[k] == record[k] for k in evidence) and parsed["socket"] == record["socket"],
        "Backend bootstrap identity changed",
    )


def _project_path(path: str, project: str) -> str:
    try:
        parsed = urlsplit(path)
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise BackendFence("Invalid bound backend query path") from None
    _require(
        not parsed.scheme and not parsed.netloc and "#" not in path
        and (parsed.path == "/1.0" or parsed.path.startswith("/1.0/"))
        and not any(ord(c) <= 32 or ord(c) == 127 for c in path),
        "Backend queries must use an unqualified API path",
    )
    projects = [v for k, v in pairs if k == "project"]
    _require(not projects or projects == [project], "Backend query project override refused")
    if projects:
        return path
    prefix = parsed.path + ("?" + parsed.query + "&" if parsed.query else "?")
    return prefix + "project=" + quote(project, safe="")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate backend response field")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise BackendFence("Non-JSON backend response value")


def _query(record: dict, path: str) -> dict:
    _check_local(record)
    try:
        result = subprocess.run(
            [record["executable"]["path"], "query", _project_path(path, record["scope"]["project"])],
            env=_environment(record), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=False, timeout=record["query_timeout"],
        )
        _require(result.returncode == 0, "Backend identity query failed")
        _require(
            isinstance(result.stdout, (str, bytes)) and len(result.stdout) <= _QUERY_LIMIT,
            "Backend identity response exceeds its bound",
        )
        value = json.loads(
            result.stdout, object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant,
        )
        _require(isinstance(value, dict), "Invalid backend identity response")
        _require(
            not value.get("error_code") and not value.get("error"),
            "Backend identity query reported an error",
        )
        # `incus query` normally prints metadata directly, not the REST envelope.
        if "metadata" in value or "type" in value:
            _require(
                value.get("type") == "sync"
                and type(value.get("status_code")) is int and value["status_code"] == 200
                and isinstance(value.get("metadata"), dict),
                "Invalid backend identity envelope",
            )
            value = value["metadata"]
        return value
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, RecursionError):
        raise BackendFence("Backend identity query could not be verified") from None


def _observe(record: dict) -> dict:
    environment = _query(record, "/1.0").get("environment")
    _require(
        isinstance(environment, dict)
        and isinstance(environment.get("certificate_fingerprint"), str)
        and _HASH.fullmatch(environment["certificate_fingerprint"])
        and environment.get("project") == record["scope"]["project"],
        "Backend daemon or project identity is invalid",
    )
    devices = _query(record, "/1.0/profiles/default").get("devices")
    _require(
        isinstance(devices, dict) and all(isinstance(v, dict) for v in devices.values()),
        "Backend root storage identity is invalid",
    )
    roots = [d for d in devices.values() if d.get("type") == "disk" and d.get("path") == "/"]
    _require(
        len(roots) == 1 and _name(roots[0].get("pool")),
        "Backend root storage identity is ambiguous",
    )
    _check_local(record)
    return {
        "daemon": environment["certificate_fingerprint"],
        "project": environment["project"], "storage_pool": roots[0]["pool"],
    }


def _validate_identity(value, *, file=False):
    _require(isinstance(value, dict) and set(value) == (_FILE_KEYS if file else _STAT_KEYS))
    _require(
        all(type(v) is int and 0 <= v < 2**128 for k, v in value.items() if k != "sha256")
    )
    if file:
        _require(isinstance(value["sha256"], str) and _HASH.fullmatch(value["sha256"]))


def from_dict(record: dict) -> BoundBackend:
    """Validate a private record without reading files, contacting Incus or recapturing."""
    _require(
        isinstance(record, dict) and set(record) == {
            "version", "executable", "config", "global_config", "remote",
            "socket", "scope", "query_timeout",
        }
    )
    _require(type(record["version"]) is int and record["version"] == 1)
    _require(record["remote"] == "local" and _absolute(record["socket"]))
    _require(
        not any(c in record["socket"] for c in ",?#%:")
        and not any(c.isspace() for c in record["socket"])
    )
    scope = record["scope"]
    _require(
        isinstance(scope, dict) and set(scope) == {"daemon", "project", "storage_pool"}
        and isinstance(scope["daemon"], str) and _HASH.fullmatch(scope["daemon"])
        and _name(scope["project"]) and _name(scope["storage_pool"])
    )
    _require(type(record["query_timeout"]) is int and 1 <= record["query_timeout"] <= 3600)
    for key in ("executable", "config", "global_config"):
        item = record[key]
        keys = {"path", "identity"} if key == "executable" else {"path", "identity", "file"}
        _require(isinstance(item, dict) and set(item) == keys and _absolute(item["path"]))
        _validate_identity(item["identity"], file=key == "executable")
        if key != "executable":
            if key == "global_config" and item["file"] is None:
                continue
            _validate_identity(item["file"], file=True)
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    _require(len(encoded) <= _RECORD_LIMIT)
    return BoundBackend(encoded)


def capture() -> BoundBackend:
    """Capture explicit local configuration, then prove its daemon/project/root pool."""
    _require(os.name == "posix", "Bound backends require a POSIX client")
    config_path = os.environ.get("INCUS_CONF")
    global_path = os.environ.get("INCUS_GLOBAL_CONF")
    _require(
        _absolute(config_path) and _absolute(global_path),
        "Both private backend configuration directories must be explicit",
    )
    _require(
        os.environ.get("INCUS_REMOTE", "local") == "local",
        "Only the local backend remote is supported",
    )
    try:
        selected = shutil.which("incus")
        _require(selected is not None, "Backend executable is unavailable")
        executable = str(Path(selected).resolve(strict=True))
        evidence, parsed = _local_evidence(executable, config_path, global_path)
        project = os.environ.get(
            "AMPLIFIER_DTU_INCUS_PROJECT", os.environ.get("INCUS_PROJECT", parsed["project"])
        )
        _require(_name(project), "Invalid explicit backend project")
        timeout = int(os.environ.get(_TIMEOUT_ENV, "30"))
        _require(1 <= timeout <= 3600, "Invalid backend observation timeout")
    except (OSError, ValueError, RuntimeError):
        raise BackendFence("Backend capture prerequisites are invalid") from None
    record = {
        "version": 1, **evidence, "remote": "local", "socket": parsed["socket"],
        "scope": {"project": project}, "query_timeout": timeout,
    }
    record["scope"] = _observe(record)
    return from_dict(record)


_CURRENT: ContextVar[BoundBackend | None] = ContextVar("dtu_bound_backend", default=None)


def current() -> BoundBackend | None:
    """Return the context-local binding, or None for an unchanged legacy caller."""
    return _CURRENT.get()


@dataclass(frozen=True)
class BoundBackend:
    """Immutable binding; construct with capture() or from_dict(), not raw JSON."""

    _encoded: str = field(repr=False)

    @classmethod
    def from_dict(cls, record: dict) -> BoundBackend:
        return from_dict(record)

    def as_dict(self) -> dict:
        return json.loads(self._encoded)

    @property
    def scope(self) -> dict[str, str]:
        return self.as_dict()["scope"]

    @property
    def digest(self) -> str:
        return hashlib.sha256(self._encoded.encode("ascii")).hexdigest()

    @contextmanager
    def activate(self):
        """Install only a context variable; never mutate process environment."""
        token = _CURRENT.set(self)
        try:
            yield self
        finally:
            _CURRENT.reset(token)

    def verify(self) -> None:
        """Observe using the pinned executable directly, never incus._run/query.

        Two read-only CLI queries identify the daemon/project and default
        profile's root pool name, not a durable storage-pool UUID. Query output
        is size-checked after subprocess capture, not stream-memory limited.
        """
        record = self.as_dict()
        _require(_observe(record) == record["scope"], "Backend daemon, project or root pool changed")

    def run(self, command, *args, **kwargs):
        """Verify, then invoke a pinned CLI command with ordinary stdin/stdout options.

        Positional Popen options and execution/environment overrides are refused.
        Guest arguments after ``--`` are preserved, including guest shell commands.
        Nonzero status is returned as usual with check=False. Transport exceptions
        are sanitized and never retried: an executed command's outcome may be unknown.
        """
        allowed = {
            "stdin", "stdout", "stderr", "input", "capture_output", "text",
            "encoding", "errors", "universal_newlines", "timeout", "bufsize", "check",
        }
        _require(not args and set(kwargs) <= allowed, "Backend subprocess override refused")
        _require(
            isinstance(command, (list, tuple)) and len(command) >= 2
            and all(isinstance(v, str) and "\0" not in v for v in command)
            and command[0] == "incus" and command[1] and not command[1].startswith("-"),
            "Expected an unqualified Incus command",
        )
        command = list(command)
        boundary = command.index("--") if "--" in command else len(command)
        for argument in command[1:boundary]:
            _require(
                argument.split("=", 1)[0] not in {
                    "--project", "--remote", "--force-local", "--target",
                },
                "Backend command routing override refused",
            )
            if ":" in argument and "=" not in argument:
                _require(argument.startswith("local:"), "Nonlocal backend target refused")
        _require(
            command[1] != "alias"
            and (command[1] != "remote" or command[2:] in (["list"], ["get-default"], ["list", "--format=json"]))
            and command[1:3] != ["project", "switch"],
            "Backend client configuration mutation refused",
        )
        record = self.as_dict()
        if command[1] == "query":
            _require(len(command) >= 3, "Backend query path is required")
            command[2] = _project_path(command[2], record["scope"]["project"])
            command[0] = record["executable"]["path"]
        else:
            command = [record["executable"]["path"], "--project", record["scope"]["project"], *command[1:]]
        self.verify()
        _check_local(record)
        try:
            return subprocess.run(
                command, env=_environment(record), **{"check": False, **kwargs},
            )
        except subprocess.TimeoutExpired:
            raise BackendExecutionTimeout("Bound Incus execution failed; command outcome may be unknown") from None
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            raise BackendFence("Bound Incus execution failed; command outcome may be unknown") from None