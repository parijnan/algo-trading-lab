"""
Prometheus - Phase 2 — per-trade 1-minute path CSVs.

A Phase-2-specific rebuild of Prometheus v1's trade_paths.py: that module
assumes a single position with one exit, but here lot1 and lot2 close
independently at different times, so "running P&L" isn't well-defined by
the shared module's logic. Each bar's lot1/lot2 P&L is:
  - unrealized (marked to that bar's close vs entry) while the lot is
    still open, i.e. strictly before its exit_ts
  - frozen at the realized exit P&L from its exit_ts onward — once a lot
    is closed, further price movement doesn't change its booked result

total_pnl_{points,rs} is just lot1 + lot2 at each bar, so it correctly
shows the single-lot-open value while only one remains, and the fully
realized total once both have closed.

MAE/MFE are computed the same way as trade_paths.py (entry to the trade's
FINAL exit, direction-signed, cumulative) — that part isn't lot-specific.
"""

import os

import pandas as pd

import configs_p2 as configs


def _lot_running_pnl(direction: str, entry_price: float, close: float,
                      lot_open: bool, realized_points: float, lot_size: int):
    if lot_open:
        points = (close - entry_price) if direction == 'bullish' else (entry_price - close)
    else:
        points = realized_points
    return round(points, 2), round(points * lot_size * configs.LOTS_PER_LEG, 2)


def save_trade_logs_p2(trades: pd.DataFrame, df_1m: pd.DataFrame, logs_dir: str) -> int:
    os.makedirs(logs_dir, exist_ok=True)
    n_saved = 0

    for _, t in trades.iterrows():
        lot1_exit_ts = t.get('lot1_exit_ts')
        lot2_exit_ts = t.get('lot2_exit_ts')
        if pd.isna(lot1_exit_ts) and pd.isna(lot2_exit_ts):
            continue  # never closed

        final_exit_ts = max(ts for ts in (lot1_exit_ts, lot2_exit_ts) if pd.notna(ts))
        path = df_1m.loc[t['entry_ts']:final_exit_ts]
        if path.empty:
            continue

        direction = t['direction']
        entry_price = float(t['entry_price'])
        entry_ts = t['entry_ts']
        running_mae = 0.0
        running_mfe = 0.0
        rows = []

        for ts, bar in path.iterrows():
            if direction == 'bullish':
                adverse = max(entry_price - bar['low'], 0.0)
                favorable = max(bar['high'] - entry_price, 0.0)
            else:
                adverse = max(bar['high'] - entry_price, 0.0)
                favorable = max(entry_price - bar['low'], 0.0)
            running_mae = max(running_mae, adverse)
            running_mfe = max(running_mfe, favorable)

            lot1_open = pd.isna(lot1_exit_ts) or ts < lot1_exit_ts
            lot2_open = pd.isna(lot2_exit_ts) or ts < lot2_exit_ts
            lot1_pts, lot1_rs = _lot_running_pnl(direction, entry_price, bar['close'],
                                                  lot1_open, t.get('lot1_pnl_points'), configs.LOT_SIZE)
            lot2_pts, lot2_rs = _lot_running_pnl(direction, entry_price, bar['close'],
                                                  lot2_open, t.get('lot2_pnl_points'), configs.LOT_SIZE)

            rows.append({
                'ts':               ts,
                'bars_since_entry': int((ts - entry_ts).total_seconds() // 60),
                'open':             bar['open'],
                'high':             bar['high'],
                'low':              bar['low'],
                'close':            bar['close'],
                'lot1_pnl_points':  lot1_pts,
                'lot1_pnl_rs':      lot1_rs,
                'lot2_pnl_points':  lot2_pts,
                'lot2_pnl_rs':      lot2_rs,
                'total_pnl_points': round(lot1_pts + lot2_pts, 2),
                'total_pnl_rs':     round(lot1_rs + lot2_rs, 2),
                'running_mae':      round(running_mae, 2),
                'running_mfe':      round(running_mfe, 2),
            })

        letter = 'B' if direction == 'bullish' else 'S'
        entry_ts_pd = pd.Timestamp(entry_ts)
        filename = f"trade_{int(t['trade_id']):04d}_{entry_ts_pd.strftime('%Y-%m-%d_%H%M')}_{letter}.csv"
        log_df = pd.DataFrame(rows)
        log_df.insert(1, 'sl_price', t.get('sl_price'))
        log_df.insert(2, 'lot1_target', t['lot1_target'])
        log_df.insert(3, 'lot2_target', t['lot2_target'])
        log_df.to_csv(os.path.join(logs_dir, filename), index=False)
        n_saved += 1

    return n_saved
