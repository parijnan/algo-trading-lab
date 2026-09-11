"""
Prometheus - Phase 3 CRUDEOIL (main contract): slippage-adjusted
dynamic-sizing equity simulation (2026-09-08, user request). Same model as
the CRUDEOILM version (phase3/dynamic_sizing_sim_slippage.py), applied to
the main contract's own trades, real 1-min volume, and own tick value.

Slippage model (identical form and coefficient to the CRUDEOILM version --
see prometheus_backtest/README.md's "Position sizing -- CRUDEOIL (main
contract) liquidity comparison" section for why the same coefficient
carries over: a tick of slippage costs the same Rs/barrel on either
contract, since both quote the same underlying commodity price):
  - slippage_ticks = A * sqrt(participation_pct), participation_pct = 100 *
    order_size_lots / real_1min_volume_at_that_fill's_own_minute (CRUDEOIL's
    own 1-min series, not CRUDEOILM's).
  - A = 0.3, anchored to the same single point as the CRUDEOILM version:
    25% participation = 1.5 ticks (prometheus_backtest/README.md,
    2026-09-07 CRUDEOILM analysis, carried over to CRUDEOIL's own
    2026-09-08 liquidity comparison rather than re-anchored, since neither
    was ever a calibrated ₹ figure to begin with).
  - Tick size is 1.0 index point = Rs 100/lot on CRUDEOIL (100 bbl/lot, vs
    CRUDEOILM's Rs 10/lot) -- ticks and points remain the same unit.
  - Order sizing at each fill: entry opens both legs together (units*2
    lots, LOTS_PER_LEG=1); each lot's own exit is a separate units*1-lot
    fill. Same structure as CRUDEOILM -- SYMBOL is the only thing that
    changes in Phase 3's own cross-validation convention.
  - Real 1-min CRUDEOIL volume looked up at each fill's own timestamp (not
    a boundary-only table -- see the CRUDEOILM slippage script's own
    reasoning). One entry timestamp (trade #37, 2026-02-17 10:00:00) falls
    on a single missing 1-min bar in CRUDEOIL's raw feed (checked directly
    -- 09:59 and 10:01 both present, 10:00 isn't); the same volume-floor
    guard below (1 lot) covers it, since a missing minute and a
    non-positive-volume minute need the identical fallback.
  - Volume floor: non-positive or missing volume at a fill's own minute is
    floored at 1 lot rather than left to blow up participation. 2 of
    130,218 rows repo-wide carry negative volume on CRUDEOIL (per the
    2026-09-07/08 liquidity analyses); checked directly against this trade
    set's 1,194 fills for both that and the one missing-minute case.
  - THE key mechanic carried over unchanged: slippage feeds back into
    capital before the next trade's units are sized (units = max(1,
    post_slippage_capital // MARGIN_PER_UNIT)) -- the self-damping effect
    is the point, not a frozen-units deduction bolted onto the no-slippage
    run after the fact.

MARGIN_PER_UNIT=Rs 10,00,000 and STARTING_CAPITAL=Rs 55,00,000, both
user-supplied (see dynamic_sizing_sim.py's own docstring for why these
differ from the CRUDEOILM pair).

Output: dynamic_sizing_trades_slippage.csv, dynamic_sizing_equity_curve_slippage.csv,
written alongside the base (no-slippage) CSVs in data_sweep/mult_2.0/ --
generated data, gitignored like everything else in that folder.
"""
import os
import sys
from math import sqrt

import pandas as pd

STARTING_CAPITAL = 5_500_000
MARGIN_PER_UNIT = 1_000_000
LOT_SIZE = 100          # CRUDEOIL, barrels/lot
LOTS_PER_LEG = 1         # 1 unit = 2 lots total at entry (1 lot/leg)
VOLUME_FLOOR_LOTS = 1     # guard against non-positive/missing-volume minutes

A_ANCHOR = 0.3            # pinned: 25% participation -> 1.5 ticks (same anchor as CRUDEOILM)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
MULT_2_0_DIR = os.path.join(HERE, 'data_sweep', 'mult_2.0')

sys.path.insert(0, os.path.join(REPO_ROOT, 'prometheus_backtest'))
from data_loader import load_futures_1min  # noqa: E402

_1min = load_futures_1min('CRUDEOIL')
VOLUME = _1min['volume']

df = pd.read_csv(os.path.join(MULT_2_0_DIR, 'bespoke_trade_summary.csv'),
                  parse_dates=['entry_ts', 'lot1_exit_ts', 'lot2_exit_ts'])
df = df.sort_values('entry_ts').reset_index(drop=True)


def slippage_ticks(order_lots, ts, a_coeff, floor_hits):
    vol = VOLUME.get(ts)
    if vol is None or vol <= 0:
        floor_hits[0] += 1
        vol = VOLUME_FLOOR_LOTS
    participation_pct = 100.0 * order_lots / vol
    return a_coeff * sqrt(participation_pct)


def run_sim(a_coeff):
    capital = STARTING_CAPITAL
    trade_rows = []
    events = []
    floor_hits = [0]
    total_slippage_rs = 0.0

    for _, t in df.iterrows():
        units = max(1, int(capital // MARGIN_PER_UNIT))
        capital_before = capital

        entry_lots = units * LOTS_PER_LEG * 2
        entry_slip = slippage_ticks(entry_lots, t['entry_ts'], a_coeff, floor_hits)

        lot1_lots = units * LOTS_PER_LEG
        lot1_exit_slip = slippage_ticks(lot1_lots, t['lot1_exit_ts'], a_coeff, floor_hits)
        lot1_pnl_points_adj = t['lot1_pnl_points'] - entry_slip - lot1_exit_slip
        lot1_pnl_rs = lot1_pnl_points_adj * LOT_SIZE * units

        lot2_lots = units * LOTS_PER_LEG
        lot2_exit_slip = slippage_ticks(lot2_lots, t['lot2_exit_ts'], a_coeff, floor_hits)
        lot2_pnl_points_adj = t['lot2_pnl_points'] - entry_slip - lot2_exit_slip
        lot2_pnl_rs = lot2_pnl_points_adj * LOT_SIZE * units

        total_pnl_rs = lot1_pnl_rs + lot2_pnl_rs
        capital_after = capital_before + total_pnl_rs

        raw_total_pnl_rs = (t['lot1_pnl_points'] + t['lot2_pnl_points']) * LOT_SIZE * units
        total_slippage_rs += raw_total_pnl_rs - total_pnl_rs

        trade_rows.append({
            'trade_id': int(t['trade_id']), 'direction': t['direction'],
            'entry_ts': t['entry_ts'], 'entry_price': t['entry_price'],
            'capital_before_rs': round(capital_before, 2), 'units': units,
            'entry_slippage_ticks': round(entry_slip, 3),
            'lot1_exit_ts': t['lot1_exit_ts'], 'lot1_exit_reason': t['lot1_exit_reason'],
            'lot1_exit_slippage_ticks': round(lot1_exit_slip, 3),
            'lot1_pnl_points_raw': t['lot1_pnl_points'],
            'lot1_pnl_points_adj': round(lot1_pnl_points_adj, 3),
            'lot1_pnl_rs': round(lot1_pnl_rs, 2),
            'lot2_exit_ts': t['lot2_exit_ts'], 'lot2_exit_reason': t['lot2_exit_reason'],
            'lot2_exit_slippage_ticks': round(lot2_exit_slip, 3),
            'lot2_pnl_points_raw': t['lot2_pnl_points'],
            'lot2_pnl_points_adj': round(lot2_pnl_points_adj, 3),
            'lot2_pnl_rs': round(lot2_pnl_rs, 2),
            'total_pnl_rs': round(total_pnl_rs, 2), 'capital_after_rs': round(capital_after, 2),
        })

        events.append((t['lot1_exit_ts'], lot1_pnl_rs, f"T{int(t['trade_id'])} lot1 {t['lot1_exit_reason']}"))
        events.append((t['lot2_exit_ts'], lot2_pnl_rs, f"T{int(t['trade_id'])} lot2 {t['lot2_exit_reason']}"))

        capital = capital_after

    trades_out = pd.DataFrame(trade_rows)
    events_df = pd.DataFrame(events, columns=['ts', 'delta_rs', 'label']).sort_values('ts').reset_index(drop=True)
    events_df['equity'] = STARTING_CAPITAL + events_df['delta_rs'].cumsum()
    events_df['running_peak'] = events_df['equity'].cummax()
    events_df['drawdown_rs'] = events_df['equity'] - events_df['running_peak']
    events_df['drawdown_pct'] = events_df['drawdown_rs'] / events_df['running_peak']

    n = len(trades_out)
    final_capital = capital
    total_return_pct = (final_capital / STARTING_CAPITAL - 1) * 100
    days = (df['entry_ts'].max() - df['entry_ts'].min()).days
    years = days / 365.25
    cagr = ((final_capital / STARTING_CAPITAL) ** (1 / years) - 1) * 100 if years > 0 else float('nan')
    max_dd_row = events_df.loc[events_df['drawdown_pct'].idxmin()]
    max_dd_pct = max_dd_row['drawdown_pct']
    max_dd_rs = max_dd_row['drawdown_rs']
    calmar = (total_return_pct / 100) / abs(max_dd_pct) if max_dd_pct else float('nan')

    stats = {
        'a_coeff': a_coeff, 'n_trades': n, 'final_capital': final_capital,
        'total_return_pct': total_return_pct, 'cagr': cagr,
        'max_dd_pct': max_dd_pct, 'max_dd_rs': max_dd_rs, 'calmar': calmar,
        'peak_units': int(trades_out['units'].max()),
        'total_slippage_rs': round(total_slippage_rs, 2),
        'volume_floor_hits': floor_hits[0],
    }
    return trades_out, events_df, stats


if __name__ == '__main__':
    trades_out, events_df, stats = run_sim(A_ANCHOR)

    trades_out.to_csv(os.path.join(MULT_2_0_DIR, 'dynamic_sizing_trades_slippage.csv'), index=False)
    events_df.to_csv(os.path.join(MULT_2_0_DIR, 'dynamic_sizing_equity_curve_slippage.csv'), index=False)

    print(f"=== Slippage-adjusted run (A={A_ANCHOR}, anchored: 25% participation = 1.5 ticks) ===")
    print(f"Trades: {stats['n_trades']}")
    print(f"Starting capital: Rs {STARTING_CAPITAL:,}")
    print(f"Final capital:    Rs {stats['final_capital']:,.0f}")
    print(f"Total return:     {stats['total_return_pct']:,.1f}%")
    print(f"CAGR:             {stats['cagr']:,.1f}%")
    print(f"Max drawdown:     {stats['max_dd_pct']:.1%}  (Rs {stats['max_dd_rs']:,.0f})")
    print(f"Calmar:           {stats['calmar']:.2f}")
    print(f"Peak units:       {stats['peak_units']}")
    print(f"Total slippage cost (this run's own sizing path): Rs {stats['total_slippage_rs']:,.0f}")
    print(f"Volume-floor guard hit: {stats['volume_floor_hits']} of {stats['n_trades'] * 3} fills")
    print()
    print(f"Saved: {os.path.join(MULT_2_0_DIR, 'dynamic_sizing_trades_slippage.csv')} ({len(trades_out)} rows)")
    print(f"Saved: {os.path.join(MULT_2_0_DIR, 'dynamic_sizing_equity_curve_slippage.csv')} ({len(events_df)} rows)")

    print()
    print("=== Coefficient sensitivity (0.5x / 1x / 2x anchor) ===")
    for mult, label in [(0.5, '0.5x (12.5% participation = 1.5 ticks)'),
                         (1.0, '1.0x (anchor: 25% participation = 1.5 ticks)'),
                         (2.0, '2.0x (50% participation = 1.5 ticks)')]:
        a = A_ANCHOR * mult
        _, _, s = run_sim(a)
        print(f"A={a:.3f}  [{label}]")
        print(f"  Final capital: Rs {s['final_capital']:,.0f}   Total return: {s['total_return_pct']:,.1f}%   "
              f"Max DD: {s['max_dd_pct']:.1%}   Calmar: {s['calmar']:.2f}   Peak units: {s['peak_units']}")
