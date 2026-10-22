"""Collector protocol only: no subprocess, backend or real cancellation authority."""
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from amplifier_bundle_digital_twin_universe import admission


@pytest.fixture
def attempt(tmp_path):
    collector = importlib.import_module("amplifier_bundle_digital_twin_universe.collector")
    root = tmp_path / "admission"
    root.mkdir(mode=0o700)
    binding = dict(request_id="collector-request", owner_root="collector-owner", name="collector-case",
                   intent_digest="a" * 64, daemon="b" * 64, project="default",
                   storage_pool="pool", destination_digest="c" * 64)
    store = collector.AttemptStore(root, binding)
    calls = []

    def reserve():
        calls.append("reserve")
        receipt = {key: binding[key] for key in ("request_id", "owner_root", "name", "intent_digest")}
        receipt.update(state="launching", created_at="fixed-time")
        admission.save_receipt(root, receipt)

    def execute():
        assert store.observe()["phase"] == "submitting"
        calls.append("execute")
        receipt = admission.read_receipt(root, binding["request_id"])
        assert receipt["state"] == "launching"
        result = {key: receipt[key] for key in ("request_id", "owner_root", "name", "created_at")}
        result.update(id=receipt["name"], status="running", info=[])
        receipt.update(state="ready", result=result)
        admission.save_receipt(root, receipt)
        return result

    return collector, store, binding, calls, reserve, execute


def test_collector_reserves_before_single_consumption_and_exact_result(attempt):
    _, store, _, calls, reserve, execute = attempt
    ref = store.prepare()
    launches = []
    store.dispatch_once(ref, launches.append)
    result = store.run_once(ref, reserve, execute)
    assert calls == ["reserve", "execute"] and launches == [ref]
    assert store.observe()["result"] == result
    assert store.observe()["phase"] == "completed"
    with pytest.raises(admission.AdmissionError):
        store.dispatch_once(ref, launches.append)
    with pytest.raises(admission.AdmissionError):
        store.run_once(ref, reserve, execute)
    assert calls == ["reserve", "execute"] and launches == [ref]


@pytest.mark.parametrize("field", ["owner_root", "name", "intent_digest", "daemon", "project", "destination_digest"])
def test_collector_changed_binding_refuses_without_rewriting(attempt, field):
    collector, store, binding, _, _, _ = attempt
    store.prepare()
    before = store.path.read_bytes()
    other = {**binding, field: ("d" * 64 if field.endswith("digest") or field == "daemon" else "other")}
    with pytest.raises(admission.AdmissionError):
        collector.AttemptStore(store.root, other).prepare()
    assert store.path.read_bytes() == before


def test_collector_legacy_receipt_cannot_be_adopted(attempt):
    _, store, binding, _, reserve, _ = attempt
    reserve()
    path = admission.receipt_path(store.root, binding["request_id"])
    before = path.read_bytes()
    with pytest.raises(admission.AdmissionError):
        store.prepare()
    assert path.read_bytes() == before and not store.path.exists()


def test_collector_lost_dispatch_ack_never_respawns(attempt):
    _, store, _, calls, reserve, execute = attempt
    ref = store.prepare()
    spawned = []

    def spawn(value):
        spawned.append(value)
        raise OSError("private-token/path")

    with pytest.raises(OSError):
        store.dispatch_once(ref, spawn)
    with pytest.raises(admission.AdmissionError):
        store.dispatch_once(ref, spawn)
    assert spawned == [ref]
    # The originally dispatched worker can arrive late; no replacement is created.
    store.run_once(ref, reserve, execute)
    assert calls == ["reserve", "execute"]
    assert "private-token" not in json.dumps(store.observe())


@pytest.mark.parametrize("boundary", ["reservation", "consumption", "completion"])
def test_collector_uncertain_writes_never_reenter_or_cleanup(attempt, monkeypatch, boundary):
    _, store, _, calls, reserve, execute = attempt
    ref = store.prepare()
    store.dispatch_once(ref, lambda _: None)
    original = store._save

    def save(value):
        if (boundary == "consumption" and value["phase"] == "submitting" or
                boundary == "completion" and value["phase"] == "completed"):
            original(value)
            raise OSError("lost write acknowledgement")
        original(value)

    def reserve_lost():
        reserve()
        if boundary == "reservation":
            raise OSError("reservation acknowledgement lost")

    monkeypatch.setattr(store, "_save", save)
    with pytest.raises(OSError):
        store.run_once(ref, reserve_lost, execute)
    before = list(calls)
    with pytest.raises(admission.AdmissionError):
        store.run_once(ref, reserve, execute)
    assert calls == before
    assert calls.count("execute") == (boundary == "completion")
    assert admission.read_receipt(store.root, "collector-request") is not None


def test_collector_stale_generation_and_delayed_fenced_worker_refuse(attempt):
    _, store, _, calls, reserve, execute = attempt
    ref = store.prepare()
    store.dispatch_once(ref, lambda _: None)
    with pytest.raises(admission.AdmissionError):
        store.run_once("0" * 32, reserve, execute)
    store.fence_pending(ref)
    before = store.path.read_bytes()
    with pytest.raises(admission.AdmissionError):
        store.run_once(ref, reserve, execute)
    assert not calls and store.path.read_bytes() == before
    assert store.observe()["phase"] == "fenced"


def test_collector_custody_blocks_mutation_before_effect(attempt):
    _, store, _, calls, reserve, execute = attempt
    ref = store.prepare()
    store.dispatch_once(ref, lambda _: None)
    entered, release = Event(), Event()

    def held():
        entered.set()
        assert release.wait(5)
        return execute()

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(store.run_once, ref, reserve, held)
        assert entered.wait(5)
        try:
            with pytest.raises(admission.AdmissionError):
                with store.mutation_guard(ref):
                    calls.append("delete")
        finally:
            release.set()
        worker.result(timeout=5)
    assert calls == ["reserve", "execute"]
    with store.mutation_guard(ref):
        calls.append("allowed-after-completion")


def test_collector_observer_is_read_only_and_hides_nonce(attempt):
    _, store, _, _, reserve, execute = attempt
    assert store.observe() == {"state": "not_recorded"}
    assert not store.directory.exists()
    ref = store.prepare()
    store.dispatch_once(ref, lambda _: None)
    before = store.path.read_bytes()
    for _ in range(3):
        observed = store.observe()
        assert "result" not in observed
        assert ref not in json.dumps(observed)
        assert "destination_digest" not in observed
    assert store.path.read_bytes() == before
    store.run_once(ref, reserve, execute)
    receipt = admission.read_receipt(store.root, "collector-request")
    receipt["state"] = "removed"
    admission.save_receipt(store.root, receipt)
    assert "result" not in store.observe()
    assert store.observe()["evidence_gap"] is True


def test_collector_failure_records_only_fixed_category_no_false_cancellation(attempt):
    _, store, _, calls, reserve, _ = attempt
    ref = store.prepare()
    store.dispatch_once(ref, lambda _: None)

    def lost():
        calls.append("execute")
        raise TimeoutError("secret/private/path")

    with pytest.raises(TimeoutError):
        store.run_once(ref, reserve, lost)
    assert store.observe()["phase"] == "unknown"
    assert "secret" not in store.path.read_text()
    with pytest.raises(admission.AdmissionError):
        store.fence_pending(ref)
    with pytest.raises(admission.AdmissionError):
        with store.mutation_guard(ref):
            calls.append("delete")
    assert calls == ["reserve", "execute"]


@pytest.mark.parametrize("damage", ["version", "extra", "symlink", "permissions"])
def test_collector_private_records_fail_closed(attempt, tmp_path, damage):
    _, store, _, _, _, _ = attempt
    store.prepare()
    if damage == "symlink":
        target = tmp_path / "foreign"
        target.write_bytes(store.path.read_bytes())
        store.path.unlink()
        store.path.symlink_to(target)
    elif damage == "permissions":
        store.path.chmod(0o644)
    else:
        record = json.loads(store.path.read_text())
        if damage == "version":
            record["version"] = 999
        else:
            record["raw_environment"] = "secret"
        store.path.write_text(json.dumps(record))
    with pytest.raises((admission.AdmissionError, OSError)):
        store.observe()


def test_collector_duplicate_phase_keys_never_enable_worker(attempt):
    _, store, _, calls, reserve, execute = attempt
    ref = store.prepare()
    store.path.write_text(store.path.read_text().replace(
        '"phase": "prepared"', '"phase": "fenced", "phase": "dispatched"'
    ))
    before = store.path.read_bytes()
    with pytest.raises(admission.AdmissionError):
        store.run_once(ref, reserve, execute)
    assert not calls and store.path.read_bytes() == before


def test_collector_result_json_identity_not_python_numeric_equality(attempt):
    _, store, _, _, reserve, execute = attempt
    ref = store.prepare()
    store.dispatch_once(ref, lambda _: None)

    def wrong():
        result = execute()
        receipt = admission.read_receipt(store.root, "collector-request")
        receipt["result"]["extra"] = 1
        admission.save_receipt(store.root, receipt)
        return {**result, "extra": True}

    with pytest.raises(admission.AdmissionError):
        store.run_once(ref, reserve, wrong)
    assert store.observe()["phase"] == "submitting"
    assert "result" not in store.observe()


def test_collector_parent_directory_sync_precedes_dispatch(attempt, monkeypatch):
    import os
    _, store, _, _, _, _ = attempt
    ref = store.prepare()
    dispatched = []
    sync = admission.os.fsync

    def fail_root(fd):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (store.root.stat().st_dev, store.root.stat().st_ino):
            raise OSError("parent directory sync refused")
        sync(fd)

    monkeypatch.setattr(admission.os, "fsync", fail_root)
    with pytest.raises(OSError):
        store.dispatch_once(ref, dispatched.append)
    assert not dispatched and store.observe()["phase"] == "prepared"


@pytest.mark.parametrize("phase", ["dispatched", "accepted", "submitting"])
@pytest.mark.parametrize("sync_number", [1, 2])
def test_collector_real_fsync_failure_prevents_next_callback(attempt, monkeypatch, phase, sync_number):
    _, store, _, calls, reserve, execute = attempt
    ref = store.prepare()
    if phase != "dispatched":
        store.dispatch_once(ref, lambda _: None)
    save = store._save

    def fail(value):
        if value["phase"] != phase:
            return save(value)
        fsync = admission.os.fsync
        count = []

        def sync(fd):
            count.append(fd)
            if len(count) == sync_number:
                raise OSError("synthetic fsync failure")
            fsync(fd)

        with monkeypatch.context() as patch:
            patch.setattr(admission.os, "fsync", sync)
            save(value)

    monkeypatch.setattr(store, "_save", fail)
    with pytest.raises(OSError):
        if phase == "dispatched":
            store.dispatch_once(ref, lambda _: calls.append("dispatch"))
        else:
            store.run_once(ref, reserve, execute)
    assert "execute" not in calls and "dispatch" not in calls
    assert calls == (["reserve"] if phase == "submitting" else [])
    expected = phase if sync_number == 2 else {
        "dispatched": "prepared", "accepted": "dispatched", "submitting": "accepted",
    }[phase]
    assert store.observe()["phase"] == expected


def test_collector_lock_scope_and_competing_worker(attempt):
    _, store, _, calls, reserve, execute = attempt
    ref = store.prepare()

    def dispatch(_):
        with admission._locked_file(store.lock_path, timeout=0):
            with admission._locked_file(store.root / "admission.lock", timeout=0):
                calls.append("dispatch-unlocked")

    store.dispatch_once(ref, dispatch)

    def held_reserve():
        with pytest.raises(admission.AdmissionError):
            with admission._locked_file(store.root / "admission.lock", timeout=0):
                pytest.fail("reservation did not hold admission")
        reserve()

    def held_execute():
        with admission._locked_file(store.root / "admission.lock", timeout=0):
            calls.append("execution-admission-unlocked")
        with pytest.raises(admission.AdmissionError):
            store.run_once(ref, reserve, execute)
        return execute()

    store.run_once(ref, held_reserve, held_execute)
    assert calls == ["dispatch-unlocked", "reserve", "execution-admission-unlocked", "execute"]
