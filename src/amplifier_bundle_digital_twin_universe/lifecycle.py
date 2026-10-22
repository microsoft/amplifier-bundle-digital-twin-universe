# Copyright (c) Microsoft. All rights reserved.
"""Cooperating per-name custody, always acquired before request/admission locks."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import os
from pathlib import Path

from . import admission, incus

_HELD = ContextVar("dtu_lifecycle", default=None)


def _directory(root):
    path = root / "lifecycle"
    path.mkdir(mode=0o700, exist_ok=True)
    admission._private_directory(path)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def marker_path(root, name):
    return root / "lifecycle" / (hashlib.sha256(name.encode()).hexdigest() + ".json")


@contextmanager
def custody(ctx, name):
    root = admission.state_directory(ctx)
    key = (str(root), name)
    if _HELD.get() == key:
        yield root
        return
    directory = _directory(root)
    with admission._locked_file(directory / (hashlib.sha256(name.encode()).hexdigest() + ".lock"), timeout=0):
        token = _HELD.set(key)
        try:
            yield root
        finally:
            _HELD.reset(token)


def held(root, name):
    return _HELD.get() == (str(root), name)


def read_marker(root, name):
    try:
        return admission._read_private_json(marker_path(root, name))
    except FileNotFoundError:
        return None


@contextmanager
def mutation(name):
    if _HELD.get() is not None and _HELD.get()[1] == name:
        yield
        return
    # Preserve legacy behavior without a new daemon call when this name has
    # never participated in collection. Inspect only the hashed-name markers.
    base = Path(os.environ.get("AMPLIFIER_DTU_ADMISSION_DIR") or
                Path.home() / ".local/state/amplifier-dtu/admission")
    suffix = hashlib.sha256(name.encode()).hexdigest() + ".json"
    candidates = list(base.glob("*/lifecycle/" + suffix))
    if not candidates:
        yield
        return
    ctx = admission.context()
    expected_root = admission.state_directory(ctx)
    if any(path.parent.parent != expected_root for path in candidates):
        raise admission.AdmissionError("ownership_conflict", "Collected name is bound to a different backend scope")
    with custody(ctx, name) as root:
        marker = read_marker(root, name)
        if marker is None:
            yield
            return
        from . import collected_launch
        with collected_launch.mutation(marker, root, name):
            yield


def guarded(function):
    @wraps(function)
    def call(name, *args, **kwargs):
        # A nested call shares custody only within this exact context/thread.
        if _HELD.get() is not None and _HELD.get()[1] == name:
            return function(name, *args, **kwargs)
        with mutation(name):
            return function(name, *args, **kwargs)
    return call


def verify_instance(binding):
    name = binding["name"]
    if (incus.get_config(name, "user.dtu.request-id") != binding["request_id"]
            or incus.get_config(name, "user.dtu.owner-root") != binding["owner_root"]):
        raise admission.AdmissionError("ownership_conflict", "The collected instance identity changed")
