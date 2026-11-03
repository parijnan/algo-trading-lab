"""
Raw websocket tick log: every message Angel One's LTP feed delivers, written to disk as it arrives, for studying what the engines can see.

Why: the engines read one price a second and the minute bars hide how many prints, and how fast, a stop or target level is touched. The feed
already keeps the last price and a tick window; nothing kept the messages themselves. This keeps them (receive time, token, price, the
exchange's own timestamp, the sequence number), so message rate, price changes per second, exchange-to-receipt latency and dropped messages
(sequence gaps) can be measured per instrument.

Hot-path cost: the websocket thread only appends one tuple to a bounded deque (`put`, about 0.3 microseconds); a daemon writer thread empties
the deque about once a second, formats the lines and writes them. Nothing here can block, slow or stop the feed or an engine:
  - the deque is bounded: when the writer cannot keep up, new ticks are COUNTED and dropped, never queued without limit;
  - every writer failure (a full disk, a permission error) is caught, alerted once an hour and survived;
  - `close` flushes and joins with a short timeout and is called after the feeds have stopped.
The kill flag (touch it, no restart) stops recording within a second and resumes when the flag is removed.

Files, under the directory given (hestia_data/ticks/):
  ticks_<YYYY-MM-DD>.csv    recv_ms,token,ltp,exch_ms,seq,exch_type    one row per message, appended; the header is written once per file
  tokens_<YYYY-MM-DD>.csv   token,instrument,symbol,expiry             the first time a token is seen that day, so a later join never has to
                                                                       guess what a token meant on that day (the instrument master rolls)
`exch_ms` is the exchange timestamp the message carries (epoch milliseconds, as received); `seq` its sequence number; both are written raw.
LTP mode carries no quantity: the log shows messages and price changes, not trades or lots.
"""

from __future__ import annotations

import collections
import logging
import os
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, Optional

log = logging.getLogger('hestia_tick_log')

TICK_HEADER = 'recv_ms,token,ltp,exch_ms,seq,exch_type\n'
TOKEN_HEADER = 'token,instrument,symbol,expiry\n'
ALERT_EVERY_S = 3600.0          # one writer-failure alert an hour, however often it fails
DRAIN_BATCH = 50_000            # most rows written per drain, so one slow disk call never holds a long list


class TickLog:

    def __init__(self, directory: os.PathLike, *, flush_s: float = 1.0, max_buffered: int = 200_000, off_flag: Optional[os.PathLike] = None,
                 symbol_of: Optional[Callable[[str], Optional[Dict[str, str]]]] = None, alert: Optional[Callable[[str], None]] = None,
                 today: Callable[[float], date] = lambda ts: datetime.fromtimestamp(ts).date(), monotonic: Callable[[], float] = time.monotonic):
        self.dir = Path(directory)
        self.flush_s, self.max_buffered = flush_s, max_buffered
        self.off_flag = Path(off_flag) if off_flag else None
        self.symbol_of, self.alert, self._today, self._mono = symbol_of, alert, today, monotonic
        self._buf: collections.deque = collections.deque()
        self._on = True                                  # the kill flag's state, refreshed by the writer, read by `put`
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._file_date: Optional[date] = None
        self._tick_fh = None
        self._seen_tokens: set = set()
        self._last_alert = -ALERT_EVERY_S
        self.received = self.written = self.dropped = self.skipped = self.write_errors = 0

    # ---- the websocket thread -------------------------------------------------------------------------------------------------

    def put(self, recv_ts: float, token, ltp: float, exch_ts, seq, exch_type) -> None:
        if not self._on:
            self.skipped += 1
            return
        self.received += 1
        if len(self._buf) >= self.max_buffered:
            self.dropped += 1
            return
        self._buf.append((recv_ts, token, ltp, exch_ts, seq, exch_type))

    # ---- the writer thread ---------------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name='hestia-tick-log', daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.flush_s):
            self.drain()

    def drain(self) -> int:
        """Write what has arrived. Never raises. Returns the rows written."""
        try:
            self._on = not (self.off_flag is not None and self.off_flag.exists())
            n = 0
            while self._buf and n < DRAIN_BATCH:
                rows = []
                while self._buf and len(rows) < 5000:
                    rows.append(self._buf.popleft())
                self._write(rows)
                n += len(rows)
            if n and self._tick_fh is not None:
                self._tick_fh.flush()
            self.written += n
            return n
        except Exception as exc:                          # a full disk or a permission error must never reach the feed or an engine
            self.write_errors += 1
            self._failed(exc)
            return 0

    def _write(self, rows) -> None:
        by_file: Dict[date, list] = {}
        for r in rows:
            by_file.setdefault(self._today(r[0]), []).append(r)
        for day in sorted(by_file):
            self._open(day)
            lines = []
            for recv_ts, token, ltp, exch_ts, seq, exch_type in by_file[day]:
                if token not in self._seen_tokens:
                    self._note_token(day, token)
                lines.append(f"{int(recv_ts * 1000)},{token},{ltp},{'' if exch_ts is None else exch_ts},{'' if seq is None else seq},"
                             f"{'' if exch_type is None else exch_type}\n")
            self._tick_fh.write(''.join(lines))

    def _open(self, day: date) -> None:
        if self._file_date == day and self._tick_fh is not None:
            return
        if self._tick_fh is not None:
            self._tick_fh.close()
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f'ticks_{day.isoformat()}.csv'
        new = not path.exists() or path.stat().st_size == 0
        self._tick_fh = open(path, 'a', buffering=1 << 16)
        if new:
            self._tick_fh.write(TICK_HEADER)
        self._file_date = day
        self._seen_tokens = set()                         # every day's token file lists the tokens seen that day

    def _note_token(self, day: date, token: str) -> None:
        self._seen_tokens.add(token)
        info = None
        if self.symbol_of is not None:
            try:
                info = self.symbol_of(token)
            except Exception:
                info = None
        path = self.dir / f'tokens_{day.isoformat()}.csv'
        new = not path.exists() or path.stat().st_size == 0
        with open(path, 'a') as fh:
            if new:
                fh.write(TOKEN_HEADER)
            info = info or {}
            fh.write(f"{token},{info.get('instrument', '')},{info.get('symbol', '')},{info.get('expiry', '')}\n")

    def _failed(self, exc: Exception) -> None:
        now = self._mono()
        if now - self._last_alert >= ALERT_EVERY_S:
            self._last_alert = now
            log.error('tick log write failed (%s): ticks are not being recorded until it recovers; trading is unaffected', exc)
            if self.alert is not None:
                try:
                    self.alert(f'tick log write failed ({exc}); ticks are not being recorded, trading is unaffected')
                except Exception:
                    pass

    def close(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
        self.drain()
        try:
            if self._tick_fh is not None:
                self._tick_fh.close()
                self._tick_fh = None
        except Exception:
            pass
        log.info('tick log closed: received %d, written %d, dropped %d of those (buffer full), skipped %d more (kill flag), write errors %d',
                 self.received, self.written, self.dropped, self.skipped, self.write_errors)
