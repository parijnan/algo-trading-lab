"""
Prometheus - Phase 3: dynamic-sizing equity simulation (2026-09-08, user
request). What if Prometheus had gone live at the start of the backtest
window (2026-01-30, effectively "end of Jan 2026") with Rs 50,00,000
capital and DYNAMIC_SIZING=True the whole way through, letting units
compound with realised P&L exactly as _calculate_units() would live
(units = max(1, capital // MARGIN_PER_UNIT), recomputed once per trade at
entry -- not mid-trade).

Data: the live production combo (mult 2.0, SL 2.2%/T1 2.2%/T2 5.0%,
T1 changed from 2.0% on 2026-09-09 -- see prometheus_backtest/README.md's
Phase 3 caveat #1),
data_sweep/mult_2.0/bespoke_trade_summary.csv, 381 trades, computed at a
1-unit (1 lot/leg, LOT_SIZE=10) basis -- scaling each trade's
lot1_pnl_rs/lot2_pnl_rs by that trade's own entry-time units reproduces
production's real per-trade P&L exactly (both columns are already
points*LOT_SIZE at 1 unit).

Equity/drawdown curve uses the per-trade methodology established for this
repo's headline CRUDEOILM/CRUDEOIL Calmar tables (changed 2026-09-11 from
the earlier per-lot-exit-event convention -- see two_candidate_stats_p3.py's
docstring for the reasoning) -- each trade's lot1+lot2 P&L combined into ONE
cash-flow event, credited at the later of the two lots' own exit
timestamps, matching what _calculate_units() actually reads (capital at
trade boundaries, never a lot1-only intermediate value).

Sizing formula updated 2026-09-11 (§26 of the production plan): margin per
unit is no longer a frozen constant -- it's recomputed PER TRADE from that
trade's own entry_price, mirroring production's
Prometheus._calculate_margin_per_unit() (prometheus.py), which now computes
margin fresh from live LTP every call instead of reading the old frozen
MARGIN_PER_UNIT config constant (that constant is now only a live-feed-
failure fallback in production, not the sizing formula). entry_price is
this backtest's closest analog to "live LTP at the moment of sizing" --
production sizes off the LTP it reads immediately before placing the
order, at essentially the same price as the eventual fill. Formula:
margin_per_unit = entry_price * LOT_SIZE / MARGIN_CONTRACT_VALUE_DIVISOR *
MARGIN_SIZING_MULTIPLIER (same divisor/multiplier as prometheus_configs.py
-- keep these two constants in sync with that file if it ever changes).
Because margin now tracks price rather than sitting fixed at the old
Rs 100,000, and early-2026 crude was cheaper than the price that constant
implicitly assumed (~Rs 7,500 at LOT_SIZE=10), this simulation now starts
BIGGER (more units at trade 1) than the pre-2026-09-11 version, not
smaller -- the opposite of what "margin has risen since" intuition
suggests, because unit sizing is dominated by where price WAS at each
trade's own entry, not by where it is today.

Output CSVs (dynamic_sizing_trades.csv, dynamic_sizing_equity_curve.csv)
are written alongside bespoke_trade_summary.csv in data_sweep/mult_2.0/ --
generated data, gitignored like every other file in that folder.
"""
import os

import pandas as pd

STARTING_CAPITAL = 5_000_000
LOT_SIZE = 10                       # CRUDEOILM, barrels/lot
MARGIN_CONTRACT_VALUE_DIVISOR = 3   # keep in sync with prometheus_configs.py
MARGIN_SIZING_MULTIPLIER = 4        # keep in sync with prometheus_configs.py

HERE = os.path.dirname(os.path.abspath(__file__))
MULT_2_0_DIR = os.path.join(HERE, 'data_sweep', 'mult_2.0')

df = pd.read_csv(os.path.join(MULT_2_0_DIR, 'bespoke_trade_summary.csv'),
                  parse_dates=['entry_ts', 'lot1_exit_ts', 'lot2_exit_ts'])
df = df.sort_values('entry_ts').reset_index(drop=True)

capital = STARTING_CAPITAL
trade_rows = []
events = []  # (ts, delta_rs, kind) for the equity curve

for _, t in df.iterrows():
    margin_per_unit = t['entry_price'] * LOT_SIZE / MARGIN_CONTRACT_VALUE_DIVISOR * MARGIN_SIZING_MULTIPLIER
    units = max(1, int(capital // margin_per_unit))
    capital_before = capital

    lot1_pnl_rs = t['lot1_pnl_rs'] * units
    lot2_pnl_rs = t['lot2_pnl_rs'] * units
    total_pnl_rs = lot1_pnl_rs + lot2_pnl_rs
    capital_after = capital_before + total_pnl_rs

    trade_rows.append({
        'trade_id': int(t['trade_id']), 'direction': t['direction'],
        'entry_ts': t['entry_ts'], 'entry_price': t['entry_price'],
        'capital_before_rs': round(capital_before, 2),
        'margin_per_unit_rs': round(margin_per_unit, 2), 'units': units,
        'lot1_exit_ts': t['lot1_exit_ts'], 'lot1_exit_reason': t['lot1_exit_reason'],
        'lot1_pnl_points': t['lot1_pnl_points'], 'lot1_pnl_rs': round(lot1_pnl_rs, 2),
        'lot2_exit_ts': t['lot2_exit_ts'], 'lot2_exit_reason': t['lot2_exit_reason'],
        'lot2_pnl_points': t['lot2_pnl_points'], 'lot2_pnl_rs': round(lot2_pnl_rs, 2),
        'total_pnl_rs': round(total_pnl_rs, 2), 'capital_after_rs': round(capital_after, 2),
    })

    trade_exit_ts = max(t['lot1_exit_ts'], t['lot2_exit_ts'])
    events.append((trade_exit_ts, total_pnl_rs,
                    f"T{int(t['trade_id'])} ({t['lot1_exit_reason']}/{t['lot2_exit_reason']})"))

    capital = capital_after

trades_out = pd.DataFrame(trade_rows)

events_df = pd.DataFrame(events, columns=['ts', 'delta_rs', 'label']).sort_values('ts').reset_index(drop=True)
events_df['equity'] = STARTING_CAPITAL + events_df['delta_rs'].cumsum()
events_df['running_peak'] = events_df['equity'].cummax()
events_df['drawdown_rs'] = events_df['equity'] - events_df['running_peak']
events_df['drawdown_pct'] = events_df['drawdown_rs'] / events_df['running_peak']

trades_out.to_csv(os.path.join(MULT_2_0_DIR, 'dynamic_sizing_trades.csv'), index=False)
events_df.to_csv(os.path.join(MULT_2_0_DIR, 'dynamic_sizing_equity_curve.csv'), index=False)

# --- Summary stats ---
n = len(trades_out)
wins = int((trades_out['total_pnl_rs'] > 0).sum())
final_capital = capital
total_return_pct = (final_capital / STARTING_CAPITAL - 1) * 100
days = (df['entry_ts'].max() - df['entry_ts'].min()).days
years = days / 365.25
cagr = ((final_capital / STARTING_CAPITAL) ** (1 / years) - 1) * 100 if years > 0 else float('nan')

max_dd_row = events_df.loc[events_df['drawdown_pct'].idxmin()]
max_dd_pct = max_dd_row['drawdown_pct']
max_dd_rs = max_dd_row['drawdown_rs']
calmar = (total_return_pct / 100) / abs(max_dd_pct) if max_dd_pct else float('nan')

print(f"Trades: {n}  Wins: {wins} ({wins/n:.1%})")
print(f"Starting capital: Rs {STARTING_CAPITAL:,}")
print(f"Final capital:    Rs {final_capital:,.0f}")
print(f"Total return:     {total_return_pct:,.1f}%")
print(f"Period: {df['entry_ts'].min()} to {df['entry_ts'].max()}  ({days} days, {years:.2f} yrs)")
print(f"CAGR:             {cagr:,.1f}%")
print(f"Max drawdown:     {max_dd_pct:.1%}  (Rs {max_dd_rs:,.0f})")
print(f"Calmar:           {calmar:.2f}")
print(f"Units range:      {trades_out['units'].min()} to {trades_out['units'].max()}")
print(f"Units at trade 1: {trades_out['units'].iloc[0]}   Units at last trade: {trades_out['units'].iloc[-1]}")
print(f"Margin/unit range: Rs {trades_out['margin_per_unit_rs'].min():,.0f} to Rs {trades_out['margin_per_unit_rs'].max():,.0f}")
print(f"Margin/unit at trade 1: Rs {trades_out['margin_per_unit_rs'].iloc[0]:,.0f}   "
      f"at last trade: Rs {trades_out['margin_per_unit_rs'].iloc[-1]:,.0f}")
print()
print("Units trajectory (first appearance of each new units level):")
seen = set()
for _, r in trades_out.iterrows():
    if r['units'] not in seen:
        seen.add(r['units'])
        print(f"  units={r['units']:>4}  first at trade #{r['trade_id']} ({r['entry_ts']:%Y-%m-%d}), capital_before=Rs{r['capital_before_rs']:,.0f}")
print()
print(f"Saved: {os.path.join(MULT_2_0_DIR, 'dynamic_sizing_trades.csv')} ({len(trades_out)} rows)")
print(f"Saved: {os.path.join(MULT_2_0_DIR, 'dynamic_sizing_equity_curve.csv')} ({len(events_df)} rows)")
