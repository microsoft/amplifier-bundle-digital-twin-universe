# Copyright (c) Microsoft. All rights reserved.
"""One-shot collector custody protocol; not yet wired into the public launcher.

The caller supplies a previously verified scope directory and immutable binding.
No process creation, backend calls, cancellation authority or credential bootstrap
live here. A destination digest binds data, not proof of an actual connection.
Callbacks are trusted library integration seams, never serialized commands.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
import uuid

from . import admission

_BINDING = {"request_id", "owner_root", "name", "intent_digest", "daemon", "project",
            "storage_pool", "destination_digest"}
_PHASES = {"prepared", "dispatched", "accepted", "submitting", "completed", "unknown", "fenced"}
_KEYS = {"version", "binding", "generation", "revision", "phase", "result_digest"}


def _conflict():
    return admission.AdmissionError("collector_conflict", "Collector identity or phase no longer permits this operation")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate collector key")
        value[key] = item
    return value


class AttemptStore:
    """Fixed-scope durable state. All mutation takes lifecycle before admission.

    This protocol does not make existing engine/CLI/older writers cooperate.
    It must not be enabled for live launches before those seams are integrated.
    """

    def __init__(self, root: Path, binding: dict):
        if not isinstance(binding, dict) or set(binding) != _BINDING or any(
            not isinstance(v, str) or not v.strip() or len(v) > 256 for v in binding.values()
        ):
            raise _conflict()
        if any(not re.fullmatch(r"[a-f0-9]{64}", binding[k]) for k in
               ("intent_digest", "daemon", "destination_digest")):
            raise _conflict()
        self.root = Path(root)
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise _conflict()
        self.binding = MappingProxyType(dict(binding))
        self.directory = self.root / "collectors"
        self.path = admission.receipt_path(self.directory, binding["request_id"])
        self.lock_path = self.path.with_suffix(".lock")

    def _directories(self, create=False):
        for path in (self.directory, *self.directory.parents):
            if path.is_symlink():
                raise admission.AdmissionError("state_unsafe", "Collector directory cannot traverse symlinks")
        admission._private_directory(self.root)
        if create:
            self.directory.mkdir(mode=0o700, exist_ok=True)
            # Also repeat after a prior uncertain mkdir/fsync: syncing only the
            # child does not commit its link in the already-provisioned root.
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if self.directory.exists():
            admission._private_directory(self.directory)

    def _read(self):
        self._directories()
        try:
            value = admission._read_private_json(self.path, object_pairs_hook=_unique_object)
        except FileNotFoundError:
            return None
        if not isinstance(value, dict) or set(value) != _KEYS:
            raise _conflict()
        if type(value["version"]) is not int or value["version"] != 1:
            raise admission.AdmissionError("state_unsupported", "Unsupported collector record")
        if (value["binding"] != dict(self.binding)
                or type(value["revision"]) is not int or value["revision"] < 1
                or not isinstance(value["phase"], str) or value["phase"] not in _PHASES
                or not isinstance(value["generation"], str)
                or not re.fullmatch(r"[a-f0-9]{32}", value["generation"])
                or (value["result_digest"] is not None and
                    (not isinstance(value["result_digest"], str) or
                     not re.fullmatch(r"[a-f0-9]{64}", value["result_digest"])))):
            raise _conflict()
        if (value["phase"] == "completed") != (value["result_digest"] is not None):
            raise _conflict()
        return value

    def _save(self, value):
        admission._save_private_json(self.path, value)

    @contextmanager
    def _lifecycle(self):
        self._directories(create=True)
        # Busy is a refusal, never a request to wait for/take over another worker.
        with admission._locked_file(self.lock_path, timeout=0):
            yield

    @contextmanager
    def _admission(self):
        with admission._locked_file(self.root / "admission.lock"):
            yield

    def _current(self, generation, phase):
        value = self._read()
        if value is None or value["generation"] != generation or value["phase"] not in phase:
            raise _conflict()
        return value

    def _advance(self, current, phase, result_digest=None):
        # Compare the complete previous bytes-as-data under the lifecycle lock.
        if self._read() != current:
            raise _conflict()
        value = {**current, "revision": current["revision"] + 1,
                 "phase": phase, "result_digest": result_digest}
        self._save(value)
        return value

    def prepare(self):
        """Persist one generation before dispatch; never adopt a legacy receipt."""
        with self._lifecycle(), self._admission():
            current = self._read()
            if current is not None:
                return current["generation"]
            if admission.read_receipt(self.root, self.binding["request_id"]) is not None:
                raise _conflict()
            value = {"version": 1, "binding": dict(self.binding), "generation": uuid.uuid4().hex,
                     "revision": 1, "phase": "prepared", "result_digest": None}
            self._save(value)
            return value["generation"]

    def dispatch_once(self, generation, dispatch):
        """Consume the sole dispatch before a trusted launcher callback.

        Callback exceptions/lost acknowledgements do not refund the dispatch.
        A delayed original worker may still enter. No replacement is spawned.
        """
        with self._lifecycle(), self._admission():
            current = self._current(generation, {"prepared"})
            self._advance(current, "dispatched")
        return dispatch(generation)

    def _receipt(self):
        value = admission.read_receipt(self.root, self.binding["request_id"])
        if value is None or any(value.get(key) != self.binding[key] for key in
                                ("request_id", "name", "owner_root", "intent_digest")):
            raise _conflict()
        return value

    def run_once(self, generation, reserve, execute):
        """Worker owns custody across reservation, execution and publication.

        reserve performs the existing admission checks and writes its reservation
        under the shared lock. execute owns the full lifecycle including ordinary
        provisioning cleanup and PR42 result publication. Evidence failure here
        never invokes cleanup. Accepted generations are never resumed/re-entered.
        """
        with self._lifecycle():
            with self._admission():
                current = self._current(generation, {"dispatched"})
                current = self._advance(current, "accepted")
                if admission.read_receipt(self.root, self.binding["request_id"]) is not None:
                    raise _conflict()
                reserve()
                receipt = self._receipt()
                if receipt["state"] != "launching":
                    raise _conflict()
                current = self._advance(current, "submitting")
            # The permission was durably consumed before this callback. Even an
            # interruption immediately before invocation cannot authorize replay.
            try:
                result = execute()
            except Exception:
                with self._admission():
                    self._advance(current, "unknown")
                raise
            with self._admission():
                receipt = self._receipt()
                if (receipt["state"] != "ready" or _digest(receipt.get("result")) != _digest(result)
                        or not isinstance(result, dict)
                        or result.get("id") != self.binding["name"]
                        or result.get("status") != "running"
                        or any(result.get(key) != self.binding[key] for key in
                               ("name", "request_id", "owner_root"))
                        or not isinstance(receipt.get("created_at"), str)
                        or not receipt["created_at"].strip()
                        or result.get("created_at") != receipt["created_at"]):
                    raise _conflict()
                self._advance(current, "completed", _digest(receipt["result"]))
            return result

    def fence_pending(self, generation):
        """Revoke a never-accepted worker locally. Not authenticated host Stop.

        Does not release capacity, mark daemon cancellation, delete a resource or
        create a new generation. A dispatched but delayed worker must refuse.
        """
        with self._lifecycle(), self._admission():
            current = self._current(generation, {"prepared", "dispatched"})
            if admission.read_receipt(self.root, self.binding["request_id"]) is not None:
                raise _conflict()
            self._advance(current, "fenced")

    @contextmanager
    def owned_worker(self, generation):
        """Full native engine adapter; consume under its existing admission lock.

        Caller already holds canonical-name custody. The yielded hook may run
        exactly once, immediately after reservation and before creation.
        """
        with self._lifecycle():
            with self._admission():
                current = self._current(generation, {"dispatched"})
                current = self._advance(current, "accepted")
            consumed = [False]

            def submit(ctx, root, receipt):
                nonlocal current
                if (consumed[0] or Path(root) != self.root
                        or any(ctx.get(k) != self.binding[k] for k in ("daemon", "project", "storage_pool"))
                        or any(receipt.get(k) != self.binding[k] for k in ("request_id", "name", "owner_root", "intent_digest"))
                        or self._receipt()["state"] != "launching"):
                    raise _conflict()
                from .collected_launch import check_cancel
                check_cancel()
                current = self._advance(current, "submitting")
                consumed[0] = True

            try:
                yield submit
            except Exception:
                with self._admission():
                    self._advance(current, "unknown")
                raise
            else:
                with self._admission():
                    receipt = self._receipt()
                    result = receipt.get("result")
                    if (not consumed[0] or receipt["state"] != "ready" or not isinstance(result, dict)
                            or result.get("id") != self.binding["name"]
                            or any(result.get(k) != receipt.get(k) for k in ("name", "request_id", "owner_root", "created_at"))
                            or result.get("status") != "running"):
                        raise _conflict()
                    self._advance(current, "completed", _digest(result))

    @contextmanager
    def mutation_guard(self, generation):
        """Cooperating lifecycle entry point; no automatic uncertain takeover."""
        with self._lifecycle():
            with self._admission():
                self._current(generation, {"completed"})
                self._receipt()
            yield

    def observe(self):
        """File-only observation: no directory creation, backend calls or repair."""
        value = self._read()
        if value is None:
            return {"state": "not_recorded"}
        result = {"request_id": self.binding["request_id"], "name": self.binding["name"],
                  "phase": value["phase"], "revision": value["revision"],
                  "evidence_gap": value["phase"] in {"accepted", "submitting", "unknown"}}
        if value["phase"] == "completed":
            try:
                receipt = self._receipt()
            except admission.AdmissionError:
                result["evidence_gap"] = True
            else:
                saved = receipt.get("result")
                if receipt["state"] == "ready" and _digest(saved) == value["result_digest"]:
                    result["result"] = saved
                else:
                    result["evidence_gap"] = True
        return result
