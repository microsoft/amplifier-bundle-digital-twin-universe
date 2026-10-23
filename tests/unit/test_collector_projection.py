"""Exact future committed publication actions, never lifecycle replay/settlement."""
# ruff: noqa: F811
import json
from unittest.mock import MagicMock

import pytest

from amplifier_bundle_digital_twin_universe import admission, engine, incus
from .test_admission import launch_infra  # noqa: F401
from .test_collected_launch import collected  # noqa: F401
from .test_collector_observations import launch, record, provision_counter


def test_success_retains_two_exact_committed_actions(collected):
    result = launch(collected)
    status = engine.launch_status('observation-request')
    repair = status['publication_repair']
    assert repair['available'] is True
    assert [a['kind'] for a in repair['actions']] == ['publish_ready_result', 'complete_collector']
    assert [a['state'] for a in repair['actions']] == ['after', 'after']
    assert status['result'] == result
    path, _ = record(collected)
    commit = path.with_suffix('.publication')
    before = commit.read_bytes()
    value = json.loads(before)
    assert value['state'] == 'COMMITTED'
    assert engine.launch_status('observation-request')['publication_repair'] == repair
    assert commit.read_bytes() == before


def test_failed_final_publication_has_explicit_local_repair_without_replay(collected, monkeypatch):
    counted = provision_counter(collected, monkeypatch)
    save = admission.save_receipt
    def fail(root, value):
        if value['state'] == 'ready':
            raise OSError('final-publication-fault')
        return save(root, value)
    monkeypatch.setattr(admission, 'save_receipt', fail)
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    monkeypatch.setattr(admission, 'save_receipt', save)
    runtime = collected[0]
    status = engine.launch_status('observation-request')
    repair = status['publication_repair']
    assert [a['state'] for a in repair['actions']] == ['before', 'before']
    assert status['collector_phase'] == 'submitting'
    path, payload = record(collected)
    files = [path, path.with_suffix('.evidence'), path.with_suffix('.outcome'), path.with_suffix('.publication')]
    before = {p: p.read_bytes() for p in files}
    deleted = MagicMock()
    monkeypatch.setattr(incus, 'delete_container', deleted)
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    assert {p: p.read_bytes() for p in files} == before
    first, second = repair['actions']
    with pytest.raises(admission.AdmissionError):
        runtime.repair_record_projection('observation-request', second['action_id'], repair['commit_digest'])
    assert runtime.repair_record_projection('observation-request', first['action_id'], repair['commit_digest'])['outcome'] == 'applied'
    with pytest.raises(admission.AdmissionError):
        engine.exec_command('observation-case', ['true'])
    assert runtime.repair_record_projection('observation-request', second['action_id'], repair['commit_digest'])['outcome'] == 'applied'
    assert runtime.repair_record_projection('observation-request', second['action_id'], repair['commit_digest'])['outcome'] == 'already_applied'
    assert {p: p.read_bytes() for p in files} == before
    assert counted.call_count == 1 and collected[3].call_count == 1 and len(collected[4]) == 1
    deleted.assert_not_called()
    assert engine.launch_status('observation-request')['collector_phase'] == 'completed'


def test_missing_commit_never_grants_repair(collected):
    launch(collected)
    path, _ = record(collected)
    commit = path.with_suffix('.publication')
    if commit.exists():
        commit.unlink()  # Model historical launch without any committed pair.
    before = {p: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
    with pytest.raises(admission.AdmissionError):
        collected[0].repair_record_projection('observation-request', '0' * 64, '0' * 64)
    assert {p: p.read_bytes() for p in before} == before and not commit.exists()
