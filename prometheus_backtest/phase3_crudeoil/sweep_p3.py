"""
Prometheus - Phase 3 sweep: ST_MULTIPLIER grid (ST_PERIOD held fixed),
raw signal-following backtest -- no SL/target/EOD, single position, every
trade gets a minute-by-minute MFE/MAE log. One variable changed per run
(CLAUDE.md convention): only ST_MULTIPLIER varies across the grid.

Rollover-boundary ST-splicing artefacts across the sweep period are an
accepted, known limitation here (same issue already documented and
accepted for Phase 1/2 -- see plans/prometheus-phase2-production.md §1),
not worked around, per direct instruction (2026-09-01).

Output:
  data_sweep/sweep_p3_summary.csv           -- one row per ST_MULTIPLIER
  data_sweep/mult_<value>/trade_summary.csv -- one row per trade
  data_sweep/mult_<value>/trade_logs/*.csv  -- one row per minute per trade
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configs_p3 as configs
from backtest_p3 import run_backtest
from trade_paths_p3 import save_trade_paths_p3

sys.path.insert(0, configs.PROMETHEUS_DIR)
from data_loader import load_futures_1min, resample_ohlcv, compute_st  # noqa: E402


def _summarize(trades: pd.DataFrame, path_summaries: list, multiplier: float) -> dict:
    closed = trades[trades['exit_ts'].notna()]
    still_open = len(trades) - len(closed)
    n = len(closed)

    pl_pts = closed['pnl_points'].astype(float)
    pl_rs = closed['pnl_rs'].astype(float)
    wins = int((pl_pts > 0).sum())

    path_df = pd.DataFrame(path_summaries)
    avg_mae = round(path_df['final_mae'].mean(), 2) if not path_df.empty else float('nan')
    avg_mfe = round(path_df['final_mfe'].mean(), 2) if not path_df.empty else float('nan')
    max_mae = round(path_df['final_mae'].max(), 2) if not path_df.empty else float('nan')
    max_mfe = round(path_df['final_mfe'].max(), 2) if not path_df.empty else float('nan')
    avg_hold_hours = round(path_df['hold_hours'].mean(), 1) if not path_df.empty else float('nan')
    max_hold_hours = round(path_df['hold_hours'].max(), 1) if not path_df.empty else float('nan')

    return {
        'st_period': configs.ST_PERIOD, 'st_multiplier': multiplier,
        'n_trades': n, 'still_open_at_end': still_open,
        'win_rate_pct': round(wins / n * 100, 1) if n else float('nan'),
        'total_pnl_points': round(pl_pts.sum(), 2) if n else float('nan'),
        'total_pnl_rs': round(pl_rs.sum(), 0) if n else float('nan'),
        'avg_pnl_points': round(pl_pts.mean(), 2) if n else float('nan'),
        'avg_mae_points': avg_mae, 'max_mae_points': max_mae,
        'avg_mfe_points': avg_mfe, 'max_mfe_points': max_mfe,
        'avg_hold_hours': avg_hold_hours, 'max_hold_hours': max_hold_hours,
    }


def main():
    os.makedirs(configs.DATA_SWEEP_DIR, exist_ok=True)

    print(f'Loading {configs.SYMBOL} 1-min data...')
    df_1m = load_futures_1min(configs.SYMBOL)
    print(f'Resampling to 15-min ({len(df_1m):,} 1-min bars)...')
    df_15m_raw = resample_ohlcv(df_1m, '15min')

    results = []
    for mult in configs.ST_MULTIPLIER_GRID:
        label = f'mult_{mult:.1f}'
        print(f'\nRunning {label} (ST_PERIOD={configs.ST_PERIOD}, ST_MULTIPLIER={mult})...')

        df_15m = compute_st(df_15m_raw, configs.ST_PERIOD, mult)
        trades = run_backtest(df_15m)

        run_dir = os.path.join(configs.DATA_SWEEP_DIR, label)
        logs_dir = os.path.join(run_dir, 'trade_logs')
        os.makedirs(run_dir, exist_ok=True)

        path_summaries = save_trade_paths_p3(trades, df_1m, logs_dir)

        trades_out = trades.copy()
        path_df = pd.DataFrame(path_summaries)
        if not path_df.empty:
            trades_out = trades_out.merge(path_df, on='trade_id', how='left')
        trades_out.to_csv(os.path.join(run_dir, 'trade_summary.csv'), index=False)

        summary_row = _summarize(trades, path_summaries, mult)
        results.append(summary_row)
        print(f"  {summary_row['n_trades']} closed trade(s) "
              f"({summary_row['still_open_at_end']} still open at data end), "
              f"win rate {summary_row['win_rate_pct']}%, "
              f"total P&L {summary_row['total_pnl_rs']} Rs, "
              f"avg MAE/MFE {summary_row['avg_mae_points']}/{summary_row['avg_mfe_points']} pts")

    summary = pd.DataFrame(results)
    summary_file = os.path.join(configs.DATA_SWEEP_DIR, 'sweep_p3_summary.csv')
    summary.to_csv(summary_file, index=False)
    print(f'\nSaved sweep summary to {summary_file}')
    print(summary.to_string(index=False))


if __name__ == '__main__':
    main()
