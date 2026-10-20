"""Creation receipts survive lost waits without guessing or replaying effects."""
import http.client
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from amplifier_bundle_digital_twin_universe import admission, engine, incus, operations
from .test_admission import launch_infra  # noqa: F401


OP_ID = "12345678-1234-1234-1234-123456789abc"
ENDPOINT = {"daemon": "test-daemon", "project": "default", "socket": "/fixture/incus.sock"}


@pytest.fixture
def tracked(monkeypatch, launch_infra):  # noqa: F811 - imported pytest fixture registration
    profile, observed, legacy = launch_infra
    Path(profile).write_text("name: minimal\nbase:\n  image: local:" + "a" * 64 + "\n")
    monkeypatch.setattr(incus, "local_image_available", lambda _: True)
    monkeypatch.setattr(operations, "local_endpoint", lambda _: dict(ENDPOINT))
    monkeypatch.setattr(incus, "_get_launch_timeout_seconds", lambda: 0)
    def submit(endpoint, name, image, config, pool):
        return {**endpoint, "id": OP_ID, "name": name, "request_id": config["user.dtu.request-id"],
                "owner_root": config["user.dtu.owner-root"], "status_code": 103}
    submitted = MagicMock(side_effect=submit)
    observe = MagicMock(return_value={"id": OP_ID, "status_code": 103})
    monkeypatch.setattr(operations, "submit", submitted)
    monkeypatch.setattr(operations, "observe", observe)
    return profile, observed, legacy, submitted, observe


def start(tracked):
    return engine.launch(tracked[0], {}, name="tracked", request_id="request", owner_root="owner", offline=True)


def receipt():
    ctx = admission.context()
    with admission.locked(ctx) as root:
        return admission.read_receipt(root, "request")


def test_lost_wait_retains_identity_and_later_terminal_failure_releases_capacity(tracked):
    _, _, legacy, submit, observe = tracked
    with pytest.raises(admission.AdmissionError) as caught:
        start(tracked)
    assert caught.value.code == "launch_unknown"
    saved = receipt()
    assert saved["create_operation"]["id"] == OP_ID and saved["state"] == "unknown"
    assert engine.capacity()["counts"]["pending_unobserved"] == 1
    observe.return_value = {"id": OP_ID, "status_code": 400}
    assert engine.capacity()["counts"]["pending_unobserved"] == 0
    assert engine.launch_status("request")["state"] == "failed"
    with pytest.raises(admission.AdmissionError) as caught:
        start(tracked)
    assert caught.value.code == "launch_failed" and submit.call_count == 1
    legacy.assert_not_called()


def test_confirmed_creation_provisions_once_and_ready_retry_is_passive(tracked):
    original = tracked[3].side_effect
    def create(endpoint, name, image, config, pool):
        result = original(endpoint, name, image, config, pool)
        tracked[1].append({"name": name, "status": "Running", "config": dict(config)})
        return result
    tracked[3].side_effect = create
    tracked[4].return_value = {"id": OP_ID, "status_code": 200}
    result = start(tracked)
    assert result["status"] == "running"
    assert engine.launch_status("request")["state"] == "ready"
    assert start(tracked)["reused"]
    assert tracked[3].call_count == 1


def test_failed_create_with_remaining_instance_keeps_real_capacity(tracked):
    with pytest.raises(admission.AdmissionError):
        start(tracked)
    tracked[1].append({"name": "tracked", "status": "Stopped",
                      "config": {"user.dtu.launch-state": "provisioning"}})
    tracked[4].return_value = {"id": OP_ID, "status_code": 400}
    assert engine.capacity()["counts"]["active"] == 1
    assert receipt()["state"] == "failed"


def test_parallel_tracked_submissions_cannot_overbook_last_slot(tracked):
    def create(number):
        try:
            engine.launch(tracked[0], {}, name=f"tracked-{number}", request_id=f"r-{number}",
                          owner_root="owner", offline=True, max_running=1)
        except admission.AdmissionError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, [1, 2]))
    assert sorted(results) == ["execution_capacity", "launch_unknown"]
    assert tracked[3].call_count == 1


@pytest.mark.parametrize("error", [TimeoutError(), http.client.IncompleteRead(b"")])
def test_lost_submission_never_retries_or_guesses_failure(tracked, error):
    tracked[3].side_effect = error
    with pytest.raises(admission.AdmissionError):
        start(tracked)
    assert receipt()["state"] == "unknown" and "create_operation" not in receipt()
    assert engine.capacity()["counts"]["pending_unobserved"] == 1
    tracked[4].assert_not_called()
    with pytest.raises(admission.AdmissionError):
        start(tracked)
    assert tracked[3].call_count == 1


@pytest.mark.parametrize("error", [operations.CreationRejected("refused")])
def test_definitive_pre_admission_rejection_releases_reservation(tracked, error):
    tracked[3].side_effect = error
    with pytest.raises(admission.AdmissionError) as caught:
        start(tracked)
    assert caught.value.code == "launch_failed"
    assert receipt()["state"] == "failed"
    assert engine.capacity()["counts"]["pending_unobserved"] == 0


def test_endpoint_preflight_failure_admits_nothing(tracked, monkeypatch):
    monkeypatch.setattr(operations, "local_endpoint", MagicMock(side_effect=OSError("offline")))
    with pytest.raises(OSError):
        start(tracked)
    assert receipt() is None
    tracked[3].assert_not_called()


@pytest.mark.parametrize("exists", [False, True])
def test_terminal_creation_success_is_not_provisioning_success(tracked, exists):
    with pytest.raises(admission.AdmissionError):
        start(tracked)
    if exists:
        tracked[1].append({"name": "tracked", "status": "Running", "config": {
            "user.dtu.request-id": "request", "user.dtu.owner-root": "owner",
            "user.dtu.launch-state": "provisioning"}})
    tracked[4].return_value = {"id": OP_ID, "status_code": 200}
    result = engine.launch_status("request")
    assert result["state"] == ("unknown" if exists else "removed")
    assert engine.capacity()["counts"]["active"] == int(exists)
    assert tracked[3].call_count == 1


def test_missing_operation_after_restart_stays_unknown(tracked):
    with pytest.raises(admission.AdmissionError):
        start(tracked)
    tracked[4].side_effect = operations.OperationUnknown("operation unavailable")
    assert engine.launch_status("request")["state"] == "unknown"
    assert engine.capacity()["counts"]["pending_unobserved"] == 1


def test_wait_does_not_hold_admission_lock(tracked, monkeypatch):
    def observe(operation, ctx, saved, **kwargs):
        # This uses a separate file descriptor, and would fail immediately if
        # the original launch still held the advisory lock across its wait.
        with admission.locked(ctx):
            return {"id": OP_ID, "status_code": 103}
    monkeypatch.setenv("AMPLIFIER_DTU_ADMISSION_TIMEOUT_SECONDS", "0")
    tracked[4].side_effect = observe
    with pytest.raises(admission.AdmissionError):
        start(tracked)
    assert receipt()["create_operation"]["id"] == OP_ID


def test_late_observer_cannot_replace_newer_ready_receipt(tracked):
    with pytest.raises(admission.AdmissionError):
        start(tracked)
    def observe(operation, ctx, saved, **kwargs):
        with admission.locked(ctx) as root:
            current = admission.read_receipt(root, "request")
            current.update(state="ready", result={"proof": "preserved"})
            admission.save_receipt(root, current)
        return {"id": OP_ID, "status_code": 400}
    tracked[4].side_effect = observe
    engine.capacity()
    assert receipt()["state"] == "ready" and receipt()["result"] == {"proof": "preserved"}


@pytest.mark.parametrize("error", [OSError("disk"), PermissionError("disk"), FileNotFoundError("disk")])
def test_operation_receipt_write_failure_keeps_unknown_and_does_not_resubmit(tracked, monkeypatch, error):
    save = admission.save_receipt
    attempts = []
    def save_once(root, value):
        if "create_operation" in value:
            attempts.append(True)
            if len(attempts) == 1:
                raise error
        save(root, value)
    monkeypatch.setattr(admission, "save_receipt", save_once)
    with pytest.raises(admission.AdmissionError):
        start(tracked)
    assert receipt()["create_operation"]["id"] == OP_ID and receipt()["state"] == "unknown"
    assert tracked[3].call_count == 1


def metadata(**changes):
    return {"id": OP_ID, "class": "task", "status_code": 103,
            "resources": {"instances": ["/1.0/instances/tracked"]}, **changes}


@pytest.fixture
def raw_api(monkeypatch):
    api = MagicMock(return_value=(202, {"type": "async", "operation": "/1.0/operations/" + OP_ID,
                                      "metadata": metadata()}))
    monkeypatch.setattr(operations, "_verify_endpoint", lambda _, **kwargs: {})
    monkeypatch.setattr(operations, "_request", api)
    return api


def submit_raw():
    return operations.submit(ENDPOINT, "tracked", "local:" + "a" * 64,
                             {"user.dtu.request-id": "request", "user.dtu.owner-root": "owner"}, "pool")


def test_submit_records_bound_operation_without_default_profile_or_nic(raw_api):
    result = submit_raw()
    assert result == {**ENDPOINT, "id": OP_ID, "status_code": 103, "name": "tracked",
                      "request_id": "request", "owner_root": "owner"}
    data = raw_api.call_args.args[3]
    assert data["start"] and data["profiles"] == []
    assert data["devices"] == {"root": {"type": "disk", "path": "/", "pool": "pool"}}
    assert raw_api.call_count == 1


@pytest.mark.parametrize("changes", [
    {"id": "other"}, {"class": "websocket"}, {"status_code": True},
    {"resources": None}, {"resources": {"instances": ["/1.0/instances/peer"]}},
    {"resources": {"instances": ["/1.0/instances/tracked?project=peer"]}},
    {"resources": {"instances": ["/1.0/instances/tracked", "/1.0/instances/peer"]}},
])
def test_ambiguous_creation_response_is_not_success(raw_api, changes):
    raw_api.return_value[1]["metadata"] = metadata(**changes)
    with pytest.raises(operations.OperationUnknown):
        submit_raw()
    assert raw_api.call_count == 1


@pytest.mark.parametrize("status,rejected", [(400, True), (403, True), (409, True), (500, False), (503, False)])
def test_only_structured_client_rejections_are_definitive(raw_api, status, rejected):
    raw_api.return_value = (status, {"type": "error", "error_code": status})
    with pytest.raises(operations.CreationRejected if rejected else operations.OperationUnknown):
        submit_raw()
    assert raw_api.call_count == 1


@pytest.mark.parametrize("field", ["daemon", "project", "name", "request_id", "owner_root", "id"])
def test_operation_observation_requires_exact_binding(raw_api, field):
    operation = submit_raw()
    operation[field] = "different"
    raw_api.reset_mock()
    with pytest.raises(operations.OperationUnknown):
        operations.observe(operation, ENDPOINT, {"name": "tracked", "request_id": "request", "owner_root": "owner"})
    raw_api.assert_not_called()


def test_http_lost_response_does_not_repeat_post(monkeypatch):
    connection = MagicMock()
    connection.getresponse.side_effect = http.client.RemoteDisconnected("lost")
    monkeypatch.setattr(operations, "_UnixConnection", lambda *args: connection)
    with pytest.raises(http.client.RemoteDisconnected):
        operations._request(ENDPOINT, "POST", "/1.0/instances", {"name": "tracked"})
    assert connection.request.call_count == 1
    assert connection.request.call_args.args[1] == "/1.0/instances?project=default"
    connection.close.assert_called_once()


@pytest.mark.parametrize("error", [FileNotFoundError(), PermissionError(), TimeoutError()])
def test_socket_connect_failure_is_definite_before_any_post(monkeypatch, error):
    connection = MagicMock()
    connection.connect.side_effect = error
    monkeypatch.setattr(operations, "_UnixConnection", lambda *args: connection)
    with pytest.raises(operations.CreationRejected):
        operations._request(ENDPOINT, "POST", "/1.0/instances", {})
    connection.request.assert_not_called()
    connection.close.assert_called_once()


def test_observation_does_not_start_second_rpc_after_budget(monkeypatch, raw_api):
    operation = submit_raw()
    raw_api.reset_mock()
    clock = iter([10, 12])
    monkeypatch.setattr(operations.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError):
        operations.observe(operation, ENDPOINT, {
            "name": "tracked", "request_id": "request", "owner_root": "owner"}, timeout=1)
    raw_api.assert_not_called()


def seed_pending(count):
    ctx = admission.context()
    with admission.locked(ctx) as root:
        for number in range(count):
            saved = {"name": f"tracked-{number}", "request_id": f"request-{number}",
                     "owner_root": "owner", "state": "unknown"}
            saved["create_operation"] = {**ENDPOINT, **saved, "id": OP_ID}
            admission.save_receipt(root, saved)
    return ctx


def test_reconciliation_has_count_bound_and_rotates_even_unavailable_operations(tracked):
    seed_pending(6)
    tracked[4].side_effect = operations.OperationUnknown("expired")
    assert engine.capacity()["counts"]["pending_unobserved"] == 6
    first = {call.args[2]["request_id"] for call in tracked[4].call_args_list}
    assert len(first) == 4
    tracked[4].reset_mock()
    assert engine.capacity()["counts"]["pending_unobserved"] == 6
    second = {call.args[2]["request_id"] for call in tracked[4].call_args_list}
    assert len(second) == 4 and len(first | second) == 6
    tracked[4].reset_mock()
    engine.launch_status("request-5")
    assert tracked[4].call_count == 1
    assert tracked[4].call_args.args[2]["request_id"] == "request-5"


def test_reconciliation_shares_one_time_budget_and_keeps_unobserved_reservations(tracked, monkeypatch):
    ctx = seed_pending(6)
    tick = [10.0]
    monkeypatch.setattr(engine.time, "monotonic", lambda: tick[0])
    def delayed(operation, ctx, saved, *, timeout):
        assert 0 < timeout <= 2
        tick[0] += timeout
        raise TimeoutError()
    tracked[4].side_effect = delayed
    engine._reconcile_creations(ctx)
    assert tracked[4].call_count == 1
    with admission.locked(ctx) as root:
        assert all(admission.read_receipt(root, f"request-{n}")["state"] == "unknown" for n in range(6))


@pytest.mark.parametrize("field", ["certificate_fingerprint", "project"])
def test_changed_endpoint_before_post_is_rejected_without_submission(monkeypatch, field):
    environment = {"certificate_fingerprint": ENDPOINT["daemon"], "project": ENDPOINT["project"]}
    environment[field] = "different"
    api = MagicMock(return_value=(200, {"type": "sync", "metadata": {"environment": environment}}))
    monkeypatch.setattr(operations, "_request", api)
    with pytest.raises(operations.CreationRejected):
        submit_raw()
    assert [call.args[1] for call in api.call_args_list] == ["GET"]


@pytest.mark.parametrize("address", ["https://remote.example:8443", "unix://"])
def test_local_endpoint_keeps_remote_cli_compatibility(monkeypatch, address):
    monkeypatch.setattr(incus, "query", lambda _: {"api_extensions": ["instance_create_start"]})
    monkeypatch.setattr(incus, "_run", MagicMock(side_effect=[
        MagicMock(returncode=0, stdout="selected\n"),
        MagicMock(returncode=0, stdout=json.dumps({"selected": {"Addr": address}})),
    ]))
    verify = MagicMock(return_value={"api_extensions": ["instance_create_start"]})
    monkeypatch.setattr(operations, "_verify_endpoint", verify)
    monkeypatch.setenv("INCUS_SOCKET", "/fixture/incus.sock")
    result = operations.local_endpoint(ENDPOINT)
    if address.startswith("https:"):
        assert result is None
        verify.assert_not_called()
    else:
        assert result == ENDPOINT
        verify.assert_called_once_with(ENDPOINT)


def test_older_daemon_stays_on_legacy_cli_route(monkeypatch):
    monkeypatch.setattr(incus, "query", lambda _: {"api_extensions": []})
    cli = MagicMock()
    monkeypatch.setattr(incus, "_run", cli)
    assert operations.local_endpoint(ENDPOINT) is None
    cli.assert_not_called()
