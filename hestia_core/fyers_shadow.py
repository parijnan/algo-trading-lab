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
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple
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
        self._seen: Dict[Tuple[str, str], Set[pd.Timestamp]] = {}          # (token, side) -> minutes already written
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
            self._write_poll(ref, tick, symbol, 'angel', ok=ok, kind='ok' if ok else 'exhausted',
                             attempts=sum(s.get('attempts', 0) for s in stats),
                             after_s=(returned_at - tick).total_seconds() if present else None, rows=rows, expected_present=present,
                             exhausted=any(s.get('exhausted') for s in stats), note='')
        except Exception:                                           # noqa: BLE001
            self._once('angel_job', 'fyers shadow angel write failed')

    # -- files --------------------------------------------------------------------------------------------------------------
    def _write_minutes(self, token: str, symbol: str, side: str, df: pd.DataFrame, seen_at: datetime) -> None:
        if df is None or df.empty:
            return
        key = (token, side)
        with self._lock:
            seen = self._seen.setdefault(key, set())
            new = df[~df['time_stamp'].isin(seen)]
            if new.empty:
                return
            seen.update(new['time_stamp'])
            path = self.dir / f"{symbol.replace(':', '_')}_{side}_1m_{seen_at:%Y-%m-%d}.csv"
            exists = path.exists()
            with open(path, 'a', newline='') as f:
                w = csv.writer(f)
                if not exists:
                    w.writerow(SEEN_COLS)
                for r in new.itertuples(index=False):
                    w.writerow([r.time_stamp.isoformat(), r.open, r.high, r.low, r.close, r.volume,
                                seen_at.isoformat(timespec='milliseconds')])

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


MODES = ('angel', 'shadow')                       # rescue and smart arrive with Phases 2 and 3


def build_shadow(cfg) -> Optional[ShadowRecorder]:
    """The recorder for the configured `CANDLE_SOURCE` mode, or None when the mode is 'angel' (the default: no Fyers code runs at all).
    An unknown mode is an error at start-up, never a silent fallback."""
    cs = dict(getattr(cfg, 'CANDLE_SOURCE', None) or {})
    mode = cs.get('mode', 'angel')
    if mode not in MODES:
        raise ValueError(f'CANDLE_SOURCE mode {mode!r} is not one of {MODES}')
    if mode == 'angel':
        return None
    scfg = ShadowConfig(instruments=tuple(cs.get('instruments', ShadowConfig.instruments)), timeout_s=cs.get('timeout_s', 3.0),
                        retry_s=cs.get('retry_s', 1.0), max_wait_s=cs.get('max_wait_s', 6.0))
    return ShadowRecorder(cfg.SHADOW_DIR, TokenGate(cfg.FYERS_TOKEN_FILE, cfg.FYERS_OFF_FLAG), FyersClient(timeout_s=scfg.timeout_s), scfg)
