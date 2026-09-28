"""
One Slack queue for the whole Hestia process (plan section 2, Reporting).

Ported from Prometheus's Slack worker, with the lessons written into it: sends happen off the caller's thread through a
SINGLE worker draining a FIFO queue (so a burst of messages arrives in call order, not raced over the network); every post has
a timeout (a hung call once parked the worker forever and ~300 messages piled up unnoticed for 95 minutes); failures are logged
at once; the queue is bounded (the oldest message is dropped and counted rather than growing without limit); and `flush` lets
the shutdown wait for the worker, because a daemon worker is silently killed with whatever is still queued.

`post(channel, text)` is injectable: the live one lazily builds a slack_sdk WebClient, tests pass a recorder.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Optional

log = logging.getLogger('hestia_slack')


class SlackQueue:

    def __init__(self, token: str = '', post: Optional[Callable[[str, str], None]] = None, timeout_s: float = 10.0,
                 maxsize: int = 1000):
        self._token, self._timeout = token, timeout_s
        self._post = post
        self._q: 'queue.Queue' = queue.Queue(maxsize=maxsize)
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.dropped = 0
        self.sent = 0
        self.failed = 0

    @property
    def enabled(self) -> bool:
        return self._post is not None or bool(self._token)

    def send(self, channel: Optional[str], text: str) -> None:
        if not channel or not self.enabled:
            return
        self._ensure_worker()
        try:
            self._q.put_nowait((channel, text))
        except queue.Full:
            try:
                self._q.get_nowait()                       # drop the oldest, keep the newest
                self._q.task_done()
            except queue.Empty:
                pass
            self.dropped += 1
            log.error('slack queue full: dropped the oldest message (%d dropped so far)', self.dropped)
            try:
                self._q.put_nowait((channel, text))
            except queue.Full:
                self.dropped += 1

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name='hestia-slack', daemon=True)
                self._worker.start()

    def _poster(self) -> Callable[[str, str], None]:
        if self._post is not None:
            return self._post
        from slack_sdk import WebClient                     # imported only when a real send is needed
        client = WebClient(token=self._token, timeout=self._timeout)
        return lambda channel, text: client.chat_postMessage(channel=channel, text=text)

    def _run(self) -> None:
        try:
            post = self._poster()
        except Exception as exc:                            # noqa: BLE001
            log.error('slack unavailable: %r', exc)
            post = None
        while True:
            channel, text = self._q.get()
            try:
                if post is not None:
                    post(channel, text)
                    self.sent += 1
            except Exception as exc:                        # noqa: BLE001
                self.failed += 1
                log.error('slack delivery failed (channel=%s): %s', channel, exc)
            finally:
                self._q.task_done()

    def flush(self, timeout: float = 10.0) -> bool:
        """Block until everything queued has been handed to the poster, up to `timeout`. True if drained."""
        if self._worker is None:
            return True
        done = threading.Event()

        def waiter():
            self._q.join()
            done.set()
        threading.Thread(target=waiter, daemon=True).start()
        return done.wait(timeout)
