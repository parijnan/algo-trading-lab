"""
forecast.py — durable VIX-direction forecast interface.

This is the stable API consumed by Phase 5 routing backtest and later Hestia.
All no-lookahead logic lives here — consumers cannot accidentally cheat.

Durable interface (§14.2):
  build_forecast(vix_1m, nifty_1m, horizon_days, config) -> pd.DataFrame
  forecast_at(forecast_df, entry_date) -> dict | None

Validation-only target (never fed back into build_forecast):
  forward_vix_change(vix_daily, horizon_days) -> pd.Series  [lives in validate.py]

Config keys (§10 parameter budget ≤ 3):
  vrp_window     : int   = 10   (trailing sessions for realised vol; try 10 or 20)
  bb_window      : int   = 20   (fixed; matches annotate_athena.py)
  fall_threshold : float = 0.6  (p_fall >= this → direction='fall')
  rise_threshold : float = 0.4  (p_fall <= this → direction='rise'; gap [0.4,0.6] = 'neutral')
"""

import os
import sys
import pandas as pd
import numpy as np

_THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, 'research', 'range_detection'))

from research.vix_router.data_layer import load_combined_daily  # noqa: E402
from research.vix_router.signals import vrp as compute_vrp      # noqa: E402
from research.vix_router.signals import bb_pct as compute_bb    # noqa: E402
from resample import resample_daily                              # noqa: E402

_DEFAULT_CONFIG = {
    'vrp_window'     : 10,
    'bb_window'      : 20,
    'fall_threshold' : 0.6,
    'rise_threshold' : 0.4,
}


def build_forecast(
    vix_1m   : pd.DataFrame,
    nifty_1m : pd.DataFrame,
    horizon_days : int,
    config   : dict | None = None,
) -> pd.DataFrame:
    """
    Build the VIX-direction forecast table.

    Inputs: raw 1-min DataFrames (as loaded by data_layer; tz_localize(None) already applied).
    Returns: one row per trading day, indexed by entry date (tz-naive date).

    Columns:
      vrp          - VIX minus annualised realised vol (prev day bar; no lookahead)
      bb_pct       - VIX BB %B (prev day bar)
      score        - rank-average of vrp_rank and bb_rank in [0,1]
                     (high score → high VRP + high %B → VIX expected to fall)
      p_fall       - alias of score (calibration-free; interpretable as ordinal probability)
      direction    - 'fall' | 'rise' | 'neutral' per fall_threshold / rise_threshold in config
    """
    cfg = {**_DEFAULT_CONFIG, **(config or {})}

    vix_daily   = resample_daily(vix_1m)
    nifty_daily = resample_daily(nifty_1m)

    vrp_s  = compute_vrp(vix_daily['close'],   nifty_daily['close'], window=cfg['vrp_window'])
    bb_s   = compute_bb(vix_daily['close'],    window=cfg['bb_window'])

    # Shift by 1 day: at 10:30 entry we only know yesterday's close
    vrp_prev = vrp_s.shift(1)
    bb_prev  = bb_s.shift(1)

    df = pd.DataFrame({'vrp': vrp_prev, 'bb_pct': bb_prev}).dropna()

    # Rank-average (§10: no fitted weights, near-zero parameter cost)
    df['vrp_rank']  = df['vrp'].rank(pct=True)
    df['bb_rank']   = df['bb_pct'].rank(pct=True)
    df['score']     = (df['vrp_rank'] + df['bb_rank']) / 2
    df['p_fall']    = df['score']

    def _direction(p):
        if p >= cfg['fall_threshold']:
            return 'fall'
        if p <= cfg['rise_threshold']:
            return 'rise'
        return 'neutral'

    df['direction'] = df['p_fall'].map(_direction)

    return df[['vrp', 'bb_pct', 'score', 'p_fall', 'direction']]


def forecast_at(forecast_df: pd.DataFrame, entry_date) -> dict | None:
    """
    Point lookup for the routing backtest.
    entry_date: date-like (str, datetime.date, pd.Timestamp).
    Returns the forecast row as a dict, or None if entry_date is not in the index.
    """
    key = pd.Timestamp(entry_date).normalize()
    if key not in forecast_df.index:
        return None
    row = forecast_df.loc[key]
    return row.to_dict()
