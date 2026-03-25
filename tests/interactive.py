# Copyright (c) Microsoft. All rights reserved.

"""Interactive subprocess driver for E2E testing.

Provides InteractiveSession: start a long-running subprocess with a PTY,
send lines to stdin, and wait for regex patterns in stdout -- useful for
testing interactive CLI applications like ``amplifier-digital-twin exec``
or ``amplifier``.

A PTY (pseudo-terminal) is used instead of bare pipes so that the child
process (and everything it spawns, e.g. ``incus exec --force-interactive``)
sees a real terminal.  Without a PTY, interactive programs like
prompt_toolkit refuse to read from stdin or relay input incorrectly.
"""

import os
import pty
import re
import signal
import subprocess
import sys
import time
from threading import Lock, Thread

_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]"  # CSI sequences (colours, cursor, etc.)
    r"|\x1b\].*?(?:\x07|\x1b\\)"  # OSC sequences
    r"|\x1b[()][A-B0-2]"  # character set selection
    r"|\x08"  # backspace (BS)
    r"|\r"  # carriage return
)


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences, backspace, and CR from *text*."""
    return _ANSI_RE.sub("", text)


class InteractiveSession:
    """Drive an interactive subprocess over a PTY.

    A background thread continuously reads the PTY master so the pipe never
    blocks.  ``wait_for`` scans the accumulated buffer for a regex match.
    """

    def __init__(self, *cmd: str):
        self._cmd = cmd

        # Allocate a PTY so the subprocess chain gets a real terminal.
        master_fd, slave_fd = pty.openpty()

        self._proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            # Start in a new process group so we can signal the whole tree.
            preexec_fn=os.setsid,
        )
        os.close(slave_fd)  # Only the child uses the slave side.

        self._master_fd = master_fd
        self._buf = ""
        self._lock = Lock()
        self._reader = Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # -- internal ----------------------------------------------------------

    def _read_loop(self) -> None:
        """Continuously read PTY master into the shared buffer."""
        while True:
            try:
                chunk = os.read(self._master_fd, 4096)
            except OSError:
                # PTY closed (child exited or master fd closed).
                break
            if not chunk:
                break
            with self._lock:
                self._buf += chunk.decode("utf-8", errors="replace")

    # -- public API --------------------------------------------------------

    def send(self, text: str) -> None:
        """Write *text* to the subprocess via the PTY master."""
        print(f"[interactive] send: {text.rstrip()!r}", file=sys.stderr)
        os.write(self._master_fd, text.encode())

    def wait_for(self, pattern: str, timeout: int = 120) -> str:
        """Block until *pattern* appears in accumulated output.

        ANSI escape sequences are stripped before matching so callers can
        use simple patterns like ``r'>\\s*$'`` without worrying about
        terminal colour codes or cursor movement sequences.

        Returns all output captured *before* the match (ANSI-stripped).
        Raises ``TimeoutError`` if *timeout* seconds elapse without a match.
        """
        deadline = time.monotonic() + timeout
        regex = re.compile(pattern)
        while time.monotonic() < deadline:
            with self._lock:
                clean = _strip_ansi(self._buf)
                m = regex.search(clean)
                if m:
                    captured = clean[: m.end()]
                    self._buf = ""
                    return captured
            time.sleep(0.1)
        # Timeout -- dump what we have for debugging
        with self._lock:
            have = _strip_ansi(self._buf)
        raise TimeoutError(
            f"Pattern {pattern!r} not found after {timeout}s.\n"
            f"Buffer ({len(have)} chars):\n{have[-2000:]}"
        )

    def close(self, timeout: int = 10) -> int:
        """Close the PTY master, wait for process exit, return exit code."""
        try:
            os.close(self._master_fd)
        except OSError:
            pass
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Kill the whole process group.
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                self._proc.kill()
            self._proc.wait(timeout=5)
        return self._proc.returncode

    @property
    def all_output(self) -> str:
        """Return all captured output so far (non-destructive)."""
        with self._lock:
            return self._buf
