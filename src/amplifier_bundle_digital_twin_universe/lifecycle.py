# Copyright (c) Microsoft. All rights reserved.
"""Cooperating per-name custody, always before request/admission locks.

Name locks span configured scopes for this user's common state base. This is
intentionally conservative for qualified aliases; it is not daemon-wide custody.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import inspect
import os
from pathlib import Path

from . import admission, incus

_HELD = ContextVar("dtu_lifecycle", default=None)


def base_directory():
    base = Path(os.environ.get("AMPLIFIER_DTU_ADMISSION_DIR") or
                Path.home() / ".local/state/amplifier-dtu/admission")
    if not base.is_absolute() or ".." in base.parts or any(p.is_symlink() for p in (base, *base.parents)):
        raise admission.AdmissionError("state_unsafe", "Invalid lifecycle directory")
    return base


def _directory(root, label="lifecycle"):
    path = root / label
    path.mkdir(mode=0o700, exist_ok=True)
    admission._private_directory(path)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def canonical_name(name):
    if not isinstance(name, str) or not name:
        raise ValueError("Instance name is required")
    return name.rsplit(":", 1)[-1]


@contextmanager
def name_custody(name, timeout=0):
    name = canonical_name(name)
    base = base_directory()
    key = (str(base), name)
    if _HELD.get() == key:
        yield base
        return
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    admission._private_directory(base)
    directory = _directory(base, "lifecycle-locks")
    with admission._locked_file(directory / (hashlib.sha256(name.encode()).hexdigest() + ".lock"), timeout=timeout):
        token = _HELD.set(key)
        try:
            yield base
        finally:
            _HELD.reset(token)


def marker_path(root, name):
    return root / "lifecycle" / (hashlib.sha256(canonical_name(name).encode()).hexdigest() + ".json")


@contextmanager
def custody(ctx, name, timeout=0):
    with name_custody(name, timeout):
        root = admission.state_directory(ctx)
        _directory(root)
        yield root


def read_marker(root, name):
    try:
        return admission._read_private_json(marker_path(root, name))
    except FileNotFoundError:
        return None


def markers(base, name):
    suffix = hashlib.sha256(canonical_name(name).encode()).hexdigest() + ".json"
    return list(base.glob("*/lifecycle/" + suffix))


@contextmanager
def mutation(name):
    canonical = canonical_name(name)
    key = (str(base_directory()), canonical)
    if _HELD.get() == key:
        yield
        return
    # Lock before marker lookup, including the legacy/no-marker case. No new
    # backend query is needed for legacy mutation. Aliases cannot bypass custody.
    with name_custody(canonical) as base:
        candidates = markers(base, canonical)
        if not candidates:
            yield
            return
        if len(candidates) != 1 or name not in {canonical, "local:" + canonical}:
            raise admission.AdmissionError("ownership_conflict", "Collected instance scope is ambiguous")
        from . import collected_launch
        path = candidates[0]
        marker = admission._read_private_json(path)
        with collected_launch.mutation(marker, path.parent.parent, canonical):
            yield


def guarded(function):
    first = next(iter(inspect.signature(function).parameters))

    @wraps(function)
    def call(*args, **kwargs):
        name = args[0] if args else kwargs[first]
        with mutation(name):
            return function(*args, **kwargs)
    return call


def verify_instance(binding):
    name = binding["name"]
    if (incus.get_config(name, "user.dtu.request-id") != binding["request_id"]
            or incus.get_config(name, "user.dtu.owner-root") != binding["owner_root"]):
        raise admission.AdmissionError("ownership_conflict", "The collected instance identity changed")
