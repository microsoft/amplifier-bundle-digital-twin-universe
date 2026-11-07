# Copyright (c) Microsoft. All rights reserved.

"""Capacity accounting and same-user, same-host launch admission.

The lock is keyed by the actual daemon certificate and project, not a CLI
remote alias. It coordinates this library's clients sharing this state
directory; it cannot constrain older clients, other users or other hosts.
Unknown launches retain reservations. No age-based deletion or replay occurs.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from amplifier_bundle_digital_twin_universe import incus

MANAGED_KEY = "user.dtu.managed-by"
MANAGED_VALUE = "amplifier-digital-twin"
DEFAULT_MAX_RUNNING = 15
DEFAULT_MAX_INSTANCES = 256
DEFAULT_MIN_FREE_BYTES = 10 * 1024**3


class AdmissionError(RuntimeError):
    """A machine-readable refusal or unresolved prior launch."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def as_dict(self) -> dict:
        return {
            "schema_version": 1,
            "error": self.code,
            "message": str(self),
            **self.details,
        }


def resolve_limit(value: int | None, env_name: str, default: int) -> int:
    raw = value if value is not None else os.environ.get(env_name, "").strip()
    if raw == "":
        return default
    if not isinstance(raw, (str, int)) or isinstance(raw, bool):
        raise ValueError(f"Invalid {env_name}: expected a nonnegative integer")
    try:
        resolved = int(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid {env_name}: expected a nonnegative integer") from exc
    if resolved < 0 or isinstance(raw, bool):
        raise ValueError(f"Invalid {env_name}: expected a nonnegative integer")
    return resolved


def policy(max_running=None, max_instances=None, min_free_bytes=None) -> dict:
    return {
        "max_running": resolve_limit(
            max_running, "AMPLIFIER_DTU_MAX_RUNNING", DEFAULT_MAX_RUNNING
        ),
        "max_instances": resolve_limit(
            max_instances, "AMPLIFIER_DTU_MAX_INSTANCES", DEFAULT_MAX_INSTANCES
        ),
        "min_free_bytes": resolve_limit(
            min_free_bytes, "AMPLIFIER_DTU_MIN_FREE_BYTES", DEFAULT_MIN_FREE_BYTES
        ),
    }


def context() -> dict:
    env = incus.query("/1.0")["environment"]
    fingerprint = env.get("certificate_fingerprint")
    project = env.get("project")
    if not fingerprint or not project:
        raise AdmissionError(
            "backend_unknown", "Incus did not identify its daemon and project"
        )
    # This backend launches with Incus's default profile. Read its actual root
    # pool instead of measuring the caller filesystem (which may be remote).
    devices = incus.query("/1.0/profiles/default").get("devices", {})
    pools = {
        d.get("pool")
        for d in devices.values()
        if d.get("type") == "disk" and d.get("path") == "/"
    }
    if len(pools) != 1 or None in pools:
        raise AdmissionError(
            "storage_unknown", "Cannot identify the default profile's root storage pool"
        )
    return {"daemon": fingerprint, "project": project, "storage_pool": pools.pop()}


def _private_file(info, maximum: int = 262144) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
        or info.st_size > maximum
    ):
        raise AdmissionError(
            "state_unsafe",
            "Admission files must be bounded private regular files owned by this user",
        )


def _private_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise AdmissionError(
                "state_unsafe",
                "Admission directories must be private and owned by this user",
            )
    finally:
        os.close(descriptor)


def state_directory(ctx: dict) -> Path:
    base = Path(
        os.environ.get("AMPLIFIER_DTU_ADMISSION_DIR")
        or Path.home() / ".local/state/amplifier-dtu/admission"
    )
    if not base.is_absolute() or ".." in base.parts:
        raise AdmissionError(
            "state_unsafe", "Admission directory must be an absolute normalized path"
        )
    # Do not let a symlinked ancestor silently redirect the authoritative journal.
    for parent in (base, *base.parents):
        if parent.is_symlink():
            raise AdmissionError(
                "state_unsafe", "Admission directories cannot traverse symlinks"
            )
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private_directory(base)
    key = hashlib.sha256(f"{ctx['daemon']}\n{ctx['project']}".encode()).hexdigest()
    root = base / key
    root.mkdir(exist_ok=True, mode=0o700)
    _private_directory(root)
    return root


@contextmanager
def locked(ctx: dict):
    """Bounded process lock. A waiting caller gets a retryable reason."""
    root = state_directory(ctx)
    timeout = resolve_limit(None, "AMPLIFIER_DTU_ADMISSION_TIMEOUT_SECONDS", 30)
    with _locked_file(root / "admission.lock", timeout):
        yield root


@contextmanager
def _locked_file(path: Path, timeout: int = 30):
    """Shared private lock primitive; callers define the enclosing lock order."""
    import fcntl

    _private_directory(path.parent)
    deadline = time.monotonic() + timeout
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    with os.fdopen(descriptor, "a") as handle:
        _private_file(os.fstat(handle.fileno()), 1024)
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AdmissionError(
                        "admission_busy",
                        "Another launch is being admitted; retry this request",
                    )
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def receipt_path(root: Path, request_id: str) -> Path:
    return root / (hashlib.sha256(request_id.encode()).hexdigest() + ".json")


def _validate_receipt(receipt: dict) -> None:
    if not isinstance(receipt, dict):
        raise AdmissionError("state_unsafe", "Admission receipt must be an object")
    # Persisted receipts are unversioned. schema_version=1 belongs to API
    # responses, not a supported disk format; do not guess future semantics.
    if "schema_version" in receipt:
        raise AdmissionError("state_unsupported", "Unsupported admission receipt format")
    request_id = receipt.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise AdmissionError("state_unsafe", "Admission receipt identity is invalid")
    state = receipt.get("state")
    if not isinstance(state, str):
        raise AdmissionError("state_unsafe", "Admission receipt state is invalid")
    if state not in {"launching", "unknown", "created", "ready", "failed", "removed"}:
        raise AdmissionError("state_unsupported", "Unsupported admission receipt state")


def _read_private_json(path: Path, **json_options) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor) as stream:
        _private_file(os.fstat(stream.fileno()))
        raw = stream.read(262145)
        if len(raw) > 262144:
            raise AdmissionError(
                "state_unsafe", "Admission receipt exceeded its size bound"
            )
        try:
            value = json.loads(raw, **json_options)
        except ValueError:
            raise AdmissionError("state_unsafe", "Admission receipt is not valid JSON") from None
    return value


def _read_receipt_file(path: Path) -> dict:
    value = _read_private_json(path)
    _validate_receipt(value)
    if receipt_path(path.parent, value["request_id"]) != path:
        raise AdmissionError("state_unsafe", "Admission receipt identity is invalid")
    return value


def read_receipt(root: Path, request_id: str) -> dict | None:
    try:
        return _read_receipt_file(receipt_path(root, request_id))
    except FileNotFoundError:
        return None


def save_receipt(root: Path, receipt: dict) -> None:
    _validate_receipt(receipt)
    _save_private_json(receipt_path(root, receipt["request_id"]), receipt)


def _save_private_json(path: Path, value: dict) -> None:
    """Bounded atomic storage, independent of the admission receipt schema."""
    # Bound the replacement, not only the old file. Reconciliation can grow a
    # checkpoint too; every writer must leave a receipt the reader can load.
    serialized = json.dumps(value)
    if len(serialized.encode("utf-8")) > 262144:
        raise AdmissionError("state_unsafe", "Admission receipt exceeded its size bound")
    root = path.parent
    _private_directory(root)
    try:
        _private_file(path.lstat())
    except FileNotFoundError:
        pass
    descriptor, temporary = tempfile.mkstemp(
        prefix=".receipt-", suffix=".tmp", dir=root
    )
    tmp = Path(temporary)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        tmp.unlink(missing_ok=True)


def _reservation_identity(name, request_id, owner_root) -> tuple[str, str, str] | None:
    """Only complete, exact identities can represent a pending reservation."""
    values = (name, request_id, owner_root)
    if all(isinstance(value, str) and value.strip() for value in values):
        return values
    return None


def snapshot(ctx: dict, limits: dict, root: Path) -> dict:
    instances = incus.list_instances(MANAGED_KEY, MANAGED_VALUE)
    identities = set()
    for instance in instances:
        config = instance.get("config", {})
        identity = _reservation_identity(
            instance.get("name"),
            config.get("user.dtu.request-id"),
            config.get("user.dtu.owner-root"),
        )
        if identity is not None:
            identities.add(identity)
    # Unknown states and still-provisioning stopped instances remain active.
    retained = sum(
        i.get("status", "").lower() == "stopped"
        and i.get("config", {}).get("user.dtu.launch-state") != "provisioning"
        for i in instances
    )
    pending = sum(
        r["state"] in {"launching", "unknown"}
        and _reservation_identity(r.get("name"), r["request_id"], r.get("owner_root"))
        not in identities
        for r in (_read_receipt_file(p) for p in root.glob("*.json"))
    )
    active = len(instances) - retained + pending
    total = len(instances) + pending
    blockers = []
    if limits["max_running"] and active >= limits["max_running"]:
        blockers.append(
            {
                "code": "execution_capacity",
                "current": active,
                "limit": limits["max_running"],
            }
        )
    if limits["max_instances"] and total >= limits["max_instances"]:
        blockers.append(
            {
                "code": "retained_capacity",
                "current": total,
                "limit": limits["max_instances"],
            }
        )
    storage = {
        "pool": ctx["storage_pool"],
        "free_bytes": None,
        "min_free_bytes": limits["min_free_bytes"],
    }
    try:
        from urllib.parse import quote

        space = incus.query(
            f"/1.0/storage-pools/{quote(ctx['storage_pool'], safe='')}/resources"
        )["space"]
        free = int(space["total"]) - int(space["used"])
        if free < 0 or int(space["total"]) <= 0:
            raise ValueError("Invalid storage measurement")
        storage["free_bytes"] = free
        if free < limits["min_free_bytes"]:
            blockers.append(
                {
                    "code": "storage_pressure",
                    "free_bytes": free,
                    "min_free_bytes": limits["min_free_bytes"],
                }
            )
    except (incus.IncusError, KeyError, TypeError, ValueError):
        if limits["min_free_bytes"]:
            blockers.append({"code": "storage_unknown"})
    return {
        "schema_version": 1,
        "scope": ctx,
        "coordination": "same_host_user_state_directory",
        "counts": {
            "active": active,
            "stopped_retained": retained,
            "pending_unobserved": pending,
            "total": total,
        },
        "limits": limits,
        "storage": storage,
        "blockers": blockers,
        "available": not blockers,
    }
