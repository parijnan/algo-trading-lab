"""
prometheus_backtest/phase3/data_loader_p3.py

Corrected CRUDEOILM loader for the Jan-30-to-current Phase 3 backtest
window -- replaces the naive "nearest unexpired expiry" front-month
selection in ../data_loader.py's load_futures_1min with production's
actual tender-margin early-roll rule (TENDER_ROLL_TRADING_DAYS=5,
prometheus_production/prometheus_configs.py), mirroring
resolve_effective_contract (prometheus_production/prometheus_functions.py)
exactly: for each date, trading_days_left = trading days from that date
through the front-month contract's own expiry inclusive (weekends and
MCX full-holidays excluded, mcx_holidays.csv); roll to the next contract
once trading_days_left <= 5. Confirmed directly against production's own
Sept-21 expiry -> Oct-19 rollover this session: the naive rule kept
trading Sept prices through 2026-09-18 while production had already
rolled onto Oct -- a ~450-point (~4.5%) divergence, real, not
hypothetical (see 2026-09-19 investigation).

Per-date price source: prefer Fyers (data_pipeline/data/mcx_fyers/,
generally the cleaner source), fall back to AngelOne
(data_pipeline/data/mcx/) for any date the effective contract's Fyers
file is missing entirely or has no rows that day -- covers both the
already-documented 2026-03-13->2026-06-29 Fyers void and the fact Fyers
has no Sept/Oct-2026 CRUDEOILM contract data at all yet. User's own
2026-09-19 decision: accept AngelOne as the fallback throughout, rollover
weeks included, rather than leaving a gap.

Each returned row carries `data_source` ('fyers' or 'angelone_fallback')
so any trade touching a fallback stretch stays identifiable, same
convention as phase3_fyers/data_loader_fyers.py.
"""
import os
import sys
import glob
from pathlib import Path

import pandas as pd

_PHASE3_DIR = Path(__file__).parent
_PROMETHEUS_BACKTEST_DIR = _PHASE3_DIR.parent
_REPO_ROOT = _PROMETHEUS_BACKTEST_DIR.parent

sys.path.insert(0, str(_PROMETHEUS_BACKTEST_DIR))
import data_loader as _angelone_loader  # noqa: E402

# Reused verbatim -- pure functions, no data-source dependency.
resample_ohlcv = _angelone_loader.resample_ohlcv
compute_st = _angelone_loader.compute_st
_CRUDEOILM_OPENING_BAR_CORRECTIONS = _angelone_loader._CRUDEOILM_OPENING_BAR_CORRECTIONS

ANGELONE_DATA_DIR = os.path.join(_REPO_ROOT, 'data_pipeline', 'data', 'mcx')
FYERS_DATA_DIR = os.path.join(_REPO_ROOT, 'data_pipeline', 'data', 'mcx_fyers')
MCX_HOLIDAYS_FILE = os.path.join(_REPO_ROOT, 'data_pipeline', 'data', 'mcx_holidays.csv')

TENDER_ROLL_TRADING_DAYS = 5  # must match prometheus_production/prometheus_configs.py


def _load_fully_closed_dates() -> set:
    if not os.path.exists(MCX_HOLIDAYS_FILE):
        return set()
    df = pd.read_csv(MCX_HOLIDAYS_FILE)
    df['date'] = pd.to_datetime(df['date']).dt.date
    closed = df[df['morning_session_closed'] & df['evening_session_closed']]
    return set(closed['date'])


def _count_trading_days_inclusive(start_date, end_date, fully_closed: set) -> int:
    """Verbatim mirror of prometheus_functions._count_trading_days_inclusive."""
    if start_date > end_date:
        return 0
    days = pd.date_range(start_date, end_date, freq='D')
    return sum(1 for d in days if d.weekday() < 5 and d.date() not in fully_closed)


def _discover_expiries(symbol: str) -> list:
    """Union of every expiry either source has a contract file for."""
    expiries = set()
    for base in (ANGELONE_DATA_DIR, FYERS_DATA_DIR):
        for f in glob.glob(os.path.join(base, symbol, '*_futures.csv')):
            expiries.add(pd.Timestamp(os.path.basename(f).replace('_futures.csv', '')).date())
    return sorted(expiries)


def _effective_contract_for_date(d, expiry_calendar: list, fully_closed: set):
    """Mirrors resolve_effective_contract (prometheus_functions.py) exactly:
    front = nearest expiry >= d; roll to the next listed contract once
    <= TENDER_ROLL_TRADING_DAYS trading days remain until front's own expiry."""
    candidates = [e for e in expiry_calendar if e >= d]
    if not candidates:
        return None
    front = candidates[0]
    trading_days_left = _count_trading_days_inclusive(d, front, fully_closed)
    if trading_days_left <= TENDER_ROLL_TRADING_DAYS and len(candidates) > 1:
        return candidates[1]
    return front


def _naive_front_month_for_date(d, expiry_calendar: list):
    """The pre-fix, naive "nearest unexpired expiry" pick -- last-resort
    fallback only, for the handful of old rollover weeks (2026-03/04/05/06)
    where neither source has data yet for the correctly early-rolled
    contract at all, because neither pipeline was tracking a "next month"
    contract that far back historically. User's own 2026-09-19 decision:
    make an exception and use whatever data exists rather than drop these
    days -- this is that exception, applied only when the effective
    contract has no data from either source on this date."""
    candidates = [e for e in expiry_calendar if e >= d]
    return candidates[0] if candidates else None


def _read_contract_file(base_dir: str, symbol: str, expiry) -> pd.DataFrame:
    path = os.path.join(base_dir, symbol, f'{expiry}_futures.csv')
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path, parse_dates=['time_stamp'])
    df['time_stamp'] = pd.to_datetime(df['time_stamp']).dt.tz_localize(None)
    df = df[(df['close'].notna()) & (df['close'] > 0)]
    return df


def load_futures_1min(symbol: str, start: str = '2026-01-30') -> pd.DataFrame:
    if symbol != 'CRUDEOILM':
        raise NotImplementedError(
            f"data_loader_p3 is scoped to CRUDEOILM only (the corrected-rollover fix was "
            f"requested for the live-traded instrument, 2026-09-19) -- got {symbol!r}.")

    start_date = pd.Timestamp(start).date()
    expiry_calendar = _discover_expiries(symbol)
    fully_closed = _load_fully_closed_dates()

    # load every contract file from both sources once, grouped by expiry + which
    # calendar dates each source actually covers for that expiry
    fyers = {e: _read_contract_file(FYERS_DATA_DIR, symbol, e) for e in expiry_calendar}
    angelone = {e: _read_contract_file(ANGELONE_DATA_DIR, symbol, e) for e in expiry_calendar}
    fyers_dates = {e: set(df['time_stamp'].dt.date) for e, df in fyers.items() if len(df)}
    angelone_dates = {e: set(df['time_stamp'].dt.date) for e, df in angelone.items() if len(df)}

    all_dates = sorted(set().union(*fyers_dates.values(), *angelone_dates.values()) if (fyers_dates or angelone_dates) else set())
    all_dates = [d for d in all_dates if d >= start_date and pd.Timestamp(d).weekday() < 5]

    day_frames = []
    for d in all_dates:
        expiry = _effective_contract_for_date(d, expiry_calendar, fully_closed)
        if expiry is None:
            continue
        if d in fyers_dates.get(expiry, set()):
            df = fyers[expiry]
            chunk = df[df['time_stamp'].dt.date == d].copy()
            chunk['data_source'] = 'fyers'
        elif d in angelone_dates.get(expiry, set()):
            df = angelone[expiry]
            chunk = df[df['time_stamp'].dt.date == d].copy()
            chunk['data_source'] = 'angelone_fallback'
        else:
            # correctly early-rolled contract has no data from either source
            # yet (historically true for the 2026-03/04/05/06 rollover weeks,
            # before either pipeline tracked a "next month" contract this far
            # back) -- last-resort exception, per the user's own 2026-09-19
            # decision: fall back to whichever contract the naive rule would
            # have used, rather than drop the day.
            naive_expiry = _naive_front_month_for_date(d, expiry_calendar)
            if naive_expiry is not None and d in fyers_dates.get(naive_expiry, set()):
                df = fyers[naive_expiry]
                chunk = df[df['time_stamp'].dt.date == d].copy()
                chunk['data_source'] = 'fyers_naive_fallback'
                expiry = naive_expiry
            elif naive_expiry is not None and d in angelone_dates.get(naive_expiry, set()):
                df = angelone[naive_expiry]
                chunk = df[df['time_stamp'].dt.date == d].copy()
                chunk['data_source'] = 'angelone_naive_fallback'
                expiry = naive_expiry
            else:
                continue  # truly no data anywhere for this date -- skip, don't fabricate
        chunk['contract_expiry'] = str(expiry)
        day_frames.append(chunk)

    full = pd.concat(day_frames, ignore_index=True)

    for ts, ohlc in _CRUDEOILM_OPENING_BAR_CORRECTIONS.items():
        mask = (full['time_stamp'] == ts) & (full['data_source'] == 'angelone_fallback')
        if mask.any():
            full.loc[mask, ['open', 'high', 'low', 'close']] = [
                ohlc['open'], ohlc['high'], ohlc['low'], ohlc['close']]

    full = full.set_index('time_stamp').sort_index()
    return full
