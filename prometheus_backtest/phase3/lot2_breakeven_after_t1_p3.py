"""
Prometheus - Phase 3, mult 2.0: test a candidate lot2 stop-management rule
(README.md's Supporting analysis, 2026-09-09 lot1/lot2 cross-tab finding).

Finding: once lot1 hits target1, lot2's eventual win rate is 88.2%
(vs. 37.4% for lot2's trend_flip bucket overall, which blends this
85.9%-win subgroup with a separate 18.6%-win subgroup where lot1 never
reached target1 at all). Of the 136 trades where lot1 hits target1, lot2
still loses on 16 of them (4 via the original wide stop_loss, 12 via a
later trend_flip that nets negative despite lot1's win).

Candidate rule tested here: once lot1 exits via target1, move lot2's own
stop-loss up to breakeven (entry price) instead of leaving it at the
original SL_PCT distance. Reasoning checked by simulation, not just
argued: a breakeven stop sitting below the already-reached target1 level
can only fire if price actually retraces all the way back to entry, and
in every one of the 16 currently-losing post-target1 trades, price must
cross breakeven before reaching either the original SL level (further
away) or a below-entry trend_flip price -- so in principle this rule can
only convert those 16 trades' losses into ~0 scratches, never worse. The
real question this script answers: does it also cut off upside on trades
that dip to breakeven and then recover to a bigger win (target2 or a
still-positive trend_flip), which the categorical reasoning above can't
see without walking the actual minute-by-minute path?

One variable changed vs. the production combo: lot2's stop level after
lot1's target1 exit. Lot1's own behavior, T2, and the raw trend_flip
signal are all unchanged.

Output: data_sweep/mult_2.0/lot2_breakeven_after_t1_comparison.csv
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configs_p3 as configs  # noqa: E402
from exit_calib_p3 import SWEEP_DIR, _load_multiplier_data, _target_fill_price, _stop_fill_price  # noqa: E402

MULT = 2.0
SL_PCT = 2.2
T1_PCT = 2.2
T2_PCT = 5.0


def _simulate_baseline_and_breakeven(trade_row: pd.Series, path_df: pd.DataFrame) -> dict:
    direction = trade_row['direction']
    entry_price = float(trade_row['entry_price'])

    sl_dist = entry_price * SL_PCT / 100
    t1_dist = entry_price * T1_PCT / 100
    t2_dist = entry_price * T2_PCT / 100

    if direction == 'bullish':
        sl_price = entry_price - sl_dist
        t1_price = entry_price + t1_dist
        t2_price = entry_price + t2_dist
    else:
        sl_price = entry_price + sl_dist
        t1_price = entry_price - t1_dist
        t2_price = entry_price - t2_dist

    lot1_open, lot2_open_base, lot2_open_be = True, True, True
    lot1_exit = lot2_exit_base = lot2_exit_be = None
    lot2_be_active = False  # becomes True once lot1 hits target1, in the breakeven-variant sim

    rows = path_df.iloc[:-1] if len(path_df) > 1 else path_df.iloc[0:0]

    for _, bar in rows.iterrows():
        if not lot1_open and not lot2_open_base and not lot2_open_be:
            break
        bar_open, bar_high, bar_low = bar['open'], bar['high'], bar['low']

        # --- lot1 (identical to production; same for both variants) ---
        if lot1_open:
            sl_hit = (bar_low <= sl_price) if direction == 'bullish' else (bar_high >= sl_price)
            if sl_hit:
                fill = _stop_fill_price(direction, sl_price, bar_open)
                lot1_exit = (fill, 'stop_loss')
                lot1_open = False
            else:
                hit = (bar_high >= t1_price) if direction == 'bullish' else (bar_low <= t1_price)
                if hit:
                    fill = _target_fill_price(direction, t1_price, bar_open)
                    lot1_exit = (fill, 'target1')
                    lot1_open = False
                    lot2_be_active = True  # arms the breakeven stop for the breakeven-variant sim

        # --- lot2, BASELINE variant: original wide SL throughout ---
        if lot2_open_base:
            sl_hit = (bar_low <= sl_price) if direction == 'bullish' else (bar_high >= sl_price)
            if sl_hit:
                fill = _stop_fill_price(direction, sl_price, bar_open)
                lot2_exit_base = (fill, 'stop_loss')
                lot2_open_base = False
            else:
                hit = (bar_high >= t2_price) if direction == 'bullish' else (bar_low <= t2_price)
                if hit:
                    fill = _target_fill_price(direction, t2_price, bar_open)
                    lot2_exit_base = (fill, 'target2')
                    lot2_open_base = False

        # --- lot2, BREAKEVEN variant: SL moves to entry_price once lot1 hits target1 ---
        if lot2_open_be:
            active_stop = entry_price if lot2_be_active else sl_price
            sl_hit = (bar_low <= active_stop) if direction == 'bullish' else (bar_high >= active_stop)
            if sl_hit:
                fill = _stop_fill_price(direction, active_stop, bar_open)
                reason = 'breakeven_stop' if lot2_be_active else 'stop_loss'
                lot2_exit_be = (fill, reason)
                lot2_open_be = False
            else:
                hit = (bar_high >= t2_price) if direction == 'bullish' else (bar_low <= t2_price)
                if hit:
                    fill = _target_fill_price(direction, t2_price, bar_open)
                    lot2_exit_be = (fill, 'target2')
                    lot2_open_be = False

    flip_price = float(trade_row['exit_price'])
    if lot1_open:
        lot1_exit = (flip_price, 'trend_flip')
    if lot2_open_base:
        lot2_exit_base = (flip_price, 'trend_flip')
    if lot2_open_be:
        lot2_exit_be = (flip_price, 'trend_flip')

    def _pnl_pts(exit_price):
        return (exit_price - entry_price) if direction == 'bullish' else (entry_price - exit_price)

    return {
        'lot1_exit_reason': lot1_exit[1], 'lot1_pnl_points': round(_pnl_pts(lot1_exit[0]), 2),
        'lot2_base_exit_reason': lot2_exit_base[1], 'lot2_base_pnl_points': round(_pnl_pts(lot2_exit_base[0]), 2),
        'lot2_be_exit_reason': lot2_exit_be[1], 'lot2_be_pnl_points': round(_pnl_pts(lot2_exit_be[0]), 2),
    }


def main():
    trades, paths = _load_multiplier_data(MULT)
    rows = []
    for _, t in trades.iterrows():
        tid = int(t['trade_id'])
        if tid not in paths:
            continue
        result = _simulate_baseline_and_breakeven(t, paths[tid])
        result['trade_id'] = tid
        rows.append(result)

    df = pd.DataFrame(rows)
    df['lot2_base_pnl_rs'] = df['lot2_base_pnl_points'] * configs.LOT_SIZE
    df['lot2_be_pnl_rs'] = df['lot2_be_pnl_points'] * configs.LOT_SIZE
    df['lot2_delta_rs'] = df['lot2_be_pnl_rs'] - df['lot2_base_pnl_rs']

    out_path = os.path.join(SWEEP_DIR, 'mult_2.0', 'lot2_breakeven_after_t1_comparison.csv')
    df.to_csv(out_path, index=False)

    n = len(df)
    after_t1 = df[df['lot1_exit_reason'] == 'target1']
    print(f'Total trades: {n}')
    print(f'Trades where lot1 hit target1: {len(after_t1)}\n')

    base_total = df['lot2_base_pnl_rs'].sum()
    be_total = df['lot2_be_pnl_rs'].sum()
    print(f"Lot2 total P&L -- baseline (wide SL throughout):     Rs {base_total:,.0f}")
    print(f"Lot2 total P&L -- breakeven-after-T1 variant:        Rs {be_total:,.0f}")
    print(f"Delta:                                               Rs {be_total - base_total:,.0f}\n")

    changed = df[df['lot2_delta_rs'] != 0]
    print(f'Trades where the two variants disagree: {len(changed)}')
    if len(changed):
        improved = changed[changed['lot2_delta_rs'] > 0]
        worsened = changed[changed['lot2_delta_rs'] < 0]
        print(f'  Improved by the rule: {len(improved)}  (sum Rs {improved["lot2_delta_rs"].sum():,.0f})')
        print(f'  Worsened by the rule: {len(worsened)}  (sum Rs {worsened["lot2_delta_rs"].sum():,.0f})')
        print()
        pd.set_option('display.width', 220)
        pd.set_option('display.max_columns', None)
        cols = ['trade_id', 'lot1_exit_reason', 'lot2_base_exit_reason', 'lot2_base_pnl_rs',
                'lot2_be_exit_reason', 'lot2_be_pnl_rs', 'lot2_delta_rs']
        print(changed[cols].sort_values('lot2_delta_rs').to_string(index=False))

    # Overall total P&L impact (lot1 unchanged + lot2 delta)
    lot1_total = df['lot1_pnl_points'].sum() * configs.LOT_SIZE
    print(f"\nOverall total P&L -- baseline:  Rs {lot1_total + base_total:,.0f}")
    print(f"Overall total P&L -- breakeven: Rs {lot1_total + be_total:,.0f}")

    print(f'\nSaved to {out_path}')


if __name__ == '__main__':
    main()
