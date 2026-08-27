"""
Prometheus - Phase 2 entry point: load 1-min CRUDEOILM -> compute ST_15 +
daily pivots -> run the two-lot scale-out backtest -> save consolidated
trade summary + individual per-trade logs -> print analysis.

Usage: python prometheus_backtest/phase2/run_p2.py
"""

import os

import pandas as pd

import configs_p2 as configs
from data_loader_p2 import load_futures_1min, resample_ohlcv, compute_st, compute_daily_pivots
from backtest_p2 import run_backtest
from trade_paths_p2 import save_trade_logs_p2
from analysis_p2 import run_analysis


def main():
    print(f'=== Prometheus Phase 2 backtest — {configs.SYMBOL} ===')

    df_1m = load_futures_1min(configs.SYMBOL)
    n_days = df_1m.index.normalize().nunique()
    print(f'Loaded {len(df_1m)} 1-min bars across {n_days} trading days '
          f'({df_1m.index.min()} to {df_1m.index.max()})')

    df_15m = resample_ohlcv(df_1m, '15min')
    df_15m = compute_st(df_15m, configs.ST_PERIOD, configs.ST_MULTIPLIER)

    day = df_15m.index.normalize()
    day_last_bar_ts = df_15m.groupby(day).apply(lambda g: g.index.max())
    df_15m['mins_to_close'] = (day.map(day_last_bar_ts) - df_15m.index).total_seconds() / 60
    df_15m['is_day_end'] = df_15m['mins_to_close'] <= 0

    daily_pivots = compute_daily_pivots(df_1m)

    n_flips = int(df_15m['trend_flip'].sum())
    print(f'{len(df_15m)} 15-min bars, {n_flips} ST_15 flips '
          f'(period={configs.ST_PERIOD}, multiplier={configs.ST_MULTIPLIER})')

    trades = run_backtest(df_15m, daily_pivots)
    trades['total_pnl_points'] = trades[['lot1_pnl_points', 'lot2_pnl_points']].sum(axis=1, min_count=1)
    trades['total_pnl_rs'] = trades[['lot1_pnl_rs', 'lot2_pnl_rs']].sum(axis=1, min_count=1)

    os.makedirs(configs.DATA_DIR, exist_ok=True)
    trades.to_csv(configs.TRADE_SUMMARY_FILE, index=False)
    print(f'Saved {len(trades)} trade(s) to {configs.TRADE_SUMMARY_FILE}')

    # Per-trade 1-min path CSVs — lot1/lot2/total running P&L (realized once
    # a lot closes, mark-to-market until then) plus running MAE/MFE. A
    # "trade" spans entry to whichever lot exits LAST.
    n_logs = save_trade_logs_p2(trades, df_1m, configs.TRADE_LOGS_DIR)
    print(f'Saved {n_logs} per-trade log(s) to {configs.TRADE_LOGS_DIR}/')

    run_analysis(configs.TRADE_SUMMARY_FILE)


if __name__ == '__main__':
    main()
