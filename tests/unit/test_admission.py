# Copyright (c) Microsoft. All rights reserved.

"""Admission regressions: retained storage, races, lost replies and safe retries."""

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from amplifier_bundle_digital_twin_universe import admission, engine, incus
from amplifier_bundle_digital_twin_universe.cli import main


def instances(running=0, stopped=0):
    return [{"name": f"active-{i}", "status": "Running"} for i in range(running)] + [
        {"name": f"retained-{i}", "status": "Stopped"} for i in range(stopped)
    ]


def test_stopped_instances_do_not_use_execution_slots(monkeypatch, admission_infra):
    monkeypatch.setattr(incus, "list_instances", lambda *args: instances(4, 11))
    result = engine.capacity()
    assert result["available"]
    assert result["counts"] == {
        "active": 4,
        "stopped_retained": 11,
        "pending_unobserved": 0,
        "total": 15,
    }


def test_explicit_legacy_cap_retains_total_semantics(monkeypatch, admission_infra):
    monkeypatch.setattr(incus, "list_instances", lambda *args: instances(4, 11))
    monkeypatch.setenv("AMPLIFIER_DTU_MAX_INSTANCES", "15")
    assert engine.capacity()["blockers"] == [
        {"code": "retained_capacity", "current": 15, "limit": 15}
    ]
    assert engine.capacity(max_instances=0)["available"]


def test_disabling_total_cap_does_not_disable_execution_cap(
    monkeypatch, admission_infra
):
    monkeypatch.setattr(incus, "list_instances", lambda *args: instances(15))
    assert (
        engine.capacity(max_instances=0)["blockers"][0]["code"] == "execution_capacity"
    )


def test_default_retained_backstop(monkeypatch, admission_infra):
    monkeypatch.setattr(incus, "list_instances", lambda *args: instances(stopped=256))
    assert engine.capacity()["blockers"][0]["code"] == "retained_capacity"


@pytest.mark.parametrize(
    "status", ["Starting", "Stopping", "Frozen", "Error", "UNKNOWN"]
)
def test_non_stopped_states_count_conservatively(monkeypatch, admission_infra, status):
    monkeypatch.setattr(
        incus, "list_instances", lambda *args: [{"name": "one", "status": status}]
    )
    assert engine.capacity(max_running=1)["blockers"][0]["code"] == "execution_capacity"


def test_provisioning_stopped_instance_still_counts(monkeypatch, admission_infra):
    monkeypatch.setattr(
        incus,
        "list_instances",
        lambda *args: [
            {
                "name": "one",
                "status": "Stopped",
                "config": {"user.dtu.launch-state": "provisioning"},
            }
        ],
    )
    assert engine.capacity(max_running=1)["blockers"][0]["code"] == "execution_capacity"


@pytest.mark.parametrize("unknown", [True, False])
def test_disk_guard_does_not_use_the_callers_filesystem(
    monkeypatch, admission_infra, unknown
):
    monkeypatch.setattr(incus, "list_instances", lambda *args: [])
    original = incus.query

    def query(path):
        if path.endswith("/resources"):
            if unknown:
                raise incus.IncusError("storage unavailable")
            return {"space": {"total": 100, "used": 90}}
        return original(path)

    monkeypatch.setattr(incus, "query", query)
    result = engine.capacity(min_free_bytes=20)
    assert result["blockers"][0]["code"] == (
        "storage_unknown" if unknown else "storage_pressure"
    )
    assert result["storage"]["pool"] == "pool"


@pytest.mark.parametrize("value", [-1, "oops", True, 1.5])
def test_invalid_policy_is_rejected(value):
    with pytest.raises(ValueError):
        admission.policy(max_running=value)


def test_daemon_and_project_determine_lock_identity(admission_infra):
    a = admission.state_directory({"daemon": "same", "project": "a"})
    assert a == admission.state_directory({"daemon": "same", "project": "a"})
    assert a != admission.state_directory({"daemon": "same", "project": "b"})
    assert a != admission.state_directory({"daemon": "other", "project": "a"})


@pytest.fixture
def launch_infra(monkeypatch, tmp_path, admission_infra):
    profile = tmp_path / "minimal.yaml"
    profile.write_text(
        "name: minimal\ndescription: Admission test\nbase:\n  image: ubuntu:24.04\n"
    )
    observed = []
    monkeypatch.setattr(incus, "check_incus", lambda: None)
    monkeypatch.setattr(incus, "list_instances", lambda *args: list(observed))
    monkeypatch.setattr(
        incus, "container_exists", lambda name: any(i["name"] == name for i in observed)
    )

    def create(name, image, config):
        time.sleep(0.04)
        observed.append({"name": name, "status": "Running", "config": dict(config)})

    create_mock = MagicMock(side_effect=create)
    monkeypatch.setattr(incus, "create_container", create_mock)

    def set_config(name, key, value):
        next(i for i in observed if i["name"] == name)["config"][key] = value

    monkeypatch.setattr(incus, "set_config", set_config)
    monkeypatch.setattr(
        incus,
        "get_config",
        lambda name, key: next(i for i in observed if i["name"] == name)["config"].get(
            key, ""
        ),
    )
    monkeypatch.setattr(incus, "file_push", lambda *a, **k: None)
    monkeypatch.setattr(incus, "exec_command", lambda *a, **k: (0, "", ""))
    monkeypatch.setattr(engine, "_wait_for_gateway", lambda *a: "10.1.0.1")
    monkeypatch.setattr(engine, "_write_env", lambda *a: None)
    monkeypatch.setattr(engine, "_write_visual_id_profile_d", lambda *a: None)
    from amplifier_bundle_digital_twin_universe.hostname import HostnameManager

    monkeypatch.setattr(HostnameManager, "register", lambda *a: None)
    return str(profile), observed, create_mock


def test_concurrent_launches_cannot_take_same_last_slot(launch_infra):
    profile, observed, create = launch_infra

    def launch(index):
        try:
            return engine.launch(profile, {}, name=f"candidate-{index}", max_running=1)
        except admission.AdmissionError as exc:
            return exc.as_dict()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(launch, [1, 2]))
    assert len(observed) == 1
    assert create.call_count == 1
    assert sum(r.get("error") == "execution_capacity" for r in results) == 1


def test_stable_retry_returns_same_result_without_provisioning(launch_infra):
    profile, observed, create = launch_infra
    args = {"name": "stable", "request_id": "request-1", "owner_root": "parent-1"}
    first = engine.launch(profile, {}, **args)
    retry = engine.launch(profile, {}, **args)
    assert retry == {**first, "reused": True}
    assert create.call_count == 1
    assert observed[0]["config"]["user.dtu.owner-root"] == "parent-1"
    assert engine.launch_status("request-1")["state"] == "ready"
    with pytest.raises(admission.AdmissionError, match="different launch"):
        engine.launch(profile, {"changed": "true"}, **args)
    assert create.call_count == 1


def test_completed_request_is_not_recreated_after_removal(launch_infra):
    profile, observed, create = launch_infra
    engine.launch(profile, {}, request_id="request-1")
    observed.clear()
    assert engine.launch_status("request-1")["state"] == "removed"
    with pytest.raises(admission.AdmissionError) as raised:
        engine.launch(profile, {}, request_id="request-1")
    assert raised.value.code == "launch_removed"
    assert create.call_count == 1


def test_stopped_retry_does_not_restart(launch_infra):
    profile, observed, create = launch_infra
    engine.launch(profile, {}, request_id="request-1")
    observed[0]["status"] = "Stopped"
    assert engine.launch_status("request-1")["state"] == "stopped"
    with pytest.raises(admission.AdmissionError) as raised:
        engine.launch(profile, {}, request_id="request-1")
    assert raised.value.code == "launch_stopped"
    assert create.call_count == 1


def test_timeout_is_unknown_reserved_and_never_replayed(monkeypatch, launch_infra):
    profile, _observed, create = launch_infra
    create.side_effect = subprocess.TimeoutExpired(["incus", "launch"], 120)
    with pytest.raises(admission.AdmissionError) as raised:
        engine.launch(profile, {}, name="unknown", request_id="request-1")
    assert raised.value.code == "launch_unknown"
    assert engine.launch_status("request-1")["state"] == "unknown"
    snapshot = engine.capacity(max_running=1)
    assert snapshot["counts"]["pending_unobserved"] == 1
    assert snapshot["blockers"][0]["code"] == "execution_capacity"
    with pytest.raises(admission.AdmissionError):
        engine.launch(profile, {}, name="unknown", request_id="request-1")
    assert create.call_count == 1


def test_late_arrival_is_counted_once_and_observed_without_replay(launch_infra):
    profile, observed, create = launch_infra
    create.side_effect = subprocess.TimeoutExpired(["incus", "launch"], 120)
    with pytest.raises(admission.AdmissionError):
        engine.launch(profile, {}, name="late", request_id="r", owner_root="o")
    observed.append(
        {
            "name": "late",
            "status": "Running",
            "config": {
                "user.dtu.request-id": "r",
                "user.dtu.owner-root": "o",
                "user.dtu.launch-state": "provisioning",
            },
        }
    )
    counts = engine.capacity()["counts"]
    assert counts["active"] == 1 and counts["pending_unobserved"] == 0
    assert engine.launch_status("r")["instance_status"] == "Running"
    assert create.call_count == 1


def test_name_conflict_preserves_existing_instance(launch_infra):
    profile, observed, create = launch_infra
    observed.append({"name": "occupied", "status": "Stopped"})
    with pytest.raises(admission.AdmissionError) as raised:
        engine.launch(profile, {}, name="occupied")
    assert raised.value.code == "name_conflict"
    create.assert_not_called()


def test_cli_capacity_and_error_are_machine_readable(monkeypatch, admission_infra):
    monkeypatch.setattr(incus, "list_instances", lambda *args: instances(15))
    result = CliRunner().invoke(main, ["capacity"])
    assert result.exit_code == 0
    assert json.loads(result.output)["blockers"][0]["code"] == "execution_capacity"
    monkeypatch.setattr(
        engine,
        "launch",
        MagicMock(
            side_effect=admission.AdmissionError(
                "launch_unknown", "reconcile", {"id": "a"}
            )
        ),
    )
    result = CliRunner().invoke(main, ["launch", "minimal", "--request-id", "r"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "launch_unknown"


def test_project_selection_is_explicit(monkeypatch):
    run = MagicMock(
        return_value=subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
    )
    monkeypatch.setattr(incus.subprocess, "run", run)
    monkeypatch.setenv("AMPLIFIER_DTU_INCUS_PROJECT", "owned-project")
    incus.query("/1.0")
    assert run.call_args.args[0] == ["incus", "query", "/1.0?project=owned-project"]
    incus.list_instances("key", "value")
    assert run.call_args.args[0][:3] == ["incus", "--project", "owned-project"]


def test_destroy_checks_owner_and_retires_receipt(monkeypatch, launch_infra):
    profile, observed, _create = launch_infra
    engine.launch(profile, {}, name="owned", request_id="r", owner_root="o")
    deleted = MagicMock(side_effect=lambda *a, **k: observed.clear())
    monkeypatch.setattr(incus, "delete_container", deleted)
    monkeypatch.setattr(incus, "stop_container", lambda *a: None)
    monkeypatch.setattr(engine, "_stop_mock_containers", lambda *a: None)
    with pytest.raises(admission.AdmissionError) as raised:
        engine.destroy("owned", owner_root="wrong", request_id="r")
    assert raised.value.code == "ownership_conflict"
    deleted.assert_not_called()
    assert engine.destroy("owned", owner_root="o", request_id="r")["destroyed"]
    assert engine.launch_status("r")["state"] == "removed"
    assert engine.capacity()["counts"]["pending_unobserved"] == 0


def test_offline_launch_omits_inherited_profiles_and_nics(monkeypatch, launch_infra):
    profile, observed, create = launch_infra
    create.side_effect = lambda name, image, config, isolated_pool: observed.append(
        {"name": name, "status": "Running", "config": dict(config)}
    )
    gateway = MagicMock(side_effect=AssertionError("offline must not wait for gateway"))
    monkeypatch.setattr(engine, "_wait_for_gateway", gateway)
    result = engine.launch(
        profile, {}, name="offline", request_id="offline-r", offline=True
    )
    assert result["offline"] is True
    assert create.call_args.kwargs["isolated_pool"] == "pool"
    assert observed[0]["config"]["user.dtu.offline"] == "true"
    gateway.assert_not_called()


def test_isolated_container_cli_has_no_inherited_profile(monkeypatch):
    run = MagicMock(
        return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    monkeypatch.setattr(incus.subprocess, "run", run)
    incus.create_container("owned", "local:image", isolated_pool="pool")
    assert run.call_args.args[0] == [
        "incus",
        "launch",
        "local:image",
        "owned",
        "--no-profiles",
        "--storage",
        "pool",
    ]


@pytest.mark.parametrize("kind", ["symlink", "public"])
def test_admission_root_rejects_unsafe_directories(tmp_path, monkeypatch, kind):
    base = tmp_path / "admission"
    if kind == "symlink":
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        base.symlink_to(target, target_is_directory=True)
    else:
        base.mkdir(mode=0o755)
    monkeypatch.setenv("AMPLIFIER_DTU_ADMISSION_DIR", str(base))
    with pytest.raises((admission.AdmissionError, OSError)):
        admission.state_directory({"daemon": "d", "project": "p"})


@pytest.mark.parametrize("kind", ["symlink", "public", "hardlink", "oversize"])
def test_receipts_are_private_regular_bounded_files(tmp_path, admission_infra, kind):
    ctx = admission.context()
    root = admission.state_directory(ctx)
    path = admission.receipt_path(root, "r")
    target = tmp_path / "target"
    target.write_text('{"request_id":"r"}')
    target.chmod(0o600)
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        import os

        os.link(target, path)
    else:
        path.write_text("x" * 262145 if kind == "oversize" else '{"request_id":"r"}')
        path.chmod(0o644 if kind == "public" else 0o600)
    with pytest.raises((admission.AdmissionError, OSError)):
        admission.read_receipt(root, "r")
    assert target.read_text() == '{"request_id":"r"}'


def test_lock_symlink_is_not_followed(tmp_path, admission_infra):
    ctx = admission.context()
    root = admission.state_directory(ctx)
    target = tmp_path / "victim"
    target.write_text("unchanged")
    (root / "admission.lock").symlink_to(target)
    with pytest.raises(OSError), admission.locked(ctx):
        pass
    assert target.read_text() == "unchanged"


def test_receipt_write_does_not_follow_old_predictable_temp_name(
    tmp_path, admission_infra
):
    root = admission.state_directory(admission.context())
    target = tmp_path / "victim"
    target.write_text("unchanged")
    admission.receipt_path(root, "r").with_suffix(".tmp").symlink_to(target)
    admission.save_receipt(root, {"request_id": "r", "state": "unknown"})
    assert admission.read_receipt(root, "r")["state"] == "unknown"
    assert target.read_text() == "unchanged"


@pytest.mark.parametrize(
    "key,value",
    [("user.dtu.owner-root", "different"), ("user.dtu.launch-state", "provisioning")],
)
def test_ready_retry_rechecks_owner_and_ready_marker(launch_infra, key, value):
    profile, observed, create = launch_infra
    engine.launch(profile, {}, request_id="r", owner_root="o")
    observed[0]["config"][key] = value
    with pytest.raises(admission.AdmissionError) as raised:
        engine.launch(profile, {}, request_id="r", owner_root="o")
    assert raised.value.code == "ownership_conflict"
    assert create.call_count == 1


def _exact_offline_profile(profile):
    from pathlib import Path
    Path(profile).write_text('name: pinned\nbase:\n  image: local:' + 'a' * 64 + '\n')


def test_missing_exact_image_never_creates_or_reserves(monkeypatch, launch_infra):
    profile, observed, create = launch_infra
    _exact_offline_profile(profile)
    monkeypatch.setattr(incus, 'local_image_available', lambda fingerprint: False)
    with pytest.raises(admission.AdmissionError) as error:
        engine.launch(profile, {}, name='missing-image', request_id='missing-image', offline=True)
    assert error.value.code == 'approved_image_missing'
    assert error.value.details['admitted'] is False
    create.assert_not_called()
    assert engine.launch_status('missing-image')['state'] == 'not_recorded'
    assert engine.capacity()['counts']['active'] == 0
    assert engine.capacity()['counts']['pending_unobserved'] == 0


def test_unobservable_image_never_creates_or_reserves(monkeypatch, launch_infra):
    profile, _, create = launch_infra
    _exact_offline_profile(profile)
    monkeypatch.setattr(incus, 'local_image_available', MagicMock(side_effect=incus.IncusError('private path /secret')))
    with pytest.raises(admission.AdmissionError) as error:
        engine.launch(profile, {}, name='unobserved-image', request_id='unobserved-image', offline=True)
    assert error.value.code == 'approved_image_unavailable'
    assert '/secret' not in json.dumps(error.value.as_dict())
    create.assert_not_called()
    assert engine.launch_status('unobserved-image')['state'] == 'not_recorded'


def test_present_exact_image_launch_and_ready_retry_are_unchanged(monkeypatch, launch_infra):
    profile, observed, create = launch_infra
    _exact_offline_profile(profile)
    present = MagicMock(return_value=True)
    monkeypatch.setattr(incus, 'local_image_available', present)
    create.side_effect = lambda name, image, config, isolated_pool: observed.append(
        {'name': name, 'status': 'Running', 'config': dict(config)})
    result = engine.launch(profile, {}, name='pinned', request_id='pinned', offline=True)
    assert result['id'] == 'pinned' and create.call_count == 1
    present.side_effect = AssertionError('ready reconciliation must not depend on cached image')
    assert engine.launch(profile, {}, name='pinned', request_id='pinned', offline=True)['reused']
    assert create.call_count == 1


def test_creation_failure_retains_sanitized_cause_and_unknown_reservation(monkeypatch, launch_infra):
    profile, _, create = launch_infra
    create.side_effect = incus.IncusError('Image not found; sensitive path /secret and token=private')
    with pytest.raises(admission.AdmissionError) as error:
        engine.launch(profile, {}, name='race', request_id='race')
    assert error.value.code == 'launch_unknown'
    receipt = engine.launch_status('race')
    assert receipt['state'] == 'unknown'
    assert receipt['failure_cause'] == {'stage': 'create', 'code': 'image_not_found'}
    assert 'secret' not in json.dumps(receipt) and 'private' not in json.dumps(error.value.as_dict())
    assert engine.capacity()['counts']['pending_unobserved'] == 1


@pytest.mark.parametrize('present', [True, False])
def test_exact_local_image_observation_is_read_only(monkeypatch, present):
    fingerprint = 'a' * 64
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, json.dumps([{'fingerprint':fingerprint}] if present else []), ''))
    monkeypatch.setattr(incus, '_run', run)
    assert incus.local_image_available(fingerprint) is present
    assert run.call_args.args[0] == ['incus', 'image', 'list', 'local:', fingerprint, '--format=json']


def test_prior_unknown_launch_is_not_reclassified_when_image_disappears(monkeypatch, launch_infra):
    profile, _, create = launch_infra
    _exact_offline_profile(profile)
    present = MagicMock(return_value=True)
    monkeypatch.setattr(incus, 'local_image_available', present)
    create.side_effect = subprocess.TimeoutExpired('incus', 30)
    with pytest.raises(admission.AdmissionError):
        engine.launch(profile, {}, name='uncertain-pinned', request_id='uncertain-pinned', offline=True)
    present.side_effect = AssertionError('An uncertain request must reconcile before any new admission')
    with pytest.raises(admission.AdmissionError) as error:
        engine.launch(profile, {}, name='uncertain-pinned', request_id='uncertain-pinned', offline=True)
    assert error.value.code == 'launch_unknown' and create.call_count == 1
    assert engine.launch_status('uncertain-pinned')['state'] == 'unknown'


# DTU-001 preparation: exercise the existing public boundary before adding any
# settlement API. These tests are not a substitute for real Incus fault tests.
@pytest.fixture
def recovery_infra(monkeypatch, launch_infra):
    """Keep every new recovery case off a real daemon, even if mocks drift."""
    def unexpected_command(*args, **kwargs):
        raise AssertionError("Recovery unit case attempted an external command")

    monkeypatch.setattr(incus, "_run", unexpected_command)
    return launch_infra


def _record_uncertain_attempt(recovery_infra, *, state="unknown"):
    profile, _observed, create = recovery_infra
    create.side_effect = subprocess.TimeoutExpired(["incus", "launch"], 120)
    with pytest.raises(admission.AdmissionError, match="confirmed outcome"):
        engine.launch(
            profile, {}, name="recovery-case", request_id="recovery-request",
            owner_root="recovery-owner",
        )
    with admission.locked(admission.context()) as root:
        receipt = admission.read_receipt(root, "recovery-request")
        receipt["state"] = state
        # Synthetic old date: age must never be a cancellation certificate.
        receipt["created_at"] = "2000-01-01T00:00:00Z"
        admission.save_receipt(root, receipt)
        path = admission.receipt_path(root, "recovery-request")
    return path, path.read_bytes()


@pytest.mark.parametrize("state", ["launching", "unknown"])
def test_recovery_observation_preserves_legacy_bytes_and_reservation(
    recovery_infra, state
):
    profile, _observed, create = recovery_infra
    path, original = _record_uncertain_attempt(recovery_infra, state=state)
    for _ in range(3):
        assert engine.launch_status("recovery-request")["state"] == "unknown"
        assert engine.capacity(max_running=1)["counts"]["pending_unobserved"] == 1
        assert path.read_bytes() == original
    with pytest.raises(admission.AdmissionError):
        engine.launch(
            profile, {}, name="recovery-case", request_id="recovery-request",
            owner_root="recovery-owner",
        )
    assert create.call_count == 1
    assert path.read_bytes() == original


@pytest.mark.parametrize("change", [
    {"owner_root": "other-owner"},
    {"name": "other-name"},
    {"variables": {"changed": "intent"}},
])
def test_recovery_retry_conflicts_do_not_rewrite_or_recreate(recovery_infra, change):
    profile, _observed, create = recovery_infra
    path, original = _record_uncertain_attempt(recovery_infra)
    args = {
        "profile_arg": profile, "variables": {}, "name": "recovery-case",
        "request_id": "recovery-request", "owner_root": "recovery-owner",
        **change,
    }
    with pytest.raises(admission.AdmissionError) as error:
        engine.launch(**args)
    assert error.value.code == "request_conflict"
    assert create.call_count == 1
    assert path.read_bytes() == original


@pytest.mark.parametrize("partial", [
    '{"operation": "/1.0/operations/incomplete',
    b'{"operation":"/1.0/operations/00000000-0000-4000-8000-000000000000"}',
])
def test_recovery_partial_timeout_output_is_not_terminal_evidence(
    recovery_infra, partial
):
    profile, _observed, create = recovery_infra
    create.side_effect = subprocess.TimeoutExpired(
        ["incus", "launch"], 120, output=partial,
        stderr=b"synthetic token=DO_NOT_PERSIST /private/example",
    )
    with pytest.raises(admission.AdmissionError):
        engine.launch(
            profile, {}, name="partial", request_id="partial", owner_root="owner"
        )
    status = engine.launch_status("partial")
    assert status["state"] == "unknown"
    assert status["failure_cause"] == {"stage": "create", "code": "timeout"}
    with admission.locked(admission.context()) as root:
        raw = admission.receipt_path(root, "partial").read_text()
    assert "DO_NOT_PERSIST" not in raw and "/private/example" not in raw
    assert engine.capacity()["counts"]["pending_unobserved"] == 1
    assert create.call_count == 1


@pytest.mark.parametrize("instance_status", ["Running", "Stopped"])
@pytest.mark.parametrize("ownership", ["matching", "wrong-owner", "wrong-request"])
def test_recovery_residual_identity_controls_pending_accounting(
    recovery_infra, instance_status, ownership
):
    _profile, observed, create = recovery_infra
    path, original = _record_uncertain_attempt(recovery_infra)
    observed.append({
        "name": "recovery-case",
        "status": instance_status,
        "config": {
            "user.dtu.managed-by": admission.MANAGED_VALUE,
            "user.dtu.owner-root": (
                "other" if ownership == "wrong-owner" else "recovery-owner"
            ),
            "user.dtu.request-id": (
                "other" if ownership == "wrong-request" else "recovery-request"
            ),
            "user.dtu.launch-state": "provisioning",
        },
    })
    status = engine.launch_status("recovery-request")
    counts = engine.capacity()["counts"]
    matched = ownership == "matching"
    assert status["state"] == ("unknown" if matched else "conflict")
    # An unrelated same-name instance cannot stand in for the unresolved
    # attempt. Count the observed resource AND the unmatched reservation.
    assert counts["pending_unobserved"] == (0 if matched else 1)
    assert counts["active"] == (1 if matched else 2)
    assert counts["total"] == (1 if matched else 2)
    assert path.read_bytes() == original
    assert create.call_count == 1


def test_recovery_concurrent_reads_keep_unknown_history(recovery_infra):
    _profile, _observed, create = recovery_infra
    path, original = _record_uncertain_attempt(recovery_infra)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda _: engine.launch_status("recovery-request"), range(4)
        ))
    assert all(result["state"] == "unknown" for result in results)
    assert path.read_bytes() == original
    assert create.call_count == 1


@pytest.mark.parametrize("state", ["unknown", "failed", "removed"])
def test_recovery_future_receipt_schema_fails_closed(recovery_infra, state):
    path, _original = _record_uncertain_attempt(recovery_infra, state=state)
    with admission.locked(admission.context()) as root:
        receipt = admission.read_receipt(root, "recovery-request")
        receipt["schema_version"] = 999
        admission.save_receipt(root, receipt)
    original = path.read_bytes()
    # Unsupported accounting semantics must not silently free an unknown
    # reservation. Legacy receipts WITHOUT schema_version remain readable.
    with pytest.raises(admission.AdmissionError):
        engine.capacity()
    assert path.read_bytes() == original
