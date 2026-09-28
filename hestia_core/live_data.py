"""
The live data service: the DataPort for the live Hestia (plans/hestia-p4-live-services.md, slice 4).

What it does, ported from Prometheus's data half (never imported from it) and pinned against it where numbers are involved:
  * seeding a contract: past days from the pipeline's per-contract file, today from a private per-token cache plus a live gap
    fetch, resampled to 15-minute bars anchored at 09:00, refused if the series has a hole;
  * one candle poll per minute per active token, staggered, under the gateway's candle budget, with a queue that re-fetches
    windows a failed burst left behind (Angel One's AB1021 stretches);
  * 15-minute bar building at each boundary, waiting up to a cutoff for the window to complete, with quality flags:
    COMPLETE, RECOVERED (completed only after waiting), PARTIAL (built at the cutoff from what was on hand), GAP (nothing);
  * a provisional bar from the tick feed when the window is incomplete at the boundary, computed with each engine's own
    Supertrend, and reconciled by the real bar (`reconciles_provisional`);
  * Supertrend per (token, engine spec) from one implementation (hestia_core.indicators);
  * prices and their age, feed-stale and DPL-freeze events, contract tracking for a roll.
Every tracked contract's bar for a boundary is readable before the trading contract's `BarComplete` is delivered.

Threading: the core calls this on the dispatcher (or under the core lock from an engine thread). Blocking work (candle and
quote calls, file writes for the cache, seeding) runs on the executor; each completion is posted back with `kernel.post`.
`prepare` is the one blocking entry point, for the host's main thread before the engines launch.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from hestia_core.candle_fetch import FetchConfig, fetch_history, fetch_one_minute_window
from hestia_core.feed_port import FeedPort
from hestia_core.history import (OHLCV, TodayCache, _naive, _safe_concat, empty_frame, find_gaps, merge_and_save, read_past,
                                 resample_1m)
from hestia_core.indicators import compute_st
from hestia_core.interface import (Bar, BarComplete, BarQuality, ContractInfo, ContractRef, Direction, DplFrozen,
                                   FeedRecovered, FeedStale, LtpQuote, ProvisionalBar, SupertrendPoint, TrackFailed,
                                   TrackReady)
from hestia_core.mcx_market import ContractCatalog, ContractRow, MarketCalendar, closing_time_str

log = logging.getLogger('hestia_live_data')


@dataclass
class LiveDataConfig:
    seed_days: int = 18
    seed_skip_dates: tuple = ()
    bar_min: int = 15
    session_start: str = '09:00'
    poll_window_min: int = 5
    poll_stagger_s: float = 5.0                 # offset between tokens inside a minute, so candle calls never bunch up
    deferred_bar_cutoff_min: float = 1.0        # wait this long past a boundary for an incomplete window
    seed_contracts_per_instrument: int = 2      # the front live contract and the next one (for a roll)
    seed_retry_attempts: int = 5
    seed_retry_interval_s: float = 120.0
    ltp_refresh_s: float = 5.0
    feed_stale_after_s: float = 30.0
    dpl_enabled: bool = True
    hold_release_after_min: float = 3.0         # deliver a held bar anyway when a tracked contract's bar never finalizes
    stop_after_close_min: float = 5.0
    fetch: FetchConfig = field(default_factory=FetchConfig)


def _empty_acc() -> dict:
    return {'open': None, 'high': None, 'low': None, 'close': None}


class _Stream:
    def __init__(self, row: ContractRow):
        self.row, self.ref, self.token = row, row.ref, row.ref.token
        self.raw_past = empty_frame()             # 1-minute rows of the seeded past days (static)
        self.raw_today = empty_frame()            # 1-minute rows of today (grows every minute; small, so windows are cheap)
        self.bars = pd.DataFrame()                # completed 15-minute bars (seeded and live)
        self.version = 0
        self.st_cache: Dict[tuple, tuple] = {}
        self.pending_recovery: List[tuple] = []
        self.pending_boundary: Optional[datetime] = None
        self.deadline: Optional[datetime] = None
        self.deferred = False                     # the window was incomplete when its boundary first arrived
        self.provisional_sent = False
        self.tick_acc = _empty_acc()
        self.polling = False
        self.finalized_boundary: Optional[datetime] = None
        self.seeded = False
        self.seeding = False
        self.rest_ltp: Optional[Tuple[float, datetime]] = None
        self.stale = False
        self.dpl_uc = self.dpl_lc = None
        self.dpl_frozen = False
        self.dpl_frozen_price: Optional[float] = None
        self.dpl_fetching = False
        self.ltp_fetching = False

    @property
    def raw_all(self) -> pd.DataFrame:
        return _safe_concat([self.raw_past, self.raw_today], ignore_index=True)

    def today_count(self, d: date) -> int:
        return len(self.raw_today)


class LiveData:

    def __init__(self, kernel, gateway, executor, catalog: ContractCatalog, calendar: MarketCalendar, feed: Optional[FeedPort],
                 cache_dir, config: Optional[LiveDataConfig] = None, sleep: Callable[[float], None] = time.sleep,
                 alert: Optional[Callable[[str, str], None]] = None):
        self.kernel, self.gateway, self.executor = kernel, gateway, executor
        self.catalog, self.calendar, self.feed = catalog, calendar, feed
        self.cfg = config or LiveDataConfig()
        self.cache = TodayCache(cache_dir)
        self.cache_dir = Path(cache_dir)
        self._sleep = sleep
        self._alert_fn = alert
        self.core = None
        self.streams: Dict[str, _Stream] = {}
        self._held: List[dict] = []
        self.session_date: Optional[date] = None
        self.session_open: Optional[datetime] = None
        self.session_close: Optional[datetime] = None
        self._ticking = False
        self._ltp_started = False

    # ---- plumbing --------------------------------------------------------------------------------------------------------

    def attach(self, core) -> None:
        self.core = core

    def _alert(self, level: str, text: str) -> None:
        if self._alert_fn is not None:
            self._alert_fn(level, text)
        elif self.core is not None:
            self.core._alert(level, None, text)
        else:
            log.warning('%s: %s', level, text)

    def _async(self, work: Callable, done: Callable) -> None:
        """Run `work` on the executor and `done(future)` on the dispatcher."""
        fut = self.executor.submit(work)
        fut.add_done_callback(lambda f: self.kernel.post(lambda: done(f)))

    def _tasks(self) -> list:
        return list(self.core.tasks_snapshot().values()) if self.core is not None else []

    # ---- DataPort: facts -------------------------------------------------------------------------------------------------

    def knows(self, token: str) -> bool:
        return self.catalog.row(token) is not None

    def _today(self) -> date:
        return self.session_date or self.kernel.now.date()

    def infos(self, instrument: str) -> Tuple[ContractInfo, ...]:
        return self.catalog.infos(instrument, self._today(), self.calendar)

    def info(self, ref: ContractRef) -> Optional[ContractInfo]:
        row = self.catalog.row(ref.token)
        if row is None:
            return None
        return ContractInfo(row.ref, row.lot_size, row.tick_size, row.freeze_lots,
                            self.calendar.trading_days_left(self._today(), row.ref.expiry))

    def ref_for(self, token: str) -> Optional[ContractRef]:
        row = self.catalog.row(token)
        return None if row is None else row.ref

    def seeded(self, instrument: str) -> Tuple[ContractRef, ...]:
        return tuple(s.ref for s in self.streams.values() if s.seeded and s.ref.instrument == instrument)

    # ---- DataPort: bars --------------------------------------------------------------------------------------------------

    def _st(self, stream: _Stream, spec) -> pd.DataFrame:
        key = (spec.st_period, spec.st_multiplier)
        cached = stream.st_cache.get(key)
        if cached is None or cached[0] != stream.version:
            cached = (stream.version, compute_st(stream.bars, spec.st_period, spec.st_multiplier))
            stream.st_cache[key] = cached
        return cached[1]

    @staticmethod
    def _bar(row) -> Bar:
        return Bar(ts=pd.Timestamp(row['time_stamp']).to_pydatetime(), open=float(row['open']), high=float(row['high']),
                   low=float(row['low']), close=float(row['close']), volume=float(row['volume']))

    @staticmethod
    def _point(row) -> SupertrendPoint:
        v = row['supertrend']
        if pd.isna(v):
            return SupertrendPoint(None, None, False)
        return SupertrendPoint(float(v), Direction.BULLISH if bool(row['trend']) else Direction.BEARISH, bool(row['trend_flip']))

    def _rows(self, token: str, spec, last_n: Optional[int]) -> list:
        s = self.streams.get(token)
        if s is None or not s.seeded or s.bars.empty:
            return []
        st = self._st(s, spec)
        lo = 0 if last_n is None else max(0, len(st) - last_n)
        return [(self._bar(s.bars.iloc[i]), self._point(st.iloc[i])) for i in range(lo, len(st))]

    def latest_bar(self, token: str, spec):
        rows = self._rows(token, spec, 1)
        return rows[-1] if rows else None

    def st_series(self, token: str, spec, last_n: int):
        return tuple(self._rows(token, spec, last_n))

    def price_near(self, token: str, ts: datetime, tolerance_min: int) -> Optional[float]:
        s = self.streams.get(token)
        if s is None:
            return None
        m = s.raw_all
        if m.empty:
            return None
        window = m[(m['time_stamp'] >= pd.Timestamp(ts) - timedelta(minutes=tolerance_min))
                   & (m['time_stamp'] <= pd.Timestamp(ts) + timedelta(minutes=tolerance_min))]
        if window.empty:
            return None
        return float(window.loc[(window['time_stamp'] - pd.Timestamp(ts)).abs().idxmin(), 'close'])

    # ---- DataPort: prices ------------------------------------------------------------------------------------------------

    def price(self, token: str) -> Optional[float]:
        q = self.ltp_quote(token)
        return None if q is None else q.price

    def ltp_quote(self, token: str) -> Optional[LtpQuote]:
        now = self.kernel.now
        if self.feed is not None:
            p, age = self.feed.get_ltp(token), self.feed.last_tick_age(token)
            if p is not None and age is not None and age <= self.cfg.feed_stale_after_s:
                return LtpQuote(float(p), now - timedelta(seconds=age), float(age))
        s = self.streams.get(token)
        if s is not None and s.rest_ltp is not None and (now - s.rest_ltp[1]).total_seconds() <= 3 * self.cfg.ltp_refresh_s + 5:
            return LtpQuote(s.rest_ltp[0], s.rest_ltp[1], (now - s.rest_ltp[1]).total_seconds())
        if self.feed is not None:                       # a stale feed price is still better than none, and says how old it is
            p, age = self.feed.get_ltp(token), self.feed.last_tick_age(token)
            if p is not None and age is not None:
                return LtpQuote(float(p), now - timedelta(seconds=age), float(age))
        if s is not None and not (s.raw_today.empty and s.raw_past.empty):
            last = (s.raw_today if not s.raw_today.empty else s.raw_past).iloc[-1]
            ts = pd.Timestamp(last['time_stamp']).to_pydatetime()
            return LtpQuote(float(last['close']), ts, (now - ts).total_seconds())
        return None

    # ---- seeding -------------------------------------------------------------------------------------------------------------

    def _backfill_path(self, token: str) -> Path:
        return self.cache_dir / f'{token}_backfill_1m.csv'

    def _read_past(self, row: ContractRow, now: datetime) -> pd.DataFrame:
        past = read_past(row.filepath, now, self.cfg.seed_days, self.cfg.seed_skip_dates)
        bp = self._backfill_path(row.ref.token)
        if bp.exists():
            extra = pd.read_csv(bp, parse_dates=['time_stamp'], float_precision='round_trip')
            extra['time_stamp'] = _naive(extra['time_stamp'])
            cutoff = pd.Timestamp(now).normalize() - timedelta(days=self.cfg.seed_days)
            extra = extra[(extra['time_stamp'] >= cutoff) & (extra['time_stamp'].dt.date < now.date())]
            past = _safe_concat([past, extra], ignore_index=True).drop_duplicates('time_stamp', keep='first')
        return past.sort_values('time_stamp').reset_index(drop=True) if not past.empty else past

    def _first_history_ts(self, row: ContractRow) -> Optional[pd.Timestamp]:
        firsts = []
        for path in (Path(row.filepath), self._backfill_path(row.ref.token)):
            if path.exists():
                head = pd.read_csv(path, nrows=1, parse_dates=['time_stamp'])
                if not head.empty:
                    firsts.append(_naive(head['time_stamp']).iloc[0])
        return min(firsts) if firsts else None

    def _backfill_if_needed(self, row: ContractRow, now: datetime) -> None:
        """Older history a new contract lacks is fetched into a PRIVATE file: Hestia never writes the pipeline's own files."""
        needed_from = now - timedelta(days=self.cfg.seed_days)
        first = self._first_history_ts(row)
        if first is not None and first.date() <= needed_from.date():     # history reaches back to the day it is needed from
            return
        fetch_to = min(datetime.combine(row.ref.expiry, datetime.min.time()), now)
        if needed_from > fetch_to:
            return
        log.info('backfilling %s from %s for seeding', row.ref.symbol, needed_from.date())
        df = fetch_history(self.gateway, row.ref.token, needed_from, fetch_to, self.cfg.fetch, self._sleep)
        merge_and_save(self._backfill_path(row.ref.token), df)

    def _seed_blocking(self, row: ContractRow, now: datetime) -> Optional[_Stream]:
        token = row.ref.token
        self._backfill_if_needed(row, now)
        past = self._read_past(row, now)
        cached = self.cache.read(now, token)
        session_start = pd.Timestamp(f'{now.date()} {self.cfg.session_start}')
        gap_from = cached['time_stamp'].max() + timedelta(minutes=1) if not cached.empty else session_start
        if gap_from < now:
            gap = fetch_one_minute_window(self.gateway, token, gap_from.to_pydatetime(), now, self.cfg.fetch, self._sleep)
            if gap is not None:
                gap = self._closed_minutes(gap, now)
            if gap is not None and not gap.empty:
                self.cache.merge(token, gap)
                cached = _safe_concat([cached, gap], ignore_index=True).drop_duplicates('time_stamp', keep='last')
                cached = cached.sort_values('time_stamp').reset_index(drop=True)
            elif gap is None and cached.empty:
                log.error('seed %s: no cached data for today and the live gap fetch failed', row.ref.symbol)
                return None
            elif gap is None:
                log.warning('seed %s: live gap fetch failed, proceeding with cached data through %s', row.ref.symbol,
                            cached['time_stamp'].max())
        raw = _safe_concat([past, cached], ignore_index=True)
        if raw.empty:
            log.error('seed %s: no 1-minute history after backfill, cache and live fetch', row.ref.symbol)
            return None
        raw = raw.drop_duplicates('time_stamp', keep='last').sort_values('time_stamp').reset_index(drop=True)
        bars = resample_1m(raw, self.cfg.bar_min, now, self.cfg.session_start, closing_time_str)
        if bars.empty:
            log.error('seed %s: resample produced no bars', row.ref.symbol)
            return None
        gaps = find_gaps(bars, self.cfg.bar_min)
        if gaps:
            log.error('seed %s: gap(s) in the reconstructed %dm series, refusing to seed: %s', row.ref.symbol,
                      self.cfg.bar_min, gaps)
            return None
        stream = _Stream(row)
        today = pd.Timestamp(now).normalize()
        stream.raw_past, stream.raw_today = raw[raw['time_stamp'] < today], raw[raw['time_stamp'] >= today].reset_index(drop=True)
        stream.bars, stream.seeded = bars, True
        return stream

    def _seed_with_retry(self, row: ContractRow) -> Optional[_Stream]:
        for attempt in range(1, self.cfg.seed_retry_attempts + 1):
            stream = self._seed_blocking(row, self.kernel.now)
            if stream is not None:
                return stream
            if attempt < self.cfg.seed_retry_attempts:
                self._sleep(self.cfg.seed_retry_interval_s)
        return None

    def prepare(self, instruments: List[str]) -> Dict[str, bool]:
        """Blocking: seed the front live contracts of each instrument. Call from the host's main thread before the engines
        launch. Returns {symbol: seeded}. A contract that cannot be seeded is alerted and left out of `seeded()`."""
        out = {}
        today = self._today()
        for instrument in instruments:
            for row in self.catalog.live_rows(instrument, today)[:self.cfg.seed_contracts_per_instrument]:
                stream = self._seed_with_retry(row)
                if stream is None:
                    self._alert('critical', f'could not seed {row.ref.symbol}; engines will not see it as seeded')
                else:
                    self.streams[row.ref.token] = stream
                    if self.feed is not None:
                        self.feed.subscribe([row.ref.token])
                out[row.ref.symbol] = stream is not None
        return out

    # ---- session and the minute cycle ----------------------------------------------------------------------------------------

    def begin_session(self, session_date: Optional[date] = None) -> None:
        d = session_date or self.kernel.now.date()
        self.session_date = d
        self.session_open = self.calendar.session_open(d)
        self.session_close = self.calendar.session_close(d)
        if not self._ticking:
            self._ticking = True
            now = self.kernel.now
            first = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
            self.kernel.at(first, lambda: self._minute_tick(first))
        if self.cfg.ltp_refresh_s and not self._ltp_started:
            self._ltp_started = True
            self.kernel.after(self.cfg.ltp_refresh_s, self._ltp_tick)

    def _active_tokens(self) -> List[str]:
        wanted = []
        for task in self._tasks():
            if task.state == 'done':
                continue
            for tok in ([task.trading_token] if task.trading_token else []) + sorted(task.tracked):
                if tok not in wanted:
                    wanted.append(tok)
        return [t for t in wanted if t in self.streams and self.streams[t].seeded]

    def _minute_tick(self, boundary: datetime) -> None:
        stop_at = self.session_close + timedelta(minutes=self.cfg.stop_after_close_min)
        if self.kernel.now > stop_at:
            self._ticking = False
            return
        for i, token in enumerate(self._active_tokens()):
            self.kernel.after(i * self.cfg.poll_stagger_s, lambda tok=token: self._cycle(tok, boundary))
        self._flush_held()
        nxt = boundary + timedelta(minutes=1)
        self.kernel.at(nxt, lambda: self._minute_tick(nxt))

    def _harvest_ticks(self, s: _Stream) -> None:
        if self.feed is None:
            return
        chunk = self.feed.get_ohlc(s.token)
        if chunk is None:
            return
        acc = s.tick_acc
        acc['open'] = chunk['open'] if acc['open'] is None else acc['open']
        acc['high'] = chunk['high'] if acc['high'] is None else max(acc['high'], chunk['high'])
        acc['low'] = chunk['low'] if acc['low'] is None else min(acc['low'], chunk['low'])
        acc['close'] = chunk['close']

    def _cycle(self, token: str, boundary: datetime) -> None:
        s = self.streams.get(token)
        if s is None or not s.seeded:
            return
        self._harvest_ticks(s)
        self._check_feed(s)
        self._check_dpl(s)
        win_to = self.kernel.now
        win_from = win_to - timedelta(minutes=self.cfg.poll_window_min)
        if s.polling:                                           # the previous poll has not finished: queue this window
            s.pending_recovery.append((win_from, win_to))
            return
        s.polling = True
        pending, s.pending_recovery = list(s.pending_recovery), []

        def job():
            results = []
            for (f, t) in pending + [(win_from, win_to)]:       # recovery first, then the current window
                df = fetch_one_minute_window(self.gateway, token, f, t, self.cfg.fetch, self._sleep)
                if df is not None:
                    df = self._closed_minutes(df, t)
                    if not df.empty:
                        self.cache.merge(token, df)
                results.append(((f, t), df))
            return results

        def done(fut):
            s.polling = False
            try:
                results = fut.result()
            except Exception as exc:                            # noqa: BLE001
                log.error('poll job for %s failed: %r', token, exc)
                s.pending_recovery.extend(pending + [(win_from, win_to)])
                return
            for window, df in results:
                if df is None:
                    s.pending_recovery.append(window)
                elif not df.empty:
                    self._merge_memory(s, df)
            self._after_merge(s, boundary)
        self._async(job, done)

    @staticmethod
    def _closed_minutes(df: pd.DataFrame, as_of: datetime) -> pd.DataFrame:
        """Drop the minute still forming at `as_of`: a partial candle stored now would be kept by the cache's keep-first merge
        and seed a wrong bar after a restart."""
        return df[df['time_stamp'] < pd.Timestamp(as_of).floor('min')]

    def _merge_memory(self, s: _Stream, df: pd.DataFrame) -> None:
        today = self.kernel.now.date()
        new_today = df[df['time_stamp'].dt.date == today]
        if new_today.empty:
            return
        combined = _safe_concat([s.raw_today, new_today], ignore_index=True)
        s.raw_today = combined.drop_duplicates('time_stamp', keep='last').sort_values('time_stamp').reset_index(drop=True)

    # ---- boundaries and bars --------------------------------------------------------------------------------------------------

    def _window(self, s: _Stream, boundary: datetime) -> pd.DataFrame:
        start = boundary - timedelta(minutes=self.cfg.bar_min)
        m = s.raw_today
        return m[(m['time_stamp'] >= start) & (m['time_stamp'] < boundary)]

    def _after_merge(self, s: _Stream, boundary: datetime) -> None:
        is_bar_boundary = boundary.minute % self.cfg.bar_min == 0
        if is_bar_boundary and s.pending_boundary is None:
            s.pending_boundary = boundary
            s.deadline = boundary + timedelta(minutes=self.cfg.deferred_bar_cutoff_min)
            s.deferred, s.provisional_sent = False, False
            if len(self._window(s, boundary)) < self.cfg.bar_min:
                s.deferred = True
                self._send_provisional(s, boundary)
        if s.pending_boundary is None:
            return
        pb = s.pending_boundary
        window = self._window(s, pb)
        complete = len(window) >= self.cfg.bar_min
        if not complete:
            s.deferred = True
        if complete or self.kernel.now >= s.deadline:
            self._finalize(s, pb, window, complete)

    def _send_provisional(self, s: _Stream, boundary: datetime) -> None:
        acc = s.tick_acc
        if acc['open'] is None or acc['close'] is None:
            return
        start = boundary - timedelta(minutes=self.cfg.bar_min)
        prov = pd.DataFrame([{'time_stamp': pd.Timestamp(start), 'open': acc['open'], 'high': acc['high'], 'low': acc['low'],
                              'close': acc['close'], 'volume': 0.0}])
        for task in self.core.traders_of(s.token):
            spec = task.engine.spec
            if not spec.provisional.enabled:
                continue
            combined = _safe_concat([s.bars, prov], ignore_index=True).drop_duplicates('time_stamp', keep='last')
            st = compute_st(combined.sort_values('time_stamp').reset_index(drop=True), spec.st_period, spec.st_multiplier)
            idx = len(st) - 1
            prev = st.iloc[idx - 1] if idx >= 1 else None
            prev_st = None if prev is None or pd.isna(prev['supertrend']) else float(prev['supertrend'])
            self.core.deliver(task, ProvisionalBar(s.ref, boundary, self._bar(st.iloc[idx]), self._point(st.iloc[idx]), prev_st))
            s.provisional_sent = True

    def _finalize(self, s: _Stream, boundary: datetime, window: pd.DataFrame, complete: bool) -> None:
        start = boundary - timedelta(minutes=self.cfg.bar_min)
        reconciles = s.provisional_sent
        s.pending_boundary = s.deadline = None
        s.provisional_sent = False
        deferred, s.deferred = s.deferred, False
        s.tick_acc = _empty_acc()
        if start < pd.Timestamp(self.session_open):            # a window before today's real open is not a gap
            return
        if window.empty:
            self._alert('critical', f'{s.ref.symbol}: no 1-minute data for {start:%H:%M}-{boundary:%H:%M}; 15m bar skipped, '
                                    f'the ST series has a gap')
            s.finalized_boundary = boundary
            for task in self.core.traders_of(s.token):
                self.core.deliver(task, BarComplete(s.ref, boundary, None, None, None, BarQuality.GAP, 0))
            self._flush_held()
            return
        bar = {'time_stamp': pd.Timestamp(start), 'open': window['open'].iloc[0], 'high': window['high'].max(),
               'low': window['low'].min(), 'close': window['close'].iloc[-1], 'volume': window['volume'].sum()}
        s.bars = (_safe_concat([s.bars, pd.DataFrame([bar])], ignore_index=True)
                  .drop_duplicates('time_stamp', keep='last').sort_values('time_stamp').reset_index(drop=True))
        s.version += 1
        s.finalized_boundary = boundary
        if not complete:
            self._alert('warning', f'{s.ref.symbol}: 15m bar {start:%H:%M}-{boundary:%H:%M} still incomplete '
                                   f'({len(window)}/{self.cfg.bar_min}) after the cutoff; built from what is on hand')
        quality = (BarQuality.COMPLETE if complete and not deferred else BarQuality.RECOVERED if complete
                   else BarQuality.PARTIAL)
        for task in self.core.traders_of(s.token):
            self._held.append({'task': task, 'token': s.token, 'boundary': boundary, 'start': pd.Timestamp(start),
                               'quality': quality, 'minutes': len(window), 'reconciles': reconciles,
                               'release_at': boundary + timedelta(minutes=self.cfg.hold_release_after_min)})
        self._flush_held()

    def _flush_held(self) -> None:
        """Deliver held bars whose tracked contracts have finalized the same boundary (or that waited too long)."""
        remaining = []
        for h in self._held:
            task = h['task']
            if task.state == 'done':
                continue
            waiting = [t for t in task.tracked if t != h['token'] and t in self.streams and self.streams[t].seeded
                       and (self.streams[t].finalized_boundary is None or self.streams[t].finalized_boundary < h['boundary'])]
            if waiting and self.kernel.now < h['release_at']:
                remaining.append(h)
                continue
            if waiting:
                self._alert('warning', f"{h['token']}: delivering the {h['boundary']:%H:%M} bar without tracked contract(s) "
                                       f"{waiting} having finalized theirs")
            self.core.deliver(task, self._bar_event(h, task))
        self._held = remaining

    def _bar_event(self, h: dict, task) -> BarComplete:
        s = self.streams[h['token']]
        st = self._st(s, task.engine.spec)
        idx = int(st.index[st['time_stamp'] == h['start']][0])
        row = st.iloc[idx]
        prev = st.iloc[idx - 1] if idx >= 1 else None
        prev_st = None if prev is None or pd.isna(prev['supertrend']) else float(prev['supertrend'])
        return BarComplete(s.ref, h['boundary'], self._bar(s.bars.iloc[idx]), self._point(row), prev_st, h['quality'],
                           h['minutes'], h['reconciles'])

    # ---- feed staleness, DPL, REST prices --------------------------------------------------------------------------------

    def _watchers(self, s: _Stream, attr: Optional[str] = None) -> list:
        tasks = self.core.traders_of(s.token)
        return [t for t in tasks if attr is None or getattr(t.engine.spec, attr, False)]

    def _check_feed(self, s: _Stream) -> None:
        if self.feed is None or s.today_count(self._today()) == 0:
            return
        age = self.feed.last_tick_age(s.token)
        if age is None:
            return
        if not s.stale and age > self.cfg.feed_stale_after_s:
            s.stale = True
            for t in self._watchers(s):
                self.core.deliver(t, FeedStale(s.ref, float(age)))
        elif s.stale and age <= self.cfg.feed_stale_after_s:
            s.stale = False
            for t in self._watchers(s):
                self.core.deliver(t, FeedRecovered(s.ref))

    def _fetch_dpl(self, s: _Stream) -> None:
        if s.dpl_fetching:
            return
        s.dpl_fetching = True
        exch = self.cfg.fetch.exchange

        def job():
            resp = self.gateway.market_data('FULL', {exch: [s.token]})
            row = (resp.get('data', {}).get('fetched') or [None])[0]
            return None if row is None else (float(row['upperCircuit']), float(row['lowerCircuit']))

        def done(fut):
            s.dpl_fetching = False
            try:
                band = fut.result()
            except Exception as exc:                            # noqa: BLE001
                log.warning('DPL circuit-limit fetch failed for %s: %s', s.ref.symbol, exc)
                return
            if band:
                s.dpl_uc, s.dpl_lc = band
        self._async(job, done)

    def _check_dpl(self, s: _Stream) -> None:
        """Alert-only, like Prometheus's: a real freeze pins the price at exactly the circuit limit tick after tick, and
        'unfrozen' is the price moving off that value (the band has then very likely widened, so it is re-fetched)."""
        watchers = self._watchers(s, 'watch_dpl')
        if not self.cfg.dpl_enabled or not watchers or s.today_count(self._today()) == 0:
            return
        if s.dpl_uc is None or s.dpl_lc is None:
            self._fetch_dpl(s)
            return
        price = self.price(s.token)
        if price is None:
            return
        if not s.dpl_frozen:
            if price >= s.dpl_uc or price <= s.dpl_lc:
                s.dpl_frozen, s.dpl_frozen_price = True, price
                for t in watchers:
                    self.core.deliver(t, DplFrozen(s.ref, price, True))
        elif price != s.dpl_frozen_price:
            was = s.dpl_frozen_price
            s.dpl_frozen, s.dpl_frozen_price = False, None
            self._fetch_dpl(s)
            for t in watchers:
                self.core.deliver(t, DplFrozen(s.ref, was, False))

    def _ltp_tick(self) -> None:
        """A REST price for any traded token the feed is not covering, so `price` never blocks and is never absent."""
        for token in self._active_tokens():
            s = self.streams[token]
            covered = (self.feed is not None and self.feed.get_ltp(token) is not None
                       and (self.feed.last_tick_age(token) or 0) <= self.cfg.feed_stale_after_s)
            if covered or s.ltp_fetching or s.today_count(self._today()) == 0:
                continue
            s.ltp_fetching = True
            self._async(lambda s=s: self.gateway.ltp(self.cfg.fetch.exchange, s.ref.symbol, s.token),
                        lambda fut, s=s: self._on_rest_ltp(s, fut))
        self.kernel.after(self.cfg.ltp_refresh_s, self._ltp_tick)

    def _on_rest_ltp(self, s: _Stream, fut) -> None:
        s.ltp_fetching = False
        try:
            ltp = fut.result()['data']['ltp']
            if ltp is not None:
                s.rest_ltp = (float(ltp), self.kernel.now)
        except Exception as exc:                                # noqa: BLE001
            log.warning('REST ltp fallback failed for %s: %s', s.ref.symbol, exc)

    # ---- tracking and the trading-contract hook -----------------------------------------------------------------------------

    def _ensure_stream(self, row: ContractRow, on_ready: Callable[[Optional[_Stream]], None]) -> None:
        existing = self.streams.get(row.ref.token)
        if existing is not None and existing.seeded:
            self.kernel.post(lambda: on_ready(existing))
            return
        if self.feed is not None:
            self.feed.subscribe([row.ref.token])
        self._async(lambda: self._seed_blocking(row, self.kernel.now), lambda fut: self._install(row, fut, on_ready))

    def _install(self, row: ContractRow, fut, on_ready) -> None:
        try:
            stream = fut.result()
        except Exception as exc:                                # noqa: BLE001
            log.error('seeding %s failed: %r', row.ref.symbol, exc)
            stream = None
        if stream is not None:
            self.streams[row.ref.token] = stream
        on_ready(stream)

    def track(self, task, contract: ContractRef) -> None:
        row = self.catalog.row(contract.token)
        if row is None:
            self.kernel.after(0, lambda: self.core.deliver_to(task.name, TrackFailed(contract, 'unknown contract')))
            return
        task.tracked.add(contract.token)

        def ready(stream):
            if stream is None:
                task.tracked.discard(contract.token)
                self.core.deliver_to(task.name, TrackFailed(contract, 'seeding failed'))
            else:
                self.core.deliver_to(task.name, TrackReady(contract))
        self._ensure_stream(row, ready)

    def untrack(self, task, contract: ContractRef) -> None:
        task.tracked.discard(contract.token)                    # the stream is kept: it is cheap and a roll may come back to it

    def trading_contract_set(self, task, contract: ContractRef) -> None:
        row = self.catalog.row(contract.token)
        if row is not None:
            self._ensure_stream(row, lambda stream: None if stream is not None else self._alert(
                'critical', f'{contract.symbol} could not be seeded for {task.name}'))
