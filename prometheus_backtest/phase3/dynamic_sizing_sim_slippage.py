"""
Prometheus - Phase 3: slippage-adjusted dynamic-sizing equity simulation
(2026-09-08, user request, methodology reviewed via advisor before writing
this). Extends dynamic_sizing_sim.py with a participation-based slippage
model, using the real CRUDEOILM 1-min volume data behind the 2026-09-07
liquidity analysis (prometheus_backtest/README.md, "Position sizing --
volume/participation analysis").

Slippage model (explicit assumptions):
  - slippage_ticks = A * sqrt(participation_pct), participation_pct = 100 *
    order_size_lots / real_1min_volume_at_that_fill's_own_minute.
    Square-root form per the README's own framing (impact grows with size,
    sublinearly) -- the coefficient A is pinned to a single point already
    stated and accepted in that README: "past ~20-30% participation...
    expect to... walk 1-2 ticks beyond" -> anchored at 25% participation =
    1.5 ticks, giving A = 1.5 / sqrt(25) = 0.3. This IS the uncalibrated-
    constant tradeoff the README explicitly declined to make when it said
    "isn't a real number" -- here it's accepted deliberately, stated
    plainly, and stress-tested via the sensitivity sweep below (0.5x/1x/2x
    A) rather than presented as precise.
  - Tick size is 1.0 index point = Rs 10/lot on CRUDEOILM, so
    slippage_ticks == slippage points directly.
  - Order sizing at each fill, from the strategy's own structure
    (LOTS_PER_LEG=1, "1 unit = 2 lots, 1 lot per leg" --
    prometheus_configs.py): the entry order opens both legs together, i.e.
    units*2 lots; each lot's own exit (target1/target2/stop_loss/
    trend_flip) is a separate units*1-lot order. Entry slippage degrades
    the entry price for both legs equally; each lot also eats its own exit
    slippage.
  - Real 1-min volume is looked up at each fill's own timestamp (not the
    15-min-boundary table from the README, which was built on a different,
    boundary-only minute population -- entries/trend-flip exits do land on
    15-min boundaries, but target/stop exits can fire at any minute, so
    every fill uses its own minute's real volume, never the boundary
    table).
  - Volume floor: if a fill's own minute has non-positive volume (a known
    small data-pipeline artifact -- 8 of 131,236 rows repo-wide, per the
    README), floor it at 1 lot rather than let participation go infinite.
    Zero fills in this trade set actually hit one (checked directly against
    the 381 trades' 1,143 fill timestamps) -- the guard exists for
    robustness, not because it fires here.
  - THE key mechanic this script adds over the base sim: slippage feeds
    back into capital before the next trade's units are sized (units =
    max(1, post_slippage_capital // MARGIN_PER_UNIT), recomputed at every
    entry) -- exactly like _calculate_units() would live. This is what lets
    the simulation show self-damping: worse fills at bigger size reduce
    capital, which reduces the next trade's units, which reduces
    participation and therefore slippage. Bolting a slippage deduction onto
    the existing (frozen-units) dynamic_sizing_trades.csv after the fact
    would overstate the damage, since post-slippage capital would never
    have reached the same units level in the first place.

Output: dynamic_sizing_trades_slippage.csv, dynamic_sizing_equity_curve_slippage.csv,
written alongside the base (no-slippage) CSVs in data_sweep/mult_2.0/ --
generated data, gitignored like everything else in that folder.
"""
import os
import sys
from math import sqrt

import pandas as pd

STARTING_CAPITAL = 5_000_000
MARGIN_PER_UNIT = 100_000
LOT_SIZE = 10          # CRUDEOILM, barrels/lot
LOTS_PER_LEG = 1        # 1 unit = 2 lots total at entry (1 lot/leg)
VOLUME_FLOOR_LOTS = 1    # guard against non-positive-volume minutes

A_ANCHOR = 0.3           # pinned: 25% participation -> 1.5 ticks (README, 2026-09-07)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
MULT_2_0_DIR = os.path.join(HERE, 'data_sweep', 'mult_2.0')

sys.path.insert(0, os.path.join(REPO_ROOT, 'prometheus_backtest'))
from data_loader import load_futures_1min  # noqa: E402

_1min = load_futures_1min('CRUDEOILM')
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
    max_dd_pct = events_df['drawdown_pct'].min()
    max_dd_rs = events_df['drawdown_rs'].min()
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
    # --- primary run at the anchored coefficient ---
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
    print(f"Peak units:       {stats['peak_units']}  (no-slippage run peaked at 247)")
    print(f"Total slippage cost (this run's own sizing path): Rs {stats['total_slippage_rs']:,.0f}")
    print(f"Volume-floor guard hit: {stats['volume_floor_hits']} of {stats['n_trades'] * 3} fills")
    print()
    print(f"Saved: {os.path.join(MULT_2_0_DIR, 'dynamic_sizing_trades_slippage.csv')} ({len(trades_out)} rows)")
    print(f"Saved: {os.path.join(MULT_2_0_DIR, 'dynamic_sizing_equity_curve_slippage.csv')} ({len(events_df)} rows)")

    # --- coefficient sensitivity sweep ---
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
