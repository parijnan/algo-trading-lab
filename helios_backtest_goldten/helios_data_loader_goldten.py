"""
Helios - GOLDTEN 1-minute data assembly (plan §3).

Built on prometheus_backtest/data_loader_p3.py's own rollover machinery,
imported rather than copied so the two can never drift: the per-date
early-roll rule (resolve_effective_contract's mirror, TENDER_ROLL_TRADING_DAYS
trading days before final expiry, holiday-aware) plus production's eve-lookahead
(_check_rollover_tonight's mirror). Gold Petal's confirmed tender-margin start
is also 5 working days before expiry (user, 2026-09-29), so the same rule
applies unchanged. data_loader_p3.py's own load_futures_1min() is gated to
CRUDEOILM/CRUDEOIL only -- this loader calls its lower-level, symbol-generic
helpers directly instead (same approach as selene_data_loader.py), so
data_loader_p3.py itself stays untouched and load-bearing for Prometheus.

Per-date source preference for the date's effective (early-rolled) contract,
same five-tier structure as Selene's loader:
  1. 'fyers'                    -- the effective contract's own Fyers file
  2. 'angelone_fallback'        -- the effective contract's own AngelOne file
  3. 'fyers_naive_fallback'     -- the un-rolled front contract's Fyers file
  4. 'angelone_naive_fallback'  -- the un-rolled front contract's AngelOne file
  5. 'angelone_frontmonth_fill' -- ANY AngelOne file holding that date

Not yet audited the way Selene's Phase 1 was (plan §3: "not yet audited...
before being trusted for a sweep -- not yet done, just not yet found lacking
either"). GOLDTEN is monthly cadence (like CRUDEOILM, unlike SILVERMIC's
quarterly), so day-count coverage per contract should look closer to
Prometheus's own than to Selene's -- worth confirming empirically once the
sweep actually runs, not assumed. Every row carries `data_source`, so any
trade touching a non-'fyers' stretch stays identifiable.

Known limitations, not worked around (same posture as Prometheus/Selene):
  * Holiday calendar: see _load_fully_closed_dates.
  * Rollover-week ST splicing artifacts (accepted for Prometheus, plan
    prometheus-phase2-production.md §1).
  * Fyers emits zero-volume placeholder bars where AngelOne skips untraded
    minutes; used as delivered, as in the crude/silver loaders.
  * Far-month liquidity is thin early in a contract's listing; the early-roll
    rule keeps the series on the front contract except in the final 5 days.
"""

import os
import sys

import pandas as pd

import helios_configs_goldten as configs

sys.path.insert(0, configs.PROMETHEUS_DIR)
import data_loader_p3 as _p3  # noqa: E402

assert _p3.TENDER_ROLL_TRADING_DAYS == configs.TENDER_ROLL_TRADING_DAYS, (
    'helios_configs_goldten.TENDER_ROLL_TRADING_DAYS must match data_loader_p3.TENDER_ROLL_TRADING_DAYS')


def _load_fully_closed_dates() -> set:
    """Dates on which BOTH MCX sessions were closed: the union of the user-supplied
    2022-2026 calendar (helios_configs_goldten.HOLIDAYS_FILE, same file Selene uses) and
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
