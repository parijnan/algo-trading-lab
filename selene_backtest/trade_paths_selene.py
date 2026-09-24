"""
Selene - per-trade MFE/MAE from the 1-minute path, entry to exit.

Same walk and same output columns as prometheus_backtest/phase3/
trade_paths_p3.py, except the per-minute CSV is optional (selene_configs.
SAVE_TRADE_LOGS) -- the full-history sweep over an 11-value multiplier grid
would otherwise write >10M rows just to feed a summary. A trade still open
at the end of the data is walked to the last available bar, not skipped
(same reason as Prometheus: SL/target outcomes downstream don't need the
raw signal to have reversed).
"""

import os

import numpy as np
import pandas as pd


def compute_trade_paths(trades: pd.DataFrame, df_1m: pd.DataFrame, logs_dir: str = None) -> list:
    """Returns one summary dict per trade (trade_id, final_mae, final_mfe,
    hold_hours). Writes per-trade minute CSVs only when logs_dir is given."""
    if logs_dir:
        os.makedirs(logs_dir, exist_ok=True)

    idx = df_1m.index
    highs, lows, closes = df_1m['high'].to_numpy(float), df_1m['low'].to_numpy(float), df_1m['close'].to_numpy(float)
    summaries = []

    for _, t in trades.iterrows():
        entry_ts, exit_ts = t['entry_ts'], t.get('exit_ts')
        still_open = pd.isna(exit_ts)
        lo = idx.searchsorted(entry_ts, side='left')
        hi = len(idx) if still_open else idx.searchsorted(exit_ts, side='right')
        if hi <= lo:
            continue

        entry_price = float(t['entry_price'])
        h, l, c = highs[lo:hi], lows[lo:hi], closes[lo:hi]
        if t['direction'] == 'bullish':
            adverse, favorable, unreal = np.maximum(entry_price - l, 0.0), np.maximum(h - entry_price, 0.0), c - entry_price
        else:
            adverse, favorable, unreal = np.maximum(h - entry_price, 0.0), np.maximum(entry_price - l, 0.0), entry_price - c
        run_mae, run_mfe = np.maximum.accumulate(adverse), np.maximum.accumulate(favorable)

        if logs_dir:
            path = df_1m.iloc[lo:hi]
            letter = 'B' if t['direction'] == 'bullish' else 'S'
            log_df = pd.DataFrame({
                'ts': path.index, 'direction': t['direction'],
                'mins_since_entry': ((path.index - pd.Timestamp(entry_ts)).total_seconds() // 60).astype(int),
                'open': path['open'].to_numpy(), 'high': h, 'low': l, 'close': c,
                'unrealised_pts': np.round(unreal, 2),
                'running_mae': np.round(run_mae, 2), 'running_mfe': np.round(run_mfe, 2),
            })
            log_df.to_csv(os.path.join(
                logs_dir, f"trade_{int(t['trade_id']):04d}_{pd.Timestamp(entry_ts):%Y-%m-%d_%H%M}_{letter}.csv"), index=False)

        end_ts = idx[hi - 1] if still_open else exit_ts
        summaries.append({
            'trade_id': int(t['trade_id']), 'final_mae': round(float(run_mae[-1]), 2),
            'final_mfe': round(float(run_mfe[-1]), 2),
            'hold_hours': round((pd.Timestamp(end_ts) - pd.Timestamp(entry_ts)).total_seconds() / 3600, 2),
        })
    return summaries
