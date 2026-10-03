"""
Candle fetching through the gateway: the resilient one-minute window poll and the multi-day history backfill.

Ported from prometheus_functions.fetch_one_minute_window (inner burst of attempts, a failed burst returns None so the caller
can queue the window for later recovery) and data_downloader_mcx.fetch_candle_chunk (day-range chunks). The candle budget
(3 a second, account-wide) is the gateway's, so nothing here counts calls. `AB1021` ("exceeding access rate") failures are
Angel One's own capacity refusals and are expected: they cost a retry, never an alert by themselves.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

import pandas as pd

from hestia_core.history import OHLCV, empty_frame

log = logging.getLogger('hestia_candles')

METHOD_TS_FMT = '%Y-%m-%dT%H:%M:%S%z'          # what getCandleData returns: ISO with the +05:30 offset
MARKET_OPEN, MARKET_CLOSE = '09:00', '23:30'   # the chunk request window, as in the pipeline downloader


@dataclass
class FetchConfig:
    exchange: str = 'MCX'
    inner_attempts: int = 5                    # raised from 3 on 2026-09-04 after a live day of frequent AB1021
    inner_interval_s: float = 1.0
    chunk_days: int = 2
    rate_backoff_s: float = 5.0
    rate_max_retries: int = 5


def _to_frame(raw) -> pd.DataFrame:
    df = pd.DataFrame(raw, columns=OHLCV)
    df['time_stamp'] = pd.to_datetime(df['time_stamp'], format=METHOD_TS_FMT, utc=False, errors='coerce')
    if df['time_stamp'].dt.tz is not None:
        df['time_stamp'] = df['time_stamp'].dt.tz_localize(None)     # every series in Hestia is tz-naive IST
    return df


def fetch_one_minute_window(gateway, token: str, from_dt: datetime, to_dt: datetime, cfg: FetchConfig,
                            sleep: Callable[[float], None] = time.sleep, high: bool = False,
                            stats: Optional[dict] = None) -> Optional[pd.DataFrame]:
    """A frame (possibly empty: the fetch worked, nothing new yet) or None when the whole burst failed. `stats`, when given, is
    filled with {'attempts': n, 'exhausted': bool} for the caller's own bookkeeping (the Fyers shadow's Angel-side record); it never
    changes what is fetched or returned."""
    from_str, to_str = from_dt.strftime('%Y-%m-%d %H:%M'), to_dt.strftime('%Y-%m-%d %H:%M')
    if stats is not None:
        stats.update(attempts=0, exhausted=False)
    for attempt in range(1, cfg.inner_attempts + 1):
        if stats is not None:
            stats['attempts'] = attempt
        try:
            response = gateway.candles({'exchange': cfg.exchange, 'symboltoken': str(token), 'interval': 'ONE_MINUTE',
                                        'fromdate': from_str, 'todate': to_str}, high=high)
            raw = response.get('data') if isinstance(response, dict) else None
            if raw is not None:
                log.info('fetch ok [%s -> %s] attempt %d/%d (%d candles)', from_str, to_str, attempt, cfg.inner_attempts, len(raw))
                return _to_frame(raw)
            log.warning('fetch failed (empty response) [%s -> %s] attempt %d/%d: %s', from_str, to_str, attempt,
                        cfg.inner_attempts, response)
        except Exception as exc:                                    # noqa: BLE001
            code = 'AB1021' if 'exceeding access rate' in str(exc) else 'EXCEPTION'
            (log.warning if code == 'AB1021' else log.error)('fetch failed [%s -> %s] attempt %d/%d: %s - %s', from_str,
                                                             to_str, attempt, cfg.inner_attempts, code, exc)
        if attempt < cfg.inner_attempts:
            sleep(cfg.inner_interval_s)
    log.error('inner burst exhausted [%s -> %s]: deferring to the recovery queue', from_str, to_str)
    if stats is not None:
        stats['exhausted'] = True
    return None


def fetch_history(gateway, token: str, from_dt: datetime, to_dt: datetime, cfg: FetchConfig,
                  sleep: Callable[[float], None] = time.sleep) -> pd.DataFrame:
    """One-minute history over whole days, in `chunk_days` chunks; rows outside [from_dt, to_dt] are dropped (the broker
    sometimes returns stray dates). A chunk that fails for a non-rate reason contributes nothing; the caller's gap check then
    refuses to seed over the hole."""
    frames = []
    day = from_dt
    while day <= to_dt:
        chunk_end = min(day + timedelta(days=cfg.chunk_days - 1), to_dt)
        params = {'exchange': cfg.exchange, 'symboltoken': str(token), 'interval': 'ONE_MINUTE',
                  'fromdate': f"{day:%Y-%m-%d} {MARKET_OPEN}", 'todate': f"{chunk_end:%Y-%m-%d} {MARKET_CLOSE}"}
        for attempt in range(cfg.rate_max_retries + 1):
            try:
                response = gateway.candles(params)
            except Exception as exc:                                # noqa: BLE001
                if 'exceeding access rate' in str(exc) and attempt < cfg.rate_max_retries:
                    sleep(cfg.rate_backoff_s)
                    continue
                log.warning('history chunk %s..%s failed: %s', params['fromdate'], params['todate'], exc)
                break
            raw = response.get('data') if isinstance(response, dict) else None
            if raw:
                df = _to_frame(raw)
                lo = pd.Timestamp(f"{day:%Y-%m-%d} {MARKET_OPEN}")
                hi = pd.Timestamp(f"{chunk_end:%Y-%m-%d} {MARKET_CLOSE}")
                frames.append(df[(df['time_stamp'] >= lo) & (df['time_stamp'] <= hi)])
            break
        day += timedelta(days=cfg.chunk_days)
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else empty_frame()
