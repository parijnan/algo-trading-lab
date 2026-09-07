"""
Prometheus - Phase 3: full per-trade detail for a specific bespoke-
calibrated 2-lot exit combo, one multiplier at a time -- for manually
inspecting individual trades, not for calibration (that's exit_calib_p3.py,
which this module reuses fill conventions and data loading from).

Schema matches prometheus_backtest/phase2/trade_summary_p2.csv as closely
as possible (same column names) so the two are directly comparable side by
side, minus the pivot-target machinery Phase 3 doesn't have.

Output: data_sweep/mult_<X.X>/bespoke_trade_summary.csv
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configs_p3 as configs  # noqa: E402
from exit_calib_p3 import (  # noqa: E402
    SWEEP_DIR, _load_multiplier_data, _target_fill_price, _stop_fill_price,
)


def _simulate_trade_detailed(trade_row: pd.Series, path_df: pd.DataFrame,
                              sl_pct: float, t1_pct: float, t2_pct: float) -> dict:
    direction = trade_row['direction']
    entry_price = float(trade_row['entry_price'])

    sl_dist = entry_price * sl_pct / 100
    t1_dist = entry_price * t1_pct / 100
    t2_dist = entry_price * t2_pct / 100
    assert t2_dist > t1_dist, f"target2 dist {t2_dist:.2f} must exceed target1 dist {t1_dist:.2f}"

    if direction == 'bullish':
        sl_price = entry_price - sl_dist
        t1_price = entry_price + t1_dist
        t2_price = entry_price + t2_dist
    else:
        sl_price = entry_price + sl_dist
        t1_price = entry_price - t1_dist
        t2_price = entry_price - t2_dist

    lot1_open, lot2_open = True, True
    lot1_exit = lot2_exit = None  # (ts, price, reason)

    rows = path_df.iloc[:-1] if len(path_df) > 1 else path_df.iloc[0:0]

    for _, bar in rows.iterrows():
        if not lot1_open and not lot2_open:
            break
        bar_open, bar_high, bar_low, ts = bar['open'], bar['high'], bar['low'], bar['ts']

        sl_hit = (bar_low <= sl_price) if direction == 'bullish' else (bar_high >= sl_price)
        if sl_hit:
            fill = _stop_fill_price(direction, sl_price, bar_open)
            if lot1_open:
                lot1_exit = (ts, fill, 'stop_loss'); lot1_open = False
            if lot2_open:
                lot2_exit = (ts, fill, 'stop_loss'); lot2_open = False
            break

        if lot1_open:
            hit = (bar_high >= t1_price) if direction == 'bullish' else (bar_low <= t1_price)
            if hit:
                fill = _target_fill_price(direction, t1_price, bar_open)
                lot1_exit = (ts, fill, 'target1'); lot1_open = False

        if lot2_open:
            hit = (bar_high >= t2_price) if direction == 'bullish' else (bar_low <= t2_price)
            if hit:
                fill = _target_fill_price(direction, t2_price, bar_open)
                lot2_exit = (ts, fill, 'target2'); lot2_open = False

    flip_ts, flip_price = trade_row['exit_ts'], float(trade_row['exit_price'])
    if lot1_open:
        lot1_exit = (flip_ts, flip_price, 'trend_flip')
    if lot2_open:
        lot2_exit = (flip_ts, flip_price, 'trend_flip')

    def _pnl_pts(exit_price):
        return (exit_price - entry_price) if direction == 'bullish' else (entry_price - exit_price)

    lot1_pnl_pts = round(_pnl_pts(lot1_exit[1]), 2)
    lot2_pnl_pts = round(_pnl_pts(lot2_exit[1]), 2)

    return {
        'trade_id': int(trade_row['trade_id']),
        'contract_expiry': trade_row['contract_expiry'],
        'direction': direction,
        'entry_ts': trade_row['entry_ts'],
        'entry_price': entry_price,
        'signal_ts': trade_row['signal_ts'],
        'signal_close': trade_row['signal_close'],
        'entry_slippage_points': trade_row['entry_slippage_points'],
        'sl_price': round(sl_price, 2),
        'lot1_target': round(t1_price, 2),
        'lot2_target': round(t2_price, 2),
        'lot1_exit_ts': lot1_exit[0], 'lot1_exit_price': round(lot1_exit[1], 2),
        'lot1_exit_reason': lot1_exit[2], 'lot1_pnl_points': lot1_pnl_pts,
        'lot1_pnl_rs': round(lot1_pnl_pts * configs.LOT_SIZE, 2),
        'lot2_exit_ts': lot2_exit[0], 'lot2_exit_price': round(lot2_exit[1], 2),
        'lot2_exit_reason': lot2_exit[2], 'lot2_pnl_points': lot2_pnl_pts,
        'lot2_pnl_rs': round(lot2_pnl_pts * configs.LOT_SIZE, 2),
        'total_pnl_points': round(lot1_pnl_pts + lot2_pnl_pts, 2),
        'total_pnl_rs': round((lot1_pnl_pts + lot2_pnl_pts) * configs.LOT_SIZE, 2),
        'raw_exit_ts': flip_ts, 'raw_exit_reason_if_unmanaged': 'trend_flip',
    }


def save_bespoke_summary(mult: float, sl_pct: float, t1_pct: float, t2_pct: float) -> str:
    trades, paths = _load_multiplier_data(mult)
    rows = []
    for _, t in trades.iterrows():
        tid = int(t['trade_id'])
        if tid not in paths:
            continue
        rows.append(_simulate_trade_detailed(t, paths[tid], sl_pct, t1_pct, t2_pct))

    out = pd.DataFrame(rows)
    out_path = os.path.join(SWEEP_DIR, f'mult_{mult:.1f}', 'bespoke_trade_summary.csv')
    out.to_csv(out_path, index=False)
    return out_path


if __name__ == '__main__':
    # The two bespoke-calibrated combos established this session (2026-09-01).
    runs = [
        (2.0, 2.2, 2.0, 5.0),
        (2.5, 1.0, 1.25, 4.0),
    ]
    for mult, sl, t1, t2 in runs:
        path = save_bespoke_summary(mult, sl, t1, t2)
        df = pd.read_csv(path)
        wins = int((df['total_pnl_rs'] > 0).sum())
        print(f"mult {mult}: SL={sl}% T1={t1}% T2={t2}%  "
              f"{len(df)} trades, {wins} wins, total P&L {df['total_pnl_rs'].sum():,.0f} Rs")
        print(f'  Saved to {path}')
