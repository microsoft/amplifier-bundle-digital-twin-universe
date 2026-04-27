# Copyright (c) Microsoft. All rights reserved.

"""Profile loading and variable resolution.

A profile is a YAML file that declares everything needed to launch a Digital
Twin Universe: base image, URL rewrite rules, passthrough services, and
provisioning commands.
"""

from __future__ import annotations

import difflib
import re
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class UnknownProfileFieldWarning(UserWarning):
    """Emitted when a profile YAML contains a field the parser does not recognize.

    Currently warn-only -- the unknown field is silently dropped and parsing
    continues. A future release will turn these into hard errors. Users should
    address every warning before upgrading.
    """


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class UrlRewriteAuth:
    username: str
    token_var: str


@dataclass
class UrlRewriteRule:
    match: str
    target: str


@dataclass
class UrlRewrites:
    auth: UrlRewriteAuth | None
    rules: list[UrlRewriteRule]
    allow_uv_github_fast_path: bool = False


@dataclass
class PassthroughService:
    name: str
    key_env: str | None = None


@dataclass
class Passthrough:
    allow_external: bool = True
    services: list[PassthroughService] = field(default_factory=list)


@dataclass
class Base:
    image: str
    config: dict[str, str] = field(default_factory=dict)


@dataclass
class FileEntry:
    src: str
    dest: str
    recursive: bool = False
    create_dirs: bool = True
    mode: str | None = None
    uid: int | None = None
    gid: int | None = None


@dataclass
class Provision:
    files: list[FileEntry] = field(default_factory=list)
    setup_cmds: list[str] = field(default_factory=list)


@dataclass
class Update:
    cmds: list[str] = field(default_factory=list)
    refresh_pypi: bool = False


@dataclass
class PypiOverrideGitSource:
    repo: str
    ref: str = "main"
    username: str | None = None
    token_var: str | None = None
    build_cmd: str = "uv run --with maturin maturin build --release"
    wheel_glob: str = "target/wheels/*.whl"


@dataclass
class PypiOverridePackage:
    name: str
    wheel_var: str | None = None
    wheel_path: str | None = None
    wheel_from_git: PypiOverrideGitSource | None = None


@dataclass
class PypiOverrides:
    packages: list[PypiOverridePackage] = field(default_factory=list)


@dataclass
class PortMapping:
    host: int
    container: int
    label: str = ""
    path: str = "/"
    verify: bool = True
    verify_timeout: int = 30
    verify_interval: int = 2


@dataclass
class Access:
    ports: list[PortMapping] = field(default_factory=list)
    hostname: str | None = None


@dataclass
class ReadinessCheckHttp:
    url: str
    expect_json: dict | None = None


@dataclass
class ReadinessCheckTcp:
    port: int


@dataclass
class ReadinessCheck:
    name: str
    http: ReadinessCheckHttp | None = None
    tcp: ReadinessCheckTcp | None = None
    command: str | None = None


@dataclass
class ServiceEntry:
    source: str
    config: dict[str, str] = field(default_factory=dict)


@dataclass
class Profile:
    path: Path
    name: str
    description: str
    base: Base
    url_rewrites: UrlRewrites | None = None
    passthrough: Passthrough | None = None
    provision: Provision | None = None
    update: Update | None = None
    pypi_overrides: PypiOverrides | None = None
    access: Access | None = None
    readiness: list[ReadinessCheck] | None = None
    mock_services: list[ServiceEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Strict-field validation (warn-only)
# ---------------------------------------------------------------------------
#
# A single nested schema declares every field the parser recognizes at every
# level of a profile. The parser then runs ``_validate_unknown_fields`` once
# at the top of ``_load_profile_from_text``; any key in the YAML that the
# schema does not name produces an ``UnknownProfileFieldWarning``. A future
# release will turn these into hard errors.
#
# Schema node grammar:
#   * ``dict``                    -> nested mapping; keys are the allowed set,
#                                    values describe each child's schema
#   * ``[item_schema]``           -> list of mappings; each item validated
#                                    against ``item_schema``
#   * ``None``                    -> leaf value; no further validation
#   * ``_PASS_THROUGH``           -> mapping whose inner keys are intentionally
#                                    arbitrary (e.g. Incus config flags,
#                                    env-var maps); skip inner-key checks


# Sentinel for mappings whose inner keys must NOT be validated.
_PASS_THROUGH = object()

_PROFILE_SCHEMA: dict[str, Any] = {
    "name": None,
    "description": None,
    "base": {
        "image": None,
        "config": _PASS_THROUGH,
    },
    "url_rewrites": {
        "auth": {
            "username": None,
            "token_var": None,
        },
        "rules": [
            {
                "match": None,
                "target": None,
            }
        ],
        "allow_uv_github_fast_path": None,
    },
    "passthrough": {
        "allow_external": None,
        "services": [
            {
                "name": None,
                "key_env": None,
            }
        ],
    },
    "provision": {
        "files": [
            {
                "src": None,
                "dest": None,
                "recursive": None,
                "create_dirs": None,
                "mode": None,
                "uid": None,
                "gid": None,
            }
        ],
        "setup_cmds": None,
    },
    "update": {
        "cmds": None,
        "refresh_pypi": None,
    },
    "pypi_overrides": {
        "packages": [
            {
                "name": None,
                "wheel_var": None,
                "wheel_path": None,
                "wheel_from_git": {
                    "repo": None,
                    "ref": None,
                    "username": None,
                    "token_var": None,
                    "build_cmd": None,
                    "wheel_glob": None,
                },
            }
        ],
    },
    "access": {
        "ports": [
            {
                "host": None,
                "container": None,
                "label": None,
                "path": None,
                "verify": None,
                "verify_timeout": None,
                "verify_interval": None,
            }
        ],
        "hostname": None,
    },
    "readiness": [
        {
            "name": None,
            "http": {
                "url": None,
                "expect_json": None,
            },
            "tcp": {
                "port": None,
            },
            "command": None,
        }
    ],
    "mock_services": [
        {
            "source": None,
            "config": _PASS_THROUGH,
        }
    ],
}


def _check_unknown(
    data: Mapping[str, Any] | None,
    allowed: set[str],
    where: str,
) -> None:
    """Warn for any key in *data* not present in *allowed*.

    Atomic warning emitter for a single mapping. Use directly when validating
    a flat key set; otherwise call :func:`_validate_unknown_fields` to walk a
    nested schema.

    *where* is the human-readable YAML path (e.g. ``provision.files[2]``)
    that prefixes the warning so users can locate the offending field.
    Suggestions come from :func:`difflib.get_close_matches` when a typo is
    close enough to a known key.
    """
    if not isinstance(data, Mapping):
        return
    for key in data:
        if key in allowed:
            continue
        match = difflib.get_close_matches(str(key), sorted(allowed), n=1, cutoff=0.6)
        suggestion = f" (did you mean {match[0]!r}?)" if match else ""
        warnings.warn(
            f"{where}: unknown field {key!r}{suggestion}",
            UnknownProfileFieldWarning,
            stacklevel=2,
        )


def _validate_unknown_fields(
    data: Any,
    schema: Mapping[str, Any],
    where: str,
) -> None:
    """Recursively validate *data* against *schema* and warn for unknown keys.

    No-op if *data* is not a mapping. Walks every mapping node in lockstep
    with the schema and emits one ``UnknownProfileFieldWarning`` per unknown
    key. Pass-through nodes (``_PASS_THROUGH`` sentinel) skip inner-key
    validation. Lists of mappings are validated per-item against the single
    per-item schema declared in the parent schema.
    """
    if not isinstance(data, Mapping):
        return

    _check_unknown(data, set(schema.keys()), where)

    for key, value in data.items():
        sub_schema = schema.get(key)
        if sub_schema is None or sub_schema is _PASS_THROUGH:
            continue

        # Top-level keys ("base", "url_rewrites", etc.) appear unprefixed in
        # warning messages; nested keys are dotted onto their parent path.
        sub_path = str(key) if where == "profile" else f"{where}.{key}"

        if isinstance(sub_schema, list):
            if not isinstance(value, list):
                continue
            item_schema = sub_schema[0]
            for i, item in enumerate(value):
                _validate_unknown_fields(item, item_schema, f"{sub_path}[{i}]")
        elif isinstance(sub_schema, Mapping):
            _validate_unknown_fields(value, sub_schema, sub_path)


# ---------------------------------------------------------------------------
# Variable substitution
# ---------------------------------------------------------------------------

_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _substitute_vars(text: str, variables: dict[str, str]) -> str:
    """Replace ``${VAR}`` references.  Unresolved refs are left as-is."""

    def _replacer(m: re.Match[str]) -> str:
        return variables.get(m.group(1), m.group(0))

    return _VAR_RE.sub(_replacer, text)


def _walk_substitute(obj: object, variables: dict[str, str]) -> object:
    """Recursively substitute ``${VAR}`` in all string values."""
    if isinstance(obj, str):
        return _substitute_vars(obj, variables)
    if isinstance(obj, list):
        return [_walk_substitute(item, variables) for item in obj]
    if isinstance(obj, dict):
        return {k: _walk_substitute(v, variables) for k, v in obj.items()}
    return obj


def has_unresolved_vars(text: str) -> bool:
    """Return *True* if *text* still contains ``${VAR}`` references."""
    return bool(_VAR_RE.search(text))


# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------

# Built-in profiles ship alongside the source tree.
_BUILTIN_PROFILES_DIR = Path(__file__).resolve().parent.parent.parent / "profiles"


def find_profile_path(profile_arg: str) -> Path:
    """Resolve *profile_arg* to an on-disk YAML file.

    Accepts an absolute path, a relative path, or a bare built-in name
    (e.g. ``amplifier-user-sim``). Built-in names are resolved recursively
    under the packaged ``profiles/`` directory; duplicate stems in different
    buckets raise ``ValueError``.
    """
    p = Path(profile_arg)

    # Absolute or relative path that exists on disk
    if p.exists():
        return p.resolve()

    # Built-in profile name — search recursively across bucket subdirectories.
    if _BUILTIN_PROFILES_DIR.exists():
        matches = sorted(_BUILTIN_PROFILES_DIR.rglob(f"{profile_arg}.yaml"))
        if len(matches) == 1:
            return matches[0].resolve()
        if len(matches) > 1:
            rendered = ", ".join(str(m) for m in matches)
            raise ValueError(
                f"Ambiguous profile name {profile_arg!r}: matches {len(matches)} "
                f"built-in profiles ({rendered}). Pass an explicit path instead."
            )

    raise FileNotFoundError(
        f"Profile not found: {profile_arg!r}. "
        f"Searched as path ({p}) and as built-in name under {_BUILTIN_PROFILES_DIR}."
    )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_profile(
    profile_arg: str,
    variables: dict[str, str],
    *,
    validate: bool = True,
) -> Profile:
    """Load a profile from *profile_arg* with variable substitution.

    Variables are substituted best-effort: unresolved ``${VAR}`` references
    are left in place so callers can decide what to do (e.g. skip optional
    proxy setup when ``url_rewrites`` vars are missing).

    When *validate* is True (default) the parser walks the profile schema
    and emits one ``UnknownProfileFieldWarning`` per unknown field. Pass
    *validate=False* on subsequent re-parses of the same profile within a
    single workflow (e.g. the launch flow's post-gateway-IP re-parse, or
    snapshot replay during ``update``/``destroy``) to avoid duplicate
    warnings -- validation is a launch-time concern, not an instance-
    lifetime concern.
    """
    path = find_profile_path(profile_arg)
    return _load_profile_from_text(path.read_text(), path, variables, validate=validate)


def load_profile_from_content(
    yaml_text: str,
    variables: dict[str, str],
    *,
    path: Path | None = None,
    validate: bool = True,
) -> Profile:
    """Load a profile from raw YAML *yaml_text* with variable substitution.

    Used when the profile content is sourced from somewhere other than the
    host filesystem (for example, a snapshot stored inside a DTU container
    at ``/opt/dtu/profile.yaml``).  When *path* is omitted a synthetic
    placeholder is recorded on the returned :class:`Profile` -- callers
    should not rely on it pointing to a file that exists.

    See :func:`load_profile` for the meaning of *validate*. Snapshot replay
    callers should pass ``validate=False`` since validation already ran at
    original launch time.
    """
    effective_path = path if path is not None else Path("<in-memory>")
    return _load_profile_from_text(
        yaml_text, effective_path, variables, validate=validate
    )


def _load_profile_from_text(
    yaml_text: str,
    path: Path,
    variables: dict[str, str],
    *,
    validate: bool = True,
) -> Profile:
    """Shared parse/validation pipeline used by both load_profile variants."""
    raw = yaml.safe_load(yaml_text)

    if not isinstance(raw, dict):
        raise ValueError(f"Profile must be a YAML mapping, got {type(raw).__name__}")

    data: dict = _walk_substitute(raw, variables)  # type: ignore[assignment]

    # Single-pass validation against the declared schema. Warns once per
    # unknown field; never raises. See _PROFILE_SCHEMA above. Skipped on
    # re-parses (validate=False) to avoid duplicate warnings.
    if validate:
        _validate_unknown_fields(data, _PROFILE_SCHEMA, "profile")

    name: str = data.get("name", path.stem)
    description: str = data.get("description", "")

    # base (required)
    base_data = data.get("base", {})
    if not base_data.get("image"):
        raise ValueError("Profile must specify base.image")
    base_config = base_data.get("config", {})
    if not isinstance(base_config, dict):
        raise ValueError("base.config must be a mapping of key: value pairs")
    base = Base(
        image=base_data["image"],
        config={str(k): str(v) for k, v in base_config.items()},
    )

    # url_rewrites (optional)
    url_rewrites = None
    uw = data.get("url_rewrites")
    if uw:
        auth = None
        auth_data = uw.get("auth")
        if auth_data:
            auth = UrlRewriteAuth(
                username=auth_data.get("username", ""),
                token_var=auth_data.get("token_var", ""),
            )
        rules = [
            UrlRewriteRule(match=r["match"], target=r["target"])
            for r in uw.get("rules", [])
        ]
        url_rewrites = UrlRewrites(
            auth=auth,
            rules=rules,
            allow_uv_github_fast_path=bool(uw.get("allow_uv_github_fast_path", False)),
        )

    # passthrough (optional)
    passthrough = None
    pt = data.get("passthrough")
    if pt:
        services = [
            PassthroughService(name=s["name"], key_env=s.get("key_env"))
            for s in pt.get("services", [])
        ]
        passthrough = Passthrough(
            allow_external=pt.get("allow_external", True),
            services=services,
        )

    # provision (optional)
    provision = None
    prov = data.get("provision")
    if prov:
        files: list[FileEntry] = []
        for f in prov.get("files", []):
            if "src" not in f or "dest" not in f:
                raise ValueError(
                    "Each provision.files entry must have 'src' and 'dest' fields"
                )
            files.append(
                FileEntry(
                    src=f["src"],
                    dest=f["dest"],
                    recursive=bool(f.get("recursive", False)),
                    create_dirs=bool(f.get("create_dirs", True)),
                    mode=f.get("mode"),
                    uid=int(f["uid"]) if f.get("uid") is not None else None,
                    gid=int(f["gid"]) if f.get("gid") is not None else None,
                )
            )
        provision = Provision(
            files=files,
            setup_cmds=prov.get("setup_cmds", []),
        )

    # update (optional)
    update = None
    upd = data.get("update")
    if upd:
        update = Update(
            cmds=upd.get("cmds", []),
            refresh_pypi=bool(upd.get("refresh_pypi", False)),
        )

    # pypi_overrides (optional)
    pypi_overrides = None
    po = data.get("pypi_overrides")
    if po:
        packages = []
        for p in po.get("packages", []):
            wheel_from_git = None
            git_data = p.get("wheel_from_git")
            if git_data:
                wheel_from_git = PypiOverrideGitSource(
                    repo=git_data["repo"],
                    ref=git_data.get("ref", "main"),
                    username=git_data.get("username"),
                    token_var=git_data.get("token_var"),
                    build_cmd=git_data.get(
                        "build_cmd", "uv run --with maturin maturin build --release"
                    ),
                    wheel_glob=git_data.get("wheel_glob", "target/wheels/*.whl"),
                )

            package = PypiOverridePackage(
                name=p["name"],
                wheel_var=p.get("wheel_var"),
                wheel_path=p.get("wheel_path"),
                wheel_from_git=wheel_from_git,
            )

            sources = [
                package.wheel_var is not None,
                package.wheel_path is not None,
                package.wheel_from_git is not None,
            ]
            if sum(sources) != 1:
                raise ValueError(
                    "Each pypi_overrides package must specify exactly one of "
                    "wheel_var, wheel_path, or wheel_from_git"
                )

            packages.append(package)
        pypi_overrides = PypiOverrides(packages=packages)

    # access (optional)
    access = None
    ac = data.get("access")
    if ac:
        ports = [
            PortMapping(
                host=int(p["host"]),
                container=int(p["container"]),
                label=p.get("label", ""),
                path=p.get("path", "/"),
                verify=bool(p.get("verify", True)),
                verify_timeout=int(p.get("verify_timeout", 30)),
                verify_interval=int(p.get("verify_interval", 2)),
            )
            for p in ac.get("ports", [])
        ]
        access = Access(ports=ports, hostname=ac.get("hostname"))

    # readiness (optional)
    readiness = None
    readiness_data = data.get("readiness")
    if readiness_data:
        checks: list[ReadinessCheck] = []
        for item in readiness_data:
            if "name" not in item:
                raise ValueError("Each readiness check must have a 'name' field")

            http_check = None
            tcp_check = None
            command_check = None

            http_data = item.get("http")
            tcp_data = item.get("tcp")
            command_data = item.get("command")

            sources = [
                http_data is not None,
                tcp_data is not None,
                command_data is not None,
            ]
            if sum(sources) != 1:
                raise ValueError(
                    f"Readiness check {item['name']!r} must specify exactly one of "
                    "http, tcp, or command"
                )

            if http_data:
                http_check = ReadinessCheckHttp(
                    url=http_data["url"],
                    expect_json=http_data.get("expect_json"),
                )
            if tcp_data:
                tcp_check = ReadinessCheckTcp(port=int(tcp_data["port"]))
            if command_data:
                command_check = command_data

            checks.append(
                ReadinessCheck(
                    name=item["name"],
                    http=http_check,
                    tcp=tcp_check,
                    command=command_check,
                )
            )
        readiness = checks

    # mock_services (optional)
    mock_service_entries: list[ServiceEntry] = []
    mock_services_data = data.get("mock_services")
    if mock_services_data:
        for s in mock_services_data:
            if "source" not in s:
                raise ValueError("Each mock_services entry must have a 'source' field")
            mock_service_entries.append(
                ServiceEntry(
                    source=s["source"],
                    config=s.get("config", {}),
                )
            )

    return Profile(
        path=path,
        name=name,
        description=description,
        base=base,
        url_rewrites=url_rewrites,
        passthrough=passthrough,
        provision=provision,
        update=update,
        pypi_overrides=pypi_overrides,
        access=access,
        readiness=readiness,
        mock_services=mock_service_entries,
    )
