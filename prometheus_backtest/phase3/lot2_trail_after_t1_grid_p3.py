"""
Prometheus - Phase 3, mult 2.0: grid search over lot2's trail level after
lot1 hits target1 (follow-up to lot2_breakeven_after_t1_p3.py).

That script tested one specific trail level -- breakeven (0% from entry) --
and found it Calmar-negative (12.11 -> 11.48) despite a small total-P&L
gain, because a breakeven stop can still fire on a trade that later
recovers to a bigger win (verified concretely on trade #111 -- see
README.md's Supporting analysis). Follow-up investigation (2026-09-09)
found only 6 of 25 breakeven-retracement cases were gap-driven; the other
19 retraced through breakeven during perfectly ordinary continuous trading
-- so this isn't a rare tail-event problem the rule could special-case
around, it's a routine one, worth testing whether a *tighter* trail
(giving back less of lot1's already-realized 2.2% gain before locking in
lot2) finds a better trade-off than the two extremes already understood
(0% trail = breakeven, tested; no trail at all = the original wide SL,
baseline).

Grid: trail level as a percentage of entry price, from 2.2% (right at
lot1's own target1 level -- the tightest possible trail, locks in almost
all of lot1's gain immediately) down to 0.0% (breakeven, already tested
in lot2_breakeven_after_t1_p3.py -- included here again as the grid's own
endpoint, for a single consistent comparison table) in 0.2% steps.
T1_PCT/SL_PCT/T2_PCT and lot1's own behavior are all unchanged -- only
lot2's post-target1 stop level varies, one variable per grid point.

Output: data_sweep/mult_2.0/lot2_trail_after_t1_grid.csv
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

TRAIL_GRID_PCT = [round(2.2 - 0.2 * i, 1) for i in range(12)]  # 2.2, 2.0, ..., 0.2, 0.0


def _simulate(trade_row: pd.Series, path_df: pd.DataFrame, trail_pct: float) -> dict:
    direction = trade_row['direction']
    entry_price = float(trade_row['entry_price'])

    sl_dist = entry_price * SL_PCT / 100
    t1_dist = entry_price * T1_PCT / 100
    t2_dist = entry_price * T2_PCT / 100
    trail_dist = entry_price * trail_pct / 100

    if direction == 'bullish':
        sl_price = entry_price - sl_dist
        t1_price = entry_price + t1_dist
        t2_price = entry_price + t2_dist
        trail_price = entry_price + trail_dist
    else:
        sl_price = entry_price + sl_dist
        t1_price = entry_price - t1_dist
        t2_price = entry_price - t2_dist
        trail_price = entry_price - trail_dist

    lot1_open, lot2_open = True, True
    lot1_exit = lot2_exit = None  # (fill_price, reason, ts)
    trail_active = False

    rows = path_df.iloc[:-1] if len(path_df) > 1 else path_df.iloc[0:0]

    for _, bar in rows.iterrows():
        if not lot1_open and not lot2_open:
            break
        bar_open, bar_high, bar_low, bar_ts = bar['open'], bar['high'], bar['low'], bar['ts']

        if lot1_open:
            sl_hit = (bar_low <= sl_price) if direction == 'bullish' else (bar_high >= sl_price)
            if sl_hit:
                fill = _stop_fill_price(direction, sl_price, bar_open)
                lot1_exit = (fill, 'stop_loss', bar_ts)
                lot1_open = False
            else:
                hit = (bar_high >= t1_price) if direction == 'bullish' else (bar_low <= t1_price)
                if hit:
                    fill = _target_fill_price(direction, t1_price, bar_open)
                    lot1_exit = (fill, 'target1', bar_ts)
                    lot1_open = False
                    trail_active = True

        if lot2_open:
            active_stop = trail_price if trail_active else sl_price
            sl_hit = (bar_low <= active_stop) if direction == 'bullish' else (bar_high >= active_stop)
            if sl_hit:
                fill = _stop_fill_price(direction, active_stop, bar_open)
                reason = 'trail_stop' if trail_active else 'stop_loss'
                lot2_exit = (fill, reason, bar_ts)
                lot2_open = False
            else:
                hit = (bar_high >= t2_price) if direction == 'bullish' else (bar_low <= t2_price)
                if hit:
                    fill = _target_fill_price(direction, t2_price, bar_open)
                    lot2_exit = (fill, 'target2', bar_ts)
                    lot2_open = False

    flip_price = float(trade_row['exit_price'])
    flip_ts = trade_row['exit_ts']
    if lot1_open:
        lot1_exit = (flip_price, 'trend_flip', flip_ts)
    if lot2_open:
        lot2_exit = (flip_price, 'trend_flip', flip_ts)

    def _pnl_pts(exit_price):
        return (exit_price - entry_price) if direction == 'bullish' else (entry_price - exit_price)

    return {
        'lot1_exit_reason': lot1_exit[1], 'lot1_pnl_points': round(_pnl_pts(lot1_exit[0]), 2),
        'lot1_exit_ts': lot1_exit[2],
        'lot2_exit_reason': lot2_exit[1], 'lot2_pnl_points': round(_pnl_pts(lot2_exit[0]), 2),
        'lot2_exit_ts': lot2_exit[2],
    }


def per_lot_exit_calmar(sim_df: pd.DataFrame) -> tuple:
    events = []
    for _, r in sim_df.iterrows():
        events.append((r['lot1_exit_ts'], r['lot1_pnl_points'] * configs.LOT_SIZE))
        events.append((r['lot2_exit_ts'], r['lot2_pnl_points'] * configs.LOT_SIZE))
    ev = pd.DataFrame(events, columns=['ts', 'delta']).sort_values('ts').reset_index(drop=True)
    ev['equity'] = ev['delta'].cumsum()
    ev['peak'] = ev['equity'].cummax()
    ev['dd'] = ev['equity'] - ev['peak']
    max_dd = ev['dd'].min()
    total = ev['delta'].sum()
    calmar = total / abs(max_dd) if max_dd else float('nan')
    return total, max_dd, calmar


def main():
    trades, paths = _load_multiplier_data(MULT)

    results = []
    for trail_pct in TRAIL_GRID_PCT:
        sim_rows = []
        for _, t in trades.iterrows():
            tid = int(t['trade_id'])
            if tid not in paths:
                continue
            r = _simulate(t, paths[tid], trail_pct)
            r['trade_id'] = tid
            sim_rows.append(r)
        sim_df = pd.DataFrame(sim_rows)

        total, max_dd, calmar = per_lot_exit_calmar(sim_df)
        wins = (sim_df['lot1_pnl_points'] * configs.LOT_SIZE +
                sim_df['lot2_pnl_points'] * configs.LOT_SIZE > 0).sum()
        results.append({
            'trail_pct': trail_pct, 'n_trades': len(sim_df), 'wins': int(wins),
            'total_pnl_rs': round(total, 0), 'max_dd_rs': round(max_dd, 0), 'calmar': round(calmar, 2),
        })
        print(f"trail={trail_pct:>4.1f}%  total P&L Rs {total:>10,.0f}  max DD Rs {max_dd:>10,.0f}  Calmar {calmar:>6.2f}")

    out_df = pd.DataFrame(results)
    out_path = os.path.join(SWEEP_DIR, 'mult_2.0', 'lot2_trail_after_t1_grid.csv')
    out_df.to_csv(out_path, index=False)
    print(f'\nSaved to {out_path}')

    best = out_df.loc[out_df['calmar'].idxmax()]
    print(f"\nBest by Calmar: trail={best['trail_pct']}%  Calmar={best['calmar']}  "
          f"(vs. no-rule baseline Calmar 12.11, Total P&L Rs 184,892, Max DD Rs -15,267)")


if __name__ == '__main__':
    main()
