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


def interrupt_final(data, monkeypatch):
    save = admission.save_receipt
    def fail(root, value):
        if value['state'] == 'ready':
            raise OSError('SECRET-final')
        return save(root, value)
    with monkeypatch.context() as patch:
        patch.setattr(admission, 'save_receipt', fail)
        with pytest.raises(admission.AdmissionError):
            launch(data)
    path, payload = record(data)
    return path, payload, engine.launch_status('observation-request')['publication_repair']


@pytest.mark.parametrize('target', ['admission', 'collector'])
@pytest.mark.parametrize('which', ['before-replace', 'after-replace'])
def test_explicit_repair_write_failure_preserves_commit_and_is_idempotent(collected, monkeypatch, target, which):
    path, payload, status = interrupt_final(collected, monkeypatch)
    runtime = collected[0]
    first, second = status['actions']
    apply = runtime.repair_record_projection
    if target == 'collector':
        apply('observation-request', first['action_id'], status['commit_digest'])
    action = first if target == 'admission' else second
    destination = (admission.receipt_path(path.parent.parent, 'observation-request')
                   if target == 'admission' else path.with_suffix('.json'))
    original = destination.read_bytes()
    commit = path.with_suffix('.publication').read_bytes()
    write = admission._save_private_bytes
    def fail(p, raw):
        if p == destination:
            if which == 'after-replace':
                write(p, raw)
            raise OSError('SECRET-replace')
        return write(p, raw)
    with monkeypatch.context() as patch:
        patch.setattr(admission, '_save_private_bytes', fail)
        with pytest.raises(admission.AdmissionError) as caught:
            apply('observation-request', action['action_id'], status['commit_digest'])
        assert caught.value.code == 'projection_unknown' and 'SECRET' not in str(caught.value)
    assert (destination.read_bytes() == original) is (which == 'before-replace')
    assert path.with_suffix('.publication').read_bytes() == commit
    outcome = apply('observation-request', action['action_id'], status['commit_digest'])
    assert outcome['outcome'] == ('applied' if which == 'before-replace' else 'already_applied')
    assert collected[3].call_count == 1 and len(collected[4]) == 1


@pytest.mark.parametrize('stage', ['commit', 'admission', 'collector'])
@pytest.mark.parametrize('which', ['file', 'directory'])
def test_actual_fsync_boundaries_do_not_replay_or_delete(collected, monkeypatch, stage, which):
    import os
    import stat
    write, fsync = admission._save_private_bytes, os.fsync
    armed = [False]
    failed = [False]
    deleted = MagicMock()
    monkeypatch.setattr(incus, 'delete_container', deleted)
    def fault(fd):
        if armed[0] and not failed[0] and stat.S_ISDIR(os.fstat(fd).st_mode) == (which == 'directory'):
            failed[0] = True
            raise OSError('SECRET-fsync')
        return fsync(fd)
    def intercept(p, raw):
        value = json.loads(raw)
        selected = (p.suffix == '.publication' if stage == 'commit' else
                    p.suffix == '.json' and value.get('state') == 'ready' if stage == 'admission' else
                    p.suffix == '.json' and value.get('phase') == 'completed')
        armed[0] = selected
        try:
            return write(p, raw)
        finally:
            armed[0] = False
    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', fault)
        patch.setattr(admission, '_save_private_bytes', intercept)
        try:
            launch(collected)
        except admission.AdmissionError:
            pass  # Collector completion may be visible despite its lost fsync acknowledgment.
    assert failed[0]
    deleted.assert_not_called()
    path, _ = record(collected)
    state = json.loads(path.with_suffix('.json').read_bytes())
    assert state['phase'] != 'unknown'
    current = engine.launch_status('observation-request')['publication_repair']
    if stage == 'commit' and which == 'file':
        assert current == {'available': False}
    else:
        assert current['available']
        for action in current['actions']:
            outcome = collected[0].repair_record_projection('observation-request', action['action_id'], current['commit_digest'])
            assert outcome['outcome'] in {'applied', 'already_applied'}
    assert collected[3].call_count == 1 and len(collected[4]) == 1


@pytest.mark.parametrize('change', ['action', 'digest', 'generation', 'binding', 'version', 'duplicate',
                                   'admission-whitespace', 'collector-whitespace', 'terminal', 'bootstrap', 'marker'])
def test_wrong_or_changed_commit_refuses_without_any_write(collected, monkeypatch, change):
    path, payload, status = interrupt_final(collected, monkeypatch)
    commit_path = path.with_suffix('.publication')
    value = json.loads(commit_path.read_bytes())
    action, digest = status['actions'][0]['action_id'], status['commit_digest']
    if change == 'action':
        action = '0' * 64
    elif change == 'digest':
        digest = '0' * 64
    elif change in {'generation', 'binding', 'version', 'duplicate'}:
        if change == 'generation':
            value['generation'] = '0' * 32
        elif change == 'binding':
            value['binding']['owner_root'] = 'foreign'
        elif change == 'version':
            value['version'] = True
        raw = json.dumps(value)
        if change == 'duplicate':
            raw = raw[:-1] + ', \"version\":1}'
        commit_path.write_text(raw)
    else:
        root = path.parent.parent
        p = (admission.receipt_path(root, 'observation-request') if change in {'admission-whitespace', 'terminal'}
             else path.with_suffix('.json') if change == 'collector-whitespace'
             else path.with_suffix('.input') if change == 'bootstrap'
             else collected[0].lifecycle.marker_path(root, payload['binding']['name']))
        if change == 'terminal':
            receipt = json.loads(p.read_bytes())
            receipt['state'] = 'removed'
            p.write_text(json.dumps(receipt))
        else:
            p.write_bytes(p.read_bytes() + b' ')
    before = {p: p.read_bytes() for p in path.parent.parent.rglob('*') if p.is_file()}
    with pytest.raises(admission.AdmissionError):
        collected[0].repair_record_projection('observation-request', action, digest)
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'mode', 'oversize'])
def test_unsafe_publication_is_not_repaired(collected, monkeypatch, kind):
    import os
    path, _, status = interrupt_final(collected, monkeypatch)
    commit = path.with_suffix('.publication')
    raw = commit.read_bytes()
    other = path.with_suffix('.copy')
    other.write_bytes(raw)
    other.chmod(0o600)
    if kind == 'mode':
        commit.chmod(0o644)
    elif kind == 'oversize':
        commit.write_bytes(b' ' * 262145)
    else:
        commit.unlink()
        if kind == 'symlink':
            commit.symlink_to(other)
        else:
            os.link(other, commit)
    before = commit.read_bytes()
    with pytest.raises(admission.AdmissionError):
        collected[0].repair_record_projection('observation-request', status['actions'][0]['action_id'], status['commit_digest'])
    assert commit.read_bytes() == before and other.read_bytes() == raw


def test_active_request_custody_refuses_repair_before_backend_or_write(collected, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from amplifier_bundle_digital_twin_universe import collector
    path, payload, status = interrupt_final(collected, monkeypatch)
    store = collector.AttemptStore(path.parent.parent, payload['binding'])
    entered, release = Event(), Event()
    def hold():
        with store._lifecycle():
            entered.set()
            assert release.wait(5)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(hold)
        assert entered.wait(5)
        try:
            with pytest.raises(admission.AdmissionError) as caught:
                collected[0].repair_record_projection('observation-request', status['actions'][0]['action_id'], status['commit_digest'])
            assert caught.value.code == 'admission_busy'
        finally:
            release.set()
            future.result(timeout=5)


def test_repair_performs_no_lifecycle_calls_and_preserves_partial_ack_history(collected, monkeypatch):
    path, _, status = interrupt_final(collected, monkeypatch)
    before = {p: p.read_bytes() for p in [path, path.with_suffix('.evidence'), path.with_suffix('.outcome')]}
    forbidden = MagicMock(side_effect=AssertionError('No lifecycle effect during repair'))
    for name in ('create_container', 'set_config', 'delete_container', 'stop_container', 'exec_command',
                 'file_push', 'file_pull', 'get_config', 'list_instances'):
        monkeypatch.setattr(incus, name, forbidden)
    monkeypatch.setattr(collected[0], 'spawn_worker', forbidden)
    for action in status['actions']:
        assert collected[0].repair_record_projection('observation-request', action['action_id'], status['commit_digest'])['outcome'] == 'applied'
    forbidden.assert_not_called()
    assert {p: p.read_bytes() for p in before} == before
    assert len(json.loads(path.read_bytes())['steps']) == 5  # Never backfill final acknowledgment.


@pytest.mark.parametrize('stage', ['commit', 'admission', 'collector'])
def test_worker_recorded_failure_ends_observation_without_wait_budget(collected, monkeypatch, stage):
    from types import SimpleNamespace
    runtime = collected[0]
    monkeypatch.setattr(runtime, 'time', SimpleNamespace(monotonic=lambda: 1.0,
                        sleep=MagicMock(side_effect=AssertionError('Must observe recorded worker failure promptly'))))
    save = admission._save_private_json
    def fail(path, value):
        if (stage == 'commit' and path.suffix == '.publication'
                or stage == 'admission' and value.get('state') == 'ready'
                or stage == 'collector' and value.get('phase') == 'completed'):
            raise OSError('publication failure')
        return save(path, value)
    monkeypatch.setattr(admission, '_save_private_json', fail)
    with pytest.raises(admission.AdmissionError) as caught:
        launch(collected)
    assert caught.value.code == 'launch_unknown'
    runtime.time.sleep.assert_not_called()
    assert collected[3].call_count == 1


def test_existing_maximum_collector_identity_has_completion_headroom(collected):
    from pathlib import Path
    from amplifier_bundle_digital_twin_universe import collector
    runtime, profile, _, create, launches = collected
    chosen = None
    for size in range(1, 257):
        request, owner = '\U00010000' * size, '\U00010001' * size
        binding = dict(runtime.capture_backend().scope, request_id=request, owner_root=owner, name='bounded',
                       intent_digest=runtime._intent(Path(profile).read_text(), {}, 'bounded', owner), destination_digest='c' * 64)
        value = {'version': 1, 'binding': binding, 'generation': '0' * 32, 'revision': 1, 'phase': 'prepared', 'result_digest': None}
        if 4034 <= len(json.dumps(value).encode()) <= 4096:
            chosen = request, owner
            break
    assert chosen is not None
    result = engine.launch(profile, {}, name='bounded', request_id=chosen[0], owner_root=chosen[1],
                           offline=True, collect=True)
    assert result['status'] == 'running' and create.call_count == 1 and len(launches) == 1
    root, payload = runtime.locate(chosen[0])
    assert collector.AttemptStore(root, payload['binding']).observe()['phase'] == 'completed'


@pytest.mark.parametrize('case', ['reservation-time', 'cli-operation', 'operation-id', 'operation-project', 'operation-status'])
def test_rehashed_contradictory_commit_is_not_authority(collected, monkeypatch, case):
    from amplifier_bundle_digital_twin_universe import collector_projection as cp
    if case.startswith('operation-'):
        create = collected[3].side_effect
        def submit(endpoint, name, image, config, pool):
            create(name, image, config)
            return {'id': '11111111-1111-4111-8111-111111111111', 'status_code': 200,
                    'daemon': 'b' * 64, 'project': 'default', 'name': name,
                    'request_id': config['user.dtu.request-id'], 'owner_root': config['user.dtu.owner-root']}
        monkeypatch.setattr(engine.operations, 'local_endpoint', lambda _: {'synthetic': True})
        monkeypatch.setattr(engine.operations, 'submit', submit)
    path, payload, status = interrupt_final(collected, monkeypatch)
    commit_path = path.with_suffix('.publication')
    commit = json.loads(commit_path.read_bytes())
    if case == 'reservation-time':
        ev = cp._json(bytes.fromhex(commit['sources']['evidence']['bytes_hex']))
        baseline = cp._json(bytes.fromhex(ev['baselines']['reservation']['bytes_hex']))
        baseline['created_at'] = 'different-time'
        ev['baselines']['reservation'] = cp._pack(cp._encode(baseline))
        commit['sources']['evidence'] = cp._pack(cp._encode(ev))
    else:
        action = commit['actions'][0]
        before, after = (cp._json(bytes.fromhex(action[k]['bytes_hex'])) for k in ('before', 'after'))
        for value in (before, after):
            if case == 'cli-operation':
                value['create_operation'] = {'id': 'forged'}
            elif case == 'operation-id':
                value['create_operation']['id'] = '22222222-2222-4222-8222-222222222222'
            elif case == 'operation-project':
                value['create_operation']['project'] = 'foreign'
            else:
                value['creation_status'] = 400
        b, a = cp._encode(before), cp._encode(after)
        action.update(before=cp._pack(b), after=cp._pack(a),
                      action_id=cp._action_id(payload['binding'], payload['generation'], action['kind'], b, a))
        admission.receipt_path(path.parent.parent, 'observation-request').write_bytes(b)
    raw = cp._encode(commit)
    commit_path.write_bytes(raw)
    before = {p: p.read_bytes() for p in path.parent.parent.rglob('*') if p.is_file()}
    with pytest.raises(admission.AdmissionError):
        collected[0].repair_record_projection('observation-request', commit['actions'][0]['action_id'], cp._hash(raw))
    assert {p: p.read_bytes() for p in before} == before


def test_restored_backend_drift_refuses_repair_without_writes(collected, monkeypatch):
    from amplifier_bundle_digital_twin_universe.bound_backend import BackendFence
    path, _, status = interrupt_final(collected, monkeypatch)
    runtime = collected[0]
    backend = runtime.capture_backend()
    backend.verify = MagicMock(side_effect=BackendFence('changed'))
    monkeypatch.setattr(runtime, 'restore_backend', lambda _: backend)
    before = {p: p.read_bytes() for p in path.parent.parent.rglob('*') if p.is_file()}
    with pytest.raises(BackendFence):
        runtime.repair_record_projection('observation-request', status['actions'][0]['action_id'], status['commit_digest'])
    assert {p: p.read_bytes() for p in before} == before


def test_commit_snapshot_overflow_does_not_truncate_or_delete(collected, monkeypatch):
    from amplifier_bundle_digital_twin_universe import collector_projection as cp
    monkeypatch.setattr(cp, '_ADMISSION_LIMIT', 128)
    deleted = MagicMock()
    monkeypatch.setattr(incus, 'delete_container', deleted)
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    path, payload = record(collected)
    assert not path.with_suffix('.publication').exists()
    receipt = admission.read_receipt(path.parent.parent, payload['binding']['request_id'])
    assert receipt['state'] == 'launching' and receipt['prepared_result']['result']['name'] == 'observation-case'
    assert json.loads(path.with_suffix('.json').read_bytes())['phase'] == 'submitting'
    deleted.assert_not_called()


def test_unacknowledged_ready_marker_never_constructs_commit(collected, monkeypatch):
    marker = incus.set_config
    def lost_ack(name, key, value):
        marker(name, key, value)
        if key == 'user.dtu.launch-state' and value == 'ready':
            raise OSError('marker applied but call acknowledgment lost')
    monkeypatch.setattr(incus, 'set_config', lost_ack)
    with pytest.raises(admission.AdmissionError):
        launch(collected)
    path, _ = record(collected)
    status = engine.launch_status('observation-request')
    assert status['result_recovered'] is True
    assert status['publication_repair'] == {'available': False}
    before = {p: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
    with pytest.raises(admission.AdmissionError):
        collected[0].repair_record_projection('observation-request', '0' * 64, '0' * 64)
    assert {p: p.read_bytes() for p in before} == before


def test_commit_must_resync_before_explicit_projection(collected, monkeypatch):
    import os
    from amplifier_bundle_digital_twin_universe import collector_projection as cp
    path, _, status = interrupt_final(collected, monkeypatch)
    before = admission.receipt_path(path.parent.parent, 'observation-request').read_bytes()
    real = os.fsync
    calls = []
    write = admission._save_private_bytes
    synchronize = cp._sync_commit
    active = [False]
    done = [False]
    def sync_commit(*args):
        active[0] = True
        try:
            synchronize(*args)
            done[0] = True
        finally:
            active[0] = False
    def sync(fd):
        if active[0]:
            calls.append('sync')
        return real(fd)
    def save(p, raw):
        if p.suffix == '.json':
            assert done[0] and calls == ['sync', 'sync']  # Retained commit file + directory.
            calls.append('project')
        return write(p, raw)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', sync)
        patch.setattr(cp, '_sync_commit', sync_commit)
        patch.setattr(admission, '_save_private_bytes', save)
        cp_payload = record(collected)[1]
        assert collected[0].repair_record_projection('observation-request', status['actions'][0]['action_id'],
                                                     status['commit_digest'])['outcome'] == 'applied'
    assert calls[:3] == ['sync', 'sync', 'project']
    assert admission.receipt_path(path.parent.parent, cp_payload['binding']['request_id']).read_bytes() != before
