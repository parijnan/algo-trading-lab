"""
Per-trade 1-minute price paths — the bar-by-bar detail the summary trade log
(one row per trade, whatever exit rule v1 actually used) can't provide.

For each closed trade, walk the raw 1-min bars from entry_ts to exit_ts and
record running MAE/MFE (signed to the trade's direction) at every bar. This
is what SL/target/max-hold calibration actually needs: for any candidate SL
or target value, you can find the first bar where a trade's running MAE/MFE
crosses that value and read off what would have happened — without rerunning
the state machine per candidate.

One CSV per trade in data/trade_logs/, matching the repo's established
per-trade-log convention (iris_backtest, artemis_backtest, athena_backtest,
apollo_backtest all write individual trade_NNNN_*.csv files rather than one
combined log).

Sign convention: MAE and MFE are always >= 0. MAE = how far price has moved
against the trade so far (low vs entry for bullish, high vs entry for
bearish); MFE = how far it has moved in the trade's favour.
"""

import os

import pandas as pd


def _build_path_rows(trade: pd.Series, path: pd.DataFrame) -> list:
    direction   = trade['direction']
    entry_price = float(trade['entry_price'])
    entry_ts    = trade['entry_ts']
    running_mae = 0.0
    running_mfe = 0.0
    rows = []

    for ts, bar in path.iterrows():
        if direction == 'bullish':
            adverse   = max(entry_price - bar['low'], 0.0)
            favorable = max(bar['high'] - entry_price, 0.0)
            close_pnl = bar['close'] - entry_price
        else:
            adverse   = max(bar['high'] - entry_price, 0.0)
            favorable = max(entry_price - bar['low'], 0.0)
            close_pnl = entry_price - bar['close']

        running_mae = max(running_mae, adverse)
        running_mfe = max(running_mfe, favorable)

        rows.append({
            'ts':               ts,
            'bars_since_entry': int((ts - entry_ts).total_seconds() // 60),
            'open':             bar['open'],
            'high':             bar['high'],
            'low':              bar['low'],
            'close':            bar['close'],
            'close_pnl_points': round(close_pnl, 2),
            'running_mae':      round(running_mae, 2),
            'running_mfe':      round(running_mfe, 2),
        })

    return rows


def save_trade_logs(trades: pd.DataFrame, df_1m: pd.DataFrame, logs_dir: str) -> int:
    """
    Write one CSV per closed trade to logs_dir, named
    trade_{id:04d}_{entry_date}_{entry_HHMM}_{B|S}.csv (B=bullish, S=bearish),
    mirroring iris_backtest/data/trade_logs/'s naming. Returns count saved.
    """
    os.makedirs(logs_dir, exist_ok=True)
    n_saved = 0

    for _, t in trades.iterrows():
        if pd.isna(t['exit_ts']):
            continue  # trade never closed (still open at data end)

        path = df_1m.loc[t['entry_ts']:t['exit_ts']]
        if path.empty:
            continue

        rows = _build_path_rows(t, path)
        entry_ts = pd.Timestamp(t['entry_ts'])
        letter = 'B' if t['direction'] == 'bullish' else 'S'
        filename = f"trade_{int(t['trade_id']):04d}_{entry_ts.strftime('%Y-%m-%d_%H%M')}_{letter}.csv"
        pd.DataFrame(rows).to_csv(os.path.join(logs_dir, filename), index=False)
        n_saved += 1

    return n_saved
