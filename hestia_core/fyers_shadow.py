"""
Phase 1 of plans/hestia-fyers-candle-source.md: SHADOW mode. Angel One stays the only source the engines ever see. This module asks
Fyers for the same one-minute candles, in parallel, and RECORDS what happened, so that the later phases (rescue, smart) can be
justified, or rejected, from data:

  * how soon after a minute closes Fyers has it, and the same for Angel One's own poll (the user's reason for all of this is slow flip
    detection and AB1021 exhaustion at the boundaries, so both sides are measured, boundary minutes separately);
  * whether Fyers succeeded where Angel One exhausted its retries;
  * the closed minutes each source returned, with the time each minute was first seen, for an after-session comparison of the
    Supertrend flips against what the engines actually acted on (research/fyers_mcx_validation/phase1_shadow_report.py).

Isolation is the design. Nothing here can change a decision, a state file or the order of any broker call:
  * its own daemon-thread pool (never the host's four IO workers, never joined at exit: a hung Fyers call cannot delay the engine join,
    the drain or terminateSession);
  * every entry point catches everything; a failure is logged once per kind and the poll simply is not recorded;
  * it receives COPIES of the Angel frames and never touches LiveData's streams;
  * it never posts to the kernel and never raises an alert: state changes (token fresh, stale, tripped) are single log lines;
  * a per-token in-flight guard means a slow Fyers skips that token's next minute instead of queueing work.
Phase 2 ('rescue' mode, `FyersRescue` below) adds ONE thing on top of the recording: when the Angel One burst for a window has failed, Fyers is asked for
that window, and its answer is used ONLY for minutes the engine does not already have (never overwriting an Angel One minute), only if it contains the
just-closed minute, and only once that minute has settled (Fyers's first-seen value of a minute is provisional: on 2026-10-05 22%-82% of first-seen
minutes differed from the finalized ones). Every rescue is logged and recorded in `rescues_<date>.csv`. Nothing else in the live path changes.
Phase 3 ('smart' mode, `FyersSmart` below) makes Fyers the FIRST source for a configured subset of instruments (the pilot is CRUDEOILM): each minute's poll window
is asked of Fyers, once the just-closed minute has settled (default 0.5 s after it closed, the owner's call: the 2026-10-05 settle probe found 97.5% of snapshots final at +0.5 s and every one from +0.8 s),
and only when Fyers cannot answer (no token, an error, a timeout, a lagging answer) does the poll fall through to the unchanged Angel One burst, whose own
rescue fallback and recovery queue stay behind it. The Fyers answer is filtered exactly like a rescue (only minutes the engine lacks, never the forming minute,
zero-volume placeholders dropped so the series keeps Angel One's "an untraded minute is absent" meaning). Every Fyers-first window is recorded in `smart_<date>.csv`.
The token is read from hestia_data/fyers_token.json by `TokenGate` (mode 600, file mtime and `issued_at` both today in IST, now before
`expires_at`, no kill-switch flag, breaker not tripped) and is never logged or written anywhere.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger('hestia_fyers')

IST = ZoneInfo('Asia/Kolkata')
HISTORY_URL = 'https://api-t1.fyers.in/data/history'
_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
MINUTE_COLS = ['time_stamp', 'open', 'high', 'low', 'close', 'volume']


# ---- the token gate ---------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class GateState:
    ok: bool
    reason: str
    auth: Optional[str] = field(default=None, repr=False)       # "app_id:access_token"; never shown in a repr
    fingerprint: str = ''


class TokenGate:
    """Is there a Fyers token we may use right now? Evaluated on every poll (a cached stat and parse), so a token that arrives mid-session
    starts being used at the next poll and one that expires stops being used. The freshness rule is `check_token_file`'s in
    data_pipeline/fyers_token_refresh.py (a test pins the two together). An authentication refusal trips a breaker that holds until
    the token file changes."""

    def __init__(self, token_file: os.PathLike, off_flag: Optional[os.PathLike] = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(IST)):
        self.token_file, self.off_flag, self._clock = Path(token_file), Path(off_flag) if off_flag else None, clock
        self._cache_key: Optional[tuple] = None
        self._cached: Optional[dict] = None
        self._tripped_id: Optional[tuple] = None                    # (mtime_ns, inode) of the token file the breaker tripped on
        self._tripped_reason = ''
        self._last_reason: Optional[str] = None
        self._lock = threading.Lock()

    def trip(self, reason: str) -> None:
        with self._lock:
            try:
                st = self.token_file.stat()
                self._tripped_id = (st.st_mtime_ns, st.st_ino)
            except OSError:
                self._tripped_id = (-1, -1)
            self._tripped_reason = reason

    def check(self, now: Optional[datetime] = None) -> GateState:
        state = self._evaluate((now or self._clock()).astimezone(IST))
        with self._lock:
            if state.reason != self._last_reason:                  # one log line per state change, never per poll
                self._last_reason = state.reason
                log.info('fyers token gate: %s%s', 'usable' if state.ok else 'not usable',
                         f' ({state.reason})' if state.reason else f' (fingerprint {state.fingerprint})')
        return state

    def _evaluate(self, now: datetime) -> GateState:
        if self.off_flag is not None and self.off_flag.exists():
            return GateState(False, 'kill-switch flag present')
        try:
            st = self.token_file.stat()
        except OSError:
            return GateState(False, 'no token file')
        with self._lock:
            if self._tripped_id is not None:
                if (st.st_mtime_ns, st.st_ino) == self._tripped_id:
                    return GateState(False, f'breaker tripped ({self._tripped_reason}); waiting for a new token file')
                self._tripped_id = None                             # a new file (new mtime or inode, as an atomic rewrite gives): reset
        if st.st_mode & 0o077:
            return GateState(False, f'token file mode is {oct(st.st_mode & 0o777)}, not 600')
        mtime = datetime.fromtimestamp(st.st_mtime, IST)
        if mtime.date() != now.date():
            return GateState(False, f'token file modified {mtime:%Y-%m-%d}, not today')
        key = (st.st_mtime_ns, st.st_size)
        if self._cache_key != key:
            try:
                rec = json.loads(self.token_file.read_text())
                issued = datetime.fromisoformat(rec['issued_at']).astimezone(IST)
                expires = datetime.fromisoformat(rec['expires_at']).astimezone(IST)
                parsed = {'auth': f"{rec['app_id']}:{rec['access_token']}", 'issued': issued, 'expires': expires,
                          'fp': _fingerprint(rec['access_token'])}
                if not rec['access_token'] or not rec['app_id']:
                    raise KeyError('empty')
            except Exception as exc:                                # noqa: BLE001
                self._cache_key, self._cached = key, None
                return GateState(False, f'token file unreadable or malformed ({type(exc).__name__})')
            self._cache_key, self._cached = key, parsed
        if self._cached is None:
            return GateState(False, 'token file unreadable or malformed')
        if self._cached['issued'].date() != now.date():
            return GateState(False, f"issued_at {self._cached['issued']:%Y-%m-%d} is not today")
        if now >= self._cached['expires']:
            return GateState(False, f"past expires_at {self._cached['expires']:%Y-%m-%d %H:%M}")
        return GateState(True, '', self._cached['auth'], self._cached['fp'])


def _fingerprint(secret: str) -> str:
    import hashlib
    return hashlib.sha256(secret.encode()).hexdigest()[:8]


# ---- the Fyers client -------------------------------------------------------------------------------------------------------

@dataclass
class FetchResult:
    kind: str                                   # ok | empty | auth | rate | timeout | symbol | http | error
    frame: Optional[pd.DataFrame] = None
    latency_ms: float = 0.0
    detail: str = ''


def fyers_symbol(instrument: str, expiry) -> str:
    """Angel One contract to Fyers symbol: SILVERMIC30NOV26FUT -> MCX:SILVERMIC26NOVFUT (verified for all four instruments 2026-10-03)."""
    return f'MCX:{instrument}{expiry:%y%b}FUT'.upper()


def _http_get(url: str, params: dict, auth: str, timeout: float) -> Tuple[int, Optional[dict]]:
    req = urllib.request.Request(f'{url}?{urllib.parse.urlencode(params)}', headers={'Authorization': auth, 'User-Agent': _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except Exception:                                           # noqa: BLE001
            return exc.code, None


class FyersClient:
    def __init__(self, get: Callable[[str, dict, str, float], Tuple[int, Optional[dict]]] = _http_get, timeout_s: float = 3.0):
        self._get, self.timeout_s = get, timeout_s

    def minutes(self, symbol: str, start: datetime, end: datetime, auth: str) -> FetchResult:
        """One-minute candles for [start, end] (tz-naive IST datetimes). The frame is tz-naive IST like every series in Hestia. A failure
        carries its KIND only: never the auth header, never a response body that could echo a token."""
        t0 = time.monotonic()
        params = {'symbol': symbol, 'resolution': '1', 'date_format': '0', 'cont_flag': '0',
                  'range_from': int(start.replace(tzinfo=IST).timestamp()), 'range_to': int(end.replace(tzinfo=IST).timestamp())}
        try:
            status, body = self._get(HISTORY_URL, params, auth, self.timeout_s)
        except (socket.timeout, TimeoutError):
            return FetchResult('timeout', latency_ms=(time.monotonic() - t0) * 1000)
        except urllib.error.URLError as exc:
            kind = 'timeout' if isinstance(getattr(exc, 'reason', None), (socket.timeout, TimeoutError)) else 'http'
            return FetchResult(kind, latency_ms=(time.monotonic() - t0) * 1000, detail=type(exc).__name__)
        except Exception as exc:                                    # noqa: BLE001
            return FetchResult('error', latency_ms=(time.monotonic() - t0) * 1000, detail=type(exc).__name__)
        ms = (time.monotonic() - t0) * 1000
        body = body or {}
        code = body.get('code')
        if status == 429 or code == 429:
            return FetchResult('rate', latency_ms=ms)
        if status in (401, 403) or code in (-16, -8, -15, -17):
            return FetchResult('auth', latency_ms=ms, detail=f'http {status} code {code}')
        if status >= 500:
            return FetchResult('http', latency_ms=ms, detail=f'http {status}')
        if body.get('s') == 'ok':
            df = pd.DataFrame(body.get('candles', []), columns=['epoch', 'open', 'high', 'low', 'close', 'volume'])
            df['time_stamp'] = pd.to_datetime(df['epoch'], unit='s', utc=True).dt.tz_convert(IST).dt.tz_localize(None)
            return FetchResult('ok', df[MINUTE_COLS].drop_duplicates('time_stamp').sort_values('time_stamp'), ms)
        if body.get('s') == 'no_data':
            return FetchResult('empty', pd.DataFrame(columns=MINUTE_COLS), ms)
        if 'symbol' in str(body.get('message', '')).lower():
            return FetchResult('symbol', latency_ms=ms, detail=f'code {code}')
        return FetchResult('error', latency_ms=ms, detail=f'code {code}')


# ---- a pool that can never delay the process --------------------------------------------------------------------------------

class DaemonPool:
    """Plain daemon worker threads on a queue: unlike ThreadPoolExecutor's they are not joined at interpreter exit, so a hung network call
    here can never delay shutdown. `close()` is non-blocking and drops queued work."""

    def __init__(self, workers: int, name: str = 'hestia-fyers'):
        self._q: 'queue.Queue' = queue.Queue()
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._run, name=f'{name}-{i}', daemon=True) for i in range(workers)]
        for t in self._threads:
            t.start()

    def submit(self, fn: Callable, *args) -> None:
        if not self._stop.is_set():
            self._q.put((fn, args))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                fn, args = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                fn(*args)
            except BaseException:                                   # noqa: BLE001 - a job never kills a worker
                log.exception('fyers shadow job failed')

    def close(self) -> None:
        self._stop.set()
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass


# ---- the recorder -----------------------------------------------------------------------------------------------------------

@dataclass
class ShadowConfig:
    instruments: Sequence[str] = ('CRUDEOILM', 'SILVERMIC', 'GOLDPETAL', 'NATGASMINI')
    timeout_s: float = 3.0
    retry_s: float = 1.0                         # between attempts while the just-closed minute has not appeared yet
    max_wait_s: float = 6.0                      # stop waiting for the just-closed minute this long after the tick
    workers: int = 8                             # at least one per active token, so retry waits never queue behind each other


POLL_COLS = ['date', 'tick', 'token', 'symbol', 'side', 'boundary', 'ok', 'kind', 'attempts', 'after_s', 'rows', 'expected_present',
             'exhausted', 'note']
SEEN_COLS = MINUTE_COLS + ['seen_at']


class ShadowRecorder:
    """The hooks LiveData calls: `begin` at the minute tick (starts the Fyers measurement immediately, independent of how long Angel One
    takes) and `angel_result` when the Angel One poll for that tick has finished (with copies of its frames)."""

    def __init__(self, out_dir: os.PathLike, gate: TokenGate, client: FyersClient, config: Optional[ShadowConfig] = None,
                 pool: Optional[DaemonPool] = None, clock: Callable[[], datetime] = datetime.now,
                 sleep: Callable[[float], None] = time.sleep):
        self.dir = Path(out_dir)
        self.gate, self.client, self.cfg = gate, client, config or ShadowConfig()
        self.pool = pool or DaemonPool(self.cfg.workers)
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._inflight: Set[str] = set()
        self._seen: Dict[Tuple[str, str], Dict[pd.Timestamp, tuple]] = {}   # (token, side) -> minute -> the values last written for it
        self._warned: Set[str] = set()
        self.skipped_inflight = 0
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- entry points (never raise) -----------------------------------------------------------------------------------------
    def begin(self, ref, tick: datetime, win_from: datetime, win_to: datetime) -> None:
        try:
            if ref.instrument not in self.cfg.instruments:
                return
            with self._lock:
                if ref.token in self._inflight:
                    self.skipped_inflight += 1
                    return
                self._inflight.add(ref.token)
            try:
                self.pool.submit(self._fyers_job, ref, tick, win_from, win_to)
            except Exception:                                       # noqa: BLE001
                with self._lock:
                    self._inflight.discard(ref.token)               # a job that never started must not block the token's later minutes
                raise
        except Exception:                                           # noqa: BLE001
            self._once('begin', 'fyers shadow begin failed')

    def angel_result(self, ref, tick: datetime, frames: Sequence[Optional[pd.DataFrame]], stats: Sequence[dict],
                     returned_at: datetime) -> None:
        try:
            if ref.instrument not in self.cfg.instruments:
                return
            copies = [None if f is None else f.copy() for f in frames]
            self.pool.submit(self._angel_write, ref, tick, copies, [dict(s) for s in stats], returned_at)
        except Exception:                                           # noqa: BLE001
            self._once('angel', 'fyers shadow angel_result failed')

    def close(self) -> None:
        self.pool.close()

    # -- the jobs (on the shadow pool) --------------------------------------------------------------------------------------
    def _fyers_job(self, ref, tick: datetime, win_from: datetime, win_to: datetime) -> None:
        try:
            state = self.gate.check()
            if not state.ok:
                return                                              # the gate logs its own state change; nothing is recorded
            symbol = fyers_symbol(ref.instrument, ref.expiry)
            expected = pd.Timestamp(tick).floor('min') - pd.Timedelta(minutes=1)
            t0, attempts, first_seen, kind, rows, note = self._clock(), 0, None, 'error', 0, ''
            while True:
                attempts += 1
                res = self.client.minutes(symbol, win_from, win_to, state.auth)
                kind = res.kind
                if res.kind == 'auth':
                    self.gate.trip('fyers refused the token')
                    note = res.detail
                    break
                if res.kind in ('rate', 'symbol'):
                    note = res.detail
                    break                                           # never retry a rate limit; a bad symbol will not fix itself
                if res.kind in ('ok', 'empty') and res.frame is not None:
                    now = self._clock()
                    closed = res.frame[res.frame['time_stamp'] < pd.Timestamp(now).floor('min')]
                    rows = len(closed)
                    self._write_minutes(ref.token, symbol, 'fyers', closed, now)
                    if (closed['time_stamp'] == expected).any():
                        first_seen = (now - tick).total_seconds()
                        break
                waited = (self._clock() - t0).total_seconds()
                if waited + self.cfg.retry_s > self.cfg.max_wait_s:
                    break
                self._sleep(self.cfg.retry_s)
            self._write_poll(ref, tick, symbol, 'fyers', ok=first_seen is not None, kind=kind, attempts=attempts, after_s=first_seen,
                             rows=rows, expected_present=first_seen is not None, exhausted='', note=note)
        except Exception:                                           # noqa: BLE001
            self._once('job', 'fyers shadow job failed')
        finally:
            with self._lock:
                self._inflight.discard(ref.token)

    def _angel_write(self, ref, tick: datetime, frames: List[Optional[pd.DataFrame]], stats: List[dict], returned_at: datetime) -> None:
        try:
            symbol = fyers_symbol(ref.instrument, ref.expiry)
            expected = pd.Timestamp(tick).floor('min') - pd.Timedelta(minutes=1)
            present, rows = False, 0
            for f in frames:
                if f is not None and not f.empty:
                    rows += len(f)
                    present = present or bool((f['time_stamp'] == expected).any())
                    self._write_minutes(ref.token, symbol, 'angel', f[MINUTE_COLS], returned_at)
            ok = bool(frames) and all(f is not None for f in frames)
            kind = 'ok' if ok else ('rescued' if any(s.get('rescued') for s in stats) else 'exhausted')
            self._write_poll(ref, tick, symbol, 'angel', ok=ok, kind=kind,
                             attempts=sum(s.get('attempts', 0) for s in stats),
                             after_s=(returned_at - tick).total_seconds() if present else None, rows=rows, expected_present=present,
                             exhausted=any(s.get('exhausted') for s in stats), note='')
        except Exception:                                           # noqa: BLE001
            self._once('angel_job', 'fyers shadow angel write failed')

    # -- files --------------------------------------------------------------------------------------------------------------
    def _write_minutes(self, token: str, symbol: str, side: str, df: pd.DataFrame, seen_at: datetime) -> None:
        """A minute is written the first time it is seen and again whenever a later poll shows DIFFERENT values for it (the 5-minute poll windows
        overlap, so every minute is seen about five times). The file therefore holds each minute's whole history, first-seen to final, with the
        time of each sighting; a consumer wanting the settled value takes the last row per minute, one wanting the provisional one the first."""
        if df is None or df.empty:
            return
        key = (token, side)
        with self._lock:
            seen = self._seen.setdefault(key, {})
            rows = []
            for r in df.itertuples(index=False):
                vals = (r.open, r.high, r.low, r.close, r.volume)
                if seen.get(r.time_stamp) != vals:
                    seen[r.time_stamp] = vals
                    rows.append((r.time_stamp, vals))
            if not rows:
                return
            path = self.dir / f"{symbol.replace(':', '_')}_{side}_1m_{seen_at:%Y-%m-%d}.csv"
            exists = path.exists()
            with open(path, 'a', newline='') as f:
                w = csv.writer(f)
                if not exists:
                    w.writerow(SEEN_COLS)
                for ts, vals in rows:
                    w.writerow([ts.isoformat(), *vals, seen_at.isoformat(timespec='milliseconds')])

    def _write_poll(self, ref, tick: datetime, symbol: str, side: str, ok: bool, kind: str, attempts: int, after_s, rows: int,
                    expected_present: bool, exhausted, note: str) -> None:
        with self._lock:
            path = self.dir / f'polls_{tick:%Y-%m-%d}.csv'
            exists = path.exists()
            with open(path, 'a', newline='') as f:
                w = csv.writer(f)
                if not exists:
                    w.writerow(POLL_COLS)
                w.writerow([f'{tick:%Y-%m-%d}', tick.isoformat(timespec='seconds'), ref.token, symbol, side, int(tick.minute % 15 == 0),
                            int(ok), kind, attempts, '' if after_s is None else round(after_s, 3), rows, int(expected_present),
                            '' if exhausted == '' else int(bool(exhausted)), note])

    def _once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.exception(message)


MODES = ('angel', 'shadow', 'rescue', 'smart')


@dataclass
class RescueConfig:
    instruments: Sequence[str] = ('CRUDEOILM', 'SILVERMIC', 'GOLDPETAL', 'NATGASMINI')
    after_attempts: int = 5                      # failed Angel One attempts before Fyers is asked; 5 = only once the whole burst has failed
    settle_s: float = 0.0                        # a just-closed minute is trusted only this many seconds after it closed: measured 54% final at +0.1 s, 95% at +0.4 s, 100% from +0.8 s
    timeout_s: float = 3.0


RESCUE_COLS = ['ts', 'token', 'symbol', 'win_from', 'win_to', 'expected_minute', 'kind', 'minutes_returned', 'minutes_used', 'latency_ms', 'note']


class FyersRescue:
    """`fetch_window` is the fallback `candle_fetch.fetch_one_minute_window` calls after Angel One's burst has failed. It returns a frame of the
    window's closed minutes that the engine does NOT already have (an empty frame if there is nothing new), or None when Fyers could not
    answer, in which case the caller's burst ends exactly as it would have without a rescue. It never raises."""

    def __init__(self, out_dir: os.PathLike, gate: TokenGate, client: FyersClient, config: Optional[RescueConfig] = None,
                 clock: Callable[[], datetime] = datetime.now, sleep: Callable[[float], None] = time.sleep):
        self.dir = Path(out_dir)
        self.gate, self.client, self.cfg = gate, client, config or RescueConfig()
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self.counts = {'ok': 0, 'failed': 0}
        self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def after_attempts(self) -> int:
        return self.cfg.after_attempts

    def fetch_window(self, ref, win_from: datetime, win_to: datetime, known: Iterable = ()) -> Optional[pd.DataFrame]:
        try:
            return self._fetch(ref, win_from, win_to, set(known))
        except Exception:                                           # noqa: BLE001
            log.exception('fyers rescue failed')
            self._count('failed')
            return None

    def _fetch(self, ref, win_from: datetime, win_to: datetime, known: set) -> Optional[pd.DataFrame]:
        if ref.instrument not in self.cfg.instruments:
            return None
        state = self.gate.check()
        if not state.ok:
            return None                                             # the gate logs its own state change
        symbol = fyers_symbol(ref.instrument, ref.expiry)
        window_end = pd.Timestamp(win_to).floor('min')              # the just-closed minute is window_end - 1 min, and it closed AT window_end
        expected = window_end - pd.Timedelta(minutes=1)
        wait = self.cfg.settle_s - (pd.Timestamp(self._clock()) - window_end).total_seconds()
        if wait > 0:
            self._sleep(wait)
        res = self.client.minutes(symbol, win_from, win_to, state.auth)
        if res.kind == 'auth':
            self.gate.trip('fyers refused the token')
        returned = used = 0
        out = None
        note = res.detail
        if res.kind in ('ok', 'empty') and res.frame is not None:
            closed = res.frame[res.frame['time_stamp'] < window_end]
            returned = len(closed)
            if (closed['time_stamp'] == expected).any():
                new = closed[~closed['time_stamp'].isin(known) & (closed['volume'] > 0)]
                out = new[MINUTE_COLS].reset_index(drop=True)
                used = len(out)
            else:
                note = 'the just-closed minute is not in the Fyers answer'
        kind = 'stale' if out is None and res.kind in ('ok', 'empty') else res.kind
        self._record(ref, symbol, win_from, win_to, expected, kind, returned, used, res.latency_ms, note)
        if out is None:
            self._count('failed')
            log.info('fyers rescue: %s window %s->%s could not be filled (%s)', symbol, f'{win_from:%H:%M}', f'{win_to:%H:%M}', note or res.kind)
            return None
        self._count('ok')
        log.info('fyers rescue: %s window %s->%s filled %d minute(s) from Fyers after Angel One failed', symbol, f'{win_from:%H:%M}',
                 f'{win_to:%H:%M}', used)
        return out

    def _count(self, key: str) -> None:
        with self._lock:
            self.counts[key] += 1

    def summary(self) -> Tuple[int, int]:
        with self._lock:
            return self.counts['ok'], self.counts['failed']

    def _record(self, ref, symbol, win_from, win_to, expected, kind, returned, used, latency_ms, note) -> None:
        try:
            with self._lock:
                path = self.dir / f'rescues_{win_to:%Y-%m-%d}.csv'
                exists = path.exists()
                with open(path, 'a', newline='') as f:
                    w = csv.writer(f)
                    if not exists:
                        w.writerow(RESCUE_COLS)
                    w.writerow([self._clock().isoformat(timespec='milliseconds'), ref.token, symbol, win_from.isoformat(timespec='minutes'),
                                win_to.isoformat(timespec='minutes'), pd.Timestamp(expected).isoformat(), kind, returned, used,
                                round(latency_ms, 1), note])
        except Exception:                                           # noqa: BLE001
            log.exception('fyers rescue record failed')


@dataclass
class SmartConfig:
    instruments: Sequence[str] = ('CRUDEOILM',)  # the Fyers-first pilot; every other instrument stays Angel One first (with rescue)
    settle_s: float = 0.5                        # seconds after a minute closes before it is trusted (owner's call): measured 54% final at +0.1 s, 95% at +0.4 s, 97.5% at +0.5 s, 100% from +0.8 s (n=40)
    retry_s: float = 0.5                         # between pulls while the just-closed minute has not appeared in the answer yet
    max_wait_s: float = 3.0                      # give up on Fyers this long after the minute closed (Angel One's own median is 2.75 s, so waiting longer buys nothing)
    timeout_s: float = 3.0


SMART_COLS = ['ts', 'token', 'symbol', 'win_from', 'win_to', 'expected_minute', 'kind', 'served', 'attempts', 'after_s', 'latency_ms',
              'minutes_returned', 'minutes_used', 'note']


class FyersSmart:
    """Phase 3: `fetch_window` is asked BEFORE the Angel One burst for the instruments in `cfg.instruments`. It returns a frame of the window's closed
    minutes the engine does NOT already have (possibly empty: the answer was complete and nothing was new), or None when Fyers could not give a
    trustworthy answer, in which case the caller runs the Angel One burst exactly as it always did. It never raises. A Fyers answer is trusted only
    if it contains the just-closed minute (a lagging Fyers is a failure, not a quiet market) and the minute has settled `settle_s` after it closed;
    a rate limit is never retried and an authentication refusal trips the shared breaker until the token file changes."""

    def __init__(self, out_dir: os.PathLike, gate: TokenGate, client: FyersClient, config: Optional[SmartConfig] = None,
                 clock: Callable[[], datetime] = datetime.now, sleep: Callable[[float], None] = time.sleep):
        self.dir = Path(out_dir)
        self.gate, self.client, self.cfg = gate, client, config or SmartConfig()
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self.counts = {'served': 0, 'fallback': 0}
        self.dir.mkdir(parents=True, exist_ok=True)

    def handles(self, instrument: str) -> bool:
        return instrument in self.cfg.instruments

    def summary(self) -> Tuple[int, int]:
        with self._lock:
            return self.counts['served'], self.counts['fallback']

    def fetch_window(self, ref, win_from: datetime, win_to: datetime, known: Iterable = ()) -> Optional[pd.DataFrame]:
        try:
            if not self.handles(ref.instrument):
                return None
            return self._fetch(ref, win_from, win_to, set(known))
        except Exception:                                           # noqa: BLE001
            log.exception('fyers smart fetch failed')
            self._count('fallback')
            return None

    def _fetch(self, ref, win_from: datetime, win_to: datetime, known: set) -> Optional[pd.DataFrame]:
        symbol = fyers_symbol(ref.instrument, ref.expiry)
        window_end = pd.Timestamp(win_to).floor('min')              # the just-closed minute is window_end - 1 min, and it closed AT window_end
        expected = window_end - pd.Timedelta(minutes=1)
        state = self.gate.check()
        if not state.ok:
            self._finish(ref, symbol, win_from, win_to, expected, 'gate', False, 0, None, 0.0, 0, 0, state.reason)
            return None
        wait = self.cfg.settle_s - (pd.Timestamp(self._clock()) - window_end).total_seconds()
        if wait > 0:
            self._sleep(wait)
        attempts, res, note = 0, None, ''
        while True:
            attempts += 1
            res = self.client.minutes(symbol, win_from, win_to, state.auth)
            note = res.detail
            if res.kind == 'auth':
                self.gate.trip('fyers refused the token')
                break
            if res.kind in ('ok', 'empty') and res.frame is not None:
                closed = res.frame[res.frame['time_stamp'] < window_end]
                if (closed['time_stamp'] == expected).any():
                    new = closed[~closed['time_stamp'].isin(known) & (closed['volume'] > 0)]
                    out = new[MINUTE_COLS].reset_index(drop=True)
                    after = (pd.Timestamp(self._clock()) - window_end).total_seconds()
                    self._finish(ref, symbol, win_from, win_to, expected, 'ok', True, attempts, after, res.latency_ms, len(closed), len(out), '')
                    return out
                note = 'the just-closed minute is not in the Fyers answer'
                if (pd.Timestamp(self._clock()) - window_end).total_seconds() + self.cfg.retry_s <= self.cfg.max_wait_s:
                    self._sleep(self.cfg.retry_s)
                    continue
                res = FetchResult('stale', res.frame, res.latency_ms, note)
            break                                                   # rate, timeout, http, error, symbol, stale, auth: Angel One takes over now
        after = (pd.Timestamp(self._clock()) - window_end).total_seconds()
        self._finish(ref, symbol, win_from, win_to, expected, res.kind, False, attempts, after, res.latency_ms, 0, 0, note)
        return None

    def _finish(self, ref, symbol, win_from, win_to, expected, kind, served, attempts, after_s, latency_ms, returned, used, note) -> None:
        self._count('served' if served else 'fallback')
        if not served and kind != 'gate':
            log.info('fyers smart: %s window %s->%s not served (%s); Angel One takes over', symbol, f'{win_from:%H:%M}', f'{win_to:%H:%M}', note or kind)
        self._record(ref, symbol, win_from, win_to, expected, kind, served, attempts, after_s, latency_ms, returned, used, note)

    def _count(self, key: str) -> None:
        with self._lock:
            self.counts[key] += 1

    def _record(self, ref, symbol, win_from, win_to, expected, kind, served, attempts, after_s, latency_ms, returned, used, note) -> None:
        try:
            with self._lock:
                path = self.dir / f'smart_{win_to:%Y-%m-%d}.csv'
                exists = path.exists()
                with open(path, 'a', newline='') as f:
                    w = csv.writer(f)
                    if not exists:
                        w.writerow(SMART_COLS)
                    w.writerow([self._clock().isoformat(timespec='milliseconds'), ref.token, symbol, win_from.isoformat(timespec='minutes'),
                                win_to.isoformat(timespec='minutes'), pd.Timestamp(expected).isoformat(), kind, int(served), attempts,
                                '' if after_s is None else round(after_s, 3), round(latency_ms, 1), returned, used, note])
        except Exception:                                           # noqa: BLE001
            log.exception('fyers smart record failed')


def build_shadow(cfg) -> Optional[ShadowRecorder]:
    """The recorder for the configured `CANDLE_SOURCE` mode ('shadow', 'rescue' and 'smart' all record), or None when the mode is 'angel' (the default:
    no Fyers code runs at all). An unknown mode is an error at start-up, never a silent fallback."""
    cs = dict(getattr(cfg, 'CANDLE_SOURCE', None) or {})
    mode = cs.get('mode', 'angel')
    if mode not in MODES:
        raise ValueError(f'CANDLE_SOURCE mode {mode!r} is not one of {MODES}')
    if mode == 'angel':
        return None
    scfg = ShadowConfig(instruments=tuple(cs.get('instruments', ShadowConfig.instruments)), timeout_s=cs.get('timeout_s', 3.0),
                        retry_s=cs.get('retry_s', 1.0), max_wait_s=cs.get('max_wait_s', 6.0))
    return ShadowRecorder(cfg.SHADOW_DIR, TokenGate(cfg.FYERS_TOKEN_FILE, cfg.FYERS_OFF_FLAG), FyersClient(timeout_s=scfg.timeout_s), scfg)


def build_rescue(cfg, shadow: Optional[ShadowRecorder]) -> Optional[FyersRescue]:
    """The rescue fallback, in 'rescue' and 'smart' mode (in smart mode it is the second chance after the Angel One burst has failed behind a Fyers-first
    miss); it shares the recorder's token gate (so one authentication refusal stops both) and client."""
    cs = dict(getattr(cfg, 'CANDLE_SOURCE', None) or {})
    if cs.get('mode', 'angel') not in ('rescue', 'smart') or shadow is None:
        return None
    rcfg = RescueConfig(instruments=tuple(cs.get('instruments', RescueConfig.instruments)), after_attempts=int(cs.get('rescue_after_attempts', 5)),
                        settle_s=float(cs.get('settle_s', 0.0)), timeout_s=cs.get('timeout_s', 3.0))
    return FyersRescue(cfg.SHADOW_DIR, shadow.gate, shadow.client, rcfg)


def build_smart(cfg, shadow: Optional[ShadowRecorder]) -> Optional[FyersSmart]:
    """The Fyers-first source, only in 'smart' mode and only for `smart_instruments`; it shares the recorder's token gate and client."""
    cs = dict(getattr(cfg, 'CANDLE_SOURCE', None) or {})
    if cs.get('mode', 'angel') != 'smart' or shadow is None:
        return None
    scfg = SmartConfig(instruments=tuple(cs.get('smart_instruments', SmartConfig.instruments)), settle_s=float(cs.get('smart_settle_s', SmartConfig.settle_s)),
                       retry_s=float(cs.get('smart_retry_s', SmartConfig.retry_s)), max_wait_s=float(cs.get('smart_max_wait_s', SmartConfig.max_wait_s)),
                       timeout_s=cs.get('timeout_s', 3.0))
    return FyersSmart(cfg.SHADOW_DIR, shadow.gate, shadow.client, scfg)
