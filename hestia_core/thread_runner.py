"""
Engine threads for the live Hestia: the ThreadTask the core launches (the counterpart of fake_kernel.EngineTask, which passes
a baton in simulated time) and the LockedContext that lets an engine thread call the core safely.

An engine runs on its own thread, blocking in `next_event` / `wait` in real time. Every other context call takes the core
lock for its duration, the same lock the reactor holds while it runs a callback, so an engine thread and the dispatcher never
touch core state at once. Events are delivered on a queue; delivery never blocks. When the thread ends, normally or by an
exception, the owner is told on the dispatcher (`_on_task_done`), which is where crash containment and auto-resume live.
A hung engine cannot be killed from outside: `abort` only wakes one that is blocked in `next_event` / `wait`.
"""

from __future__ import annotations

import queue
import threading
import traceback
from typing import Optional

from hestia_core.fake_kernel import TaskAborted

_ABORT = object()


class LockedContext:
    """Wraps a CoreContext so each call runs under the core lock, except the blocking calls and the clock."""
    _UNLOCKED = frozenset({'next_event', 'wait', 'now'})

    def __init__(self, ctx, lock):
        self._ctx, self._lock = ctx, lock

    def __getattr__(self, name):
        attr = getattr(self._ctx, name)
        if not callable(attr) or name in self._UNLOCKED:
            return attr

        def locked(*args, **kwargs):
            with self._lock:
                return attr(*args, **kwargs)
        return locked


class ThreadTask:

    def __init__(self, owner, name: str, engine, generation: int, ctx_factory):
        self.owner = owner
        self.reactor = owner.kernel
        self.name, self.engine, self.generation = name, engine, generation
        self.state = 'new'                                   # new | running | done
        self.error: Optional[BaseException] = None
        self.error_trace = ''
        self.returned = False
        self.aborted = False
        self.stop_delivered = False
        self.trading_token: Optional[str] = None
        self.tracked: set = set()
        self.silence_level = 0
        self.last_critical_ts = None
        self.last_heartbeat = self.reactor.now
        self._q: queue.Queue = queue.Queue()
        self._abort_evt = threading.Event()
        self.ctx = LockedContext(ctx_factory(self), self.reactor.lock)
        self.thread = threading.Thread(target=self._main, name=f'hestia-engine-{name}-{generation}', daemon=True)

    # -- the core's side ----------------------------------------------------------------------------------------------
    def start(self) -> None:
        self.state = 'running'
        self.thread.start()

    def deliver(self, event) -> None:
        self._q.put(event)

    def abort(self) -> None:
        self.aborted = True
        self._abort_evt.set()
        self._q.put(_ABORT)

    # -- the engine thread's side --------------------------------------------------------------------------------------
    def _main(self) -> None:
        try:
            if not self.aborted:
                self.engine.run(self.ctx)
                self.returned = True
        except TaskAborted:
            pass
        except BaseException as exc:                         # noqa: BLE001 - any engine failure is a crash Hestia must contain
            self.error = exc
            self.error_trace = traceback.format_exc()
        finally:
            self.state = 'done'
            if not self.aborted:
                self.reactor.post(lambda: self.owner._on_task_done(self))

    def next_event(self, timeout: float):
        self.last_heartbeat = self.reactor.now
        try:
            ev = self._q.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            self.last_heartbeat = self.reactor.now
            return None
        if ev is _ABORT:
            raise TaskAborted()
        self.last_heartbeat = self.reactor.now
        return ev

    def wait(self, seconds: float) -> None:
        if self._abort_evt.wait(max(seconds, 0.0)):
            raise TaskAborted()
