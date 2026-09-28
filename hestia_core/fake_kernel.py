"""
Deterministic simulation kernel for the fake/replay Hestia.

Simulated time only moves when the scheduler pops the next scheduled callback; nothing here sleeps or reads the wall
clock. Engines run on real threads (they are written as ordinary blocking code against EngineContext) but only ever one
thread runs at a time: the scheduler hands the baton to an engine task, the task runs until it blocks in `next_event` or
`wait`, and hands the baton back. That makes every run bit-for-bit repeatable while still exercising the real blocking
call shapes an engine will use against the live Hestia.

Every handoff waits with a wall-clock timeout (WALL_TIMEOUT_S) and raises FakeHestiaDeadlock when it expires, so an engine
that spins or sleeps on the real clock fails the test in seconds instead of hanging the suite.
"""

import heapq
import itertools
import threading
import traceback
from collections import deque
from datetime import datetime, timedelta
from typing import Callable, Optional

WALL_TIMEOUT_S = 20.0


class FakeHestiaDeadlock(RuntimeError):
    """An engine thread failed to hand control back within WALL_TIMEOUT_S of real time."""


class TaskAborted(BaseException):
    """Raised inside an engine thread to unwind it when the fake is closed (BaseException so engines cannot swallow it)."""


class Handle:
    __slots__ = ('cancelled',)

    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class SimKernel:
    def __init__(self, start: datetime):
        self.now = start
        self._heap = []
        self._seq = itertools.count()
        self.max_same_time = 10_000
        self.max_callbacks = 1_000_000

    def at(self, when: datetime, fn: Callable[[], None]) -> Handle:
        h = Handle()
        heapq.heappush(self._heap, (max(when, self.now), next(self._seq), h, fn))
        return h

    def after(self, seconds: float, fn: Callable[[], None]) -> Handle:
        return self.at(self.now + timedelta(seconds=seconds), fn)

    def post(self, fn: Callable[[], None]) -> Handle:
        """Run `fn` on the dispatcher as soon as possible. The live reactor's version is thread-safe (worker threads post
        their completions with it); this one is single-threaded and is used with an inline executor."""
        return self.after(0, fn)

    def run_until(self, t_end: datetime) -> None:
        """Runs callbacks up to `t_end`. Two budgets turn a runaway engine into a failure instead of a hang: at most
        `max_same_time` callbacks at one simulated instant (a loop that never advances time) and `max_callbacks` in one
        call (a loop that advances time but never stops, e.g. an engine re-submitting on every outcome)."""
        count, at_now = 0, 0
        while self._heap and self._heap[0][0] <= t_end:
            when, _, h, fn = heapq.heappop(self._heap)
            if h.cancelled:
                continue
            at_now = at_now + 1 if when == self.now else 1
            count += 1
            if at_now > self.max_same_time or count > self.max_callbacks:
                raise FakeHestiaDeadlock(
                    f'runaway simulation at {when}: {at_now} callbacks at this instant, {count} in this run '
                    f'(budgets {self.max_same_time}/{self.max_callbacks}); an engine or a scenario is looping')
            self.now = when
            fn()
        if self.now < t_end:
            self.now = t_end

    def run_for(self, seconds: float) -> None:
        self.run_until(self.now + timedelta(seconds=seconds))

    def pending(self) -> int:
        return sum(1 for e in self._heap if not e[2].cancelled)


class EngineTask:
    """One engine instance on its own thread, driven by baton passing. `owner` supplies `kernel`, `_on_task_done`."""

    def __init__(self, owner, name: str, engine, generation: int, ctx_factory):
        self.owner = owner
        self.kernel: SimKernel = owner.kernel
        self.name = name
        self.engine = engine
        self.generation = generation
        self.queue: deque = deque()
        self.state = 'new'                 # new | running | blocked | done
        self.error: Optional[BaseException] = None
        self.error_trace = ''
        self.returned = False
        self.aborted = False
        self.waiting = False               # blocked inside next_event (an event may wake it early)
        self.wake: Optional[Handle] = None
        self._wake_scheduled = False
        self.last_heartbeat = self.kernel.now
        self.stop_delivered = False
        self.trading_token: Optional[str] = None
        self.tracked: set = set()
        self.silence_level = 0
        self.last_critical_ts: Optional[datetime] = None
        self._resume = threading.Semaphore(0)
        self._yielded = threading.Semaphore(0)
        self.ctx = ctx_factory(self)
        self.thread = threading.Thread(target=self._main, name=f'fake-engine-{name}-{generation}', daemon=True)

    # -- scheduler side -----------------------------------------------------------------------------------------
    def start(self) -> None:
        self.thread.start()
        self.enter()

    def enter(self) -> None:
        if self.state == 'done':
            return
        self.state = 'running'
        self._resume.release()
        if not self._yielded.acquire(timeout=WALL_TIMEOUT_S):
            raise FakeHestiaDeadlock(f'engine {self.name} did not yield within {WALL_TIMEOUT_S}s of real time '
                                     f'(sim now={self.kernel.now}); does it sleep or spin on the real clock?')
        if self.state == 'done':
            self.owner._on_task_done(self)

    def abort(self) -> None:
        if self.state in ('done', 'new'):
            self.state = 'done'
            return
        self.aborted = True
        self._resume.release()
        self._yielded.acquire(timeout=WALL_TIMEOUT_S)

    def deliver(self, event) -> None:
        self.queue.append(event)
        if self.waiting and not self._wake_scheduled:
            self._wake_scheduled = True
            self.kernel.after(0, self._on_event_wake)

    def _on_event_wake(self) -> None:
        self._wake_scheduled = False
        if self.waiting and self.state == 'blocked' and self.queue:
            self.enter()

    def _on_timer(self) -> None:
        self.wake = None
        if self.state == 'blocked':
            self.enter()

    # -- engine side --------------------------------------------------------------------------------------------
    def _main(self) -> None:
        self._resume.acquire()
        try:
            if not self.aborted:
                self.engine.run(self.ctx)
                self.returned = True
        except TaskAborted:
            pass
        except BaseException as exc:            # noqa: BLE001 - any engine failure is a crash Hestia must contain
            self.error = exc
            self.error_trace = traceback.format_exc()
        finally:
            self.state = 'done'
            self._yielded.release()

    def _block(self) -> None:
        self.state = 'blocked'
        self._yielded.release()
        self._resume.acquire()
        if self.aborted:
            raise TaskAborted()

    def next_event(self, timeout: float):
        self.last_heartbeat = self.kernel.now
        if self.queue:
            return self.queue.popleft()
        self.waiting = True
        self.wake = self.kernel.after(timeout, self._on_timer)
        self._block()
        self.waiting = False
        if self.wake is not None:
            self.wake.cancel()
            self.wake = None
        self.last_heartbeat = self.kernel.now
        return self.queue.popleft() if self.queue else None

    def wait(self, seconds: float) -> None:
        self.wake = self.kernel.after(seconds, self._on_timer)
        self._block()
        self.wake = None
