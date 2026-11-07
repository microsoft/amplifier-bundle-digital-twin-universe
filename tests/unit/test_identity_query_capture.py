"""Deterministic pipe/selector tests; no real processes, sockets or signals."""
from types import SimpleNamespace
from unittest.mock import Mock
import subprocess

import pytest

from amplifier_bundle_digital_twin_universe import bound_backend as backend


@pytest.fixture
def pipe(monkeypatch):
    state = SimpleNamespace(data=b'{}', stderr=b'', offset=0, err_offset=0, requests=[], clock=0.0, continuous=False,
                            no_events=False, blocking_once=False, read_error=None,
                            exit_code=0, wait_timeout=False, close_error=False, kill_error=False,
                            reap_timeout=False, close_delay=0)
    class Stream:
        closed = False
        def __init__(self, descriptor):
            self.descriptor = descriptor
        def fileno(self):
            return self.descriptor
        def close(self):
            self.closed = True
            state.clock += state.close_delay
            if state.close_error:
                raise OSError('SECRET-close')
    class Process:
        stdout = Stream(12345)
        stderr = Stream(12346)
        returncode = None
        killed = False
        waits = []
        def poll(self):
            return self.returncode
        def kill(self):
            if state.kill_error:
                raise OSError('SECRET-kill')
            self.killed = True
            self.returncode = -9
        def wait(self, timeout=None):
            assert timeout is not None and timeout >= 0
            self.waits.append(timeout)
            if state.reap_timeout or state.wait_timeout and not self.killed:
                raise subprocess.TimeoutExpired('SECRET', timeout)
            self.returncode = self.returncode if self.returncode is not None else state.exit_code
            return self.returncode
    class Selector:
        closed = False
        registered = []
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.closed = True
        def register(self, fd, mask):
            self.registered.append((fd, mask))
        def unregister(self, fd):
            self.registered = [(n, mask) for n, mask in self.registered if n != fd]
        def select(self, timeout):
            assert timeout > 0
            state.clock += .1
            return [] if state.no_events else [(SimpleNamespace(fd=n), mask) for n, mask in sorted(self.registered)]
    process, selector = Process(), Selector()
    def read(fd, size):
        assert fd in (12345, 12346) and 0 < size <= 65536
        state.requests.append((fd, size))
        if state.read_error:
            raise state.read_error
        if state.blocking_once:
            state.blocking_once = False
            raise BlockingIOError()
        if fd == 12346:
            part = state.stderr[state.err_offset:state.err_offset+size]
            state.err_offset += len(part)
            return part
        if state.continuous:
            state.offset += 1
            return b'x'
        part = state.data[state.offset:state.offset+size]
        state.offset += len(part)
        return part
    popen = Mock(return_value=process)
    monkeypatch.setattr(backend.subprocess, 'Popen', popen)
    monkeypatch.setattr(backend.subprocess, 'run', Mock(side_effect=AssertionError('No whole-output run')))
    monkeypatch.setattr(backend.selectors, 'DefaultSelector', lambda: selector)
    monkeypatch.setattr(backend.os, 'read', read)
    nonblocking = Mock()
    monkeypatch.setattr(backend.os, 'set_blocking', nonblocking)
    monkeypatch.setattr(backend, 'time', SimpleNamespace(monotonic=lambda: state.clock))
    state.process, state.selector, state.popen, state.nonblocking = process, selector, popen, nonblocking
    return state


def capture():
    return backend._capture_identity_query(['/owned/incus', 'query', '/1.0?project=owned'],
                                           env={'PINNED': 'value'}, timeout=2)


def test_exact_byte_limit_and_complete_eof(pipe, monkeypatch):
    monkeypatch.setattr(backend, '_QUERY_LIMIT', 131072)
    pipe.data = b' ' * 131070 + b'{}'
    assert capture() == pipe.data
    assert [size for fd, size in pipe.requests if fd == 12345] == [65536, 65536, 1]
    assert {c.args for c in pipe.nonblocking.call_args_list} == {(12345, False), (12346, False)}
    assert pipe.selector.closed and pipe.process.stdout.closed and pipe.process.stderr.closed
    assert not pipe.process.killed and pipe.process.returncode == 0
    pipe.popen.assert_called_once_with(['/owned/incus', 'query', '/1.0?project=owned'],
                                     env={'PINNED': 'value'}, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_large_output_stops_at_limit_plus_one_not_whole_output(pipe, monkeypatch):
    monkeypatch.setattr(backend, '_QUERY_LIMIT', 131072)
    pipe.data = b'{}' + b' ' * (1024 * 1024 * 8)
    with pytest.raises(backend.BackendFence, match='exceeds its bound'):
        capture()
    assert pipe.offset == 131073 and [size for fd, size in pipe.requests if fd == 12345] == [65536, 65536, 1]
    assert pipe.process.killed and pipe.process.returncode == -9
    assert pipe.selector.closed and pipe.process.stdout.closed
    assert pipe.popen.call_count == 1


@pytest.mark.parametrize('kind', ['continuous', 'no-events', 'eof-before-exit', 'read-error', 'nonzero', 'cancel'])
def test_failure_closes_and_reaps_owned_child_without_retry(pipe, kind):
    if kind == 'continuous':
        pipe.continuous = True
    elif kind == 'no-events':
        pipe.no_events = True
    elif kind == 'eof-before-exit':
        pipe.wait_timeout = True
    elif kind == 'read-error':
        pipe.read_error = OSError('SECRET-read')
    elif kind == 'cancel':
        pipe.read_error = KeyboardInterrupt()
    else:
        pipe.exit_code = 37
    with pytest.raises((TimeoutError, subprocess.TimeoutExpired, OSError, backend.BackendFence, KeyboardInterrupt)):
        capture()
    assert pipe.selector.closed and pipe.process.stdout.closed and pipe.process.stderr.closed
    assert pipe.process.returncode is not None and pipe.popen.call_count == 1
    assert pipe.process.killed is (kind != 'nonzero')
    if kind == 'continuous':
        assert pipe.offset <= 21 and pipe.clock <= 2.2


def test_child_exit_does_not_allow_inherited_stdout_to_hang(pipe):
    pipe.process.returncode = 0  # Original child exited, writer has not sent EOF.
    pipe.no_events = True
    with pytest.raises(TimeoutError):
        capture()
    assert pipe.process.stdout.closed and pipe.selector.closed and not pipe.process.killed


def test_nonblocking_race_is_retried_only_as_read(pipe):
    pipe.blocking_once = True
    assert capture() == b'{}'
    assert pipe.popen.call_count == 1 and not pipe.process.killed


def test_close_failure_does_not_skip_owned_termination(pipe):
    pipe.read_error = OSError('SECRET-read')
    pipe.close_error = True
    with pytest.raises(OSError) as caught:
        capture()
    assert pipe.process.killed and pipe.process.returncode == -9
    assert 'Identity-query child cleanup could not be confirmed' in caught.value.__notes__


def test_unconfirmed_termination_is_not_reported_as_clean_cleanup(pipe):
    pipe.no_events = True
    pipe.kill_error = True
    pipe.wait_timeout = True
    with pytest.raises(TimeoutError) as caught:
        capture()
    assert pipe.process.returncode is None
    assert len(pipe.process.waits) == 1
    assert 'Identity-query child cleanup could not be confirmed' in caught.value.__notes__


@pytest.mark.parametrize('data', [b'\xff', b'{"type":"sync"', b'{}\xff'])
def test_raw_decode_failure_sanitized_after_complete_capture(pipe, monkeypatch, data):
    pipe.data = data
    monkeypatch.setattr(backend, '_check_local', lambda _: None)
    monkeypatch.setattr(backend, '_environment', lambda _: {})
    record = {'executable': {'path': '/owned/incus'}, 'scope': {'project': 'owned'}, 'query_timeout': 2}
    with pytest.raises(backend.BackendFence, match='could not be verified'):
        backend._query(record, '/1.0')
    assert pipe.process.returncode == 0 and pipe.process.stdout.closed


def test_overflow_never_decodes_or_parses_valid_prefix(pipe, monkeypatch):
    monkeypatch.setattr(backend, '_QUERY_LIMIT', 8)
    pipe.data = b'{}          SECRET'
    monkeypatch.setattr(backend, '_check_local', lambda _: None)
    monkeypatch.setattr(backend, '_environment', lambda _: {})
    parse = Mock(side_effect=AssertionError('Must not parse a truncated prefix'))
    monkeypatch.setattr(backend.json, 'loads', parse)
    record = {'executable': {'path': '/owned/incus'}, 'scope': {'project': 'owned'}, 'query_timeout': 2}
    with pytest.raises(backend.BackendFence) as caught:
        backend._query(record, '/1.0')
    assert 'SECRET' not in str(caught.value)
    parse.assert_not_called()
    assert pipe.offset == 9


@pytest.mark.parametrize("overflow", [False, True])
def test_combined_limit_drains_both_pipes_and_discards_stderr(pipe, monkeypatch, overflow):
    monkeypatch.setattr(backend, '_QUERY_LIMIT', 65540)
    pipe.data = b'{}'
    pipe.stderr = b'x' * (65538 + int(overflow))
    if overflow:
        with pytest.raises(backend.BackendFence, match='exceeds its bound'):
            capture()
        assert pipe.process.killed
    else:
        assert capture() == b'{}'
        assert not pipe.process.killed
    assert pipe.offset + pipe.err_offset == 65540 + int(overflow)
    assert pipe.process.stdout.closed and pipe.process.stderr.closed
    assert {fd for fd, _ in pipe.requests} == {12345, 12346}


@pytest.mark.parametrize("kind", ["kill", "reap"])
def test_query_boundary_retains_sanitized_cleanup_uncertainty(pipe, monkeypatch, kind):
    pipe.no_events = True
    pipe.wait_timeout = True
    pipe.kill_error = kind == "kill"
    pipe.reap_timeout = kind == "reap"
    monkeypatch.setattr(backend, '_check_local', lambda _: None)
    monkeypatch.setattr(backend, '_environment', lambda _: {})
    record = {'executable': {'path': '/owned/incus'}, 'scope': {'project': 'owned'}, 'query_timeout': 2}
    with pytest.raises(backend.BackendFence) as caught:
        backend._query(record, '/1.0')
    assert not isinstance(caught.value, backend.BackendExecutionTimeout)
    assert caught.value.__notes__ == ['Identity-query child cleanup could not be confirmed']
    assert 'SECRET' not in str(caught.value) and 'SECRET' not in str(caught.value.__notes__)
    assert all(0 < budget <= 2 for budget in pipe.process.waits)


def test_cleanup_never_extends_expired_deadline(pipe):
    pipe.no_events = True
    pipe.close_delay = 3
    with pytest.raises(TimeoutError):
        capture()
    assert pipe.process.waits == [0.0]
    assert pipe.process.killed
