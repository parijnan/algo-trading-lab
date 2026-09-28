"""
Session lifecycle for the live Hestia: signals and the teardown order (plans/hestia-p4-live-services.md, slice 3).

Teardown, in this order and no other:
  1. tell every engine to stop (Stop event; no order is sent, positions stay open) and stop bringing crashed engines back;
  2. wait for every engine thread to finish, up to a timeout;
  3. let requests already at the broker finish (a KILL or a stop never cancels an in-flight order), and report anything
     left UNCONFIRMED, because its position is unknown to whoever reads the alert;
  4. flush the Slack queue;
  5. terminateSession, exactly once, and only if every engine thread finished. A hung engine could still be about to place an
     order, so with one alive the session is left open and a critical alert says so (it expires at midnight, and the next
     login supersedes it), unless `terminate_despite_hung` is set;
  6. stop the reactor and the worker pool.
An engine crash never reaches any of this: crash containment and auto-resume are the core's, the session stays up.

Signals are handled in the main thread and only set a flag; the teardown itself runs on the main thread, not in the handler.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional

from hestia_core.interface import StopReason

log = logging.getLogger('hestia_lifecycle')


@dataclass
class LifecycleConfig:
    engine_join_timeout_s: float = 60.0
    drain_timeout_s: float = 60.0
    flush_timeout_s: float = 10.0
    terminate_despite_hung: bool = False
    poll_s: float = 0.05


@dataclass
class TeardownReport:
    reason: StopReason
    engines_hung: List[str] = field(default_factory=list)
    unconfirmed: List[tuple] = field(default_factory=list)
    drained: bool = True
    flushed: bool = True
    terminated: bool = False
    steps: List[str] = field(default_factory=list)


class Lifecycle:

    def __init__(self, core, reactor, executor, terminate: Callable[[], None], flush: Callable[[float], bool],
                 cfg: Optional[LifecycleConfig] = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, before_flush: Optional[Callable[[], None]] = None):
        self.core, self.reactor, self.executor = core, reactor, executor
        self._terminate, self._flush = terminate, flush
        self._before_flush = before_flush                            # e.g. queue the session report, so the flush carries it
        self.cfg = cfg or LifecycleConfig()
        self._sleep, self._clock = sleep, clock
        self._shutdown = threading.Event()
        self._reason: Optional[StopReason] = None
        self._teardown_lock = threading.Lock()
        self._report: Optional[TeardownReport] = None
        self._old_handlers: dict = {}

    # ---- signals ---------------------------------------------------------------------------------------------------

    def install_signals(self, signums=(signal.SIGINT, signal.SIGTERM)) -> bool:
        """Install handlers; only possible in the main thread (returns False elsewhere)."""
        if threading.current_thread() is not threading.main_thread():
            return False
        for s in signums:
            self._old_handlers[s] = signal.signal(s, self.handle_signal)
        return True

    def restore_signals(self) -> None:
        if threading.current_thread() is threading.main_thread():
            for s, h in self._old_handlers.items():
                signal.signal(s, h)
        self._old_handlers.clear()

    def handle_signal(self, signum, frame=None) -> None:
        self.request_shutdown(StopReason.SHUTDOWN)          # flag only: never do the teardown inside a signal handler

    def request_shutdown(self, reason: StopReason = StopReason.SHUTDOWN) -> None:
        if self._reason is None:
            self._reason = reason
        self._shutdown.set()

    def run_until_shutdown(self, session_end: Optional[datetime] = None, poll_s: float = 0.5) -> StopReason:
        """Block the main thread until a signal or shutdown request arrives or `session_end` passes; return why."""
        while not self._shutdown.wait(poll_s):
            if session_end is not None and self.reactor.now >= session_end:
                self.request_shutdown(StopReason.SESSION_END)
        return self._reason or StopReason.SHUTDOWN

    # ---- teardown --------------------------------------------------------------------------------------------------

    def _alert(self, level: str, text: str) -> None:
        with self.reactor.lock:
            self.core._alert(level, None, text)

    def teardown(self, reason: Optional[StopReason] = None) -> TeardownReport:
        with self._teardown_lock:
            if self._report is not None:
                return self._report
            report = TeardownReport(reason or self._reason or StopReason.SESSION_END)
            cfg = self.cfg

            with self.reactor.lock:                                                     # 1. stop the engines
                self.core.stop_all(report.reason, leave_position=True)
            report.steps.append('stop_engines')

            deadline = self._clock() + cfg.engine_join_timeout_s                        # 2. wait for their threads
            while True:
                with self.reactor.lock:
                    alive = [n for n, t in self.core.tasks_snapshot().items() if t.state != 'done']
                if not alive or self._clock() >= deadline:
                    break
                self._sleep(cfg.poll_s)
            deadline = self._clock() + 2.0                                              # a finished thread reports to the core
            while self._clock() < deadline:                                             # on the dispatcher: let those land
                with self.reactor.lock:
                    pending = [n for n, t in self.core.tasks_snapshot().items()
                               if t.state == 'done' and self.core.engine_state.get(n) == 'running' and not t.aborted
                               and t.error is None]
                if not pending:
                    break
                self._sleep(cfg.poll_s)
            report.engines_hung = alive
            report.steps.append('engines_finished' if not alive else 'engines_hung')
            if alive:
                self._alert('critical', f'engine(s) {", ".join(alive)} did not stop within {cfg.engine_join_timeout_s:.0f}s')

            deadline = self._clock() + cfg.drain_timeout_s                              # 3. let in-flight requests finish
            while True:
                with self.reactor.lock:
                    busy = self.core.in_flight_count()
                if not busy or self._clock() >= deadline:
                    break
                self._sleep(cfg.poll_s)
            with self.reactor.lock:
                report.unconfirmed = self.core.unconfirmed_requests()
            report.drained = not busy
            report.steps.append('drained' if report.drained else 'drain_timeout')
            if not report.drained:
                self._alert('critical', f'{busy} request(s) were still in flight at shutdown')
            if report.unconfirmed:
                self._alert('critical', f'requests left UNCONFIRMED at shutdown (position unknown, check the broker): '
                                        f'{report.unconfirmed}')

            if self._before_flush is not None:
                try:
                    self._before_flush()
                except Exception:                                                       # noqa: BLE001
                    log.exception('before_flush failed')
            try:                                                                        # 4. flush Slack
                report.flushed = bool(self._flush(cfg.flush_timeout_s))
            except Exception:                                                           # noqa: BLE001
                log.exception('flush failed')
                report.flushed = False
            report.steps.append('flushed')

            if report.engines_hung and not cfg.terminate_despite_hung:                  # 5. terminate, only if all finished
                self._alert('critical', 'session NOT terminated: an engine thread is still alive and could still place orders')
                report.steps.append('terminate_skipped')
            else:
                try:
                    self._terminate()
                    report.terminated = True
                except Exception as exc:                                                # noqa: BLE001
                    self._alert('critical', f'terminateSession failed: {exc!r}')
                report.steps.append('terminated')

            self.reactor.stop()                                                         # 6. stop the machinery
            try:
                self.executor.shutdown(wait=False)
            except Exception:                                                           # noqa: BLE001
                pass
            report.steps.append('reactor_stopped')
            self._report = report
            return report
