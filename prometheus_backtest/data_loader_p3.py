"""
prometheus_backtest/data_loader_p3.py

Corrected CRUDEOILM/CRUDEOIL loader for the Jan-30-to-current Phase 3
backtest window -- replaces the naive "nearest unexpired expiry"
front-month selection in ./data_loader.py's load_futures_1min with
production's actual tender-margin early-roll rule
(TENDER_ROLL_TRADING_DAYS=5, prometheus_production/prometheus_configs.py).
Shared between phase3/ (CRUDEOILM) and phase3_crudeoil/ (CRUDEOIL) --
originally built CRUDEOILM-only (2026-09-19), generalized the same day
once the user asked for CRUDEOIL too; nothing below is actually
CRUDEOILM-specific except the opening-bar-correction table, which stays
explicitly guarded to that symbol only (see load_futures_1min). Two
layers, both mirrored exactly from prometheus_production/prometheus.py,
not just approximated:

1. resolve_effective_contract's own per-date rule (prometheus_functions.py):
   trading_days_left = trading days from that date through the front-month
   contract's own expiry inclusive (weekends and MCX full-holidays
   excluded, mcx_holidays.csv); roll to the next contract once
   trading_days_left <= 5.
2. _check_rollover_tonight's eve-lookahead on top of that (prometheus.py):
   production checks, at every session's setup and again on every mid-day
   flat transition (2026-09-14 self-heal fix), whether TOMORROW would
   resolve differently -- if so and the position is flat, it switches
   RIGHT NOW rather than waiting, one trading day earlier than layer 1
   alone would. Confirmed empirically: this reproduces the real
   2026-09-14 live roll to the October contract exactly (Sept-14 alone
   has 6 trading days left under layer 1, Sept-15 has exactly 5 -- the
   eve-check is what pulls the switch back to the 14th). Known, documented
   simplification: doesn't model the in-trade deferral branch (evening
   ROLLOVER_TIME / same-day coincident-flip) -- see
   _effective_contract_for_date's own docstring for why.

Confirmed directly against production's own Sept-21 expiry -> Oct-19
rollover this session: the naive (pre-fix) rule kept trading Sept prices
through 2026-09-18 while production had already rolled onto Oct -- a
~450-point (~4.5%) divergence, real, not hypothetical.

Per-date price source: prefer Fyers (data_pipeline/data/mcx_fyers/,
generally the cleaner source), fall back to AngelOne
(data_pipeline/data/mcx/) for any date the effective contract's Fyers
file is missing entirely or has no rows that day -- covers both the
already-documented 2026-03-13->2026-06-29 Fyers void and the fact Fyers
has no Sept/Oct-2026 contract data at all yet, for either symbol. User's own
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

_PROMETHEUS_BACKTEST_DIR = Path(__file__).parent
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


def _next_trading_day(d, fully_closed: set):
    """Verbatim mirror of prometheus_functions.next_trading_day -- the next
    date after d that isn't fully closed (weekend or both-sessions-closed
    MCX holiday). A date closing only one session (e.g. 2026-09-14,
    Ganesh Chaturthi -- morning closed, evening open) still counts as a
    trading day here, same as production."""
    import datetime as _dt
    nd = d + _dt.timedelta(days=1)
    while nd.weekday() >= 5 or nd in fully_closed:
        nd += _dt.timedelta(days=1)
    return nd


def _plain_resolve(d, expiry_calendar: list, fully_closed: set):
    """The single-date resolve_effective_contract computation with no
    lookahead -- front = nearest unexpired expiry >= d, rolled early once
    <= TENDER_ROLL_TRADING_DAYS trading days remain. Used both standalone
    (via _effective_contract_for_date below, which adds the eve-lookahead)
    and internally by that lookahead itself."""
    candidates = [e for e in expiry_calendar if e >= d]
    if not candidates:
        return None
    front = candidates[0]
    trading_days_left = _count_trading_days_inclusive(d, front, fully_closed)
    if trading_days_left <= TENDER_ROLL_TRADING_DAYS and len(candidates) > 1:
        return candidates[1]
    return front


def _discover_expiries(symbol: str) -> list:
    """Union of every expiry either source has a contract file for."""
    expiries = set()
    for base in (ANGELONE_DATA_DIR, FYERS_DATA_DIR):
        for f in glob.glob(os.path.join(base, symbol, '*_futures.csv')):
            expiries.add(pd.Timestamp(os.path.basename(f).replace('_futures.csv', '')).date())
    return sorted(expiries)


def _effective_contract_for_date(d, expiry_calendar: list, fully_closed: set):
    """Mirrors _check_rollover_tonight (prometheus.py), not just
    resolve_effective_contract alone -- production checks, at every
    session's own setup (and again on every mid-day flat transition, the
    2026-09-14 self-heal fix), whether TOMORROW's trading day would
    resolve to a different contract than today's; if so AND the position
    is flat (the default/common case -- see caveat below), it switches
    RIGHT NOW rather than waiting. Net effect, confirmed by direct
    derivation and empirically matching the real 2026-09-14 live roll
    exactly (one trading day before resolve_effective_contract(today)
    alone would have rolled, since Sept-14 evaluated on its own still had
    6 trading days left (>5) but Sept-15 had exactly 5): a date's
    effective contract is resolve_effective_contract's plain, no-lookahead
    computation evaluated on next_trading_day(d), not on d itself.

    KNOWN SIMPLIFICATION, not modeled: if a backtested position happens to
    be genuinely open exactly at a roll-eve, production defers to the
    scheduled evening mechanism (ROLLOVER_TIME) or an even-earlier
    same-day coincident-flip transition (§18 Phase 3) instead of switching
    at setup -- a materially different, path-dependent timing this pure
    date-function can't see (contract selection would need to know the
    backtest's own trade-simulation state, which itself depends on
    contract selection -- circular, not resolved here). Treating "flat"
    as universal is the pragmatic choice: it's production's default path,
    the 2026-09-14 fix makes it fire promptly even mid-day, and modeling
    the in-trade branch would need a fundamentally different (iterative,
    state-aware) backtest architecture."""
    return _plain_resolve(_next_trading_day(d, fully_closed), expiry_calendar, fully_closed)


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
    if symbol not in ('CRUDEOILM', 'CRUDEOIL'):
        raise NotImplementedError(f"data_loader_p3 supports CRUDEOILM and CRUDEOIL only -- got {symbol!r}.")

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

    # Confirmed CRUDEOILM-specific thin-liquidity artifact (../data_loader.py's own
    # docstring) -- never applied to CRUDEOIL, which has no evidence of the same defect.
    if symbol == 'CRUDEOILM':
        for ts, ohlc in _CRUDEOILM_OPENING_BAR_CORRECTIONS.items():
            mask = (full['time_stamp'] == ts) & (full['data_source'] == 'angelone_fallback')
            if mask.any():
                full.loc[mask, ['open', 'high', 'low', 'close']] = [
                    ohlc['open'], ohlc['high'], ohlc['low'], ohlc['close']]

    full = full.set_index('time_stamp').sort_index()
    return full
