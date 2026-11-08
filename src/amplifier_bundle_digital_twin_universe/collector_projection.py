# Copyright (c) Microsoft. All rights reserved.
"""Exact future successful-publication commits; not settlement or legacy authority.

One immutable record commits two fixed local projections. Only the owning worker
can prepare it after acknowledged provisioning and readiness. Explicit repair
uses the existing name/request/admission custody and never executes a lifecycle
command. Hashes detect changed bytes, not a malicious same-UID writer.
"""
import hashlib
import json
import os
import re

from . import admission, collector, collector_evidence, collector_observation, lifecycle, operations

_KINDS = ('publish_ready_result', 'complete_collector')
_LIMIT = 262144
_ADMISSION_LIMIT = 16384
_COLLECTOR_LIMIT = 8192  # Initial 4-KiB collector grows by its completion digest.
_KEYS = {'version', 'state', 'binding', 'generation', 'bootstrap_digest', 'marker_digest', 'sources', 'actions'}


class ProjectionError(admission.AdmissionError):
    def __init__(self, code='projection_conflict'):
        super().__init__(code, 'Exact committed publication is unavailable, changed or not applicable')


def _require(condition):
    if not condition:
        raise ProjectionError()


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(raw):
    return json.loads(raw, object_pairs_hook=collector._unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ProjectionError()))


def _encode(value):
    return json.dumps(value, allow_nan=False).encode('utf-8')


def _pack(raw):
    return {'bytes_hex': raw.hex(), 'sha256': _hash(raw)}


def _unpack(value, limit):
    _require(isinstance(value, dict) and set(value) == {'bytes_hex', 'sha256'})
    _require(isinstance(value['bytes_hex'], str) and len(value['bytes_hex']) <= limit * 2
             and re.fullmatch(r'(?:[a-f0-9]{2})*', value['bytes_hex']) is not None)
    raw = bytes.fromhex(value['bytes_hex'])
    _require(_hash(raw) == value['sha256'])
    return raw


def _path(store):
    return store.path.with_suffix('.publication')


def _read_bytes(path, limit=_LIMIT):
    return collector_evidence._bytes(path, limit)


def _bootstrap_paths(store):
    return store.path.with_suffix('.input'), lifecycle.marker_path(store.root, store.binding['name'])


def _action_id(binding, generation, kind, before, after):
    return collector._digest([dict(binding), generation, kind, _hash(before), _hash(after)])


def _validate(store, payload, value):
    _require(isinstance(value, dict) and set(value) == _KEYS
             and type(value['version']) is int and value['version'] == 1 and value['state'] == 'COMMITTED'
             and value['binding'] == dict(store.binding) and value['generation'] == payload['generation'])
    for key in ('bootstrap_digest', 'marker_digest'):
        _require(isinstance(value[key], str) and re.fullmatch(r'[a-f0-9]{64}', value[key]))
    _require(isinstance(value['sources'], dict) and set(value['sources']) == {'observations', 'evidence'})
    obs = _json(_unpack(value['sources']['observations'], 32768))
    ev = _json(_unpack(value['sources']['evidence'], 32768))
    collector_observation._validate(store, payload['generation'], obs)
    collector_evidence._validate(store, payload['generation'], ev)
    _require(obs['steps'] == list(collector_observation.STEPS[:5])
             and ev['primary'] is None and ev['cleanup'] is None and ev['receipt_failure'] is None
             and ev['baselines']['reservation'] is not None)
    _require((obs['creation_transport'] == 'cli' and ev['cli'] == {'outcome': 'returned', 'returncode': 0})
             or (obs['creation_transport'] == 'operation' and ev['operation'] is not None
                 and ev['operation']['terminal_status'] == 200))
    actions = value['actions']
    _require(isinstance(actions, list) and len(actions) == 2)
    images = []
    for index, action in enumerate(actions):
        _require(isinstance(action, dict) and set(action) == {'kind', 'action_id', 'before', 'after'}
                 and action['kind'] == _KINDS[index])
        limit = _ADMISSION_LIMIT if index == 0 else _COLLECTOR_LIMIT
        before, after = (_unpack(action[key], limit) for key in ('before', 'after'))
        _require(action['action_id'] == _action_id(store.binding, payload['generation'], _KINDS[index], before, after))
        images.append((_json(before), _json(after), after))
    receipt, final, encoded_final = images[0]
    admission._validate_receipt(receipt)
    _require(receipt['state'] in {'launching', 'created', 'unknown'}
             and all(receipt.get(k) == store.binding[k] for k in ('request_id', 'name', 'owner_root', 'intent_digest')))
    reservation = _json(bytes.fromhex(ev['baselines']['reservation']['bytes_hex']))
    _require(all(reservation.get(k) == receipt.get(k) for k in
                 ('request_id', 'name', 'owner_root', 'intent_digest', 'created_at')))
    operation = receipt.get('create_operation')
    if obs['creation_transport'] == 'cli':
        _require(operation is None and 'creation_status' not in receipt)
    else:
        scope = {k: store.binding[k] for k in ('daemon', 'project', 'storage_pool')}
        _require(operations.validate_binding(operation, scope, receipt) == ev['operation']['id']
                 and operation.get('status_code') == ev['operation']['initial_status']
                 and receipt.get('creation_status') == 200)
        # The public wrapper has already verified the exact restored backend.
        # Synthetic unit backends omit socket; real bootstrap bindings never do.
        if 'socket' in payload['backend']:
            _require(operation.get('socket') == payload['backend']['socket'])
    checkpoint = receipt.get('prepared_result')
    _require(isinstance(checkpoint, dict) and set(checkpoint) == {'daemon', 'project', 'intent_digest', 'result'}
             and all(checkpoint[k] == store.binding[k] for k in ('daemon', 'project', 'intent_digest')))
    result = checkpoint['result']
    _require(isinstance(result, dict) and result.get('id') == store.binding['name']
             and result.get('status') == 'running'
             and all(result.get(k) == store.binding[k] for k in ('request_id', 'name', 'owner_root'))
             and isinstance(receipt.get('created_at'), str) and bool(receipt['created_at'].strip())
             and result.get('created_at') == receipt['created_at'] and obs['result_digest'] == collector._digest(result))
    expected = dict(receipt)
    expected.pop('prepared_result')
    expected.update(state='ready', result=result)
    _require(_encode(expected) == encoded_final and _encode(final) == encoded_final)
    current, completed, encoded_completed = images[1]
    _require(isinstance(current, dict) and set(current) == collector._KEYS
             and type(current['version']) is int and current['version'] == 1
             and current['binding'] == dict(store.binding) and current['generation'] == payload['generation']
             and type(current['revision']) is int and current['revision'] >= 1
             and current['phase'] == 'submitting' and current['result_digest'] is None)
    expected = {**current, 'revision': current['revision'] + 1, 'phase': 'completed',
                'result_digest': collector._digest(result)}
    _require(_encode(expected) == encoded_completed and _encode(completed) == encoded_completed)
    _require(len(_encode(value)) <= _LIMIT)


def read(store, payload):
    """Read and validate the immutable intent; never infer or backfill one."""
    try:
        store._directories()
        try:
            raw = _read_bytes(_path(store))
        except FileNotFoundError:
            return None
        value = _json(raw)
        _validate(store, payload, value)
        bootstrap, marker = _bootstrap_paths(store)
        _require(_hash(_read_bytes(bootstrap)) == value['bootstrap_digest']
                 and _hash(_read_bytes(marker)) == value['marker_digest'])
        return value, raw
    except Exception:
        raise ProjectionError() from None


def _targets(store):
    return admission.receipt_path(store.root, store.binding['request_id']), store.path


def _joint(store, value):
    states = []
    for path, action in zip(_targets(store), value['actions']):
        raw = _read_bytes(path)
        states.append('before' if raw == bytes.fromhex(action['before']['bytes_hex']) else
                      'after' if raw == bytes.fromhex(action['after']['bytes_hex']) else 'changed')
    return states


def projection(store, payload):
    found = read(store, payload)
    if found is None:
        return {'available': False}
    value, raw = found
    try:
        states = _joint(store, value)
    except (OSError, admission.AdmissionError):
        states = ['unavailable', 'unavailable']
    return {'available': True, 'commit_digest': _hash(raw),
            'actions': [{'kind': action['kind'], 'action_id': action['action_id'], 'state': state}
                        for action, state in zip(value['actions'], states)],
            'meaning': 'historical_local_publication_not_current_readiness'}


def _sync_commit(path, raw):
    """A later explicit apply acknowledges this exact retained commit, not an old fsync."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        admission._private_file(os.fstat(stream.fileno()))
        _require(stream.read(_LIMIT + 1) == raw)
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    _require(_read_bytes(path) == raw)


def apply_action(store, payload, action_id, expected_digest):
    """Caller holds name -> request -> admission and has reverified backend."""
    try:
        found = read(store, payload)
        _require(found is not None)
        value, raw = found
        _require(_hash(raw) == expected_digest)
        matches = [i for i, action in enumerate(value['actions']) if action['action_id'] == action_id]
        _require(len(matches) == 1)
        index = matches[0]
        states = _joint(store, value)
        _require(states in (['before', 'before'], ['after', 'before'], ['after', 'after']))
        if index == 1:
            _require(states[0] == 'after')
        if states[index] == 'after':
            return {'action_id': action_id, 'outcome': 'already_applied', 'current_readiness': 'not_asserted'}
        _sync_commit(_path(store), raw)
        _require(_joint(store, value) == states)
        admission._save_private_bytes(_targets(store)[index], bytes.fromhex(value['actions'][index]['after']['bytes_hex']))
        _require(_read_bytes(_targets(store)[index]) == bytes.fromhex(value['actions'][index]['after']['bytes_hex']))
        return {'action_id': action_id, 'outcome': 'applied', 'current_readiness': 'not_asserted'}
    except ProjectionError:
        raise
    except Exception:
        raise ProjectionError('projection_unknown') from None


class Publication:
    """Per-worker capability; no public caller supplies desired projection bytes."""
    def __init__(self, store, payload):
        self.store, self.payload = store, payload
        self.attempted = False
        self.digest = None

    def commit(self, ctx, root, final):
        _require(not self.attempted)
        self.attempted = True  # Even an uncertain save must not overwrite before-images.
        try:
            _require(root == self.store.root and all(ctx[k] == self.store.binding[k] for k in ('daemon', 'project', 'storage_pool')))
            _require(not _path(self.store).exists())
            before = _read_bytes(_targets(self.store)[0], _ADMISSION_LIMIT)
            original = _read_bytes(self.store.path, _COLLECTOR_LIMIT)
            current = self.store._current(self.payload['generation'], {'submitting'})
            result = final['result']
            after_collector = _encode({**current, 'revision': current['revision'] + 1,
                                      'phase': 'completed', 'result_digest': collector._digest(result)})
            bootstrap, marker = _bootstrap_paths(self.store)
            bootstrap_raw, marker_raw = _read_bytes(bootstrap), _read_bytes(marker)
            _require(_json(bootstrap_raw) == self.payload)
            _require(_json(marker_raw) == {'request_id': self.store.binding['request_id'],
                                          'name': self.store.binding['name'],
                                          'generation': self.payload['generation'],
                                          'input_digest': collector._digest(self.payload)})
            value = {'version': 1, 'state': 'COMMITTED', 'binding': dict(self.store.binding),
                     'generation': self.payload['generation'], 'bootstrap_digest': _hash(bootstrap_raw),
                     'marker_digest': _hash(marker_raw),
                     'sources': {k: _pack(_read_bytes(self.store.path.with_suffix(suffix), 32768))
                                 for k, suffix in [('observations', '.observations'), ('evidence', '.evidence')]},
                     'actions': []}
            for kind, previous, after in zip(_KINDS, (before, original), (_encode(final), after_collector)):
                value['actions'].append({'kind': kind, 'action_id': _action_id(self.store.binding, self.payload['generation'], kind, previous, after),
                                         'before': _pack(previous), 'after': _pack(after)})
            _validate(self.store, self.payload, value)
            raw = _encode(value)
            admission._save_private_json(_path(self.store), value)
            _require(_read_bytes(_path(self.store)) == raw)
            self.digest = _hash(raw)
        except Exception:
            raise ProjectionError('projection_unknown') from None

    def complete(self, current, result):
        """Native success path retains the canonical collector transition seam."""
        _require(self.digest is not None)
        value, raw = read(self.store, self.payload)
        _require(_hash(raw) == self.digest and _joint(self.store, value) == ['after', 'before'])
        _require(_encode({**current, 'revision': current['revision'] + 1, 'phase': 'completed',
                          'result_digest': collector._digest(result)}) == bytes.fromhex(value['actions'][1]['after']['bytes_hex']))
        self.store._advance(current, 'completed', collector._digest(result))
        _require(_joint(self.store, value) == ['after', 'after'])
