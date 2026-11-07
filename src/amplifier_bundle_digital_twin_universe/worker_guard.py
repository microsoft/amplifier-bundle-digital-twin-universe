# Copyright (c) Microsoft. All rights reserved.
"""Narrow effect guard for the bounded collected-worker subset.

This supplements the caller's OS isolation; Python audit is not a security
sandbox for hostile native code. It never grants permission to a backend.
"""
import os
import signal
import socket
import subprocess
import sys
from threading import local

from . import admission
from .bound_backend import _environment, BackendFence


def install(backend, report_path):
    record = backend.as_dict()
    expected_env = _environment(record)
    counts, denied = {}, []
    termination = local()
    blocked = {"os.system", "os.posix_spawn", "os.fork", "os.forkpty", "os.exec",
               "os.kill", "os.killpg", "signal.pthread_kill", "signal.delivery",
               "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo",
               "socket.bind", "socket.sendto", "socket.sendmsg"}

    def audit(event, args):
        permit = False
        if event == "os.kill":
            permit = args == getattr(termination, "permit", None)
        elif event == "subprocess.Popen":
            executable, argv, cwd, env = args
            permit = (executable == record["executable"]["path"] and isinstance(argv, list)
                      and argv[0] == executable and env == expected_env and cwd is None)
            if permit:
                termination.construction = (executable, tuple(argv), dict(env))
        elif event == "os.posix_spawn":
            permit = args == getattr(termination, "spawn", None)
            termination.spawn = None
        elif event == "socket.__new__":
            permit = args[1:3] == (socket.AF_UNIX, socket.SOCK_STREAM)
        elif event == "socket.connect":
            permit = args[1] == record["socket"]
        elif event not in blocked:
            return
        if permit:
            counts[event] = counts.get(event, 0) + 1
        else:
            if len(denied) < 64:
                denied.append(event)
            raise BackendFence("Collected worker attempted an unbound effect")

    sys.addaudithook(audit)
    original_popen = subprocess.Popen

    class OwnedPopen(original_popen):
        def __init__(self, *args, **kwargs):
            termination.construction = None
            try:
                super().__init__(*args, **kwargs)
            finally:
                termination.construction = None
                termination.spawn = None

        def _posix_spawn(self, args, executable, env, *rest, **kwargs):
            # Python 3.13 may implement the approved Popen with posix_spawn.
            # Permit its exact native event only inside this constructor path.
            if (executable, tuple(args), env) != getattr(termination, "construction", None):
                raise BackendFence("Unbound native child construction")
            termination.construction = None
            termination.spawn = (executable, args, env)
            try:
                return super()._posix_spawn(args, executable, env, *rest, **kwargs)
            finally:
                termination.spawn = None

        def send_signal(self, sig):
            # subprocess.run(timeout=...) must be able to terminate its exact
            # owned, unreaped child. Never authorize arbitrary PID/group calls.
            if self.poll() is not None:
                return
            if sig not in (signal.SIGTERM, signal.SIGKILL):
                raise BackendFence("Unsupported owned-child signal")
            termination.permit = (self.pid, sig)
            try:
                return super().send_signal(sig)
            finally:
                termination.permit = None

    subprocess.Popen = OwnedPopen

    def deny_signal(*args, **kwargs):
        audit("signal.delivery", ())

    for name in ("raise_signal", "pthread_kill", "pidfd_send_signal"):
        if hasattr(signal, name):
            setattr(signal, name, deny_signal)

    def retain():
        admission._save_private_json(report_path, {"version": 1, "pid": os.getpid(),
                                                   "allowed_counts": counts, "denied": denied})
    return retain
