"""
prometheus_backtest/phase3_fyers/options_iv/resample_utils.py

Day-aware 1-min -> N-min resample for MULTI-YEAR HISTORICAL data spanning
many US DST transitions -- adapted from prometheus_production/
prometheus_functions.py's own _resample_1m_to_Nmin (used for Prometheus's
live ST_15), NOT a re-derivation. Kept as its own copy rather than
parameterizing the shared production function -- same reasoning
data_loader_fyers.py's own docstring gives for _load_fyers_front_month:
that function is load-bearing for live trading and must not change
behavior as a side effect of a change made for this research track.

**The one real difference, found and confirmed directly (2026-09-17)**:
prometheus_functions.py's version reads CLOSING_TIME as a bare module-level
constant, resolved ONCE at import time from *today's* real date
(`CLOSING_TIME = _resolve_closing_time()`, prometheus_configs.py). That's
exactly correct for live use -- "today" is always actually today -- but
silently wrong here: a historical day whose own DST state differs from
whatever day this script happens to be RUN on gets bucketed against the
wrong closing time. Confirmed empirically: resampling a real non-DST day
(2026-01-15, true close 23:55) through the unmodified live function while
running this script during a DST day (CLOSING_TIME fixed at import to
23:30) silently dropped that whole day's real 23:30-23:55 data instead of
forming the correct trailing 10-minute bucket -- exactly the DST edge case
this research explicitly needs to get right. Fixed here by re-resolving
the closing time per DAY being processed via prometheus_configs's own
_resolve_closing_time(day), which already takes a date and is unaffected
by this bug (only the bare CLOSING_TIME constant is).

Same bucketing principle otherwise, deliberately unchanged: anchored at
SESSION_START_TIME (09:00), walked in `minutes`-sized steps, and the final
bucket of a DST-shortened day (e.g. 23:45-23:55, only 10 real minutes) is
still included as ONE candle -- never dropped, never split -- exactly
matching what the live chart (and Prometheus's own ST_15) shows.

No live-partial-window guard here (prometheus_functions.py's `now`/
`day_has_closed` check) -- irrelevant for backfill: every day being
processed is, by definition, fully in the past, so every window is always
"done." Omitting that branch entirely (rather than passing some `now` that
technically satisfies it) keeps this function's own logic simple and
un-coupled from wall-clock time altogether.
"""
import sys
from pathlib import Path

import pandas as pd

_PROMETHEUS_PRODUCTION_DIR = Path(__file__).resolve().parents[3] / 'prometheus_production'
if str(_PROMETHEUS_PRODUCTION_DIR) not in sys.path:
    sys.path.insert(0, str(_PROMETHEUS_PRODUCTION_DIR))

from prometheus_configs import SESSION_START_TIME, _resolve_closing_time  # noqa: E402


def resample_1m_to_Nmin_historical(df_1m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """
    df_1m: columns include 'time_stamp' (tz-naive), 'open','high','low',
    'close','volume', and optionally other columns to carry through
    unchanged (e.g. 'contract_expiry', 'data_source') -- the LAST value of
    any extra column within each bucket is kept, matching how 'close'
    itself is taken (both represent "the state as of the bucket's end").

    Returns one row per (day, bucket), bucket anchored at SESSION_START_TIME
    in `minutes` steps through that SPECIFIC day's own DST-correct
    CLOSING_TIME.
    """
    extra_cols = [c for c in df_1m.columns
                  if c not in ('time_stamp', 'open', 'high', 'low', 'close', 'volume')]
    candles = []
    for day, day_df in df_1m.groupby(df_1m['time_stamp'].dt.date):
        closing_time = _resolve_closing_time(day)
        anchor = pd.Timestamp(f'{day} {SESSION_START_TIME}')
        day_cutoff = pd.Timestamp(f'{day} {closing_time}')
        while anchor <= day_cutoff:
            window_end = anchor + pd.Timedelta(minutes=minutes) - pd.Timedelta(minutes=1)
            window = day_df[(day_df['time_stamp'] >= anchor) & (day_df['time_stamp'] <= window_end)]
            if not window.empty:
                row = {
                    'time_stamp': anchor,
                    'open': window['open'].iloc[0],
                    'high': window['high'].max(),
                    'low': window['low'].min(),
                    'close': window['close'].iloc[-1],
                    'volume': window['volume'].sum(),
                }
                for c in extra_cols:
                    row[c] = window[c].iloc[-1]
                candles.append(row)
            anchor += pd.Timedelta(minutes=minutes)
    if not candles:
        return pd.DataFrame()
    return pd.DataFrame(candles).reset_index(drop=True)
