"""
Prometheus - Phase 3: dynamic-sizing equity simulation (2026-09-08, user
request). What if Prometheus had gone live at the start of the backtest
window (2026-01-30, effectively "end of Jan 2026") with Rs 50,00,000
capital and DYNAMIC_SIZING=True the whole way through, letting units
compound with realised P&L exactly as _calculate_units() would live
(units = max(1, capital // MARGIN_PER_UNIT), recomputed once per trade at
entry -- not mid-trade).

Data: the live production combo (mult 2.0, SL 2.2%/T1 2.0%/T2 5.0%),
data_sweep/mult_2.0/bespoke_trade_summary.csv, 381 trades, computed at a
1-unit (1 lot/leg, LOT_SIZE=10) basis -- scaling each trade's
lot1_pnl_rs/lot2_pnl_rs by that trade's own entry-time units reproduces
production's real per-trade P&L exactly (both columns are already
points*LOT_SIZE at 1 unit).

Equity/drawdown curve uses the per-lot-exit-event methodology already
established for this repo's headline CRUDEOILM/CRUDEOIL Calmar tables --
each lot's P&L credited at its own exit timestamp as a separate
chronological cash-flow event, not bundled at trade completion.

Output CSVs (dynamic_sizing_trades.csv, dynamic_sizing_equity_curve.csv)
are written alongside bespoke_trade_summary.csv in data_sweep/mult_2.0/ --
generated data, gitignored like every other file in that folder.
"""
import os

import pandas as pd

STARTING_CAPITAL = 5_000_000
MARGIN_PER_UNIT = 100_000

HERE = os.path.dirname(os.path.abspath(__file__))
MULT_2_0_DIR = os.path.join(HERE, 'data_sweep', 'mult_2.0')

df = pd.read_csv(os.path.join(MULT_2_0_DIR, 'bespoke_trade_summary.csv'),
                  parse_dates=['entry_ts', 'lot1_exit_ts', 'lot2_exit_ts'])
df = df.sort_values('entry_ts').reset_index(drop=True)

capital = STARTING_CAPITAL
trade_rows = []
events = []  # (ts, delta_rs, kind) for the equity curve

for _, t in df.iterrows():
    units = max(1, int(capital // MARGIN_PER_UNIT))
    capital_before = capital

    lot1_pnl_rs = t['lot1_pnl_rs'] * units
    lot2_pnl_rs = t['lot2_pnl_rs'] * units
    total_pnl_rs = lot1_pnl_rs + lot2_pnl_rs
    capital_after = capital_before + total_pnl_rs

    trade_rows.append({
        'trade_id': int(t['trade_id']), 'direction': t['direction'],
        'entry_ts': t['entry_ts'], 'entry_price': t['entry_price'],
        'capital_before_rs': round(capital_before, 2), 'units': units,
        'lot1_exit_ts': t['lot1_exit_ts'], 'lot1_exit_reason': t['lot1_exit_reason'],
        'lot1_pnl_points': t['lot1_pnl_points'], 'lot1_pnl_rs': round(lot1_pnl_rs, 2),
        'lot2_exit_ts': t['lot2_exit_ts'], 'lot2_exit_reason': t['lot2_exit_reason'],
        'lot2_pnl_points': t['lot2_pnl_points'], 'lot2_pnl_rs': round(lot2_pnl_rs, 2),
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

max_dd_pct = events_df['drawdown_pct'].min()
max_dd_rs = events_df['drawdown_rs'].min()
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
