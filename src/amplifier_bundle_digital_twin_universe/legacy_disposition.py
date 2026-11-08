# Copyright (c) Microsoft. All rights reserved.
"""Exact legacy accounting decisions under a designated POSIX account.

This is not a human authentication service. The operator provisions the fixed
root-owned configuration and excludes agent/old writers from this account,
runtime, state domain and backend. Root and the designated operator are trusted.
No privilege escalation, arbitrary commands, daemon mutation or receipt rewrite.
"""
import hashlib
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

from . import admission, bound_backend, collector, lifecycle

CONFIG = Path("/etc/amplifier-dtu/legacy-disposition.json")
KEEP = "record_unknown_keep_reservation"
RELEASE = "accept_unknown_risk_release_reservation"
_LIMIT = 262144
_HASH = re.compile(r"[a-f0-9]{64}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}")


def _require(ok, code="disposition_conflict"):
    if not ok:
        raise admission.AdmissionError(code, "Protected legacy disposition refused; reservation retained unless already applied")


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _parse(raw):
    try:
        return json.loads(raw, object_pairs_hook=collector._unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError):
        raise admission.AdmissionError("disposition_conflict", "Invalid disposition data") from None


def _bytes(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise
    except OSError:
        raise admission.AdmissionError("state_unsafe", "Protected file unavailable") from None
    with os.fdopen(fd, "rb") as stream:
        admission._private_file(os.fstat(stream.fileno()), _LIMIT)
        raw = stream.read(_LIMIT + 1)
    _require(len(raw) <= _LIMIT)
    return raw


def _trusted_path(path, owners, *, private=False):
    path = Path(path)
    _require(path.is_absolute() and ".." not in path.parts, "authority_unavailable")
    for item in (path, *path.parents):
        info = item.lstat()
        _require(not stat.S_ISLNK(info.st_mode) and info.st_uid in owners
                 and not info.st_mode & 0o022, "authority_unavailable")
    if private:
        admission._private_directory(path)


def _configuration():
    _trusted_path(CONFIG, {0})
    fd = os.open(CONFIG, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == 0
                 and info.st_nlink == 1 and info.st_size <= _LIMIT, "authority_unavailable")
        raw = stream.read(_LIMIT + 1)
    value = _parse(raw)
    _require(set(value) == {"version", "operator_uid", "admission_base", "backend",
                           "generation", "enabled", "python_prefix", "package_path",
                           "writer_boundary"}, "authority_unavailable")
    _require(type(value["version"]) is int and value["version"] == 1
             and type(value["operator_uid"]) is int and value["operator_uid"] > 0
             and value["enabled"] is True
             and value["writer_boundary"] == "exclusive_operator"
             and isinstance(value["generation"], str) and _HASH.fullmatch(value["generation"]),
             "authority_unavailable")
    return value, _hash(raw)


class Executor:
    def __init__(self, config, config_digest, backend):
        self.config, self.config_digest, self.backend = config, config_digest, backend
        self.base = Path(config["admission_base"])
        self.root = self.base / _hash(f"{backend.scope['daemon']}\n{backend.scope['project']}".encode())

    @classmethod
    def from_system(cls):
        """Fixed operator deployment, never a caller's UID/config/environment."""
        try:
            cfg, digest = _configuration()
            uid = cfg["operator_uid"]
            _require(os.getuid() == os.geteuid() == uid, "authority_unavailable")
            _require(sys.flags.isolated and sys.prefix == cfg["python_prefix"]
                     and str(Path(__file__).parent) == cfg["package_path"], "authority_unavailable")
            for path in [Path(sys.executable).resolve(), Path(sys.prefix), Path(__file__).parent]:
                _trusted_path(path, {0, uid})
            for path in sys.path:
                if Path(path).exists():
                    _trusted_path(Path(path), {0, uid})
            _trusted_path(cfg["admission_base"], {0, uid}, private=True)
            backend = bound_backend.from_dict(cfg["backend"])
            for key in ("executable", "config", "global_config"):
                _trusted_path(cfg["backend"][key]["path"], {0, uid})
            result = cls(cfg, digest, backend)
            _trusted_path(result.root, {0, uid}, private=True)
            return result
        except admission.AdmissionError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise admission.AdmissionError("authority_unavailable", "Protected operator deployment is unavailable") from None

    @contextmanager
    def scope(self):
        with admission.state_scope(self.base), self.backend.activate():
            yield

    def inventory(self):
        # Unfiltered inventory: a foreign or untagged same-name guest is a conflict.
        record = self.backend.as_dict()
        self.backend.verify()
        bound_backend._check_local(record)
        raw = bound_backend._capture_identity_query(
            [record["executable"]["path"], "query",
             bound_backend._project_path("/1.0/instances?recursion=1", record["scope"]["project"])],
            env=bound_backend._environment(record), timeout=record["query_timeout"],
        )
        rows = _parse(raw.decode("utf-8"))
        if isinstance(rows, dict):
            _require(rows.get("type") == "sync" and rows.get("status_code") == 200
                     and not rows.get("error") and not rows.get("error_code"))
            rows = rows.get("metadata")
        _require(isinstance(rows, list))
        for row in rows:
            _require(isinstance(row, dict) and isinstance(row.get("name"), str)
                     and isinstance(row.get("status"), str)
                     and isinstance(row.get("config"), dict))
        self.backend.verify()
        return rows

    def revalidate(self):
        fresh = type(self).from_system()
        _require(fresh.config_digest == self.config_digest, "authority_unavailable")
        self.backend.verify()

    @contextmanager
    def locked(self, request_id):
        _require(isinstance(request_id, str) and 0 < len(request_id) <= 256)
        with self.scope():
            receipt = _parse(_bytes(admission.receipt_path(self.root, request_id)))
            admission._validate_receipt(receipt)
            _require(receipt is not None and _NAME.fullmatch(receipt.get("name", "")))
            with lifecycle.name_custody(receipt["name"]):
                directory = lifecycle._directory(self.root, "collectors")
                lock = admission.receipt_path(directory, request_id).with_suffix(".lock")
                with admission._locked_file(lock, timeout=0), admission.locked(self.backend.scope):
                    self.revalidate()
                    yield


def _paths(root, request_id):
    intent = admission.receipt_path(root / "legacy", request_id).with_suffix(".intent")
    return intent, intent.with_suffix(".applied")


def has_intent(root, request_id):
    # lexists also fences unsafe/broken links. Never treat malformed state as absent.
    return os.path.lexists(_paths(root, request_id)[0])


def _baseline(executor, request_id, owner_root, effect):
    _require(effect in {KEEP, RELEASE})
    path = admission.receipt_path(executor.root, request_id)
    raw = _bytes(path)
    receipt = _parse(raw)
    admission._validate_receipt(receipt)
    _require(receipt["request_id"] == request_id and receipt["owner_root"] == owner_root
             and isinstance(owner_root, str) and owner_root.strip()
             and receipt["state"] in {"launching", "unknown"}
             and _NAME.fullmatch(receipt["name"])
             and isinstance(receipt.get("intent_digest"), str)
             and _HASH.fullmatch(receipt["intent_digest"])
             and isinstance(receipt.get("created_at"), str) and receipt["created_at"])
    # This increment handles legacy CLI uncertainty, not an active collected
    # generation or future-operation settlement. Do not invent missing history.
    _require("create_operation" not in receipt and "prepared_result" not in receipt)
    key = path.stem
    _require(not any((executor.root / "collectors").glob(key + ".*"))
             or all(p.suffix == ".lock" for p in (executor.root / "collectors").glob(key + ".*")))
    _require(not os.path.lexists(lifecycle.marker_path(executor.root, receipt["name"])))
    rows = executor.inventory()
    residual = [r["name"] for r in rows if r["name"] == receipt["name"]
                or r["config"].get("user.dtu.request-id") == request_id]
    _require(not residual, "residual_conflict")
    preview = {
        "version": 1, "request_id": request_id, "owner_root": owner_root,
        "name": receipt["name"], "effect": effect, "scope": executor.backend.scope,
        "destination_digest": executor.backend.digest, "config_digest": executor.config_digest,
        "generation": executor.config["generation"], "receipt_hex": raw.hex(),
        "receipt_sha256": _hash(raw), "late_cleanup_owner": owner_root,
        "original_outcome": "unknown", "residual_names": [],
        "revision": 1,
    }
    _require(len(_encode(preview)) <= 200000)
    return preview


def _read_intent(root, request_id):
    path, _ = _paths(root, request_id)
    try:
        raw = _bytes(path)
    except FileNotFoundError:
        return None
    row = _parse(raw)
    _require(isinstance(row, dict) and set(row) == {"version", "preview", "digest", "operation_id", "state"})
    _require(type(row["version"]) is int and row["version"] == 1 and row["state"] == "COMMITTED")
    p = row["preview"]
    _require(isinstance(p, dict) and set(p) == {
        "version", "request_id", "owner_root", "name", "effect", "scope",
        "destination_digest", "config_digest", "generation", "receipt_hex", "receipt_sha256",
        "late_cleanup_owner", "original_outcome", "residual_names", "revision"})
    _require(type(p["version"]) is int and p["version"] == 1
             and type(p["revision"]) is int and p["revision"] == 1
             and p["request_id"] == request_id and p["effect"] in {KEEP, RELEASE}
             and p["original_outcome"] == "unknown" and p["residual_names"] == []
             and p["late_cleanup_owner"] == p["owner_root"]
             and row["digest"] == _hash(_encode(p)))
    _require(isinstance(row["operation_id"], str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", row["operation_id"]))
    original = bytes.fromhex(p["receipt_hex"])
    _require(p["receipt_sha256"] == _hash(original))
    receipt = _parse(original)
    admission._validate_receipt(receipt)
    _require(receipt["state"] in {"launching", "unknown"} and all(
        receipt[k] == p[k] for k in ("name", "request_id", "owner_root")))
    return row, raw


def _marker(row):
    return _encode({"version": 1, "digest": row["digest"], "operation_id": row["operation_id"]})


def _state(root, request_id, row):
    _, path = _paths(root, request_id)
    try:
        raw = _bytes(path)
    except FileNotFoundError:
        return "committed_pending"
    _require(raw == _marker(row))
    return "applied"


def _sync(path, expected):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        admission._private_file(os.fstat(stream.fileno()))
        _require(stream.read(_LIMIT + 1) == expected)
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prepare(request_id, owner_root, effect):
    executor = Executor.from_system()
    with executor.locked(request_id):
        _require(not has_intent(executor.root, request_id), "disposition_exists")
        preview = _baseline(executor, request_id, owner_root, effect)
        return {"digest": _hash(_encode(preview)), "preview": preview}


def _apply(executor, request_id, owner_root, effect, digest, operation_id, *, recovery=False):
    _require(isinstance(digest, str) and _HASH.fullmatch(digest))
    _require(isinstance(operation_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", operation_id))
    with executor.locked(request_id):
        found = _read_intent(executor.root, request_id)
        preview = _baseline(executor, request_id, owner_root, effect)
        _require(_hash(_encode(preview)) == digest)
        if found:
            row, raw = found
            _require(row["preview"] == preview and row["digest"] == digest
                     and row["operation_id"] == operation_id)
            if _state(executor.root, request_id, row) == "applied":
                if recovery:
                    intent, applied = _paths(executor.root, request_id)
                    try:
                        _sync(intent, raw)
                        _sync(applied, _marker(row))
                    except OSError:
                        raise admission.AdmissionError("projection_unknown", "Retained disposition durability unconfirmed") from None
                return {"outcome": "already_applied", "digest": digest, "original_outcome": "unknown"}
            _require(recovery, "projection_pending")
        else:
            _require(not recovery, "commit_missing")
            row = {"version": 1, "state": "COMMITTED", "preview": preview,
                   "digest": digest, "operation_id": operation_id}
            raw = _encode(row)
        directory = lifecycle._directory(executor.root, "legacy")
        intent, applied = _paths(executor.root, request_id)
        try:
            if not found:
                admission._save_private_bytes(intent, raw)
            # Explicitly sync retained bytes; never infer an earlier acknowledgment.
            _sync(intent, raw)
            executor.revalidate()
            _require(_baseline(executor, request_id, owner_root, effect) == preview)
            _require(_bytes(intent) == raw and directory == intent.parent)
            admission._save_private_bytes(applied, _marker(row))
            _require(_state(executor.root, request_id, row) == "applied")
        except Exception as exc:
            if isinstance(exc, admission.AdmissionError) and exc.code in {
                "authority_unavailable", "residual_conflict", "disposition_conflict"}:
                raise
            raise admission.AdmissionError("projection_unknown", "Disposition persistence was not acknowledged; observe without replay") from None
        return {"outcome": "applied", "digest": digest, "original_outcome": "unknown"}


def apply(request_id, owner_root, effect, expected_digest, operation_id):
    return _apply(Executor.from_system(), request_id, owner_root, effect, expected_digest, operation_id)


def reconcile(request_id):
    """Read only: no synchronization, projection repair or daemon mutation."""
    executor = Executor.from_system()
    with executor.scope():
        found = _read_intent(executor.root, request_id)
        if found is None:
            return {"outcome": "not_recorded", "reservation": "kept"}
        row, _ = found
        original = _bytes(admission.receipt_path(executor.root, request_id))
        _require(_hash(original) == row["preview"]["receipt_sha256"])
        return {"outcome": _state(executor.root, request_id, row),
                "digest": row["digest"], "operation_id": row["operation_id"],
                "effect": row["preview"]["effect"], "original_outcome": "unknown",
                "replay_permitted": False}


def recover(request_id, expected_digest, operation_id):
    executor = Executor.from_system()
    found = _read_intent(executor.root, request_id)
    _require(found is not None, "commit_missing")
    p = found[0]["preview"]
    return _apply(executor, request_id, p["owner_root"], p["effect"],
                  expected_digest, operation_id, recovery=True)


def capacity():
    """Canonical capacity under the fixed operator domain, never ambient state."""
    executor = Executor.from_system()
    with executor.scope(), admission.locked(executor.backend.scope) as root:
        executor.revalidate()
        return admission.snapshot(executor.backend.scope, admission.policy(), root)


def reservation_released(root, receipt):
    if not has_intent(root, receipt["request_id"]):
        return False
    row, _ = _read_intent(root, receipt["request_id"])
    if _state(root, receipt["request_id"], row) != "applied" or row["preview"]["effect"] != RELEASE:
        return False
    _require(_hash(_bytes(admission.receipt_path(root, receipt["request_id"])))
             == row["preview"]["receipt_sha256"])
    try:
        executor = Executor.from_system()
        _require(executor.root == root and row["preview"]["config_digest"] == executor.config_digest)
        executor.backend.verify()
    except (admission.AdmissionError, bound_backend.BackendFence, OSError):
        return False  # Lost/revoked protection restores conservative accounting.
    return True


def accounting_view(ctx, root, receipts, managed_instances):
    """Count late resources before excluding a released reservation.

    The ordinary managed-by filter misses partial/untagged late arrivals.
    Only applied, currently verified releases expand the accounting selection:
    their exact names or request IDs, in the same daemon/project, count even
    without a managed tag. A full inventory failure refuses the snapshot rather
    than publishing an available slot from incomplete observation.
    Caller holds the admission lock. No receipt or disposition is changed.
    """
    released = {r["request_id"]: r for r in receipts
                if r["state"] in {"launching", "unknown"} and reservation_released(root, r)}
    if not released:
        return managed_instances, set()
    executor = Executor.from_system()
    _require(executor.root == root and executor.backend.scope == ctx)
    with executor.scope():
        executor.revalidate()
        for request_id in released:
            row, _ = _read_intent(root, request_id)
            _require(row["preview"]["config_digest"] == executor.config_digest
                     and row["preview"]["destination_digest"] == executor.backend.digest
                     and row["preview"]["scope"] == ctx)
        rows = executor.inventory()
        executor.revalidate()
    names = {r["name"] for r in released.values()}
    # A complete project inventory has one nonblank name per resource; reject
    # duplicates, not silently count or collapse inconsistent query output.
    _require(all(r["name"].strip() for r in rows)
             and len({r["name"] for r in rows}) == len(rows))
    selected = [r for r in rows if
                r["config"].get(admission.MANAGED_KEY) == admission.MANAGED_VALUE
                or r["name"] in names
                or r["config"].get("user.dtu.request-id") in released]
    return selected, set(released)


def guard_name(root, name):
    directory = root / "legacy"
    if not directory.exists():
        return
    admission._private_directory(directory)
    for path in directory.glob("*.intent"):
        raw = _parse(_bytes(path))
        request_id = raw["preview"]["request_id"]
        row, _ = _read_intent(root, request_id)
        _require(path == _paths(root, request_id)[0])
        if row["preview"]["name"] == name:
            raise admission.AdmissionError("legacy_disposition_fenced", "Legacy name remains non-replayable; cleanup needs separate authority")