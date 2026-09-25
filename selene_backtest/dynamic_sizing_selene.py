"""
Selene - dynamic-sizing equity simulation, no slippage (plan §14).

What if Selene had gone live at the start of the parity backtest with Rs 1,00,000 and
DYNAMIC_SIZING on the whole way, letting units compound with realised P&L exactly as
Prometheus's _calculate_units() does: units = max(1, capital // margin_per_unit), recomputed once
per trade at entry, never mid-trade. Same methodology as prometheus_backtest/phase3/
dynamic_sizing_sim.py.

margin_per_unit = entry_price * LOT_SIZE / MARGIN_CONTRACT_VALUE_DIVISOR * MARGIN_SIZING_MULTIPLIER
                = entry_price * 1 / 8 * 2 = entry_price / 4      (user, 2026-09-24; selene_configs)
computed per trade from that trade's own entry price (the analogue of the LTP production reads
just before placing the order). One unit = one lot (1 kg): the 2-lot scale-out was dropped (plan §12).

Trades are the production-parity backtest's (parity_trades.csv, 1 lot each, P&L in points = Rs per
lot); a trade's Rs P&L is pnl_pts * units. The position is single and sequential, so a trade's P&L
lands in capital before the next trade is sized. The equity curve credits each trade at its exit time.

Flags, not enforced (max(1, ...) mirrors Prometheus's simulation, but production would refuse):
  * capital_below_margin  -- capital < margin for even one unit at entry (production's
    _check_margin_sufficient would block the entry; here it is still taken at 1 unit)
  * over_freeze           -- units exceed MCX_FREEZE_QTY_LOTS (production splits the order)
Liquidity/slippage of large unit counts is NOT modelled here (a separate step). A sensitivity table
re-runs the same trades with a hard cap on units to show how much of the result depends on size.

Output (data_sweep/): dynamic_sizing_trades.csv, dynamic_sizing_equity_curve.csv
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selene_configs as configs


def simulate(trades: pd.DataFrame, start_capital: float, max_units: int = None) -> tuple:
    capital = start_capital
    rows, events = [], []
    for _, t in trades.sort_values('entry_ts').iterrows():
        margin = t['entry_px'] * configs.LOT_SIZE / configs.MARGIN_CONTRACT_VALUE_DIVISOR * configs.MARGIN_SIZING_MULTIPLIER
        units = max(1, int(capital // margin))
        if max_units is not None:
            units = min(units, max_units)
        pnl_rs = t['pnl_pts'] * configs.LOT_SIZE * units
        after = capital + pnl_rs
        rows.append({
            'trade_id': int(t['trade_id']), 'direction': t['direction'], 'entry_ts': t['entry_ts'], 'exit_ts': t['exit_ts'],
            'entry_px': t['entry_px'], 'capital_before_rs': round(capital, 2), 'margin_per_unit_rs': round(margin, 2),
            'units': units, 'capital_below_margin': capital < margin, 'over_freeze': units > configs.MCX_FREEZE_QTY_LOTS,
            'pnl_pts_per_lot': t['pnl_pts'], 'pnl_rs': round(pnl_rs, 2), 'capital_after_rs': round(after, 2),
            'last_reason': t['last_reason'], 'legs': int(t['legs']),
        })
        events.append((t['exit_ts'], pnl_rs, int(t['trade_id'])))
        capital = after
    ev = pd.DataFrame(events, columns=['ts', 'delta_rs', 'trade_id']).sort_values('ts').reset_index(drop=True)
    ev['equity'] = start_capital + ev['delta_rs'].cumsum()
    ev['running_peak'] = ev['equity'].cummax()
    ev['drawdown_rs'] = ev['equity'] - ev['running_peak']
    ev['drawdown_pct'] = ev['drawdown_rs'] / ev['running_peak']
    return pd.DataFrame(rows), ev


def main():
    trades = pd.read_csv(os.path.join(configs.DATA_SWEEP_DIR, 'parity_trades.csv'), parse_dates=['entry_ts', 'exit_ts'])
    tr = trades.sort_values('entry_ts').reset_index(drop=True)
    overlap = int((tr['entry_ts'].iloc[1:].to_numpy() < tr['exit_ts'].iloc[:-1].to_numpy()).sum())
    out, ev = simulate(tr, configs.DYNAMIC_SIZING_START_CAPITAL)
    out.to_csv(os.path.join(configs.DATA_SWEEP_DIR, 'dynamic_sizing_trades.csv'), index=False)
    ev.to_csv(os.path.join(configs.DATA_SWEEP_DIR, 'dynamic_sizing_equity_curve.csv'), index=False)

    start = configs.DYNAMIC_SIZING_START_CAPITAL
    final = out['capital_after_rs'].iloc[-1]
    days = (tr['entry_ts'].max() - tr['entry_ts'].min()).days
    yrs = days / 365.25
    dd = ev.loc[ev['drawdown_pct'].idxmin()]
    ret = (final / start - 1) * 100
    print(f"Trades: {len(out)}  Wins: {int((out['pnl_rs'] > 0).sum())} ({(out['pnl_rs'] > 0).mean():.1%})   overlapping trades: {overlap}")
    print(f"Starting capital: Rs {start:,}")
    print(f"Final capital:    Rs {final:,.0f}")
    print(f"Total return:     {ret:,.1f}%   ({final / start:,.1f}x)")
    print(f"Period: {tr['entry_ts'].min()} to {tr['entry_ts'].max()}  ({days} days, {yrs:.2f} yrs)")
    print(f"CAGR:             {((final / start) ** (1 / yrs) - 1) * 100:,.1f}%")
    print(f"Max drawdown:     {dd['drawdown_pct']:.1%}  (Rs {dd['drawdown_rs']:,.0f}) at {dd['ts']}")
    print(f"Calmar:           {(ret / 100) / abs(dd['drawdown_pct']):.2f}")
    print(f"Units range:      {out['units'].min()} to {out['units'].max()}   at trade 1: {out['units'].iloc[0]}   at last trade: {out['units'].iloc[-1]}")
    print(f"Margin/unit range: Rs {out['margin_per_unit_rs'].min():,.0f} to Rs {out['margin_per_unit_rs'].max():,.0f}")
    print(f"Trades with capital below one unit's margin: {int(out['capital_below_margin'].sum())}   over freeze qty ({configs.MCX_FREEZE_QTY_LOTS}): {int(out['over_freeze'].sum())}")
    print(f"Lowest capital before a trade: Rs {out['capital_before_rs'].min():,.0f}   ruin (capital <= 0): {bool((out['capital_after_rs'] <= 0).any())}")
    print('\nSensitivity to a hard cap on units (same trades, same start capital):')
    rows = []
    for cap in (10, 25, 50, 100, 250, 600, None):
        o, e = simulate(tr, start, cap)
        f = o['capital_after_rs'].iloc[-1]
        d = e['drawdown_pct'].min()
        rows.append({'max_units': 'none' if cap is None else cap, 'final_capital': round(f), 'return_x': round(f / start, 1),
                     'max_dd_pct': round(d * 100, 1), 'calmar': round((f / start - 1) / abs(d), 1),
                     'peak_units': int(o['units'].max()), 'trades_at_cap': int((o['units'] == cap).sum()) if cap else 0})
    print(pd.DataFrame(rows).to_string(index=False))
    print('\nBy entry year (units at first trade of year, capital start -> end):')
    out['year'] = out['entry_ts'].dt.year
    g = out.groupby('year')
    print(pd.DataFrame({'trades': g.size(), 'units_first': g['units'].first(), 'units_max': g['units'].max(),
                        'cap_start': g['capital_before_rs'].first().round(0), 'cap_end': g['capital_after_rs'].last().round(0),
                        'pnl_rs': g['pnl_rs'].sum().round(0)}).to_string())
    print('\nUnits trajectory (first appearance of each level, first 25 and last 8):')
    first = out.drop_duplicates('units')[['trade_id', 'entry_ts', 'units', 'capital_before_rs']]
    print(pd.concat([first.head(25), first.tail(8)]).drop_duplicates().to_string(index=False))


if __name__ == '__main__':
    main()
