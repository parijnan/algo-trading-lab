"""
The live scheduler: a real-time reactor (plans/hestia-p4-live-services.md, slice 3).

One dispatcher thread runs every scheduled callback, one at a time, while holding `lock` (the core lock). That is what makes
the core single-threaded in the live Hestia exactly as in the fake: blocking work (broker HTTP) runs on executor threads and
reports back with `post`, which is safe to call from any thread, and engine threads take the same lock for the duration of
each context call (thread_runner.LockedContext). A callback that raises is logged and reported to `on_error`; it never kills
the dispatcher, because a dead dispatcher would silently stop every engine's orders and alerts.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
from datetime import datetime, timedelta
from typing import Callable, Optional

from hestia_core.fake_kernel import Handle

log = logging.getLogger('hestia_reactor')


class RealReactor:

    def __init__(self, clock: Callable[[], datetime] = datetime.now,
                 on_error: Optional[Callable[[BaseException], None]] = None):
        self._clock = clock
        self.on_error = on_error or (lambda exc: log.error('scheduled callback failed: %r', exc, exc_info=exc))
        self.lock = threading.RLock()                       # the core lock: held while any callback or context call runs
        self._cv = threading.Condition()
        self._heap: list = []
        self._seq = itertools.count()
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self.callbacks_run = 0

    @property
    def now(self) -> datetime:
        return self._clock()

    def at(self, when: datetime, fn: Callable[[], None]) -> Handle:
        h = Handle()
        with self._cv:
            heapq.heappush(self._heap, (when, next(self._seq), h, fn))
            self._cv.notify()
        return h

    def after(self, seconds: float, fn: Callable[[], None]) -> Handle:
        return self.at(self._clock() + timedelta(seconds=seconds), fn)

    def post(self, fn: Callable[[], None]) -> Handle:
        return self.after(0, fn)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name='hestia-reactor', daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while True:
            with self._cv:
                while True:
                    if self._stop:
                        return
                    now = self._clock()
                    if self._heap and self._heap[0][0] <= now:
                        _, _, handle, fn = heapq.heappop(self._heap)
                        break
                    wait = (self._heap[0][0] - now).total_seconds() if self._heap else None
                    self._cv.wait(wait)
            if handle.cancelled:
                continue
            with self.lock:
                try:
                    fn()
                except BaseException as exc:                # noqa: BLE001 - the dispatcher must survive any callback
                    try:
                        self.on_error(exc)
                    except Exception:                       # noqa: BLE001
                        log.exception('on_error failed')
                self.callbacks_run += 1
