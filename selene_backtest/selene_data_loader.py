"""
Selene - SILVERMIC 1-minute data assembly (plan §3).

Built on prometheus_backtest/data_loader_p3.py's own rollover machinery,
imported rather than copied so the two can never drift: the per-date
early-roll rule (resolve_effective_contract's mirror, TENDER_ROLL_TRADING_DAYS
trading days before final expiry, holiday-aware) plus production's eve-lookahead
(_check_rollover_tonight's mirror). SILVERMIC's confirmed tender-margin start
is also 5 working days before expiry (user, 2026-09-24), and §1.4's observed
liquidity crossover lands at the same 4-5 days, so the same rule applies
unchanged. data_loader_p3.py itself is CRUDE-guarded and load-bearing for every
published Prometheus number, so it is not modified; this loader adds the
silver-specific source blend on top.

Per-date source preference for the date's effective (early-rolled) contract:
  1. 'fyers'                    -- the effective contract's own Fyers file
  2. 'angelone_fallback'        -- the effective contract's own AngelOne file
  3. 'fyers_naive_fallback'     -- the un-rolled front contract's Fyers file
  4. 'angelone_naive_fallback'  -- the un-rolled front contract's AngelOne file
  5. 'angelone_frontmonth_fill' -- ANY AngelOne file holding that date

Tier 5 is the Fyers-void gap fill (user's instruction 2026-09-24: plug the
Fyers gaps with AngelOne as best as possible). Fyers has no SILVERMIC data at
all from 2026-04-01 through 2026-06-29 (its April-2026 contract ends 03-31,
its June-2026 contract 03-31, its August-2026 contract starts 06-30), so
those ~58 sessions come from data_pipeline/data/mcx/SILVERMIC/. AngelOne's
2026-08-31 file reaches back to 2026-01-30 because AngelOne's getCandleData
returns the THEN front-month contract's real prices under a not-yet-front
token (the documented pre-front-month "mislabeling", data_downloader_mcx.py's
header) -- for this purpose that is exactly the series wanted: checked against
Fyers directly, the file's March prices match Fyers' April contract and its
July-August prices match Fyers' August contract (85% of 1-min closes
identical, the rest small deviations). What it cannot reproduce is the
early roll to the next contract inside the gap: those sessions trade the
then-front contract throughout. Every row carries `data_source`, so any trade
touching a non-'fyers' stretch stays identifiable.

Known limitations, not worked around (same posture as Prometheus Phase 3):
  * Holiday calendar: see _load_fully_closed_dates -- 2022-2026 covered, 2021
    not needed for any roll. One weekday with no data anywhere and no listed
    holiday, 2026-09-01 (the AngelOne front/next-month tracker starts 09-02).
  * Rollover-week ST splicing artifacts (accepted for Prometheus, plan
    prometheus-phase2-production.md §1).
  * Fyers emits zero-volume placeholder bars where AngelOne skips untraded
    minutes; used as delivered, as in the crude loader.
  * Far-month liquidity is thin early in a contract's listing; the early-roll
    rule keeps the series on the front contract except in the final 5 days.
"""

import os
import sys

import pandas as pd

import selene_configs as configs

sys.path.insert(0, configs.PROMETHEUS_DIR)
import data_loader_p3 as _p3  # noqa: E402

assert _p3.TENDER_ROLL_TRADING_DAYS == configs.TENDER_ROLL_TRADING_DAYS, (
    'selene_configs.TENDER_ROLL_TRADING_DAYS must match data_loader_p3.TENDER_ROLL_TRADING_DAYS')

def _load_fully_closed_dates() -> set:
    """Dates on which BOTH MCX sessions were closed: the union of the user-supplied
    2022-2026 calendar (selene_configs.HOLIDAYS_FILE) and production's own
    mcx_holidays.csv (2026 only; adds e.g. ad-hoc closures). Weekends are handled by
    the caller's own weekday test.

    2021 rows were added from truedata.in's MCX 2021 list (weekday holidays only) and
    verified against the data: every listed morning-closed day has no bars before
    17:00, and no unlisted 2021 weekday is missing data. Two listed full closures
    did trade a Diwali muhurat session (2022-10-24, 2024-11-01) -- irrelevant to any
    roll window, so left as closed."""
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
