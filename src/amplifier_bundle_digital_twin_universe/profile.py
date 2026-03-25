# Copyright (c) Microsoft. All rights reserved.

"""Profile loading and variable resolution.

A profile is a YAML file that declares everything needed to launch a Digital
Twin Universe: base image, URL rewrite rules, passthrough services, and
provisioning commands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


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


@dataclass
class Provision:
    setup_cmds: list[str] = field(default_factory=list)


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
class Profile:
    path: Path
    name: str
    description: str
    base: Base
    url_rewrites: UrlRewrites | None = None
    passthrough: Passthrough | None = None
    provision: Provision | None = None
    pypi_overrides: PypiOverrides | None = None


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
    (e.g. ``amplifier-user-sim``).
    """
    p = Path(profile_arg)

    # Absolute or relative path that exists on disk
    if p.exists():
        return p.resolve()

    # Built-in profile name
    builtin = _BUILTIN_PROFILES_DIR / f"{profile_arg}.yaml"
    if builtin.exists():
        return builtin

    # CWD/profiles/<name>.yaml
    cwd_profiles = Path.cwd() / "profiles" / f"{profile_arg}.yaml"
    if cwd_profiles.exists():
        return cwd_profiles

    raise FileNotFoundError(
        f"Profile not found: {profile_arg!r}.  Searched: {p}, {builtin}, {cwd_profiles}"
    )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_profile(profile_arg: str, variables: dict[str, str]) -> Profile:
    """Load a profile from *profile_arg* with variable substitution.

    Variables are substituted best-effort: unresolved ``${VAR}`` references
    are left in place so callers can decide what to do (e.g. skip optional
    proxy setup when ``url_rewrites`` vars are missing).
    """
    path = find_profile_path(profile_arg)
    raw = yaml.safe_load(path.read_text())

    if not isinstance(raw, dict):
        raise ValueError(f"Profile must be a YAML mapping, got {type(raw).__name__}")

    data: dict = _walk_substitute(raw, variables)  # type: ignore[assignment]

    name: str = data.get("name", path.stem)
    description: str = data.get("description", "")

    # base (required)
    base_data = data.get("base", {})
    if not base_data.get("image"):
        raise ValueError("Profile must specify base.image")
    base = Base(image=base_data["image"])

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
        url_rewrites = UrlRewrites(auth=auth, rules=rules)

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
        provision = Provision(setup_cmds=prov.get("setup_cmds", []))

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

    return Profile(
        path=path,
        name=name,
        description=description,
        base=base,
        url_rewrites=url_rewrites,
        passthrough=passthrough,
        provision=provision,
        pypi_overrides=pypi_overrides,
    )
