"""
Typhon - Phase 2 regime identification (plan Step 2). NatGas's own known regime drivers are
seasonal (Nov-Feb heating-demand season) and volatility-clustering, not a single hand-picked
split date the way Prometheus/Selene/Helios's own WALKFORWARD_SPLIT_DATE checks were -- both
cuts are computed here, plus the walk-forward split as a secondary, non-seasonal check.

Reads each shortlisted multiplier's own data_sweep/mult_<x>/trade_summary.csv (sweep_typhon.py
must have already run). Realized vol is computed once, off the back-adjusted DAILY close series
(typhon_configs.REALIZED_VOL_LOOKBACK_DAYS trailing stdev of daily returns), independent of
multiplier -- every trade's entry DATE is mapped to that date's own vol tercile.
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import typhon_configs as configs
from typhon_data_loader import load_futures_1min, compute_roll_gaps, back_adjust


def _daily_vol_terciles() -> pd.Series:
    """date -> 'low'/'mid'/'high', trailing REALIZED_VOL_LOOKBACK_DAYS realized vol of the
    back-adjusted daily close, tercile-cut over the whole available history."""
    df_1m = load_futures_1min()
    gaps = compute_roll_gaps()
    adj = back_adjust(df_1m, gaps)
    daily_close = adj['close'].resample('1D').last().dropna()
    daily_ret = daily_close.pct_change()
    realized_vol = daily_ret.rolling(configs.REALIZED_VOL_LOOKBACK_DAYS).std()
    valid = realized_vol.dropna()
    labels = ['low', 'mid', 'high'][:configs.REALIZED_VOL_BUCKETS]
    terciles = pd.qcut(valid, configs.REALIZED_VOL_BUCKETS, labels=labels)
    return terciles.reindex(realized_vol.index)


def _bucket_stats(trades: pd.DataFrame, group_col: str) -> pd.DataFrame:
    rows = []
    for key, g in trades.groupby(group_col, observed=True):
        n = len(g)
        rows.append({
            group_col: key, 'n_trades': n,
            'win_rate_pct': round((g['pnl_points'] > 0).mean() * 100, 1) if n else float('nan'),
            'total_pnl_rs': round(g['pnl_rs'].sum(), 0),
            'avg_pnl_rs': round(g['pnl_rs'].mean(), 1) if n else float('nan'),
            'avg_pnl_points': round(g['pnl_points'].mean(), 2) if n else float('nan'),
        })
    return pd.DataFrame(rows)


def analyze(multiplier: float, vol_terciles: pd.Series) -> dict:
    label = f'mult_{multiplier:.1f}'
    path = os.path.join(configs.DATA_SWEEP_DIR, label, 'trade_summary.csv')
    trades = pd.read_csv(path, parse_dates=['entry_ts', 'exit_ts'])
    trades = trades[trades['exit_ts'].notna()].copy()

    trades['month'] = trades['entry_ts'].dt.month
    trades['season'] = np.where(trades['month'].isin(configs.WINTER_MONTHS), 'winter (Nov-Feb)', 'rest of year')
    trades['entry_date'] = trades['entry_ts'].dt.normalize()
    trades['vol_regime'] = trades['entry_date'].map(vol_terciles).astype('object')
    trades['vol_regime'] = trades['vol_regime'].fillna('unclassified (insufficient trailing history)')
    trades['walkforward'] = np.where(trades['entry_ts'] < configs.WALKFORWARD_SPLIT_DATE, 'pre', 'post')

    return {
        'multiplier': multiplier,
        'season': _bucket_stats(trades, 'season'),
        'vol_regime': _bucket_stats(trades, 'vol_regime'),
        'walkforward': _bucket_stats(trades, 'walkforward'),
        'n_trades': len(trades),
    }


def main():
    print('Computing realized-vol terciles off the back-adjusted daily series...')
    vol_terciles = _daily_vol_terciles()
    print(vol_terciles.value_counts(dropna=False).to_string())
    print()

    shortlist = [3.0, 3.5, 4.0, 5.5]   # the sweep's own local peaks (see sweep_summary.csv)
    for mult in shortlist:
        result = analyze(mult, vol_terciles)
        print(f"=== mult {mult:.1f} ({result['n_trades']} closed trades) ===")
        print('-- by season --')
        print(result['season'].to_string(index=False))
        print('-- by realized-vol tercile (entry date) --')
        print(result['vol_regime'].to_string(index=False))
        print('-- walk-forward split (' + configs.WALKFORWARD_SPLIT_DATE + ') --')
        print(result['walkforward'].to_string(index=False))
        print()


if __name__ == '__main__':
    main()
