"""
Prometheus entry point: load 1-min futures data -> compute 5m/15m Supertrend
-> run the state-machine backtest -> save trade log -> print analysis.

Usage: python prometheus_backtest/run.py
Symbol (CRUDEOILM vs CRUDEOIL) is set in configs.py.
"""

import os

import pandas as pd

import configs
from data_loader import load_futures_1min, resample_ohlcv, compute_st
from backtest import run_backtest
from trade_paths import save_trade_logs
from analysis import run_analysis


def main():
    print(f'=== Prometheus backtest — {configs.SYMBOL} ===')

    df_1m = load_futures_1min(configs.SYMBOL)
    n_days = df_1m.index.normalize().nunique()
    print(f'Loaded {len(df_1m)} 1-min bars across {n_days} trading days '
          f'({df_1m.index.min()} to {df_1m.index.max()})')

    df_5m  = resample_ohlcv(df_1m, f'{configs.ENTRY_TF_MIN}min')
    df_15m = resample_ohlcv(df_1m, f'{configs.REGIME_TF_MIN}min')

    df_5m  = compute_st(df_5m,  configs.ST_PERIOD, configs.ST_MULTIPLIER)
    df_15m = compute_st(df_15m, configs.ST_PERIOD, configs.ST_MULTIPLIER)

    day = df_5m.index.normalize()
    df_5m['is_day_end'] = (pd.Series(day, index=df_5m.index)
                            .ne(pd.Series(day, index=df_5m.index).shift(-1)))

    n_flips_5m = int(df_5m['trend_flip'].sum())
    print(f'{len(df_5m)} 5-min bars, {n_flips_5m} Supertrend flips detected on the entry timeframe')

    trade_log = run_backtest(df_5m, df_15m)

    os.makedirs(configs.DATA_DIR, exist_ok=True)
    trade_log.to_csv(configs.TRADE_LOG_FILE, index=False)
    print(f'Saved {len(trade_log)} trade(s) to {configs.TRADE_LOG_FILE}')

    # One 1-min bar-by-bar path CSV per trade (running MAE/MFE) — needed to
    # calibrate SL/target/max-hold without rerunning the state machine per
    # candidate value. Matches the per-trade trade_logs/ convention used by
    # iris_backtest/artemis_backtest/athena_backtest/apollo_backtest.
    n_logs = save_trade_logs(trade_log, df_1m, configs.TRADE_LOGS_DIR)
    print(f'Saved {n_logs} per-trade log(s) to {configs.TRADE_LOGS_DIR}/')

    run_analysis(configs.TRADE_LOG_FILE)


if __name__ == '__main__':
    main()
