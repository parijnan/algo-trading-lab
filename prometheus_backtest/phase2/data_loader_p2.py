"""
Reuses Prometheus v1's data_loader (load_futures_1min/resample_ohlcv/
compute_st — identical data source and ST computation, no phase-specific
differences) and adds the daily pivot calculation this phase needs.

Verified 2026-08-27: no CRUDEOILM session crosses midnight (latest observed
bar is 23:54, matching the documented DST-shifted close), so grouping by
calendar date for daily H/L/C is safe.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # -> prometheus_backtest/
from data_loader import load_futures_1min, resample_ohlcv, compute_st  # noqa: E402,F401


def compute_daily_pivots(df_1m: pd.DataFrame) -> pd.DataFrame:
    """
    Classic floor-trader pivots from the PREVIOUS trading day's H/L/C,
    indexed by date. First day of the series has no previous day -> NaN row.
    """
    daily = df_1m.groupby(df_1m.index.date).agg(
        high=('high', 'max'), low=('low', 'min'), close=('close', 'last'),
    )
    daily.index = pd.to_datetime(daily.index)
    daily = daily.sort_index()

    prev = daily.shift(1).rename(columns={'high': 'prev_high', 'low': 'prev_low', 'close': 'prev_close'})
    piv = prev.copy()
    piv['pp'] = (piv['prev_high'] + piv['prev_low'] + piv['prev_close']) / 3
    piv['r1'] = 2 * piv['pp'] - piv['prev_low']
    piv['s1'] = 2 * piv['pp'] - piv['prev_high']
    piv['r2'] = piv['pp'] + (piv['prev_high'] - piv['prev_low'])
    piv['s2'] = piv['pp'] - (piv['prev_high'] - piv['prev_low'])
    piv['r3'] = piv['prev_high'] + 2 * (piv['pp'] - piv['prev_low'])
    piv['s3'] = piv['prev_low'] - 2 * (piv['prev_high'] - piv['pp'])
    piv.index.name = 'date'
    return piv
