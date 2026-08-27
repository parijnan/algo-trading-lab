"""
Prep computation for the new scale-out crude oil strategy (not yet named/
built — see plans discussion). Before writing any strategy code, compute
over the full CRUDEOILM series:
  1. ST_15 — Supertrend on the 15-min timeframe (entry trigger + trend-flip
     SL for the new strategy design).
  2. Daily classic floor-trader pivots (PP, R1-R3, S1-S3) — the levels used
     to pick where the 2nd lot books, above the fixed 100-point 1st target.

Pivots use the previous trading day's H/L/C, applied across the current
day's session (standard convention) — computed once per day, not
recalculated intraday. First day of the series has no previous day, so its
pivot row is NaN.

Output (this dir's outputs/):
  crudeoilm_st15.csv          — 15-min OHLC + supertrend/trend/trend_flip
  crudeoilm_daily_pivots.csv  — one row per day: prev H/L/C, PP, R1-3, S1-3
"""

import os
import sys

import pandas as pd

_REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
sys.path.insert(0, os.path.join(_REPO_ROOT, 'prometheus_backtest'))
from data_loader import load_futures_1min, resample_ohlcv, compute_st  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'outputs')

# Same day-1 starting values as Iris/Prometheus — not yet calibrated for
# this strategy specifically.
ST_PERIOD     = 10
ST_MULTIPLIER = 3.0


def compute_daily_pivots(df_1m: pd.DataFrame) -> pd.DataFrame:
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
    return piv.reset_index()


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    df_1m = load_futures_1min('CRUDEOILM')
    n_days = df_1m.index.normalize().nunique()
    print(f'Loaded {len(df_1m)} 1-min bars across {n_days} trading days '
          f'({df_1m.index.min()} to {df_1m.index.max()})')

    df_15m = resample_ohlcv(df_1m, '15min')
    df_15m = compute_st(df_15m, ST_PERIOD, ST_MULTIPLIER)
    n_flips = int(df_15m['trend_flip'].sum())
    print(f'{len(df_15m)} 15-min bars, {n_flips} ST_15 flips '
          f'(period={ST_PERIOD}, multiplier={ST_MULTIPLIER})')

    st15_path = os.path.join(OUT_DIR, 'crudeoilm_st15.csv')
    df_15m.to_csv(st15_path)
    print(f'Saved ST_15 series to {st15_path}')

    pivots = compute_daily_pivots(df_1m)
    pivots_path = os.path.join(OUT_DIR, 'crudeoilm_daily_pivots.csv')
    pivots.to_csv(pivots_path, index=False)
    print(f'Saved {len(pivots)} day(s) of pivot levels to {pivots_path}')

    print('\nSample (last 5 trading days):')
    print(pivots.tail(5).to_string(index=False))


if __name__ == '__main__':
    main()
