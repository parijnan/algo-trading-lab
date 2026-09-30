"""
Typhon - NATGASMINI 1-minute data assembly (plan Step 1/2), WITH additive
back-adjustment across contract rolls -- the one structural difference from
Prometheus/Selene/Helios's loaders (see typhon_configs.py's module docstring
for why: NATGASMINI's roll gap is too large to splice naively).

load_futures_1min() is identical in shape to helios_data_loader.py's own
function -- same five-tier per-date source preference, built on
prometheus_backtest/data_loader_p3.py's rollover machinery (imported, not
copied, so the two can never drift). It returns REAL, un-adjusted per-contract
prices; every row carries `data_source` and `contract_expiry` as before.

back_adjust() is new. At each early-roll effective-contract switch (the same
switch load_futures_1min() already applies day by day), it looks up the
OUTGOING contract's own last observed price before the switch and the
INCOMING contract's own price at essentially the same time (both contracts
trade in parallel before a roll -- that overlap is what makes the gap
observable at all), and computes an ADDITIVE gap = incoming - outgoing.
Working backward from the most recent (unadjusted) segment, it cumulatively
sums these gaps and shifts every earlier segment's OHLC by the running total.

Why additive, not the more common ratio/Panama adjustment: for a position
held across exactly one roll, additive back-adjustment reproduces the real
point-for-point P&L of "sell the outgoing contract at the roll price, buy the
incoming one at the roll price" EXACTLY (algebraic identity, plan Step 1) --
ratio adjustment only preserves %-returns, not absolute Rs P&L, and this
directory's sweep/calibration work is P&L-ranking-based throughout. The
known cost of additive adjustment (price level can drift from the real
historical level over a long enough back-adjusted window) is accepted and
bounded: it only affects Phase 2's signal-discovery series, never the
Phase 3 parity backtest, which will use real per-contract prices with real
roll execution like Prometheus/Selene/Helios's own parity backtests do.

Known limitations, same posture as Selene's/Helios's own loaders:
  * Holiday calendar: see _load_fully_closed_dates.
  * Fyers emits zero-volume placeholder bars where AngelOne skips untraded
    minutes; used as delivered.
  * Far-month liquidity is thin early in a contract's listing; the early-roll
    rule keeps the series on the front contract except in the final 5 days.
  * back_adjust()'s gap lookup uses each contract's own last-observed price
    before/at the switch date, not a literal same-timestamp tick match (the
    two contracts' bar grids need not align to the minute) -- same
    "at or before" tolerance used in the plan's own Step 1 gap measurement.
"""

import os
import sys

import numpy as np
import pandas as pd

import typhon_configs as configs

sys.path.insert(0, configs.PROMETHEUS_DIR)
import data_loader_p3 as _p3  # noqa: E402

assert _p3.TENDER_ROLL_TRADING_DAYS == configs.TENDER_ROLL_TRADING_DAYS, (
    'typhon_configs.TENDER_ROLL_TRADING_DAYS must match data_loader_p3.TENDER_ROLL_TRADING_DAYS')


def _load_fully_closed_dates() -> set:
    """Dates on which BOTH MCX sessions were closed: the union of the user-supplied
    2022-2026 calendar (typhon_configs.HOLIDAYS_FILE, same file Selene/Helios use) and
    production's own mcx_holidays.csv (2026 only). Weekends handled by the caller's
    own weekday test."""
    h = pd.read_csv(configs.HOLIDAYS_FILE)
    h['d'] = pd.to_datetime(h['Date'], format='%d %b %Y').dt.date
    closed = set(h[(h['Morning Session'] == 'Closed') & (h['Evening Session'] == 'Closed')]['d'])
    return closed | _p3._load_fully_closed_dates()


# Reused verbatim -- pure functions, no data-source dependency.
resample_ohlcv = _p3.resample_ohlcv
compute_st = _p3.compute_st


def load_futures_1min(symbol: str = configs.SYMBOL, start: str = configs.DATA_START) -> pd.DataFrame:
    start_date = pd.Timestamp(start).date()
    expiry_calendar = _p3._discover_expiries(symbol)
    fully_closed = _load_fully_closed_dates()

    fyers = {e: _p3._read_contract_file(_p3.FYERS_DATA_DIR, symbol, e) for e in expiry_calendar}
    angelone = {e: _p3._read_contract_file(_p3.ANGELONE_DATA_DIR, symbol, e) for e in expiry_calendar}
    fyers_dates = {e: set(df['time_stamp'].dt.date) for e, df in fyers.items() if len(df)}
    angelone_dates = {e: set(df['time_stamp'].dt.date) for e, df in angelone.items() if len(df)}

    all_dates = sorted(set().union(*fyers_dates.values(), *angelone_dates.values()))
    all_dates = [d for d in all_dates if d >= start_date and pd.Timestamp(d).weekday() < 5]

    def _chunk(frames: dict, expiry, d, source: str):
        df = frames[expiry]
        chunk = df[df['time_stamp'].dt.date == d].copy()
        chunk['data_source'] = source
        chunk['contract_expiry'] = str(expiry)
        return chunk

    day_frames = []
    for d in all_dates:
        eff = _p3._effective_contract_for_date(d, expiry_calendar, fully_closed)
        naive = _p3._naive_front_month_for_date(d, expiry_calendar)
        if eff is not None and d in fyers_dates.get(eff, set()):
            day_frames.append(_chunk(fyers, eff, d, 'fyers'))
        elif eff is not None and d in angelone_dates.get(eff, set()):
            day_frames.append(_chunk(angelone, eff, d, 'angelone_fallback'))
        elif naive is not None and d in fyers_dates.get(naive, set()):
            day_frames.append(_chunk(fyers, naive, d, 'fyers_naive_fallback'))
        elif naive is not None and d in angelone_dates.get(naive, set()):
            day_frames.append(_chunk(angelone, naive, d, 'angelone_naive_fallback'))
        else:
            holder = next((e for e in expiry_calendar if d in angelone_dates.get(e, set())), None)
            if holder is None:
                continue   # no data anywhere for this date -- skip, never fabricate
            chunk = _chunk(angelone, holder, d, 'angelone_frontmonth_fill')
            chunk['contract_expiry'] = str(naive) if naive is not None else str(holder)
            day_frames.append(chunk)

    full = pd.concat(day_frames, ignore_index=True)
    return full.set_index('time_stamp').sort_index()


def _price_at_or_before(frames: dict, expiry, target_date):
    df = frames.get(expiry)
    if df is None or df.empty:
        return None
    day = df[df['time_stamp'].dt.date <= target_date]
    return float(day['close'].iloc[-1]) if len(day) else None


def compute_roll_gaps(symbol: str = configs.SYMBOL, start: str = configs.DATA_START) -> pd.DataFrame:
    """One row per early-roll effective-contract switch within [start, data end]: the additive
    price gap (incoming contract's own price MINUS outgoing contract's own price), both read on
    the SAME reference date -- the last trading day BEFORE the switch, while the two contracts
    are still trading in parallel. This is the only pair of observations that isolates the roll
    basis itself; comparing the outgoing contract the day before against the incoming contract
    AT/AFTER the switch (a real bug, found and fixed 2026-09-30 via a continuity check that
    still showed a ~27pt jump at a switch whose own measured gap was ~27pt: the incoming
    contract's own intraday move during the switch day was silently getting counted as roll
    basis) would bake a full day of the incoming contract's own price movement into the gap."""
    start_date = pd.Timestamp(start).date()
    expiry_calendar = _p3._discover_expiries(symbol)
    fully_closed = _load_fully_closed_dates()
    fyers = {e: _p3._read_contract_file(_p3.FYERS_DATA_DIR, symbol, e) for e in expiry_calendar}
    angelone = {e: _p3._read_contract_file(_p3.ANGELONE_DATA_DIR, symbol, e) for e in expiry_calendar}

    fyers_dates = {e: set(df['time_stamp'].dt.date) for e, df in fyers.items() if len(df)}
    angelone_dates = {e: set(df['time_stamp'].dt.date) for e, df in angelone.items() if len(df)}
    all_dates = sorted(set().union(*fyers_dates.values(), *angelone_dates.values()))
    all_dates = [d for d in all_dates if d >= start_date and pd.Timestamp(d).weekday() < 5]

    def price_at_or_before(expiry, d):
        p = _price_at_or_before(fyers, expiry, d)
        return p if p is not None else _price_at_or_before(angelone, expiry, d)

    prev_eff = None
    rows = []
    for d in all_dates:
        eff = _p3._effective_contract_for_date(d, expiry_calendar, fully_closed)
        if eff != prev_eff and prev_eff is not None and eff is not None:
            prior_date = [x for x in all_dates if x < d]
            prior_date = prior_date[-1] if prior_date else d
            old_px = price_at_or_before(prev_eff, prior_date)
            new_px = price_at_or_before(eff, prior_date)
            if old_px is not None and new_px is not None:
                rows.append({'switch_date': d, 'reference_date': prior_date, 'old_contract': str(prev_eff),
                            'new_contract': str(eff), 'old_px': old_px, 'new_px': new_px, 'gap': new_px - old_px})
        prev_eff = eff
    return pd.DataFrame(rows)


def back_adjust(df_1m: pd.DataFrame, gaps: pd.DataFrame) -> pd.DataFrame:
    """Additively back-adjusts open/high/low/close so the series is continuous across every
    roll in `gaps` -- the most recent (last-segment) prices are left untouched (offset 0); every
    earlier segment is shifted by the cumulative sum of every gap at or after its own end.
    volume/data_source/contract_expiry are untouched -- offsets are price-only."""
    out = df_1m.copy()
    offset = pd.Series(0.0, index=out.index)
    cum = 0.0
    # apply from the most recent switch backward, so each earlier segment picks up every
    # later gap too (a trade spanning two rolls needs both applied to its entry leg).
    for _, g in gaps.sort_values('switch_date', ascending=False).iterrows():
        cum += g['gap']
        before = out.index.date < g['switch_date']
        offset[before] = cum
    for col in ('open', 'high', 'low', 'close'):
        out[col] = out[col] + offset
    out['back_adjust_offset'] = offset
    return out
