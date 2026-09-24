"""
Selene - Phase 2 raw signal-quality sweep: ST_MULTIPLIER grid (ST_PERIOD held
fixed), raw signal-following backtest over the full SILVERMIC history. No SL,
no target, no EOD square-off, single 1-lot position, the only exit is the
opposite ST_15 flip. One variable changed across the grid (CLAUDE.md
convention): only ST_MULTIPLIER varies.

Mirrors prometheus_backtest/phase3/sweep_p3.py. Output:
  data_sweep/sweep_summary.csv            -- one row per ST_MULTIPLIER
  data_sweep/mult_<value>/trade_summary.csv
  data_sweep/mult_<value>/trade_logs/*.csv -- only if SAVE_TRADE_LOGS
Additionally writes data_sweep/sweep_by_year.csv (per-multiplier, per-calendar-
year trades / win rate / P&L) so a regime break (plan §4) is visible straight
away rather than averaged out.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selene_configs as configs
from backtest_selene import run_backtest
from trade_paths_selene import compute_trade_paths
from selene_data_loader import load_futures_1min, resample_ohlcv, compute_st


def _summarize(trades: pd.DataFrame, path_summaries: list, multiplier: float) -> dict:
    closed = trades[trades['exit_ts'].notna()]
    n = len(closed)
    pl_pts = closed['pnl_points'].astype(float)
    pl_rs = closed['pnl_rs'].astype(float)
    path_df = pd.DataFrame(path_summaries)
    return {
        'st_period': configs.ST_PERIOD, 'st_multiplier': multiplier,
        'n_trades': n, 'still_open_at_end': len(trades) - n,
        'win_rate_pct': round(float((pl_pts > 0).sum()) / n * 100, 1) if n else float('nan'),
        'total_pnl_points': round(pl_pts.sum(), 2) if n else float('nan'),
        'total_pnl_rs': round(pl_rs.sum(), 0) if n else float('nan'),
        'avg_pnl_points': round(pl_pts.mean(), 2) if n else float('nan'),
        'avg_mae_points': round(path_df['final_mae'].mean(), 2) if not path_df.empty else float('nan'),
        'max_mae_points': round(path_df['final_mae'].max(), 2) if not path_df.empty else float('nan'),
        'avg_mfe_points': round(path_df['final_mfe'].mean(), 2) if not path_df.empty else float('nan'),
        'max_mfe_points': round(path_df['final_mfe'].max(), 2) if not path_df.empty else float('nan'),
        'avg_hold_hours': round(path_df['hold_hours'].mean(), 1) if not path_df.empty else float('nan'),
        'max_hold_hours': round(path_df['hold_hours'].max(), 1) if not path_df.empty else float('nan'),
    }


def main():
    os.makedirs(configs.DATA_SWEEP_DIR, exist_ok=True)

    print(f'Loading {configs.SYMBOL} 1-min data...')
    df_1m = load_futures_1min()
    print(f'  {len(df_1m):,} 1-min bars, {df_1m.index.min()} -> {df_1m.index.max()}')
    print(df_1m['data_source'].value_counts().to_string())
    print(f'Resampling to 15-min...')
    df_15m_raw = resample_ohlcv(df_1m, '15min')

    results, by_year = [], []
    for mult in configs.ST_MULTIPLIER_GRID:
        label = f'mult_{mult:.1f}'
        df_15m = compute_st(df_15m_raw, configs.ST_PERIOD, mult)
        trades = run_backtest(df_15m)

        run_dir = os.path.join(configs.DATA_SWEEP_DIR, label)
        os.makedirs(run_dir, exist_ok=True)
        logs_dir = os.path.join(run_dir, 'trade_logs') if configs.SAVE_TRADE_LOGS else None
        path_summaries = compute_trade_paths(trades, df_1m, logs_dir)

        # data_source of each trade's entry bar, so trades touching a fallback stretch are identifiable
        trades_out = trades.copy()
        path_df = pd.DataFrame(path_summaries)
        if not path_df.empty:
            trades_out = trades_out.merge(path_df, on='trade_id', how='left')
        trades_out['entry_data_source'] = df_1m['data_source'].reindex(trades_out['entry_ts']).to_numpy()
        trades_out.to_csv(os.path.join(run_dir, 'trade_summary.csv'), index=False)

        row = _summarize(trades, path_summaries, mult)
        results.append(row)
        print(f"{label}: {row['n_trades']} trades ({row['still_open_at_end']} open at end), "
              f"win {row['win_rate_pct']}%, P&L {row['total_pnl_rs']:,.0f} Rs, "
              f"avg MAE/MFE {row['avg_mae_points']}/{row['avg_mfe_points']}")

        closed = trades[trades['exit_ts'].notna()].copy()
        closed['year'] = pd.to_datetime(closed['entry_ts']).dt.year
        for year, g in closed.groupby('year'):
            by_year.append({'st_multiplier': mult, 'year': year, 'n_trades': len(g),
                            'win_rate_pct': round((g['pnl_points'] > 0).mean() * 100, 1),
                            'total_pnl_points': round(g['pnl_points'].sum(), 1),
                            'total_pnl_rs': round(g['pnl_rs'].sum(), 0)})

    summary = pd.DataFrame(results)
    summary.to_csv(os.path.join(configs.DATA_SWEEP_DIR, 'sweep_summary.csv'), index=False)
    pd.DataFrame(by_year).to_csv(os.path.join(configs.DATA_SWEEP_DIR, 'sweep_by_year.csv'), index=False)
    print('\n' + summary.to_string(index=False))


if __name__ == '__main__':
    main()
