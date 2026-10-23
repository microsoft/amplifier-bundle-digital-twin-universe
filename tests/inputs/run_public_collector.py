"""Guest-only actual public caller -> installed worker -> filesystem fake Incus.

No real daemon or network. Three independently named synthetic cases cover
observer loss, cooperative cancellation, and worker loss without respawn.
Linux subreaper applies to this guest supervisor only, for exact owned descendants.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from threading import local

ROOT = Path('/workspace')
OUT = Path('/artifacts/dtu001-public-collector')
SCRIPT = ROOT / 'tests/inputs/run_public_collector.py'
CASE_NAMES = ('observer-loss', 'cancel', 'worker-loss', 'publication-repair')
DENY = {'os.system', 'os.posix_spawn', 'os.fork', 'os.forkpty', 'os.exec', 'os.killpg',
        'signal.pthread_kill', 'signal.delivery'}


def require(value, reason='Public collector fixture invariant'):
    if not value:
        raise RuntimeError(reason)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def readiness():
    require(Path.cwd() == ROOT and Path(__file__).resolve() == SCRIPT and not sys.flags.optimize)
    sys.dont_write_bytecode = True
    manifest = json.loads((ROOT / 'tests/inputs/dtu001-selection.json').read_text())
    plan = json.loads(Path('/opt/unified-prepared/plan.json').read_text())
    inv = json.loads(Path('/opt/unified-prepared/inventory.json').read_text())
    require(plan['profileId'] == inv['profileId'] == 'python-project')
    require(plan['sourceSha256'] == inv['sourceSha256'])
    for name, digest in manifest['sourceFiles'].items():
        require(sha(ROOT / name) == digest, 'Frozen source mismatch')
    from amplifier_bundle_digital_twin_universe import engine, collected_launch, collector_worker
    package = Path(engine.__file__).parent
    require(str(package).startswith('/opt/unified-prepared/venv/') and 'site-packages' in str(package), 'Noneditable installed package required')
    for source in (ROOT / 'src/amplifier_bundle_digital_twin_universe').glob('*.py'):
        require(sha(source) == sha(package / source.name), 'Installed payload differs')
    return manifest, plan, package, engine, collected_launch, collector_worker


class Guard:
    def __init__(self, case, worker, foreground=False):
        self.case, self.worker, self.foreground = case, worker, foreground
        self.allowed, self.denied, self.signal_pids = [], [], set()
        self.processes = []
        self.termination = local()
        self.pidfd_signal = signal.pidfd_send_signal
        sys.addaudithook(self.audit)
        original = subprocess.Popen
        guard = self

        class OwnedPopen(original):
            def __init__(self, *args, **kwargs):
                guard.termination.construction = None
                try:
                    super().__init__(*args, **kwargs)
                    guard.processes.append(self)
                finally:
                    guard.termination.construction = None
                    guard.termination.spawn = None

            def _posix_spawn(self, args, executable, env, *rest, **kwargs):
                require((executable, tuple(args), env) == getattr(guard.termination, 'construction', None))
                guard.termination.construction = None
                guard.termination.spawn = (executable, args, env)
                try:
                    return super()._posix_spawn(args, executable, env, *rest, **kwargs)
                finally:
                    guard.termination.spawn = None

            def send_signal(self, sig):
                if self.poll() is not None:
                    return
                require(sig in (signal.SIGTERM, signal.SIGKILL))
                guard.termination.permit = (self.pid, sig)
                try:
                    return super().send_signal(sig)
                finally:
                    guard.termination.permit = None
        subprocess.Popen = OwnedPopen
        for name in ('raise_signal', 'pthread_kill', 'pidfd_send_signal'):
            if hasattr(signal, name):
                setattr(signal, name, lambda *a, **k: self.refuse('signal.delivery'))

    def refuse(self, event):
        self.denied.append(event)
        raise RuntimeError('Fixture denied ' + event)

    def audit(self, event, args):
        if event == 'subprocess.Popen':
            exe, argv, cwd, env = args
            fake = self.case / 'bin/incus'
            permitted = exe == str(fake) and argv[0] == str(fake)
            if self.foreground:
                permitted |= (exe == sys.executable and len(argv) == 5 and argv[:3] == [exe, '-I', '-B']
                              and argv[3] == str(self.worker) and argv[4].isdigit())
            else:
                permitted |= (exe == sys.executable and argv == [exe, '-I', '-B', str(SCRIPT), 'foreground', self.case.name])
            if not permitted:
                self.refuse(event)
            self.termination.construction = (exe, tuple(argv), env)
            self.allowed.append(event)
        elif event == 'os.posix_spawn':
            permit = getattr(self.termination, 'spawn', None)
            self.termination.spawn = None
            if permit != args:
                self.refuse(event)
            self.allowed.append(event)
        elif event == 'os.kill':
            if args != getattr(self.termination, 'permit', None):
                self.refuse(event)
            self.allowed.append(event)
        elif event in DENY or event.startswith('socket.'):
            self.refuse(event)

    def signal_owned(self, initial, sig):
        require(not self.foreground and initial['pid'] in self.signal_pids
                and sig in (signal.SIGTERM, signal.SIGKILL))
        fd = os.pidfd_open(initial['pid'])
        try:
            now = identity(initial['pid'])
            require(now['start']==initial['start'] and now['uid']==initial['uid'], 'Signal identity changed')
            self.pidfd_signal(fd, sig)
            self.allowed.append('owned.pidfd_send_signal')
        finally:
            os.close(fd)


def environment(case):
    return {'PATH': str(case / 'bin') + ':/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
            'HOME': str(case), 'INCUS_CONF': str(case / 'config'), 'INCUS_GLOBAL_CONF': str(case / 'global'),
            'AMPLIFIER_DTU_INCUS_PROJECT': 'default', 'AMPLIFIER_DTU_ADMISSION_DIR': str(case / 'admission'),
            'TMPDIR': str(case / 'tmp')}


def prepare(case):
    for label in ('bin', 'config', 'global', 'admission', 'tmp'):
        (case / label).mkdir(mode=0o700, parents=True, exist_ok=False)
    fake = case / 'bin/incus'
    fake.write_bytes(('#!' + sys.executable + ' -I\n').encode() + (ROOT / 'tests/inputs/collector_fake_incus.py').read_bytes())
    fake.chmod(0o700)
    config = case / 'config/config.yml'
    config.write_text('default-remote: local\nremotes:\n  local:\n    addr: unix:///synthetic-never-contacted.sock\n    protocol: incus\n    public: false\naliases: {}\n')
    config.chmod(0o600)
    (case / 'profile.yaml').write_text('name: public-collector-fixture\nbase:\n  image: local:' + 'a'*64 + '\n  config:\n    security.privileged: "false"\n    security.nesting: "false"\nprovision:\n  setup_cmds:\n    - collector-fixture-provision\n')


def identity(pid):
    raw = Path(f'/proc/{pid}/stat').read_text()
    parts = raw[raw.rfind(')')+2:].split()
    return {'pid': pid, 'ppid': int(parts[1]), 'session': int(parts[3]), 'start': parts[19],
            'uid': Path(f'/proc/{pid}').stat().st_uid}


def wait(predicate, deadline):
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError('Fixture checkpoint deadline')
        time.sleep(0.02)


def foreground(case, package):
    policy = Guard(case, package / 'collector_worker.py', foreground=True)
    from amplifier_bundle_digital_twin_universe.cli import main
    try:
        main(['launch', str(case / 'profile.yaml'), '--offline', '--collect', '--name', 'public-'+case.name,
              '--request-id', 'public-'+case.name, '--owner-root', 'fixture-owner',
              '--wait-seconds', '3' if case.name == 'publication-repair' else '12'],
             standalone_mode=False)
    finally:
        (case / 'foreground-guard.json').write_text(json.dumps({'allowed': policy.allowed, 'denied': policy.denied}))


def query_capture_case(package):
    """Finite noisy identity queries through the installed helper and guard."""
    from amplifier_bundle_digital_twin_universe import bound_backend, worker_guard
    case = OUT / 'query-capture'
    case.mkdir(mode=0o700)
    prepare(case)
    policy = Guard(case, package / 'collector_worker.py')
    old_env = dict(os.environ)
    os.environ.clear()
    os.environ.update(environment(case))
    os.environ['AMPLIFIER_DTU_BOUND_BACKEND_QUERY_TIMEOUT_SECONDS'] = '1'
    report = {'case': 'query-capture', 'status': 'incomplete', 'realIncus': False,
              'allowed': policy.allowed, 'denied': policy.denied, 'children': [], 'cleanupErrors': []}
    retain = None
    code = 1
    try:
        bound = bound_backend.capture()
        retain = worker_guard.install(bound, case / 'worker-guard.json')
        require(bound.as_dict()['query_timeout'] == 1)
        for mode in ('small', 'stderr', 'overflow', 'combined', 'continuous', 'timeout', 'cancel', 'eof', 'nonzero', 'truncated'):
            (case / 'query-mode').write_text(mode)
            start = time.monotonic()
            previous_count = len(policy.processes)
            selector_class = bound_backend.selectors.DefaultSelector
            if mode == 'cancel':
                class InterruptedSelector(selector_class):
                    def select(self, timeout=None):
                        super().select(timeout)
                        raise KeyboardInterrupt()
                bound_backend.selectors.DefaultSelector = InterruptedSelector
            try:
                value = bound_backend._query(bound.as_dict(), '/1.0')
            except KeyboardInterrupt:
                require(mode == 'cancel')
                report[mode] = {'callerInterruptionPropagated': True, 'osStopClaimed': False}
            except bound_backend.BackendFence as exc:
                require(mode not in {'small', 'cancel'})
                require(not isinstance(exc, bound_backend.BackendExecutionTimeout))
                expected = ('Backend identity response exceeds its bound' if mode in {'stderr', 'overflow', 'combined'}
                            else 'Backend identity query failed' if mode == 'nonzero'
                            else 'Backend identity query could not be verified')
                require(str(exc) == expected)
                report[mode] = {'refused': True, 'message': str(exc)}
            else:
                require(mode == 'small' and value['environment']['certificate_fingerprint'] == 'b' * 64)
                report[mode] = {'passed': True, 'eachPipePaddingBytes': 262144}
            finally:
                bound_backend.selectors.DefaultSelector = selector_class
            require(len(policy.processes) == previous_count + 1)
            require(policy.processes[-1].returncode is not None, 'Helper returned without reaping query client')
            require(policy.processes[-1].stdout.closed and policy.processes[-1].stderr.closed)
            report[mode]['helperReapedBeforeFixtureCleanup'] = True
            report[mode]['bothPipesClosed'] = True
            require(time.monotonic() - start < 5, 'Query exceeded fixture completion bound')
        require(not (case / 'launch-count').exists() and not (case / 'provision-count').exists())
        trace = [json.loads(line) for line in (case / 'trace.jsonl').read_text().splitlines()]
        require([x['kind'] for x in trace if x['kind'].startswith('query-')] ==
                ['query-small', 'query-stderr', 'query-overflow', 'query-combined', 'query-continuous',
                 'query-timeout', 'query-cancel', 'query-eof', 'query-nonzero', 'query-truncated'])
        report.update(status='passed', noLifecycleEffects=True, runtime=bound.as_dict()['executable']['identity'])
        code = 0
    except BaseException as exc:
        report.update(errorType=type(exc).__name__, errorMessage=str(exc)[:250])
    finally:
        # These are direct children retained at Popen return, never trace PIDs.
        deadline = time.monotonic() + 5
        for process in policy.processes:
            try:
                if process.returncode is None:
                    report['cleanupErrors'].append('HelperDidNotReapQueryClient')
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=max(.01, deadline - time.monotonic()))
                report['children'].append({'pid': process.pid, 'returncode': process.returncode, 'reaped': True})
            except Exception as exc:
                report['cleanupErrors'].append(type(exc).__name__)
        if retain is not None:
            try:
                retain()
                require(not json.loads((case / 'worker-guard.json').read_text())['denied'])
            except Exception as exc:
                report['cleanupErrors'].append(type(exc).__name__)
        if policy.denied or report['cleanupErrors'] or not policy.processes:
            code = 1
        if code:
            report['status'] = 'failed'
        report['exitCode'] = code
        (case / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
        os.environ.clear()
        os.environ.update(old_env)
    return code


def one_case(label, package, engine, collected):
    case = OUT / label
    case.mkdir(mode=0o700)
    prepare(case)
    # Guard instances remain active; each admits only its case plus supervised
    # foreground creation. Run cases in separate supervisor invocations instead.
    policy = Guard(case, package / 'collector_worker.py')
    report = {'case': label, 'status': 'incomplete', 'realIncus': False, 'allowed': policy.allowed,
              'denied': policy.denied, 'children': [], 'cleanupErrors': []}
    known = {}
    foreground_process = None
    restricted_root = None
    code = 1
    old_env = dict(os.environ)
    os.environ.clear()
    os.environ.update(environment(case))
    request = 'public-'+label
    deadline = time.monotonic()+30
    try:
        with (case/'foreground.stdout').open('wb') as stdout, (case/'foreground.stderr').open('wb') as stderr:
            foreground_process = subprocess.Popen([sys.executable, '-I', '-B', str(SCRIPT), 'foreground', label],
                  cwd=str(ROOT), env=environment(case), stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                  start_new_session=True, close_fds=True)
        fg = identity(foreground_process.pid)
        require(fg['uid']==os.getuid() and fg['session']==fg['pid'])
        known[fg['pid']] = fg
        dispatch_suffix = hashlib.sha256(request.encode()).hexdigest() + '.dispatch'
        wait(lambda: bool(list((case/'admission').glob('*/collectors/'+dispatch_suffix))), deadline)
        found = collected.locate(request)
        require(found is not None)
        root, payload = found
        dispatch_path = collected._input_path(root, request).with_suffix('.dispatch')
        wait(dispatch_path.exists, deadline)
        dispatch = json.loads(dispatch_path.read_text())
        require(dispatch['generation']==payload['generation'] and dispatch['parent_pid']==fg['pid'])
        worker = identity(dispatch['pid'])
        require(worker['ppid']==fg['pid'] and worker['uid']==fg['uid'] and worker['session']==worker['pid'])
        known[worker['pid']] = worker
        policy.signal_pids.update(known)
        wait(lambda: (case/'entered').exists(), deadline)
        require((case/'launch-count').read_text()=='1\n' and (case/'provision-count').read_text()=='1\n')
        from amplifier_bundle_digital_twin_universe import admission
        try:
            engine.exec_command('local:'+request, ['true'])
        except admission.AdmissionError as exc:
            require(exc.code=='admission_busy')
        else:
            raise RuntimeError('Concurrent mutation escaped custody')
        if label == 'observer-loss':
            policy.signal_owned(fg, signal.SIGTERM)
            require(foreground_process.wait(timeout=3)==-signal.SIGTERM)
        elif label == 'cancel':
            result = collected.cancel(request, 'fixture-owner')
            require(result['cancellation_requested'])
        elif label == 'worker-loss':
            # The subreaper discovers/reaps adopted descendants from verified
            # kernel parentage during cleanup, never from child-reported PIDs.
            policy.signal_owned(worker, signal.SIGKILL)
        elif label == 'publication-repair':
            (case/'publication-pause').write_text('owned final-receipt fault\n')
        (case/'release').write_text('release\n')
        if label == 'publication-repair':
            wait(lambda: (case/'publication-entered').exists(), deadline)
            require(root.is_relative_to(case/'admission'))
            root.chmod(0o500)  # Own guest-local admission directory only.
            restricted_root = root
            (case/'publication-release').write_text('release\n')
        guard_path = collected._input_path(root, request).with_suffix('.guard')
        if label != 'worker-loss':
            wait(guard_path.exists, deadline)
            require(not json.loads(guard_path.read_text())['denied'])
        if label != 'observer-loss':
            fg_code = foreground_process.wait(timeout=max(.01,deadline-time.monotonic()))
            require(fg_code == 1, 'Expected public unknown refusal exit')
            response = json.loads((case/'foreground.stdout').read_text().strip())
            require(response.get('error')=='launch_unknown')
            report['foregroundResponse'] = response
        if restricted_root is not None:
            restricted_root.chmod(0o700)
            restricted_root = None
        status = engine.launch_status(request)
        acknowledged = ['creation_returned', 'provisioning_entered', 'provisioning_completed',
                        'checkpoint_saved', 'ready_marker_returned', 'final_receipt_saved']
        if label == 'cancel':
            # The in-flight final provisioning command returns after release.
            # Cooperative cancellation is consumed at the next backend call:
            # the ready marker, after the local prepared checkpoint was saved.
            acknowledged = acknowledged[:4]
        elif label == 'worker-loss':
            acknowledged = acknowledged[:2]
        elif label == 'publication-repair':
            acknowledged = acknowledged[:5]
        require(status['observations']['available'] is True)
        require(status['observations']['acknowledged'] == acknowledged)
        require(status['observations']['creation_transport'] == 'cli')
        require(status['observations']['all_acknowledged'] == (label == 'observer-loss'))
        facts = status['failure_evidence']
        require(facts['available'] is True and facts['cli'] == {'outcome': 'returned', 'returncode': 0})
        require(facts['operation'] is None and facts['cleanup'] is None and facts['receipt_failure'] is None)
        expected_failure = ({'stage': 'ready_marker', 'category': 'cancel_requested'} if label == 'cancel'
                            else {'stage': 'final_receipt', 'category': 'os_error'} if label == 'publication-repair' else None)
        require(facts['primary'] == expected_failure)
        private_evidence = json.loads(collected._input_path(root, request).with_suffix('.evidence').read_bytes())
        for original in private_evidence['baselines'].values():
            require(original is not None and hashlib.sha256(bytes.fromhex(original['bytes_hex'])).hexdigest() == original['sha256'])
        require('baselines' not in facts and 'generation' not in facts and 'binding' not in facts)
        if label != 'cancel':
            require('cancellation_requested' not in status)
            require(collected._outcome(root, request) != 'cancel_requested')
        if label == 'observer-loss':
            require(status['state']=='ready' and status['collector_phase']=='completed')
            receipt = admission.read_receipt(root, request)
            require(status['result']==receipt['result'] and status['result']['id']==request)
            report['exactResult'] = status['result']
            retry = engine.launch(str(case/'profile.yaml'), {}, name=request, request_id=request,
                                 owner_root='fixture-owner', offline=True, collect=True, wait_seconds=0)
            require(retry['reused'])
        elif label == 'publication-repair':
            repair = status['publication_repair']
            require(status['collector_phase'] == 'submitting' and status['result_recovered'])
            require([a['state'] for a in repair['actions']] == ['before', 'before'])
            original = {suffix: collected._input_path(root, request).with_suffix(suffix).read_bytes()
                        for suffix in ('.observations', '.evidence', '.outcome', '.publication')}
            first, second = repair['actions']
            trace_before = (case/'trace.jsonl').read_text().splitlines()
            applied = [collected.repair_record_projection(request, a['action_id'], repair['commit_digest'])
                       for a in (first, second)]
            require([x['outcome'] for x in applied] == ['applied', 'applied'])
            require(collected.repair_record_projection(request, second['action_id'], repair['commit_digest'])['outcome'] == 'already_applied')
            trace_after = (case/'trace.jsonl').read_text().splitlines()
            repair_queries = [json.loads(x)['kind'] for x in trace_after[len(trace_before):]]
            require(repair_queries == ['query'] * 6, 'Repair performed non-identity backend calls')
            require(all(collected._input_path(root, request).with_suffix(s).read_bytes() == raw for s, raw in original.items()))
            repaired = engine.launch_status(request)
            require(repaired['collector_phase'] == 'completed' and repaired['result'] == status['result'])
            report.update(repairActions=applied, repairBackendCalls=repair_queries,
                          preservedHistory=True, repairedStatus=repaired)
        else:
            require(status['state']=='unknown' and 'result' not in status)
            if label == 'cancel':
                require(status['cancellation_requested'])
                require(collected._outcome(root, request)=='cancel_requested', 'Worker did not record consuming cancellation')
            try:
                engine.launch(str(case/'profile.yaml'), {}, name=request, request_id=request,
                              owner_root='fixture-owner', offline=True, collect=True, wait_seconds=0)
            except admission.AdmissionError:
                pass
            else:
                raise RuntimeError('Uncertain request replayed')
        require((case/'launch-count').read_text()=='1\n' and (case/'provision-count').read_text()=='1\n')
        report.update(status='passed', publicStatus=status, noReplay=True, launches=1, provisions=1,
                      installedPackage=str(package), runtime=payload['runtime'], workerParent=worker['ppid'])
        code=0
    except BaseException as exc:
        report['errorType']=type(exc).__name__
        report['errorMessage']=str(exc)[:250]
    finally:
        # Release owned synthetic waits, then reap exactly owned direct/adopted
        # children. Never enumerate/signal unrelated PIDs or whole process groups.
        (case/'release').write_text('cleanup\n')
        (case/'publication-release').write_text('cleanup\n')
        if restricted_root is not None:
            restricted_root.chmod(0o700)
        cleanup_deadline=time.monotonic()+5
        # Discover direct descendants from the exact known process tree, not
        # from names or global process enumeration, before terminating parents.
        def discover():
            roots = {os.getpid(), *known}
            for parent_pid in list(roots):
                try:
                    if parent_pid != os.getpid():
                        now = identity(parent_pid)
                        original = known[parent_pid]
                        if now['start'] != original['start'] or now['uid'] != original['uid']:
                            continue
                    path = Path(f'/proc/{parent_pid}/task/{parent_pid}/children')
                    if not path.exists():
                        continue
                    child_ids = list(map(int, path.read_text().split()))
                    if parent_pid != os.getpid():
                        after = identity(parent_pid)
                        if after['start'] != original['start'] or after['uid'] != original['uid']:
                            continue
                    for child_pid in child_ids:
                        try:
                            if child_pid not in known:
                                child = identity(child_pid)
                                require(child['ppid']==parent_pid and child['uid']==os.getuid())
                                if parent_pid != os.getpid():
                                    after = identity(parent_pid)
                                    if after['start'] != original['start'] or after['uid'] != original['uid']:
                                        continue
                                known[child_pid]=child
                        except FileNotFoundError:
                            continue
                except FileNotFoundError:
                    continue
            policy.signal_pids.update(known)
        try:
            discover()
        except (OSError, RuntimeError) as exc:
            report['cleanupErrors'].append(type(exc).__name__)
        if foreground_process is not None:
            try:
                if foreground_process.poll() is None:
                    policy.signal_pids.add(foreground_process.pid)
                    policy.signal_owned(known[foreground_process.pid], signal.SIGTERM)
                foreground_process.wait(timeout=1)
            except (KeyError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                report['cleanupErrors'].append(type(exc).__name__)
        completed = set()
        while time.monotonic() < cleanup_deadline:
            try:
                discover()
            except (OSError, RuntimeError) as exc:
                report['cleanupErrors'].append(type(exc).__name__)
            for pid, initial in list(known.items()):
                if pid in completed:
                    continue
                try:
                    now = identity(pid)
                    if now['start'] != initial['start'] or now['uid'] != initial['uid']:
                        completed.add(pid)
                        continue
                    try:
                        reaped,_=os.waitpid(pid,os.WNOHANG)
                        if reaped==pid:
                            completed.add(pid)
                            continue
                    except ChildProcessError:
                        pass
                    if time.monotonic() >= cleanup_deadline-2:
                        policy.signal_owned(initial, signal.SIGKILL)
                except FileNotFoundError:
                    completed.add(pid)
                except (OSError, RuntimeError) as exc:
                    report['cleanupErrors'].append(type(exc).__name__)
            own_children = Path(f'/proc/{os.getpid()}/task/{os.getpid()}/children').read_text().split()
            if not own_children and completed == set(known):
                break
            time.sleep(.02)
        for pid, initial in known.items():
            report['children'].append({'initial':initial,'originalAbsent':pid in completed})
        if completed != set(known) or Path(f'/proc/{os.getpid()}/task/{os.getpid()}/children').read_text().split():
            report['cleanupErrors'].append('OwnedChildrenRemain')
        if policy.denied or report['cleanupErrors'] or not known:
            code=1
        if code:
            report['status']='failed'
        report['exitCode']=code
        (case/'result.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
        os.environ.clear()
        os.environ.update(old_env)
    return code


if __name__=='__main__':
    manifest, plan, package, engine, collected, worker = readiness()
    if len(sys.argv)==3 and sys.argv[1]=='foreground' and sys.argv[2] in CASE_NAMES:
        foreground(OUT/sys.argv[2],package)
    elif sys.argv[1:] == ['query-capture']:
        OUT.mkdir(mode=0o700,exist_ok=True)
        raise SystemExit(query_capture_case(package))
    elif len(sys.argv)==2 and sys.argv[1] in CASE_NAMES:
        OUT.mkdir(mode=0o700,exist_ok=True)
        # Linux PR_SET_CHILD_SUBREAPER, current process only. No kernel/global or
        # host setting, PID namespace change, arbitrary ctypes target or signal.
        libc=ctypes.CDLL(None,use_errno=True)
        require(libc.prctl(36,1,0,0,0)==0,'Guest subreaper unavailable')
        raise SystemExit(one_case(sys.argv[1],package,engine,collected))
    else:
        raise RuntimeError('Unsupported exact fixture invocation')
