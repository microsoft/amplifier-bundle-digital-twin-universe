# Copyright (c) Microsoft. All rights reserved.
"""Future-attempt facts, not authority or a second lifecycle state machine.

Existing collector/acknowledgment formats remain unchanged. The live worker owns
writes under request custody; readers never backfill. Missing facts mean
unobserved. Raw receipt baselines remain private and cannot authorize repair.
"""
from contextlib import contextmanager
import hashlib
import json
import os
import re
import subprocess

from . import admission, collector, incus, operations
from .bound_backend import BackendFence, BackendExecutionTimeout

_MAX_BYTES = 32768
_BASELINE_BYTES = 4096
_KEYS = {"version", "binding", "generation", "revision", "baselines", "cli", "operation",
         "primary", "cleanup", "receipt_failure"}
_STAGES = {"reservation", "creation", "creation_wait", "operation_receipt", "provisioning",
           "observations", "checkpoint", "ready_marker", "final_receipt", "cleanup", "collector"}
_CATEGORIES = {"timeout", "error", "os_error", "backend_fence", "cancel_requested",
               "observation_error", "evidence_error", "cli_nonzero", "creation_rejected"}
_STATUSES = {*range(100, 113), 200, 400, 401}


class EvidenceError(admission.AdmissionError):
    def __init__(self):
        super().__init__("evidence_unknown", "Collected failure evidence is unavailable or changed")


def category(exc):
    from .collected_launch import CollectionCancelled
    from .collector_observation import ObservationError
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired, BackendExecutionTimeout)):
        return "timeout"
    for kind, code in ((CollectionCancelled, "cancel_requested"), (BackendFence, "backend_fence"),
                       (ObservationError, "observation_error"), (EvidenceError, "evidence_error"),
                       (incus.CreationCommandError, "cli_nonzero"),
                       (operations.CreationRejected, "creation_rejected"), (OSError, "os_error")):
        if isinstance(exc, kind):
            return code
    return "error"


def _bytes(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        admission._private_file(os.fstat(stream.fileno()))
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise EvidenceError()
    return raw


def _baseline(path):
    raw = _bytes(path, _BASELINE_BYTES)
    return {"bytes_hex": raw.hex(), "sha256": hashlib.sha256(raw).hexdigest()}


def _validate(store, generation, value):
    if (not isinstance(value, dict) or set(value) != _KEYS
            or type(value["version"]) is not int or value["version"] != 1
            or value["binding"] != dict(store.binding) or value["generation"] != generation
            or type(value["revision"]) is not int or not 1 <= value["revision"] <= 32
            or len(json.dumps(value, allow_nan=False).encode()) > _MAX_BYTES):
        raise EvidenceError()
    baselines = value["baselines"]
    if not isinstance(baselines, dict) or set(baselines) != {"collector", "reservation"}:
        raise EvidenceError()
    for key, baseline in baselines.items():
        if key == "reservation" and baseline is None:
            continue
        if not isinstance(baseline, dict) or set(baseline) != {"bytes_hex", "sha256"}:
            raise EvidenceError()
        raw = bytes.fromhex(baseline["bytes_hex"])
        if len(raw) > _BASELINE_BYTES or hashlib.sha256(raw).hexdigest() != baseline["sha256"]:
            raise EvidenceError()
        original = json.loads(raw, object_pairs_hook=collector._unique_object)
        if key == "collector":
            if (not isinstance(original, dict) or type(original.get("version")) is not int
                    or type(original.get("revision")) is not int
                    or original != {"version": 1, "binding": dict(store.binding), "generation": generation,
                             "revision": 1, "phase": "prepared", "result_digest": None}):
                raise EvidenceError()
        elif (not isinstance(original, dict) or set(original) != {
                "request_id", "name", "owner_root", "intent_digest", "state", "created_at"}
                or original["state"] != "launching"
                or any(original[k] != store.binding[k] for k in ("request_id", "name", "owner_root", "intent_digest"))
                or not isinstance(original["created_at"], str) or not original["created_at"].strip()):
            raise EvidenceError()
    cli = value["cli"]
    if cli is not None:
        if (not isinstance(cli, dict) or set(cli) != {"outcome", "returncode"}
                or cli["outcome"] not in {"returned", "nonzero", "timeout", "error", "fenced"}):
            raise EvidenceError()
        code = cli["returncode"]
        if ((cli["outcome"] == "returned" and (type(code) is not int or code != 0))
                or (cli["outcome"] == "nonzero" and (type(code) is not int or not -2147483648 <= code <= 2147483647 or code == 0))
                or (cli["outcome"] in {"timeout", "error", "fenced"} and code is not None)):
            raise EvidenceError()
    op = value["operation"]
    if op is not None:
        if (cli is not None or not isinstance(op, dict)
                or set(op) != {"id", "initial_status", "terminal_status", "terminal_source"}
                or not isinstance(op["id"], str)
                or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", op["id"])
                or type(op["initial_status"]) is not int or op["initial_status"] not in _STATUSES
                or (op["terminal_status"] is not None and (type(op["terminal_status"]) is not int or op["terminal_status"] not in {200, 400, 401}))
                or op["terminal_source"] not in ({None} if op["terminal_status"] is None else {"submit", "observe"})):
            raise EvidenceError()
        if (op["initial_status"] in {200, 400, 401} and
                (op["terminal_status"] != op["initial_status"] or op["terminal_source"] != "submit")):
            raise EvidenceError()
        if op["initial_status"] < 200 and op["terminal_source"] == "submit":
            raise EvidenceError()
    for slot in ("primary", "receipt_failure"):
        fact = value[slot]
        if fact is not None and (not isinstance(fact, dict) or set(fact) != {"stage", "category"}
                                or fact["stage"] not in _STAGES or fact["category"] not in _CATEGORIES):
            raise EvidenceError()
    cleanup = value["cleanup"]
    if cleanup is not None and (not isinstance(cleanup, dict) or set(cleanup) != {"delete_call", "category"}
            or cleanup["delete_call"] not in {"entered", "returned", "raised"}
            or cleanup["category"] not in (_CATEGORIES if cleanup["delete_call"] == "raised" else {None})
            or value["primary"] is None or value["primary"]["stage"] != "provisioning"):
        raise EvidenceError()
    expected_revision = (1 + (baselines["reservation"] is not None) + (cli is not None)
                         + (op is not None) + (op is not None and op["terminal_source"] == "observe")
                         + (value["primary"] is not None) + (value["receipt_failure"] is not None)
                         + (cleanup is not None) + (cleanup is not None and cleanup["delete_call"] != "entered"))
    if value["revision"] != expected_revision:
        raise EvidenceError()


def _read(store, generation):
    store._directories()
    current = store._read()
    if not current or current["generation"] != generation:
        raise EvidenceError()
    try:
        raw = _bytes(store.path.with_suffix(".evidence"), _MAX_BYTES)
    except FileNotFoundError:
        return None
    value = json.loads(raw, object_pairs_hook=collector._unique_object)
    _validate(store, generation, value)
    return value


def preflight(binding):
    """Reject excessive escaped identities before any durable generation exists."""
    prepared = {"version": 1, "binding": dict(binding), "generation": "0" * 32,
                "revision": 1, "phase": "prepared", "result_digest": None}
    reservation = {k: binding[k] for k in ("request_id", "name", "owner_root", "intent_digest")}
    reservation.update(state="launching", created_at="0" * 64)
    if any(len(json.dumps(v).encode()) > _BASELINE_BYTES for v in (prepared, reservation)):
        raise admission.AdmissionError("collect_unsupported", "Collected identity exceeds the private evidence baseline bound")


def initialize(store, generation):
    try:
        with store._lifecycle(), store._admission():
            store._current(generation, {"prepared"})
            if _read(store, generation) is not None:
                raise EvidenceError()
            value = {"version": 1, "binding": dict(store.binding), "generation": generation,
                     "revision": 1, "baselines": {"collector": _baseline(store.path), "reservation": None},
                     "cli": None, "operation": None, "primary": None, "cleanup": None, "receipt_failure": None}
            _validate(store, generation, value)
            admission._save_private_json(store.path.with_suffix(".evidence"), value)
    except Exception:
        raise EvidenceError() from None


class Recorder:
    """Worker-only handle; never reacquire admission at an already locked hook."""
    def __init__(self, store, generation):
        self.store, self.generation, self.broken = store, generation, False
        try:
            self.value = _read(store, generation)
            if self.value is None or self.value["revision"] != 1:
                raise EvidenceError()
        except Exception:
            raise EvidenceError() from None

    def _update(self, **changes):
        try:
            if self.broken or _read(self.store, self.generation) != self.value:
                raise EvidenceError()
            self.store._current(self.generation, {"accepted", "submitting"})
            value = {**self.value, **changes, "revision": self.value["revision"] + 1}
            _validate(self.store, self.generation, value)
            admission._save_private_json(self.store.path.with_suffix(".evidence"), value)
            self.value = value
        except Exception:
            self.broken = True  # Never retry an uncertain evidence write.
            raise EvidenceError() from None

    def baseline(self):
        if self.value["baselines"]["reservation"] is not None:
            raise EvidenceError()
        baseline = _baseline(admission.receipt_path(self.store.root, self.store.binding["request_id"]))
        self._update(baselines={**self.value["baselines"], "reservation": baseline})

    def failure(self, stage, exc, *, secondary=False):
        slot = "receipt_failure" if secondary else "primary"
        try:
            if self.value[slot] is None:
                self._update(**{slot: {"stage": stage, "category": category(exc)}})
        except Exception:
            # Storage cannot reliably record its own failure. Preserve the
            # original exception; no retry or cleanup is derived from this gap.
            pass

    @contextmanager
    def boundary(self, stage):
        try:
            yield
        except Exception as exc:
            self.failure(stage, exc)
            raise

    def cli(self, exc=None):
        outcome, code = "returned", 0
        if exc is not None:
            code = None
            outcome = "timeout" if category(exc) == "timeout" else "fenced" if isinstance(exc, BackendFence) else "error"
            if isinstance(exc, incus.CreationCommandError):
                outcome, code = "nonzero", exc.returncode
        try:
            if self.value["cli"] is not None or self.value["operation"] is not None:
                raise EvidenceError()
            self._update(cli={"outcome": outcome, "returncode": code})
        except Exception:
            if exc is None:
                raise

    def operation(self, operation, ctx, receipt, *, terminal=None):
        identity = operations.validate_binding(operation, ctx, receipt)
        old = self.value["operation"]
        if terminal is None:
            if old is not None or self.value["cli"] is not None:
                raise EvidenceError()
            code = operation["status_code"]
            value = {"id": identity, "initial_status": code, "terminal_status": code if code in {200, 400, 401} else None,
                     "terminal_source": "submit" if code in {200, 400, 401} else None}
        else:
            if old is None or old["id"] != identity or terminal not in {200, 400, 401}:
                raise EvidenceError()
            if old["terminal_status"] is not None:
                if old["terminal_status"] != terminal:
                    raise EvidenceError()
                return
            value = {**old, "terminal_status": terminal, "terminal_source": "observe"}
        self._update(operation=value)

    def cleanup(self, outcome, exc=None):
        try:
            old = self.value["cleanup"]
            if (outcome == "entered" and old is not None) or (outcome != "entered" and (not old or old["delete_call"] != "entered")):
                raise EvidenceError()
            self._update(cleanup={"delete_call": outcome, "category": category(exc) if exc else None})
        except Exception:
            pass  # Legitimate existing cleanup is independent of recorder I/O.


def projection(store, generation):
    try:
        value = _read(store, generation)
        if value is None:
            return {"available": False}
        return {"available": True, **{k: value[k] for k in
                ("revision", "cli", "operation", "primary", "cleanup", "receipt_failure")}}
    except Exception:
        raise EvidenceError() from None
