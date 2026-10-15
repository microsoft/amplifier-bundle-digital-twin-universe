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


def state_directory(ctx: dict) -> Path:
    base = Path(
        os.environ.get("AMPLIFIER_DTU_ADMISSION_DIR")
        or Path.home() / ".local/state/amplifier-dtu/admission"
    )
    key = hashlib.sha256(f"{ctx['daemon']}\n{ctx['project']}".encode()).hexdigest()
    root = base / key
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


@contextmanager
def locked(ctx: dict):
    """Bounded process lock. A waiting caller gets a retryable reason."""
    import fcntl

    root = state_directory(ctx)
    timeout = resolve_limit(None, "AMPLIFIER_DTU_ADMISSION_TIMEOUT_SECONDS", 30)
    deadline = time.monotonic() + timeout
    with (root / "admission.lock").open("a") as handle:
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
            yield root
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def receipt_path(root: Path, request_id: str) -> Path:
    return root / (hashlib.sha256(request_id.encode()).hexdigest() + ".json")


def read_receipt(root: Path, request_id: str) -> dict | None:
    path = receipt_path(root, request_id)
    return json.loads(path.read_text()) if path.exists() else None


def save_receipt(root: Path, receipt: dict) -> None:
    path = receipt_path(root, receipt["request_id"])
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as handle:
        os.chmod(tmp, 0o600)
        json.dump(receipt, handle)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
    directory_fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def snapshot(ctx: dict, limits: dict, root: Path) -> dict:
    instances = incus.list_instances(MANAGED_KEY, MANAGED_VALUE)
    names = {i["name"] for i in instances}
    # Unknown states and still-provisioning stopped instances remain active.
    retained = sum(
        i.get("status", "").lower() == "stopped"
        and i.get("config", {}).get("user.dtu.launch-state") != "provisioning"
        for i in instances
    )
    pending = sum(
        r.get("state") in {"launching", "unknown"} and r["name"] not in names
        for r in (json.loads(p.read_text()) for p in root.glob("*.json"))
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
