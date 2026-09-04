"""
Prometheus - Phase 4: build the 1h ST series and check each ST_15 trade's
alignment against it, with no lookahead (configs_p4.py's design note 3).
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configs_p4 as configs  # noqa: E402

sys.path.insert(0, configs.PROMETHEUS_DIR)
from data_loader import resample_ohlcv, compute_st  # noqa: E402


def build_1h_st_series(df_1m: pd.DataFrame, period: int, multiplier: float) -> pd.DataFrame:
    """
    Reuses prometheus_backtest/data_loader.py's resample_ohlcv UNCHANGED
    (see configs_p4.py's module docstring point 4 for why this is already
    correct -- trailing partial bucket construction and per-day anchor both
    verified). Returns a df indexed by each bucket's own START timestamp
    (left-labeled), with a 'trend' column from compute_st.
    """
    df_60m = resample_ohlcv(df_1m, '60min')
    df_60m = compute_st(df_60m, period, multiplier)
    return df_60m


def check_alignment(st1h: pd.DataFrame, trades: pd.DataFrame,
                     decision_offset_min: int = configs.DECISION_OFFSET_MIN) -> pd.DataFrame:
    """
    For each trade, find the LAST 1h bar whose window has fully closed
    (bar_start + 60min <= decision_ts) as of decision_ts = signal_ts +
    decision_offset_min -- never a bar still in progress at that moment.
    Vectorized via merge_asof (direction='backward'), same no-lookahead
    semantics as prometheus_production's _resample_1m_to_Nmin day-end guard,
    just computed by lookup against a precomputed series rather than a
    live truncate-at-now series (equivalent for a fixed decision_ts).

    Adds two columns: 'aligned_1h' (bool -- True only if a fully-closed 1h
    bar exists AND its trend agrees with the trade's own direction; empty/
    NaN treated as disagree, matching _check_1h_alignment's convention in
    prometheus.py) and 'decision_ts' (for inspection).
    """
    trades = trades.copy()
    # _load_multiplier_data only parse_dates=['entry_ts', 'exit_ts'] -- signal_ts
    # comes back as a plain string column, would silently fail (or worse,
    # string-concat) against a Timedelta addition below without this.
    trades['signal_ts'] = pd.to_datetime(trades['signal_ts'])
    trades['decision_ts'] = trades['signal_ts'] + pd.Timedelta(minutes=decision_offset_min)

    st1h_sorted = st1h.reset_index().rename(columns={'time_stamp': 'bar_start'})
    st1h_sorted['bar_close'] = st1h_sorted['bar_start'] + pd.Timedelta(minutes=60)
    st1h_sorted = st1h_sorted.sort_values('bar_close')[['bar_close', 'trend']]

    trades_sorted = trades.sort_values('decision_ts')
    merged = pd.merge_asof(trades_sorted, st1h_sorted, left_on='decision_ts',
                            right_on='bar_close', direction='backward')

    def _agrees(row):
        if pd.isna(row['trend']):
            return False
        st1h_direction = 'bullish' if bool(row['trend']) else 'bearish'
        return st1h_direction == row['direction']

    merged['aligned_1h'] = merged.apply(_agrees, axis=1)
    return merged.sort_values('trade_id').reset_index(drop=True)
