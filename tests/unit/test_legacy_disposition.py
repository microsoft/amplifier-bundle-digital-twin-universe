# Copyright (c) Microsoft. All rights reserved.

"""Synthetic legacy-disposition contract; not OS authorization acceptance.

Only Executor.from_system is replaced: the real executor, inventory validation,
locks, atomic writer, accounting and recovery are exercised with local fixtures.
No system configuration, real daemon, subprocess or historical receipt is used.
The designated-operator/restricted-agent boundary needs separate guest evidence.
"""

import hashlib
import json
import os
import socket
import stat
import subprocess
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from amplifier_bundle_digital_twin_universe import admission, bound_backend, incus
from amplifier_bundle_digital_twin_universe import legacy_disposition as legacy

REQUEST = "synthetic-legacy-request"
OWNER = "synthetic-legacy-owner"
NAME = "synthetic-legacy-instance"
OPERATION = "synthetic-operator-decision-1"


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _config_digest(config):
    return _sha(json.dumps(config, sort_keys=True).encode())


def _private_bytes(path, raw):
    """Fixture-only write, including deliberately invalid historical bytes."""
    path.write_bytes(raw)
    path.chmod(0o600)


def _files(base):
    return {p.relative_to(base): p.read_bytes() for p in base.rglob("*") if p.is_file()}


def _original(case):
    assert case.path.read_bytes() == case.original
    assert admission.read_receipt(case.root, REQUEST) == case.receipt
    assert case.receipt["state"] == "unknown"


def _prepare(case, effect=legacy.RELEASE):
    return legacy.prepare(REQUEST, OWNER, effect)


def _apply(case, prepared, effect=legacy.RELEASE, operation_id=OPERATION):
    return legacy.apply(REQUEST, OWNER, effect, prepared["digest"], operation_id)


def _counts(case):
    return admission.snapshot(
        case.backend.scope, admission.policy(1, 2, 0), case.root
    )["counts"]


def _instance(*, name=NAME, owner=OWNER, request=REQUEST, status="Running"):
    return {
        "name": name,
        "status": status,
        "config": {
            "user.dtu.managed-by": admission.MANAGED_VALUE,
            "user.dtu.request-id": request,
            "user.dtu.owner-root": owner,
            "user.dtu.launch-state": "ready",
        },
    }


@pytest.fixture
def case(tmp_path, monkeypatch):
    base = tmp_path / "admission"
    base.mkdir(mode=0o700)
    scope = {"daemon": "b" * 64, "project": "default", "storage_pool": "pool"}
    root = base / _sha(f"{scope['daemon']}\n{scope['project']}".encode())
    root.mkdir(mode=0o700)
    receipt = {
        "request_id": REQUEST,
        "name": NAME,
        "owner_root": OWNER,
        "intent_digest": "a" * 64,
        "created_at": "2000-01-01T00:00:00Z",
        "state": "unknown",
    }
    # Noncanonical bytes deliberately make parse/reserialize detectable.
    original = (json.dumps(receipt, indent=2) + "\n ").encode()
    path = admission.receipt_path(root, REQUEST)
    _private_bytes(path, original)
    config = {
        "admission_base": str(base),
        "generation": "1" * 32,
        "operator_uid": os.getuid(),
    }

    class Backend:
        digest = "c" * 64

        def __init__(self):
            self.scope = dict(scope)
            self.active = 0
            self.verifications = 0
            self.drifted = False

        @contextmanager
        def activate(self):
            self.active += 1
            try:
                yield self
            finally:
                self.active -= 1

        def verify(self):
            self.verifications += 1
            if self.drifted:
                raise admission.AdmissionError("backend_changed", "Synthetic drift")

        def as_dict(self):
            return {"scope": self.scope, "executable": {"path": "/synthetic/incus"},
                    "config": {"path": "/synthetic/config"},
                    "global_config": {"path": "/synthetic/global"}, "query_timeout": 1}

    backend = Backend()
    data = SimpleNamespace(
        base=base, root=root, path=path, original=original, receipt=receipt,
        config=config, backend=backend, inventory=[], queries=[],
        intent=root / "legacy" / (_sha(REQUEST.encode()) + ".intent"),
        marker=root / "legacy" / (_sha(REQUEST.encode()) + ".applied"),
    )

    def from_system(cls):
        return cls(deepcopy(config), _config_digest(config), backend)

    monkeypatch.setattr(legacy.Executor, "from_system", classmethod(from_system))
    monkeypatch.setenv("AMPLIFIER_DTU_ADMISSION_DIR", str(base))
    monkeypatch.setenv("AMPLIFIER_DTU_ADMISSION_TIMEOUT_SECONDS", "0")

    def query(endpoint):
        data.queries.append(endpoint)
        if endpoint == "/1.0/storage-pools/pool/resources":
            return {"space": {"total": 100 * 1024**3, "used": 0}}
        assert endpoint == "/1.0/instances?recursion=1", "Inventory must be complete"
        assert backend.active > 0, "Inventory escaped the pinned backend"
        assert backend.verifications > 0, "Inventory used an unverified backend"
        return deepcopy(data.inventory)

    monkeypatch.setattr(incus, "query", query)
    monkeypatch.setattr(bound_backend, "_check_local", lambda record: None)
    def capture(argv, **kwargs):
        assert argv[:2] == ["/synthetic/incus", "query"]
        assert argv[2].endswith("&project=default")
        return json.dumps(query(argv[2].removesuffix("&project=default"))).encode()
    monkeypatch.setattr(bound_backend, "_capture_identity_query", capture)
    def filtered_list(key, value):
        assert (key, value) == (admission.MANAGED_KEY, admission.MANAGED_VALUE)
        return deepcopy([row for row in data.inventory if row.get("config", {}).get(key) == value])
    monkeypatch.setattr(incus, "list_instances", filtered_list)
    forbidden = Mock(side_effect=AssertionError("Legacy disposition attempted an external effect"))
    for name in (
        "_run", "create_container", "delete_container", "stop_container",
        "exec_command", "file_push", "file_pull", "set_config",
    ):
        monkeypatch.setattr(incus, name, forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    yield data
    forbidden.assert_not_called()  # Also catches swallowed unexpected calls.
    assert backend.active == 0


@pytest.fixture
def pending(case, monkeypatch):
    prepared = _prepare(case)
    save = admission._save_private_bytes
    attempted = []

    def fail_marker(path, raw):
        if path == case.marker:
            attempted.append(path)
            raise OSError("synthetic marker not published")
        return save(path, raw)

    with monkeypatch.context() as patch:
        patch.setattr(admission, "_save_private_bytes", fail_marker)
        with pytest.raises(admission.AdmissionError):
            _apply(case, prepared)
    assert attempted == [case.marker]
    assert case.intent.is_file() and not case.marker.exists()
    _original(case)
    return case, prepared


def test_effect_constants_are_explicit_risk_decisions():
    assert legacy.KEEP == "record_unknown_keep_reservation"
    assert legacy.RELEASE == "accept_unknown_risk_release_reservation"


def test_prepare_binds_exact_unversioned_bytes_without_consuming_a_decision(case):
    prepared = _prepare(case)
    preview = prepared["preview"]
    assert len(prepared["digest"]) == 64
    assert set(prepared["digest"]) <= set("0123456789abcdef")
    assert preview["receipt_sha256"] == _sha(case.original)
    assert preview["receipt_hex"] == case.original.hex()
    assert preview["scope"] == case.backend.scope
    assert preview["destination_digest"] == case.backend.digest
    assert preview["config_digest"] == _config_digest(case.config)
    assert preview["generation"] == case.config["generation"]
    assert preview["effect"] == legacy.RELEASE
    assert preview["request_id"] == REQUEST
    assert preview["name"] == NAME
    assert preview["owner_root"] == preview["late_cleanup_owner"] == OWNER
    assert preview["residual_names"] == []
    assert preview["revision"] == 1  # New decision, not invented legacy metadata.
    assert "revision" not in case.receipt and "schema_version" not in case.receipt
    assert not case.intent.exists() and not case.marker.exists()
    assert _prepare(case) == prepared
    assert _prepare(case, legacy.KEEP)["digest"] != prepared["digest"]
    assert _counts(case) == {
        "active": 1, "stopped_retained": 0, "pending_unobserved": 1, "total": 1,
    }
    _original(case)


@pytest.mark.parametrize("effect,pending_count", [(legacy.KEEP, 1), (legacy.RELEASE, 0)])
def test_applied_decision_changes_only_separate_accounting(case, effect, pending_count):
    prepared = _prepare(case, effect)
    assert _apply(case, prepared, effect)["outcome"] == "applied"
    assert case.intent.is_file() and case.marker.is_file()
    assert json.loads(case.marker.read_bytes())["digest"] == prepared["digest"]
    assert legacy.reservation_released(case.root, case.receipt) is (pending_count == 0)
    assert _counts(case) == {
        "active": pending_count, "stopped_retained": 0,
        "pending_unobserved": pending_count, "total": pending_count,
    }
    before = _files(case.base)
    assert _apply(case, prepared, effect)["outcome"] == "already_applied"
    assert _files(case.base) == before
    _original(case)


@pytest.mark.parametrize("status", ["Running", "Stopped"])
def test_release_never_hides_an_instance_that_arrives_later(case, status):
    prepared = _prepare(case)
    assert _apply(case, prepared)["outcome"] == "applied"
    case.inventory.append(_instance(status=status))
    before = _files(case.base)
    assert _counts(case) == {
        "active": int(status == "Running"), "stopped_retained": int(status == "Stopped"),
        "pending_unobserved": 0, "total": 1,
    }
    legacy.reconcile(REQUEST)
    assert _files(case.base) == before
    _original(case)


@pytest.mark.parametrize("status", ["Running", "Stopped", "Frozen", "Unrecognized"])
@pytest.mark.parametrize("metadata", ["untagged", "foreign-tag", "partial", "renamed-request", "managed"])
def test_late_resource_accounting_preserves_backend_filter(case, status, metadata):
    assert _counts(case)["pending_unobserved"] == _counts(case)["total"] == 1
    _apply(case, _prepare(case))
    assert _counts(case)["total"] == 0
    row = {"name": NAME, "status": status, "config": {}}
    if metadata == "foreign-tag":
        row["config"] = {admission.MANAGED_KEY: "other-manager"}
    elif metadata == "partial":
        row["config"] = {"user.dtu.owner-root": "foreign-owner"}
    elif metadata == "renamed-request":
        row["name"] = "renamed-instance"
        row["config"] = {"user.dtu.request-id": REQUEST}
    elif metadata == "managed":
        row = _instance(status=status)
    case.inventory[:] = [row]
    assert incus.list_instances(admission.MANAGED_KEY, admission.MANAGED_VALUE) == (
        [row] if metadata == "managed" else []
    )
    before = _files(case.base)
    snapshot = admission.snapshot(case.backend.scope, admission.policy(1, 1, 0), case.root)
    assert snapshot["counts"] == {
        "active": int(status != "Stopped"), "stopped_retained": int(status == "Stopped"),
        "pending_unobserved": 0, "total": 1,
    }
    assert {b["code"] for b in snapshot["blockers"]} == (
        {"retained_capacity"} if status == "Stopped" else {"execution_capacity", "retained_capacity"}
    )
    assert legacy.reconcile(REQUEST)["original_outcome"] == "unknown"
    with pytest.raises(admission.AdmissionError):
        legacy.guard_name(case.root, NAME)
    assert _files(case.base) == before
    _original(case)


def test_release_accounting_does_not_expand_unrelated_unmanaged_scope(case):
    _apply(case, _prepare(case))
    case.inventory[:] = [
        {"name": NAME, "status": "Running", "config": {}},
        {"name": "unrelated", "status": "Running", "config": {}},
        _instance(name="managed-other", request="other", owner="other"),
    ]
    before = _files(case.base)
    counts = _counts(case)
    assert counts == {"active": 2, "stopped_retained": 0, "pending_unobserved": 0, "total": 2}
    assert _files(case.base) == before
    _original(case)


def test_one_late_resource_matching_two_releases_counts_once(case):
    _apply(case, _prepare(case))
    other = {**case.receipt, "request_id": "other-request", "name": "other-legacy-name"}
    other_path = admission.receipt_path(case.root, other["request_id"])
    original = json.dumps(other).encode()
    _private_bytes(other_path, original)
    prepared = legacy.prepare(other["request_id"], OWNER, legacy.RELEASE)
    legacy.apply(other["request_id"], OWNER, legacy.RELEASE, prepared["digest"], "other-decision")
    case.inventory[:] = [{"name": NAME, "status": "Running",
                          "config": {"user.dtu.request-id": other["request_id"]}}]
    assert _counts(case) == {"active": 1, "stopped_retained": 0, "pending_unobserved": 0, "total": 1}
    assert other_path.read_bytes() == original
    _original(case)


@pytest.mark.parametrize("fault", ["unavailable", "truncated", "duplicate", "blank-name", "wrong-scope"])
def test_release_accounting_never_publishes_capacity_from_incomplete_inventory(case, monkeypatch, fault):
    _apply(case, _prepare(case))
    before = _files(case.base)
    # The ordinary filtered listing succeeds; only the extra full observation fails.
    if fault == "unavailable":
        def failed(*args, **kwargs):
            raise bound_backend.BackendFence("Synthetic unavailable inventory")
        monkeypatch.setattr(bound_backend, "_capture_identity_query", failed)
    elif fault == "truncated":
        monkeypatch.setattr(bound_backend, "_capture_identity_query", lambda *a, **k: b"[")
    elif fault == "duplicate":
        case.inventory[:] = [{"name": NAME, "status": "Running", "config": {}}] * 2
    elif fault == "blank-name":
        case.inventory[:] = [{"name": "", "status": "Running", "config": {}}]
    with pytest.raises((admission.AdmissionError, bound_backend.BackendFence)):
        if fault == "wrong-scope":
            admission.snapshot({**case.backend.scope, "project": "different"},
                               admission.policy(1, 2, 0), case.root)
        else:
            _counts(case)
    assert _files(case.base) == before
    _original(case)


@pytest.mark.parametrize("disposition", ["none", "keep", "pending"])
def test_no_applied_release_keeps_normal_filtered_snapshot(case, monkeypatch, disposition):
    if disposition == "keep":
        _apply(case, _prepare(case, legacy.KEEP), legacy.KEEP)
    elif disposition == "pending":
        writer = admission._save_private_bytes
        def fail_marker(path, raw):
            if path == case.marker:
                raise OSError("before marker")
            writer(path, raw)
        with monkeypatch.context() as patch:
            patch.setattr(admission, "_save_private_bytes", fail_marker)
            with pytest.raises(admission.AdmissionError):
                _apply(case, _prepare(case))
    case.inventory[:] = [{"name": "unrelated", "status": "Running", "config": {}},
                        _instance(name="managed-other", request="other", owner="other")]
    unexpected = Mock(side_effect=AssertionError("Unreleased snapshot queried complete inventory"))
    monkeypatch.setattr(bound_backend, "_capture_identity_query", unexpected)
    assert _counts(case) == {"active": 2, "stopped_retained": 0, "pending_unobserved": 1, "total": 2}
    unexpected.assert_not_called()
    _original(case)


@pytest.mark.parametrize("change", ["digest", "effect", "owner", "operation"])
def test_mismatched_decision_is_not_an_idempotent_success(case, change):
    prepared = _prepare(case)
    assert _apply(case, prepared)["outcome"] == "applied"
    args = [REQUEST, OWNER, legacy.RELEASE, prepared["digest"], OPERATION]
    args[{"digest": 3, "effect": 2, "owner": 1, "operation": 4}[change]] = {
        "digest": "0" * 64, "effect": legacy.KEEP,
        "owner": "other-owner", "operation": "different-operation",
    }[change]
    before = _files(case.base)
    with pytest.raises(admission.AdmissionError):
        legacy.apply(*args)
    assert _files(case.base) == before
    _original(case)


@pytest.mark.parametrize("change", ["digest", "effect", "owner", "whitespace", "receipt"])
def test_stale_preview_refuses_before_recording_an_intent(case, change):
    prepared = _prepare(case)
    args = [REQUEST, OWNER, legacy.RELEASE, prepared["digest"], OPERATION]
    if change == "digest":
        args[3] = "0" * 64
    elif change == "effect":
        args[2] = legacy.KEEP
    elif change == "owner":
        args[1] = "other-owner"
    elif change == "whitespace":
        case.path.write_bytes(case.original + b"\n")
    else:
        _private_bytes(case.path, json.dumps({**case.receipt, "intent_digest": "f" * 64}).encode())
    before = _files(case.base)
    with pytest.raises(admission.AdmissionError):
        legacy.apply(*args)
    assert _files(case.base) == before
    assert not case.intent.exists() and not case.marker.exists()
    assert _counts(case)["pending_unobserved"] == 1


def test_reconcile_and_repeat_apply_never_repair_pending_intent(pending, monkeypatch):
    case, prepared = pending
    before = _files(case.base)
    no_write = Mock(side_effect=AssertionError("Observation or retry wrote a disposition"))
    with monkeypatch.context() as patch:
        patch.setattr(admission, "_save_private_bytes", no_write)
        legacy.reconcile(REQUEST)
        legacy.reconcile(REQUEST)
        with pytest.raises(admission.AdmissionError) as caught:
            _apply(case, prepared)
        assert caught.value.code == "projection_pending"
        assert legacy.reservation_released(case.root, case.receipt) is False
        assert _counts(case)["pending_unobserved"] == 1
    no_write.assert_not_called()
    assert _files(case.base) == before
    assert not case.marker.exists()
    _original(case)


def test_explicit_recover_projects_only_marker_and_is_idempotent(pending, monkeypatch):
    case, prepared = pending
    intent = case.intent.read_bytes()
    save = admission._save_private_bytes
    writes = []

    def marker_only(path, raw):
        writes.append(path)
        assert path == case.marker, "Recovery must not regenerate the committed intent"
        return save(path, raw)

    with monkeypatch.context() as patch:
        patch.setattr(admission, "_save_private_bytes", marker_only)
        assert legacy.recover(REQUEST, prepared["digest"], OPERATION)["outcome"] == "applied"
        assert legacy.recover(REQUEST, prepared["digest"], OPERATION)["outcome"] == "already_applied"
    assert writes == [case.marker]
    assert case.intent.read_bytes() == intent
    assert _counts(case)["pending_unobserved"] == 0
    _original(case)


def test_recover_without_committed_intent_does_not_invent_one(case):
    prepared = _prepare(case)
    before = _files(case.base)
    with pytest.raises(admission.AdmissionError):
        legacy.recover(REQUEST, prepared["digest"], OPERATION)
    assert _files(case.base) == before
    assert not case.intent.exists() and not case.marker.exists()
    _original(case)


@pytest.mark.parametrize("change", [
    "digest", "operation", "generation", "backend", "whitespace", "owner", "residual",
])
def test_recovery_revalidates_exact_authority_and_current_evidence(pending, change):
    case, prepared = pending
    digest, operation = prepared["digest"], OPERATION
    if change == "digest":
        digest = "0" * 64
    elif change == "operation":
        operation = "different-operation"
    elif change == "generation":
        case.config["generation"] = "2" * 32
    elif change == "backend":
        case.backend.digest = "d" * 64
    elif change == "whitespace":
        case.path.write_bytes(case.original + b" ")
    elif change == "owner":
        _private_bytes(case.path, json.dumps({**case.receipt, "owner_root": "other"}).encode())
    else:
        case.inventory.append(_instance())
    before = _files(case.base)
    with pytest.raises(admission.AdmissionError):
        legacy.recover(REQUEST, digest, operation)
    assert _files(case.base) == before
    assert not case.marker.exists()
    assert legacy.reservation_released(case.root, json.loads(case.path.read_bytes())) is False


@pytest.mark.parametrize("stage", ["intent", "marker"])
def test_postreplace_failure_retains_exact_bytes_without_automatic_replay(case, monkeypatch, stage):
    prepared = _prepare(case)
    destination = case.intent if stage == "intent" else case.marker
    save = admission._save_private_bytes
    retained = []

    def lose_ack(path, raw):
        save(path, raw)
        if path == destination:
            retained.append(raw)
            raise OSError("synthetic postreplace failure")

    with monkeypatch.context() as patch:
        patch.setattr(admission, "_save_private_bytes", lose_ack)
        with pytest.raises(admission.AdmissionError):
            _apply(case, prepared)
    assert len(retained) == 1 and destination.read_bytes() == retained[0]
    assert case.marker.exists() is (stage == "marker")
    before = _files(case.base)
    legacy.reconcile(REQUEST)
    if stage == "intent":
        with pytest.raises(admission.AdmissionError) as caught:
            _apply(case, prepared)
        assert caught.value.code == "projection_pending"
        assert _counts(case)["pending_unobserved"] == 1
    else:
        assert _apply(case, prepared)["outcome"] == "already_applied"
    assert _files(case.base) == before
    assert legacy.recover(REQUEST, prepared["digest"], OPERATION)["outcome"] == (
        "applied" if stage == "intent" else "already_applied"
    )
    assert destination.read_bytes() == retained[0]
    _original(case)


@pytest.mark.parametrize("stage", ["intent", "marker"])
@pytest.mark.parametrize("boundary", ["file", "directory"])
def test_actual_fsync_fault_preserves_original_and_never_replays(case, monkeypatch, stage, boundary):
    prepared = _prepare(case)
    target = case.intent if stage == "intent" else case.marker
    save, fsync = admission._save_private_bytes, os.fsync
    armed = False
    failed = []
    writes = []

    def fault(fd):
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if armed and not failed and is_directory == (boundary == "directory"):
            failed.append(boundary)
            raise OSError("synthetic fsync failure")
        return fsync(fd)

    def intercept(path, raw):
        nonlocal armed
        writes.append(path)
        armed = path == target
        try:
            return save(path, raw)
        finally:
            armed = False

    with monkeypatch.context() as patch:
        patch.setattr(admission, "_save_private_bytes", intercept)
        patch.setattr(os, "fsync", fault)
        with pytest.raises(admission.AdmissionError):
            _apply(case, prepared)
    assert failed == [boundary]
    assert writes == ([case.intent] if stage == "intent" else [case.intent, case.marker])
    assert case.intent.exists() is (stage == "marker" or boundary == "directory")
    assert case.marker.exists() is (stage == "marker" and boundary == "directory")
    assert not list(case.root.rglob(".receipt-*.tmp"))
    before = _files(case.base)
    legacy.reconcile(REQUEST)
    assert _files(case.base) == before
    if case.intent.exists() and not case.marker.exists():
        with pytest.raises(admission.AdmissionError) as caught:
            _apply(case, prepared)
        assert caught.value.code == "projection_pending"
        assert _counts(case)["pending_unobserved"] == 1
    elif not case.intent.exists():
        with pytest.raises(admission.AdmissionError):
            legacy.recover(REQUEST, prepared["digest"], OPERATION)
        assert _counts(case)["pending_unobserved"] == 1
    assert _files(case.base) == before
    _original(case)


@pytest.mark.parametrize("raw", [b"{", b"[]", b'{"version":999}', b'{"version":1,"version":1}'])
def test_malformed_or_unsupported_intent_never_grants_recovery(pending, raw):
    case, prepared = pending
    _private_bytes(case.intent, raw)
    before = _files(case.base)
    with pytest.raises(admission.AdmissionError):
        legacy.recover(REQUEST, prepared["digest"], OPERATION)
    assert _files(case.base) == before
    assert not case.marker.exists()
    _original(case)


@pytest.mark.parametrize("kind", ["mode", "symlink", "hardlink", "oversize"])
@pytest.mark.parametrize("target", ["receipt", "intent"])
def test_unsafe_files_refuse_without_rewriting_or_following_links(pending, kind, target):
    case, prepared = pending
    path = case.path if target == "receipt" else case.intent
    raw = path.read_bytes()
    other = case.base / "untouched-copy"
    _private_bytes(other, raw)
    if kind == "mode":
        path.chmod(0o644)
    elif kind == "oversize":
        path.write_bytes(b" " * 262145)
    else:
        path.unlink()
        if kind == "symlink":
            path.symlink_to(other)
        else:
            os.link(other, path)
    before = _files(case.base)
    with pytest.raises(admission.AdmissionError):
        legacy.recover(REQUEST, prepared["digest"], OPERATION)
    assert _files(case.base) == before
    assert other.read_bytes() == raw and not case.marker.exists()
    if kind == "symlink":
        assert path.is_symlink()


@pytest.mark.parametrize("change", [
    {"schema_version": 1}, {"schema_version": None}, {"state": "ready"},
    {"state": "removed"}, {"state": "failed"}, {"state": "created"},
    {"request_id": "other-request"}, {"owner_root": ""}, {"owner_root": None},
    {"name": ""}, {"intent_digest": "not-a-hash"}, {"create_operation": {}},
])
def test_ineligible_receipt_is_not_migrated_or_adjudicated(case, change):
    raw = json.dumps({**case.receipt, **change}).encode()
    _private_bytes(case.path, raw)
    with pytest.raises(admission.AdmissionError):
        _prepare(case)
    assert case.path.read_bytes() == raw
    assert not case.intent.exists() and not case.marker.exists()


@pytest.mark.parametrize("suffix", [".json", ".input", ".evidence", ".publication"])
def test_existing_collector_evidence_is_not_a_legacy_attempt(case, suffix):
    directory = case.root / "collectors"
    directory.mkdir(mode=0o700)
    path = directory / (_sha(REQUEST.encode()) + suffix)
    _private_bytes(path, b'{"synthetic":"retained collector evidence"}')
    raw = path.read_bytes()
    with pytest.raises(admission.AdmissionError):
        _prepare(case)
    assert path.read_bytes() == raw
    assert not case.intent.exists() and not case.marker.exists()
    _original(case)


@pytest.mark.parametrize("identity", ["matching", "foreign-owner", "renamed"])
def test_present_residual_refuses_release_even_with_old_receipt(case, identity):
    case.inventory.append(_instance(
        name="renamed-instance" if identity == "renamed" else NAME,
        owner="other-owner" if identity == "foreign-owner" else OWNER,
    ))
    with pytest.raises(admission.AdmissionError):
        _prepare(case)
    assert not case.intent.exists() and not case.marker.exists()
    _original(case)


def test_residual_arriving_after_preview_refuses_before_intent(case):
    prepared = _prepare(case)
    case.inventory.append(_instance())
    before = _files(case.base)
    with pytest.raises(admission.AdmissionError):
        _apply(case, prepared)
    assert _files(case.base) == before
    assert not case.intent.exists()
    _original(case)


def test_unrelated_instance_does_not_prevent_exact_release_or_disappear_from_capacity(case):
    case.inventory.append(_instance(name="unrelated", request="other-r", owner="other-o"))
    prepared = _prepare(case)
    assert _apply(case, prepared)["outcome"] == "applied"
    assert _counts(case) == {
        "active": 1, "stopped_retained": 0, "pending_unobserved": 0, "total": 1,
    }
    _original(case)


@pytest.mark.parametrize("inventory", [None, {}, ["/1.0/instances/unexpanded"], [None], [{"name": NAME}]])
def test_incomplete_inventory_is_not_absence_evidence(case, inventory):
    case.inventory = inventory
    with pytest.raises(admission.AdmissionError):
        _prepare(case)
    assert not case.intent.exists() and not case.marker.exists()
    _original(case)


@pytest.mark.parametrize("change", ["generation", "destination", "verification"])
def test_config_or_backend_drift_revokes_prepared_authority(case, change):
    prepared = _prepare(case)
    if change == "generation":
        case.config["generation"] = "2" * 32
    elif change == "destination":
        case.backend.digest = "d" * 64
    else:
        case.backend.drifted = True
    before = _files(case.base)
    with pytest.raises(admission.AdmissionError):
        _apply(case, prepared)
    assert _files(case.base) == before
    assert not case.intent.exists()
    _original(case)


def test_missing_config_preserves_applied_history_but_keeps_accounting_conservative(case, monkeypatch):
    prepared = _prepare(case)
    assert _apply(case, prepared)["outcome"] == "applied"
    before = _files(case.base)

    def unavailable(cls):
        raise admission.AdmissionError("authority_unavailable", "Synthetic missing config")

    monkeypatch.setattr(legacy.Executor, "from_system", classmethod(unavailable))
    assert legacy.reservation_released(case.root, case.receipt) is False
    assert _counts(case)["pending_unobserved"] == 1
    assert _files(case.base) == before
    _original(case)


def test_bad_applied_digest_never_becomes_an_idempotent_success(case):
    prepared = _prepare(case)
    assert _apply(case, prepared)["outcome"] == "applied"
    value = json.loads(case.marker.read_bytes())
    value["digest"] = "0" * 64
    _private_bytes(case.marker, json.dumps(value).encode())
    before = _files(case.base)
    with pytest.raises(admission.AdmissionError):
        _apply(case, prepared)
    with pytest.raises(admission.AdmissionError):
        legacy.recover(REQUEST, prepared["digest"], OPERATION)
    assert _files(case.base) == before
    _original(case)


@pytest.mark.parametrize("effect", [legacy.KEEP, legacy.RELEASE])
def test_applied_history_fences_name_reuse_and_original_receipt_writer(case, effect):
    prepared = _prepare(case, effect)
    assert _apply(case, prepared, effect)["outcome"] == "applied"
    before = _files(case.base)
    with pytest.raises(admission.AdmissionError):
        legacy.guard_name(case.root, NAME)
    with pytest.raises(admission.AdmissionError):
        admission.save_receipt(case.root, {**case.receipt, "state": "removed"})
    legacy.guard_name(case.root, "unrelated-name")
    assert _files(case.base) == before
    _original(case)


def test_pending_history_also_fences_name_and_original_receipt_writer(pending):
    case, _ = pending
    before = _files(case.base)
    with pytest.raises(admission.AdmissionError):
        legacy.guard_name(case.root, NAME)
    with pytest.raises(admission.AdmissionError):
        admission.save_receipt(case.root, {**case.receipt, "state": "failed"})
    assert _files(case.base) == before
    _original(case)


def test_executor_uses_configured_base_not_caller_admission_environment(case, monkeypatch, tmp_path):
    other = tmp_path / "caller-controlled"
    other.mkdir(mode=0o700)
    monkeypatch.setenv("AMPLIFIER_DTU_ADMISSION_DIR", str(other))
    prepared = _prepare(case)
    assert _apply(case, prepared)["outcome"] == "applied"
    assert case.intent.exists() and case.marker.exists()
    assert list(other.iterdir()) == []
    assert os.environ["AMPLIFIER_DTU_ADMISSION_DIR"] == str(other)
    _original(case)


def test_writes_hold_name_then_request_then_admission_custody(case, monkeypatch):
    prepared = _prepare(case)
    lock, save = admission._locked_file, admission._save_private_bytes
    stack, writes = [], []
    expected = [
        case.base / "lifecycle-locks" / (_sha(NAME.encode()) + ".lock"),
        case.root / "collectors" / (_sha(REQUEST.encode()) + ".lock"),
        case.root / "admission.lock",
    ]

    @contextmanager
    def traced(path, *args, **kwargs):
        with lock(path, *args, **kwargs):
            stack.append(Path(path))
            try:
                yield
            finally:
                assert stack.pop() == Path(path)

    def under_custody(path, raw):
        assert stack == expected
        writes.append(path)
        return save(path, raw)

    monkeypatch.setattr(admission, "_locked_file", traced)
    monkeypatch.setattr(admission, "_save_private_bytes", under_custody)
    assert _apply(case, prepared)["outcome"] == "applied"
    assert writes == [case.intent, case.marker]
    assert not stack
    _original(case)


def test_backend_drift_never_releases_during_accounting(case):
    prepared = _prepare(case)
    _apply(case, prepared)
    case.backend.drifted = True
    assert legacy.reservation_released(case.root, case.receipt) is False
    assert _counts(case)["pending_unobserved"] == 1
    _original(case)


def test_collected_same_name_refuses_before_dispatch_and_keeps_recovery(pending, monkeypatch, tmp_path):
    from amplifier_bundle_digital_twin_universe import collected_launch
    case, prepared = pending
    profile = tmp_path / "profile.yaml"
    profile.write_text("base:\n  image: local:" + "a" * 64 + "\n")
    monkeypatch.setattr(collected_launch, "capture_backend", lambda: case.backend)
    spy = Mock(side_effect=AssertionError("Quarantined name dispatched"))
    monkeypatch.setattr(collected_launch, "spawn_worker", spy)
    before = _files(case.base)
    with pytest.raises(admission.AdmissionError, match="Legacy name"):
        collected_launch.launch(str(profile), {}, name=NAME, owner_root=OWNER,
                                request_id="new-request", offline=True)
    spy.assert_not_called()
    assert _files(case.base) == before
    assert legacy.recover(REQUEST, prepared["digest"], OPERATION)["outcome"] == "applied"


def test_original_request_legacy_status_is_unknown_and_launch_refuses(case, monkeypatch, tmp_path):
    from amplifier_bundle_digital_twin_universe import engine
    _apply(case, _prepare(case))
    monkeypatch.setattr(admission, "context", lambda: case.backend.scope)
    before = _files(case.base)
    status = engine.launch_status(REQUEST)
    assert status["state"] == "unknown" and status["replay_permitted"] is False
    assert "result" not in status
    profile = tmp_path / "legacy.yaml"
    profile.write_text("base:\n  image: local:" + "a" * 64 + "\n")
    with pytest.raises(admission.AdmissionError):
        engine.launch(str(profile), {}, name=NAME, owner_root=OWNER, request_id=REQUEST, offline=True)
    assert _files(case.base) == before
    _original(case)
