# Copyright (c) Microsoft. All rights reserved.
"""Bounded acknowledgment evidence for new collected attempts, not authority.

The existing v1 collector and unversioned admission formats are unchanged.
Missing records are never backfilled by observation. Only the live owning worker
records acknowledgments; neither these facts nor their absence permit replay,
settlement, cleanup or takeover. Acknowledgments do not establish host durability.
"""
import json
import re

from . import admission, collector

STEPS = ("creation_returned", "provisioning_entered", "provisioning_completed",
         "checkpoint_saved", "ready_marker_returned", "final_receipt_saved")
_KEYS = {"version", "binding", "generation", "revision", "steps", "creation_transport", "result_digest"}
_MAX_BYTES = 32768


class ObservationError(admission.AdmissionError):
    def __init__(self):
        super().__init__("observation_unknown", "Collected acknowledgment evidence is unavailable or changed")


def _path(store):
    return store.path.with_suffix(".observations")


def _validate(store, generation, value):
    if (not isinstance(value, dict) or set(value) != _KEYS
            or type(value["version"]) is not int or value["version"] != 1
            or value["binding"] != dict(store.binding) or value["generation"] != generation
            or type(value["revision"]) is not int
            or not isinstance(value["steps"], list)
            or value["steps"] != list(STEPS[:len(value["steps"])])
            or len(value["steps"]) > len(STEPS)
            or value["revision"] != len(value["steps"]) + 1):
        raise ObservationError()
    steps = value["steps"]
    if value["creation_transport"] not in ({"cli", "operation"} if steps else {None}):
        raise ObservationError()
    digest = value["result_digest"]
    if len(steps) >= 3:
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ObservationError()
    elif digest is not None:
        raise ObservationError()
    if len(json.dumps(value, allow_nan=False).encode()) > _MAX_BYTES:
        raise ObservationError()


def _read(store, generation):
    store._directories()
    current = store._read()
    if current is None or current["generation"] != generation:
        raise ObservationError()
    path = _path(store)
    try:
        if path.lstat().st_size > _MAX_BYTES:
            raise ObservationError()
        value = admission._read_private_json(path, object_pairs_hook=collector._unique_object)
    except FileNotFoundError:
        return None
    _validate(store, generation, value)
    return value


def initialize(store, generation):
    """Fresh-attempt setup under name -> request -> admission; never migration."""
    try:
        with store._lifecycle(), store._admission():
            store._current(generation, {"prepared"})
            if _read(store, generation) is not None:
                raise ObservationError()
            value = {"version": 1, "binding": dict(store.binding), "generation": generation,
                     "revision": 1, "steps": [], "creation_transport": None, "result_digest": None}
            _validate(store, generation, value)
            admission._save_private_json(_path(store), value)
    except Exception:
        raise ObservationError() from None


class Recorder:
    """Internal handle used only while its worker holds request custody.

    No admission lock is acquired here: engine hooks may already hold it. A
    stale revision is a refusal, not a retry. Writes never modify either receipt.
    """
    def __init__(self, store, generation):
        self.store, self.generation = store, generation
        try:
            self.value = _read(store, generation)
            if self.value is None or self.value["steps"]:
                raise ObservationError()
        except Exception:
            raise ObservationError() from None

    def mark(self, step, *, transport=None, result=None):
        try:
            if _read(self.store, self.generation) != self.value:
                raise ObservationError()
            self.store._current(self.generation, {"submitting"})
            index = len(self.value["steps"])
            if index >= len(STEPS) or step != STEPS[index]:
                raise ObservationError()
            value = {**self.value, "steps": [*self.value["steps"], step], "revision": index + 2}
            if step == "creation_returned":
                value["creation_transport"] = transport
            elif transport is not None:
                raise ObservationError()
            if step == "provisioning_completed":
                if (not isinstance(result, dict) or result.get("id") != self.store.binding["name"]
                        or result.get("status") != "running"
                        or any(result.get(k) != self.store.binding[k] for k in ("name", "request_id", "owner_root"))):
                    raise ObservationError()
                value["result_digest"] = collector._digest(result)
            elif result is not None:
                raise ObservationError()
            _validate(self.store, self.generation, value)
            admission._save_private_json(_path(self.store), value)
            self.value = value
        except Exception:
            raise ObservationError() from None


def projection(store, generation):
    """Read only; missing/partial acknowledgments are not inferred from receipts."""
    try:
        value = _read(store, generation)
        if value is None:
            return {"available": False}
        return {"available": True, "revision": value["revision"],
                "acknowledged": list(value["steps"]), "creation_transport": value["creation_transport"],
                "all_acknowledged": len(value["steps"]) == len(STEPS)}
    except Exception:
        raise ObservationError() from None
