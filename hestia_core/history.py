"""
One-minute history handling for the live data service: past days from the pipeline's per-contract files, the private
per-token intraday cache, resampling to N-minute bars, and gap checks.

Ported from prometheus_functions (`_tail_read_contract_csv`, `_merge_and_save`, `read_today_cache`, `_resample_1m_to_Nmin`,
`_find_Nmin_gaps`), not imported (that module reads configuration at import and logs into the real dated file), and pinned
against it by tests/test_hestia_history.py. Two deliberate differences: the intraday cache is one file per token, so two
engines never rewrite one shared file, and Hestia never writes the pipeline's own files (older history it has to fetch itself
goes into a private backfill file beside the cache).
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Callable, Iterable, List, Optional

import pandas as pd

log = logging.getLogger('hestia_history')

OHLCV = ['time_stamp', 'open', 'high', 'low', 'close', 'volume']
TS_FMT = '%Y-%m-%dT%H:%M:%S'                    # base format of the saved files; +05:30 is appended per row


def format_timestamp(ts: pd.Timestamp) -> str:
    if ts.tzinfo is None:
        ts = ts.tz_localize('Asia/Kolkata')
    offset = ts.strftime('%z')
    return ts.strftime(TS_FMT) + offset[:-2] + ':' + offset[-2:]


def _format_series(ts: pd.Series) -> pd.Series:
    """format_timestamp for a whole naive column at once. IST has no DST, so the offset is a constant; the per-row version is
    orders of magnitude slower and runs on every minute's cache write."""
    return ts.dt.strftime(TS_FMT) + '+05:30'


def _safe_concat(dfs: list, **kwargs) -> pd.DataFrame:
    """concat that ignores empty frames (avoids the dtype-inference FutureWarning and the all-NA column trap)."""
    keep = [d for d in dfs if d is not None and not d.empty]
    if not keep:
        return pd.DataFrame(columns=dfs[0].columns if dfs else OHLCV)
    return pd.concat(keep, **kwargs)


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=OHLCV)


def _naive(series: pd.Series) -> pd.Series:
    if series.dtype == object:                       # strings from a CSV: ISO 8601, with or without the +05:30 offset
        s = pd.to_datetime(series, utc=False, errors='coerce', format='ISO8601')
    else:
        s = pd.to_datetime(series, utc=False, errors='coerce')
    return s.dt.tz_localize(None) if s.dt.tz is not None else s


def merge_and_save(filepath: os.PathLike, new_df: pd.DataFrame) -> int:
    """Merge `new_df` into the CSV at `filepath` (dedupe on time_stamp keeping the first, sort) and return the number of new
    rows. Both sides are normalised to tz-naive first: mixing a fixed-offset and a named-zone dtype silently turns the older
    rows into NaT (a bug found in the original on 2026-09-04)."""
    if new_df is None or new_df.empty:
        return 0
    filepath = Path(filepath)
    on_disk = pd.read_csv(filepath, parse_dates=['time_stamp']) if filepath.exists() else pd.DataFrame(columns=list(new_df.columns))
    if not on_disk.empty:
        on_disk['time_stamp'] = _naive(on_disk['time_stamp'])
    new_df = new_df.copy()
    new_df['time_stamp'] = _naive(new_df['time_stamp'])
    before = len(on_disk)
    merged = _safe_concat([on_disk, new_df], ignore_index=True)
    merged['time_stamp'] = pd.to_datetime(merged['time_stamp'], utc=False, errors='coerce')
    merged = merged.drop_duplicates(subset=['time_stamp'], keep='first').sort_values('time_stamp').reset_index(drop=True)
    save = merged.copy()
    save['time_stamp'] = _format_series(save['time_stamp'])
    filepath.parent.mkdir(parents=True, exist_ok=True)
    save.to_csv(filepath, index=False)
    return len(merged) - before


def read_past(filepath: os.PathLike, now: datetime, n_days: int, skip_dates: Iterable[str] = ()) -> pd.DataFrame:
    """The contract's own pipeline file, tail-read (about 950 rows a day), restricted to the `n_days` before today and never
    including today (today's data comes from the private cache and live fetches)."""
    filepath = str(filepath)
    if not os.path.exists(filepath):
        return empty_frame()
    tail_n = 950 * (n_days + 5)
    header = subprocess.run(['head', '-1', filepath], capture_output=True, text=True).stdout
    tail = subprocess.run(['tail', f'-n{tail_n}', filepath], capture_output=True, text=True).stdout
    if not tail.strip():
        return empty_frame()
    df = pd.read_csv(StringIO(header + tail), float_precision='round_trip')
    df['time_stamp'] = _naive(df['time_stamp'])
    df = df[df['time_stamp'].notna()].copy()             # a file shorter than the tail repeats its header as a row
    for col in OHLCV[1:]:
        df[col] = pd.to_numeric(df[col])
    cutoff = pd.Timestamp(now).normalize() - timedelta(days=n_days)
    df = df[(df['time_stamp'] >= cutoff) & (df['time_stamp'].dt.date < now.date())]
    skip = {pd.Timestamp(d).date() for d in skip_dates}
    if skip:
        df = df[~df['time_stamp'].dt.date.isin(skip)]
    return df.sort_values('time_stamp').reset_index(drop=True)


class TodayCache:
    """Per-token private intraday cache: `<dir>/<token>_today_1m.csv`. Nothing else reads or writes these files. Read is
    date-filtered and self-pruning (a same-day restart reuses the rows; a new day drops the old ones)."""

    def __init__(self, directory: os.PathLike):
        self.dir = Path(directory)
        self._seen: dict = {}                           # token -> set of timestamps already on disk (this process is the only writer)

    def path(self, token: str) -> Path:
        return self.dir / f'{token}_today_1m.csv'

    def _seen_for(self, token: str) -> set:
        if token not in self._seen:
            p = self.path(token)
            if p.exists():
                ts = pd.read_csv(p, usecols=['time_stamp'])['time_stamp']
                self._seen[token] = set(_naive(ts))
            else:
                self._seen[token] = set()
        return self._seen[token]

    def read(self, now: datetime, token: str) -> pd.DataFrame:
        p = self.path(token)
        if not p.exists():
            return empty_frame()
        df = pd.read_csv(p, parse_dates=['time_stamp'], float_precision='round_trip')
        if df.empty:
            return empty_frame()
        df['time_stamp'] = _naive(df['time_stamp'])
        todays = df[df['time_stamp'].dt.date == now.date()]
        if len(todays) < len(df):
            self._rewrite(token, todays)
        return todays.sort_values('time_stamp').reset_index(drop=True)

    def merge(self, token: str, df: pd.DataFrame) -> int:
        """Append the rows the file does not have yet (an existing minute is kept, never overwritten). Append-only, because
        this runs every minute for every active token; readers sort."""
        if df is None or df.empty:
            return 0
        seen = self._seen_for(token)
        new = df[OHLCV].copy()
        new['time_stamp'] = _naive(new['time_stamp'])
        new = new[~new['time_stamp'].isin(seen)].drop_duplicates('time_stamp', keep='first')
        if new.empty:
            return 0
        out = new.copy()
        out['time_stamp'] = _format_series(out['time_stamp'])
        p = self.path(token)
        p.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(p, mode='a', header=not p.exists(), index=False)
        seen.update(new['time_stamp'])
        return len(new)

    def _rewrite(self, token: str, df: pd.DataFrame) -> None:
        p = self.path(token)
        self._seen.pop(token, None)
        if df.empty:
            if p.exists():
                p.unlink()
            return
        save = df.copy()
        save['time_stamp'] = _format_series(save['time_stamp'])
        save.to_csv(p, index=False)


def resample_1m(df_1m: pd.DataFrame, minutes: int, now: datetime, session_start: str,
                close_time_of: Callable[[date], str]) -> pd.DataFrame:
    """1-minute rows to N-minute bars, anchored at `session_start` clock time, day by day, each day's buckets stopping at that
    day's close. A window counts only once it is done: fully elapsed by `now`, or its day has already closed (then it is
    built from whatever real rows exist, as the chart does for the short last bucket). A window still forming in a live
    session is skipped, because more rows are still to arrive and a bar built now would silently change."""
    candles = []
    for day, day_df in df_1m.groupby(df_1m['time_stamp'].dt.date):
        anchor = pd.Timestamp(f'{day} {session_start}')
        day_cutoff = pd.Timestamp(f'{day} {close_time_of(day)}')
        day_has_closed = now >= day_cutoff
        while anchor <= day_cutoff:
            window_end = anchor + timedelta(minutes=minutes) - timedelta(minutes=1)
            if anchor + timedelta(minutes=minutes) > now and not day_has_closed:
                break
            window = day_df[(day_df['time_stamp'] >= anchor) & (day_df['time_stamp'] <= window_end)]
            if not window.empty:
                candles.append({'time_stamp': anchor, 'open': window['open'].iloc[0], 'high': window['high'].max(),
                                'low': window['low'].min(), 'close': window['close'].iloc[-1],
                                'volume': window['volume'].sum()})
            anchor += timedelta(minutes=minutes)
    return pd.DataFrame(candles).reset_index(drop=True) if candles else pd.DataFrame()


def find_gaps(df: pd.DataFrame, minutes: int) -> List[tuple]:
    """Non-contiguous stretches inside a day of N-minute bars: a series with a hole is refused, never seeded silently."""
    gaps = []
    for day, day_df in df.groupby(df['time_stamp'].dt.date):
        ts = day_df['time_stamp'].sort_values()
        expected = pd.date_range(ts.iloc[0], ts.iloc[-1], freq=f'{minutes}min')
        missing = sorted(set(expected) - set(ts))
        if missing:
            gaps.append((day, missing))
    return gaps
