"""
Selene - slippage-adjusted dynamic-sizing simulation (plan §15).

Extends dynamic_sizing_selene.py with the participation-based slippage model of
prometheus_backtest/phase3/dynamic_sizing_sim_slippage.py, run on the production-parity trades and legs:

  slippage = A * sqrt(participation_pct),   participation_pct = 100 * order_lots / volume of the fill's own minute

* Every leg has two fills, an entry and an exit, each of `units` lots (one unit = one lot). A rolled trade
  (fallback or forced roll) has a fill pair per leg, and each fill uses the volume of the contract it trades.
  Slippage always hurts: entries fill worse by the slippage, exits fill worse by it.
* Fill minutes are the legs' own entry/exit timestamps (bar-open minutes for signal fills, the stop minute for stops).
* THE mechanic that matters, as in Prometheus: slippage feeds back into capital BEFORE the next trade is sized
  (units = max(1, post-slippage capital // margin_per_unit)), so bigger size -> worse fills -> less capital -> smaller
  next size. A slippage deduction bolted onto the no-slippage unit counts would overstate the damage.
* Coefficient A. Prometheus anchored "25% participation costs 1.5 ticks" on CRUDEOILM (tick = 1 point on ~Rs 9,000).
  SILVERMIC's tick is Rs 1 on Rs 65,000-260,000, ~15x finer relative to price, so that anchor taken literally in ticks
  (A = 0.3 points) is far too cheap here. Primary variant keeps the same RELATIVE cost, 1.67 bps of price at 25%
  participation (A = 0.333 bps per sqrt(% participation)); the literal-ticks variant is reported as a floor, and the
  primary is stress-tested at 0.5x and 2x. All three are uncalibrated assumptions, not measurements.
* Volume guard (a deliberate deviation from Prometheus's flat floor): Fyers writes zero-volume placeholder bars for
  untraded minutes, and flooring those at 1 lot would make participation absurd. A zero-volume fill minute uses the
  mean of the non-zero minutes within selene_configs.VOLUME_NEIGHBOUR_MIN either side; only if none exist, the floor.
* An order larger than the minute's whole volume (participation > 100%) is filled at one price by this model; a real
  order would be sliced over minutes and the 600-lot freeze quantity forces slicing anyway. Order-splitting is not
  modelled, so results at very large unit counts are the least reliable part.

Output (data_sweep/): slippage_dynamic_trades.csv, slippage_dynamic_equity_curve.csv (primary variant),
liquidity_by_year.csv
"""

import os
import sys
from math import sqrt

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selene_configs as configs
from parity_backtest_selene import load_contracts


def fill_volume(c, ts, counters) -> float:
    i = c.idx.searchsorted(ts, side='left')
    if i < len(c.idx) and c.idx[i] == ts and c.v[i] > 0:
        return float(c.v[i])
    counters['zero_minute'] += 1
    w = pd.Timedelta(minutes=configs.VOLUME_NEIGHBOUR_MIN)
    lo, hi = c.idx.searchsorted(ts - w, side='left'), c.idx.searchsorted(ts + w, side='right')
    near = c.v[lo:hi]
    near = near[near > 0]
    if len(near):
        return float(near.mean())
    counters['floored'] += 1
    return float(configs.VOLUME_FLOOR_LOTS)


def build_fills(legs: pd.DataFrame, contracts: dict) -> dict:
    """trade_id -> list of legs, each with entry/exit fill volume and prices."""
    counters = {'zero_minute': 0, 'floored': 0}
    out = {}
    for tid, g in legs.sort_values(['trade_id', 'leg_no']).groupby('trade_id'):
        lst = []
        for _, l in g.iterrows():
            c = contracts[pd.Timestamp(l['contract']).date()]
            lst.append({'entry_px': float(l['entry_px']), 'exit_px': float(l['exit_px']), 'pnl_pts': float(l['pnl_pts']),
                        'entry_vol': fill_volume(c, pd.Timestamp(l['entry_ts']), counters),
                        'exit_vol': fill_volume(c, pd.Timestamp(l['exit_ts']), counters)})
        out[int(tid)] = lst
    return out, counters


def slip_points(price, participation_pct, mode, a) -> float:
    root = sqrt(participation_pct)
    return price * a / 1e4 * root if mode == 'rel' else a * root


def simulate(trades: pd.DataFrame, fills: dict, mode: str, a: float, start: float, max_units: int = None):
    capital = start
    rows, events = [], []
    for _, t in trades.sort_values('entry_ts').iterrows():
        margin = t['entry_px'] * configs.LOT_SIZE / configs.MARGIN_CONTRACT_VALUE_DIVISOR * configs.MARGIN_SIZING_MULTIPLIER
        units = max(1, int(capital // margin))
        if max_units is not None:
            units = min(units, max_units)
        slip_pts, worst_part, parts = 0.0, 0.0, []
        for leg in fills[int(t['trade_id'])]:
            for px, vol in ((leg['entry_px'], leg['entry_vol']), (leg['exit_px'], leg['exit_vol'])):
                part = 100.0 * units / vol
                slip_pts += slip_points(px, part, mode, a)
                parts.append(part)
        gross_per_lot = t['pnl_pts']
        net_per_lot = gross_per_lot - slip_pts
        pnl_rs = net_per_lot * configs.LOT_SIZE * units
        after = capital + pnl_rs
        rows.append({'trade_id': int(t['trade_id']), 'entry_ts': t['entry_ts'], 'exit_ts': t['exit_ts'], 'entry_px': t['entry_px'],
                     'capital_before_rs': round(capital, 2), 'units': units, 'gross_pts_per_lot': round(gross_per_lot, 2),
                     'slippage_pts_per_lot': round(slip_pts, 2), 'slippage_rs': round(slip_pts * configs.LOT_SIZE * units, 2),
                     'median_participation_pct': round(float(np.median(parts)), 2), 'max_participation_pct': round(max(parts), 2),
                     'pnl_rs': round(pnl_rs, 2), 'capital_after_rs': round(after, 2)})
        events.append((t['exit_ts'], pnl_rs, int(t['trade_id'])))
        capital = after
    ev = pd.DataFrame(events, columns=['ts', 'delta_rs', 'trade_id']).sort_values('ts').reset_index(drop=True)
    ev['equity'] = start + ev['delta_rs'].cumsum()
    ev['running_peak'] = ev['equity'].cummax()
    ev['drawdown_rs'] = ev['equity'] - ev['running_peak']
    ev['drawdown_pct'] = ev['drawdown_rs'] / ev['running_peak']
    return pd.DataFrame(rows), ev


def summary(name, out, ev, start) -> dict:
    final = out['capital_after_rs'].iloc[-1]
    dd = ev['drawdown_pct'].min()
    gross = (out['gross_pts_per_lot'] * out['units']).sum()
    return {'variant': name, 'final_capital': round(final), 'x_start': round(final / start, 1), 'max_dd_pct': round(dd * 100, 1),
            'calmar': round((final / start - 1) / abs(dd), 1) if dd else float('nan'),
            'peak_units': int(out['units'].max()), 'last_units': int(out['units'].iloc[-1]),
            'slippage_rs': round(out['slippage_rs'].sum()), 'slip_pct_of_gross': round(out['slippage_rs'].sum() / gross * 100, 1) if gross else float('nan'),
            'median_part_pct': round(float(out['median_participation_pct'].median()), 1),
            'trades_part_gt100': int((out['max_participation_pct'] > 100).sum()),
            'min_capital_before': round(out['capital_before_rs'].min())}


def main():
    start = configs.DYNAMIC_SIZING_START_CAPITAL
    trades = pd.read_csv(os.path.join(configs.DATA_SWEEP_DIR, 'parity_trades.csv'), parse_dates=['entry_ts', 'exit_ts'])
    legs = pd.read_csv(os.path.join(configs.DATA_SWEEP_DIR, 'parity_legs.csv'), parse_dates=['entry_ts', 'exit_ts'])
    contracts = load_contracts(configs.SYMBOL, configs.DATA_START, configs.PARITY_END_EXTENDED)
    fills, counters = build_fills(legs, contracts)
    print(f"fills: {2 * len(legs)} ({len(legs)} legs); zero-volume fill minutes {counters['zero_minute']} "
          f"(replaced by neighbour mean), floored to 1 lot: {counters['floored']}")

    variants = [('rel 0.5x', 'rel', configs.SLIP_A_REL_BPS * 0.5), ('rel 1x (primary)', 'rel', configs.SLIP_A_REL_BPS),
                ('rel 2x', 'rel', configs.SLIP_A_REL_BPS * 2), ('literal ticks (floor)', 'ticks', configs.SLIP_A_TICKS)]
    rows, primary = [], None
    for name, mode, a in variants:
        out, ev = simulate(trades, fills, mode, a, start)
        rows.append(summary(name, out, ev, start))
        if 'primary' in name:
            primary = (out, ev)
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', None)
    print('\nUncapped, slippage feeding back into capital before each trade is sized:')
    print(pd.DataFrame(rows).to_string(index=False))

    out, ev = primary
    out.to_csv(os.path.join(configs.DATA_SWEEP_DIR, 'slippage_dynamic_trades.csv'), index=False)
    ev.to_csv(os.path.join(configs.DATA_SWEEP_DIR, 'slippage_dynamic_equity_curve.csv'), index=False)
    out['year'] = out['entry_ts'].dt.year
    g = out.groupby('year')
    print('\nPrimary variant by entry year:')
    print(pd.DataFrame({'trades': g.size(), 'units_first': g['units'].first(), 'units_max': g['units'].max(),
                        'cap_start': g['capital_before_rs'].first().round(0), 'cap_end': g['capital_after_rs'].last().round(0),
                        'slippage_rs': g['slippage_rs'].sum().round(0), 'median_part_pct': g['median_participation_pct'].median().round(1)}).to_string())

    # liquidity: fill-minute volume by year, and the unit counts that keep participation at 10% / 25% of it
    vol_rows = []
    for _, t in trades.iterrows():
        for leg in fills[int(t['trade_id'])]:
            vol_rows.append((t['entry_ts'].year, leg['entry_vol']))
            vol_rows.append((t['entry_ts'].year, leg['exit_vol']))
    vdf = pd.DataFrame(vol_rows, columns=['year', 'vol'])
    lq = vdf.groupby('year')['vol'].agg(fills='size', median='median', p25=lambda s: s.quantile(0.25), p10=lambda s: s.quantile(0.10)).round(0)
    lq['units_at_10pct_of_median'] = (lq['median'] * 0.10).round(0)
    lq['units_at_25pct_of_median'] = (lq['median'] * 0.25).round(0)
    lq['units_at_25pct_of_p25'] = (lq['p25'] * 0.25).round(0)
    lq.to_csv(os.path.join(configs.DATA_SWEEP_DIR, 'liquidity_by_year.csv'))
    print('\nVolume of the minute each fill lands in (lots), and the order size that is 10% / 25% of it:')
    print(lq.to_string())

    print('\nHard unit caps under the primary slippage variant:')
    caprows = []
    for cap in (10, 25, 50, 100, 250, 600, None):
        o, e = simulate(trades, fills, 'rel', configs.SLIP_A_REL_BPS, start, cap)
        r = summary('none' if cap is None else f'cap {cap}', o, e, start)
        caprows.append(r)
    print(pd.DataFrame(caprows)[['variant', 'final_capital', 'x_start', 'max_dd_pct', 'calmar', 'peak_units', 'slippage_rs', 'slip_pct_of_gross', 'median_part_pct']].to_string(index=False))


if __name__ == '__main__':
    main()
