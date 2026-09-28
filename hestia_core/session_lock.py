"""
The Angel One session lock (plan section 2).

One account, one trading session: a second `generateSession` from anywhere evicts the first session's order capability
(AB1007), even in DRY_RUN. Whichever process owns the login claims this lock file; every other place that logs in (Leto if it is
ever re-enabled, the 23:56 data downloader, one-off scripts) calls `refuse_if_held` first and does not log in while a live
process holds it. This generalises today's guards, which only read strategy state files and run after the login that already
did the damage.

The lock records the owner's pid. A lock whose pid is no longer alive is stale and is reclaimed. `takeover` is for the owner's
own restart: it SIGTERMs the previous holder and polls until that pid is really gone (a fixed sleep once let a slow shutdown
delete the new process's files, 2026-09-11) before claiming.
"""

from __future__ import annotations

import json
import os
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional


class LockHeld(Exception):
    def __init__(self, holder: dict):
        super().__init__(f"the Angel One session is held by {holder.get('owner')} (pid {holder.get('pid')}, since {holder.get('since')})")
        self.holder = holder


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                                 # exists, owned by someone else: treat as not ours, so not a live holder
        return False
    return True


def wait_for_pid_exit(pid: int, timeout: float = 30.0, poll: float = 0.3,
                      sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> bool:
    deadline = clock() + timeout
    while clock() < deadline:
        if not pid_alive(pid):
            return True
        sleep(poll)
    return False


def cmdline_of(pid: int) -> str:
    try:
        return Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
    except OSError:
        return ''


def looks_like(pid: int, needle: str) -> bool:
    """Is `pid` a process whose command line mentions `needle`? Pids are reused, and a stale lock must never make us signal an
    unrelated live process (standalone Prometheus runs as the same user)."""
    return needle in cmdline_of(pid)


def holder(path: os.PathLike) -> Optional[dict]:
    """The live holder of the lock, or None (no file, unreadable file, or a dead pid)."""
    p = Path(path)
    try:
        info = json.loads(p.read_text())
        pid = int(info['pid'])
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
        return None
    return info if pid_alive(pid) else None


def refuse_if_held(path: os.PathLike, who: str) -> None:
    """For any other login site: raise LockHeld instead of logging in while a live process owns the session."""
    h = holder(path)
    if h is not None and int(h['pid']) != os.getpid():
        raise LockHeld(h)


class SessionLock:

    def __init__(self, path: os.PathLike, owner: str = 'hestia', pid: Optional[int] = None):
        self.path, self.owner = Path(path), owner
        self.pid = pid if pid is not None else os.getpid()
        self.claimed = False

    def claim(self) -> None:
        h = holder(self.path)
        if h is not None and int(h['pid']) != self.pid:
            raise LockHeld(h)
        self._write()

    def takeover(self, timeout: float = 30.0, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> Optional[int]:
        """SIGTERM the live holder (only if its command line says hestia), wait until it is really gone (SIGKILL after `timeout`),
        then claim. Returns the pid that was replaced."""
        h = holder(self.path)
        old = None
        if h is not None and int(h['pid']) != self.pid and not looks_like(int(h['pid']), 'hestia'):
            h = None                                         # a reused pid, not a Hestia process: the lock is stale, signal nothing
        if h is not None and int(h['pid']) != self.pid:
            old = int(h['pid'])
            try:
                os.kill(old, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if not wait_for_pid_exit(old, timeout, sleep=sleep, clock=clock):
                try:
                    os.kill(old, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                wait_for_pid_exit(old, 5.0, sleep=sleep, clock=clock)
        self._write()
        return old

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'pid': self.pid, 'owner': self.owner, 'since': datetime.now().isoformat(timespec='seconds')}))
        os.replace(tmp, self.path)
        self.claimed = True

    def release(self) -> None:
        """Remove the lock only if it is still ours (a successor may already have replaced it)."""
        try:
            info = json.loads(self.path.read_text())
            if int(info.get('pid', -1)) == self.pid:
                self.path.unlink()
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            pass
        self.claimed = False
