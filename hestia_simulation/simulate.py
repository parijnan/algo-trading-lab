"""
Hestia compounding simulation: size every backtest trade of the four engines by the live capital rule, charge it the real per-order costs and the live slippage, and write the
result tables the artifact page embeds.

    python hestia_simulation/simulate.py                 # three scenarios into hestia_simulation/outputs/<p|j6|j4>/
    python hestia_simulation/simulate.py --check          # also print the headline numbers

Scenarios (sim_config.SCENARIOS): p = Prometheus in the crude oil slot (the live book); j6 and j4 = Janus (3 lots a unit) in that slot, margin per unit price x 10 / 3 x 6 or x 4.
Inputs: hestia_backtest/outputs/integrated_trades.csv (refresh it with the hestia-backtest skill first), outputs/comparison.csv (live slippage), and the local Janus trade list.
"""

import argparse
import heapq
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sim_config as cfg  # noqa: E402
import janus_sync  # noqa: E402


def load_slippage():
    """Rs per unit per trade by which live trades fell short of their backtest twins (entry and exit together), from the matched live trades with the same exit, floored at 0. Janus has no live
    trades: it is assumed to slip like Prometheus per lot (Prometheus's two lots become Janus's three)."""
    c = pd.read_csv(cfg.COMPARISON_CSV)
    m = c[(c.status == 'matched') & (c.same_exit == True)].copy()      # noqa: E712
    m['slip'] = m.bt_rs - m.live_rs
    table = m.groupby('engine').slip.agg(n='size', mean='mean', sd='std')
    table['se'] = table.sd / np.sqrt(table.n)
    slip = {e: max(0.0, float(table.loc[e, 'mean'])) for e in table.index}
    slip['Janus'] = slip['Prometheus'] / 2 * cfg.JANUS_LOTS
    return slip, table


def load_janus():
    j = pd.read_csv(cfg.JANUS_TRADES, parse_dates=['entry_ts', 'exit_ts'])
    j = j[j.exit_ts.dt.normalize() <= janus_sync.integrated_end()].reset_index(drop=True)      # never beyond the day the integrated backtest ends
    return pd.DataFrame({'engine': 'Janus', 'trade_id': range(1, len(j) + 1), 'direction': np.where(j.side > 0, 'bullish', 'bearish'), 'entry_ts': j.entry_ts, 'exit_ts': j.exit_ts,
                         'entry': j.entry, 'pts': j.pnl_pts, 'rs': j.pnl_pts * 10, 'reason': j.reasons})     # pnl_pts is summed over the three lots; Rs 10 a point a lot


def exit_orders(engine, reason):
    """Exit orders a trade places: one, except a Prometheus trade that books lot 1 at a target and lot 2 later (two) and a Janus trade (one per different exit among its three lots)."""
    if engine == 'Prometheus':
        a, b = reason.split('/')
        return 1 if a == b else 2
    if engine == 'Janus':
        return len(set(reason.split(',')))
    return 1


def order_charges(engine, direction, price, units, exits):
    """Charges of one trade: an entry order and `exits` exit orders, the exit value split evenly; the entry price stands in for the exit price. Returns brokerage, gst, ctt, other."""
    value = cfg.ORDER_VALUE[engine](price) * units
    out = {'brokerage': 0.0, 'gst': 0.0, 'ctt': 0.0, 'other': 0.0}
    for leg, v in [('entry', value)] + [('exit', value / exits)] * exits:
        buy = (leg == 'entry') == (direction == 'bullish')
        exch, sebi = cfg.EXCHANGE_FEE * v, cfg.SEBI_FEE * v
        out['brokerage'] += cfg.BROKERAGE
        out['gst'] += cfg.GST * (cfg.BROKERAGE + exch + sebi)
        out['ctt'] += 0.0 if buy else cfg.CTT_SELL * v
        out['other'] += exch + sebi + (cfg.STAMP_DUTY_BUY * v if buy else 0.0)
    return out


def units_for(equity, margin_per_unit):
    """The rule: a quarter of the account over the margin per unit, rounded down, limited by the cap; 0 means the entry is skipped."""
    return min(int((equity / cfg.DIVISOR) // margin_per_unit), cfg.UNIT_CAP)


def trade_book(swap, janus):
    d = pd.read_csv(cfg.INTEGRATED_TRADES, parse_dates=['entry_ts', 'exit_ts'])
    if swap:
        d = pd.concat([d[d.engine != 'Prometheus'], janus[['engine', 'trade_id', 'direction', 'entry_ts', 'exit_ts', 'entry', 'pts', 'rs', 'reason']]])
    return d[d.entry_ts >= pd.Timestamp(cfg.START_DATE)].sort_values(['entry_ts', 'engine']).reset_index(drop=True)


def run(book, slip, janus_mult, use_charges, use_slip):
    pending, equity, rows = [], cfg.CAPITAL, []
    for i, r in book.iterrows():
        while pending and pending[0][0] <= r.entry_ts:                 # only trades closed before this entry count in the account value
            equity += heapq.heappop(pending)[1]
        mpu = cfg.MARGIN_PER_UNIT[r.engine](r.entry, janus_mult)
        units = units_for(equity, mpu)
        exits = exit_orders(r.engine, r.reason)
        taken = units >= 1
        ch = order_charges(r.engine, r.direction, r.entry, units, exits) if (taken and use_charges) else {'brokerage': 0.0, 'gst': 0.0, 'ctt': 0.0, 'other': 0.0}
        charges = sum(ch.values())
        slippage = slip[r.engine] * units if (taken and use_slip) else 0.0
        gross = r.rs * units
        net = gross - charges - slippage
        rows.append(dict(n=i + 1, engine=r.engine, direction=r.direction, entry_ts=r.entry_ts, exit_ts=r.exit_ts, entry=r.entry, reason=r.reason, equity_before=equity, share=equity / cfg.DIVISOR,
                         mpu=mpu, units=units, rs_unit=r.rs, gross=gross, orders=1 + exits if taken else 0, charges=charges, slippage=slippage, cost=charges + slippage, pnl=net, taken=taken, **ch))
        if taken:
            heapq.heappush(pending, (r.exit_ts, net, i))
    return pd.DataFrame(rows)


def end_label(end):
    return f'{end.day} {end:%b}'


def summarise(key, book, slip, janus_mult, out_dir):
    cap = cfg.CAPITAL
    al0, alc, al = run(book, slip, janus_mult, False, False), run(book, slip, janus_mult, True, False), run(book, slip, janus_mult, True, True)
    end0 = cap + al0[al0.taken].pnl.sum()
    endc = cap + alc[alc.taken].pnl.sum()
    tk = al[al.taken].copy().reset_index(drop=True)
    tk['trade_no'] = tk.index + 1
    ev = tk.sort_values(['exit_ts', 'trade_no']).copy()
    ev['cum'] = cap + ev.pnl.cumsum()
    ev['peak'] = ev.cum.cummax()
    ev['dd'] = ev.cum / ev.peak - 1
    ev['dd_rs'] = ev.cum - ev.peak
    endeq = ev.cum.iloc[-1]
    start, end = pd.Timestamp(cfg.START_DATE), ev.exit_ts.max()
    yrs = (end - start).days / 365.25
    cagr = (endeq / cap) ** (1 / yrs) - 1
    mdd, mdd_rs, mdd_date = ev.dd.min(), ev.dd_rs.min(), ev.loc[ev.dd.idxmin(), 'exit_ts']
    peak_t = ev.exit_ts.where(ev.cum >= ev.peak).ffill()
    ev['under'] = (ev.exit_ts - peak_t).dt.days
    wins, losses = tk[tk.pnl > 0], tk[tk.pnl < 0]
    streak = best = 0
    for v in (ev.pnl < 0).astype(int):
        streak = streak + 1 if v else 0
        best = max(best, streak)
    month_end = ev.set_index('exit_ts').cum.resample('ME').last()
    month_start = pd.concat([pd.Series([cap], index=[start - pd.Timedelta(days=1)]), month_end]).ffill()
    mon = pd.DataFrame({'month': month_end.index.strftime('%Y-%m'), 'end_equity': month_end.values})
    mon['start_equity'] = month_start.values[:-1]
    mon['pnl'] = mon.end_equity - mon.start_equity
    mon['return_pct'] = (mon.end_equity / mon.start_equity - 1) * 100
    sharpe = (mon.return_pct / 100).mean() / (mon.return_pct / 100).std() * np.sqrt(12)
    y = ev.copy()
    y['year'] = y.exit_ts.dt.year
    yr = []
    for k, g in y.groupby('year'):
        s0 = cap if k == start.year else y[y.year == k - 1].cum.iloc[-1]
        e1 = g.cum.iloc[-1]
        yr.append(dict(label=str(k) if k < end.year else f'{k} to {end_label(end)}', start_equity=s0, end_equity=e1, pnl=e1 - s0, return_pct=(e1 / s0 - 1) * 100, trades=len(g),
                       max_dd_pct=g.dd.min() * 100, costs=g.cost.sum()))
    yr = pd.DataFrame(yr)
    yr['share_of_profit_pct'] = yr.pnl / yr.pnl.sum() * 100
    eng = []
    for k, g in tk.groupby('engine'):
        gw, gl = g[g.pnl > 0].pnl.sum(), -g[g.pnl < 0].pnl.sum()
        eng.append(dict(engine=k, trades=len(g), skipped=int(((al.engine == k) & (~al.taken)).sum()), win_rate_pct=(g.pnl > 0).mean() * 100, pnl=g.pnl.sum(), share_of_profit_pct=g.pnl.sum() / tk.pnl.sum() * 100,
                        profit_factor=gw / gl if gl > 0 else np.nan, avg_units=g.units.mean(), peak_units=g.units.max(), avg_pnl=g.pnl.mean(), costs=g.cost.sum()))
    eng = pd.DataFrame(eng)
    kv = [('start_capital', cap), ('end_equity', endeq), ('end_equity_no_costs', end0), ('end_equity_charges_only', endc), ('multiple', endeq / cap), ('total_return_pct', (endeq / cap - 1) * 100),
          ('cagr_pct', cagr * 100), ('years', yrs), ('max_dd_pct', mdd * 100), ('max_dd_rs', mdd_rs), ('max_dd_date', mdd_date.strftime('%d %b %Y')), ('longest_underwater_days', ev.under.max()),
          ('calmar', cagr / abs(mdd) if mdd < 0 else 0), ('sharpe_monthly', sharpe), ('trades_taken', len(tk)), ('trades_skipped', int((~al.taken).sum())), ('win_rate_pct', (tk.pnl > 0).mean() * 100),
          ('profit_factor', wins.pnl.sum() / -losses.pnl.sum()), ('avg_win', wins.pnl.mean()), ('avg_loss', losses.pnl.mean()), ('expectancy', tk.pnl.mean()), ('best_trade', tk.pnl.max()),
          ('worst_trade', tk.pnl.min()), ('longest_losing_streak', best), ('positive_months_pct', (mon.return_pct > 0).mean() * 100), ('best_month_pct', mon.return_pct.max()),
          ('worst_month_pct', mon.return_pct.min()), ('months', len(mon)), ('avg_units', tk.units.mean()), ('peak_units', tk.units.max()), ('orders_placed', tk.orders.sum()),
          ('costs_total', tk.cost.sum()), ('charges_total', tk.charges.sum()), ('slippage_total', tk.slippage.sum()), ('brokerage_total', tk.brokerage.sum()), ('gst_total', tk.gst.sum()),
          ('ctt_total', tk.ctt.sum()), ('other_charges_total', tk.other.sum()), ('costs_pct_of_gross_profit', tk.cost.sum() / tk.gross.sum() * 100), ('avg_cost_per_trade', tk.cost.mean())]
    kv = pd.DataFrame(kv, columns=['metric', 'value'])
    kv['value'] = [round(v, 4) if isinstance(v, (int, float, np.floating, np.integer)) else v for v in kv.value]
    os.makedirs(out_dir, exist_ok=True)
    t = tk[['trade_no', 'engine', 'direction', 'entry_ts', 'exit_ts', 'entry', 'units', 'mpu', 'equity_before', 'rs_unit', 'gross', 'orders', 'charges', 'slippage', 'pnl', 'reason']].copy()
    for c in ['entry_ts', 'exit_ts']:
        t[c] = t[c].dt.strftime('%Y-%m-%d %H:%M')
    for c, n in [('mpu', 0), ('equity_before', 0), ('pnl', 1), ('gross', 1), ('charges', 1), ('slippage', 1), ('rs_unit', 1), ('entry', 2)]:
        t[c] = t[c].round(n)
    t.to_csv(os.path.join(out_dir, 'trades.csv'), index=False)
    e = ev[['exit_ts', 'trade_no', 'engine', 'cum', 'dd']].copy()
    e['exit_ts'] = e.exit_ts.dt.strftime('%Y-%m-%d %H:%M')
    e['cum'] = e.cum.round(1)
    e['dd_pct'] = (e.dd * 100).round(3)
    e.drop(columns='dd').to_csv(os.path.join(out_dir, 'equity.csv'), index=False)
    mon.round(2).to_csv(os.path.join(out_dir, 'monthly.csv'), index=False)
    yr.round(2).to_csv(os.path.join(out_dir, 'yearly.csv'), index=False)
    eng.round(2).to_csv(os.path.join(out_dir, 'engines.csv'), index=False)
    kv.to_csv(os.path.join(out_dir, 'metrics.csv'), index=False)
    sk = al[~al.taken][['n', 'engine', 'direction', 'entry_ts', 'entry', 'equity_before', 'share', 'mpu']].copy()
    sk['entry_ts'] = sk.entry_ts.dt.strftime('%Y-%m-%d %H:%M')
    sk.round(0).to_csv(os.path.join(out_dir, 'skipped.csv'), index=False)
    return dict(key=key, end=end, end_equity=endeq, cagr=cagr * 100, dd=mdd * 100, trades=len(tk), skipped=len(sk), engines=eng[['engine', 'trades', 'skipped', 'pnl']].round(0).values.tolist())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--allow-stale', action='store_true', help='run although the local Janus run is behind or missing')
    a = ap.parse_args()
    state, msg = janus_sync.status()
    print(f'Janus run: {state.upper()}: {msg}')
    if state in ('behind', 'missing') and not a.allow_stale:
        sys.exit('stopping: bring the Janus run up to date first (see above), or pass --allow-stale to simulate the Janus scenarios only as far as it goes')
    slip, table = load_slippage()
    janus = load_janus()
    os.makedirs(cfg.OUT_DIR, exist_ok=True)
    table.round(2).to_csv(os.path.join(cfg.OUT_DIR, 'slip_table.csv'))
    json.dump(slip, open(os.path.join(cfg.OUT_DIR, 'slip.json'), 'w'))
    end = None
    for key, sc in cfg.SCENARIOS.items():
        res = summarise(key, trade_book(sc['swap'], janus), slip, sc['janus_mult'], os.path.join(cfg.OUT_DIR, key))
        end = res['end']
        print(f"{key}: account Rs {res['end_equity']:,.0f}, {res['cagr']:.1f}% a year, deepest drop {res['dd']:.1f}%, {res['trades']} trades, {res['skipped']} skipped, to {end:%Y-%m-%d}")
        if a.check:
            print('   ', res['engines'])
    json.dump({'end_date': end.strftime('%Y-%m-%d')}, open(os.path.join(cfg.OUT_DIR, 'meta.json'), 'w'))
    print('slippage per unit per trade (Rs):', {k: round(v, 1) for k, v in slip.items()})


if __name__ == '__main__':
    main()
