"""
P&L of equal thirds with the original stop, optional re-buy of booked lots and optional counter trade at a fade's T3 (plan section 9a, items 3
and 8), on the four instruments. Four arms, one change at a time from A0, each run under both bar-order assumptions (neutral, adverse), with
no cap on entries. Gross points first, then costs per leg. Everything is provisional where the plan still has open items (cut-off, hard exit,
order types), see janus_configs.

    python janus_backtest/thirds_pnl.py CRUDEOILM       # simulate one symbol (run the four in parallel), writes the legs and counts CSVs
    python janus_backtest/thirds_pnl.py --report        # aggregate the four symbols into outputs/thirds_pnl_summary.txt
    python janus_backtest/thirds_pnl.py --parity        # first-entry, A0, adverse versus thirds_feasibility's T1-or-stop outcome

Writes outputs/thirds_pnl_summary.txt (and outputs/thirds_legs_<SYMBOL>.csv and thirds_counts_<SYMBOL>.csv, gitignored).
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import janus_configs as configs  # noqa: E402
import janus_data as data        # noqa: E402
import janus_engine as engine    # noqa: E402

pd.set_option('display.width', 220)
pd.set_option('display.max_columns', 40)


def run_symbol(symbol: str):
    """(legs DataFrame, per-session counts DataFrame) for every scenario and arm in configs.RUN_PLAN and both bar orders, loading the sessions once.
    Arm 'A0x' is A0 with one tick required through each limit level of a target or re-buy (the touch-fill optimism check)."""
    legs, counts = [], []
    for s in data.load_sessions(symbol):
        for order in configs.BAR_ORDERS:
            for scn, arm_names in configs.RUN_PLAN.items():
                for name in arm_names:
                    arm = configs.ARMS[name.rstrip('x')]
                    old = configs.TICK_THROUGH
                    configs.TICK_THROUGH = 1 if name.endswith('x') else 0
                    try:
                        res = engine.simulate_session(s, arm, order, scn=scn)
                    finally:
                        configs.TICK_THROUGH = old
                    for leg in res['legs']:
                        leg['arm'], leg['order'], leg['scn'] = name, order, scn
                    legs.extend(res['legs'])
                    counts.append({'symbol': symbol, 'date': s['date'], 'scn': scn, 'arm': name, 'order': order, 'trades': res['trades'],
                                   'signals': res['signals'], 'cancels': res['cancels'], **res['setup_entries']})
    return pd.DataFrame(legs), pd.DataFrame(counts)


def _maxdd(daily: pd.Series) -> float:
    cum = daily.sort_index().cumsum()
    return float((cum.cummax() - cum).max()) if len(cum) else 0.0


def headline(legs: pd.DataFrame, by: list) -> pd.DataFrame:
    """Per group: legs, trades, gross percent (sum over legs of percent of price per lot), break-even cost per leg, and net totals, max drawdown and
    return-to-drawdown at each cost in COSTS_PCT."""
    out = []
    for key, g in legs.groupby(by):
        n = len(g)
        row = {**dict(zip(by, key if isinstance(key, tuple) else (key,))), 'legs': n, 'trades': g.groupby(['symbol', 'date', 'trade']).ngroups,
               'gross_%': g['pnl_pct'].sum(), 'per_leg_R': g['pnl_r'].mean(), 'breakeven_cost_%': g['pnl_pct'].mean()}
        for c in configs.COSTS_PCT[1:]:
            net = g['pnl_pct'] - c
            daily = net.groupby([g['symbol'], g['date']]).sum().groupby(level='date').sum()
            row[f'net@{c}_%'] = net.sum()
            row[f'dd@{c}'] = _maxdd(daily)
        out.append(row)
    return pd.DataFrame(out).round(2)


def by_year(legs: pd.DataFrame, scn_arms, cost: float) -> pd.DataFrame:
    g = legs[[(a, b) in scn_arms for a, b in zip(legs['scn'], legs['arm'])]].copy()
    g['year'] = pd.to_datetime(g['date']).dt.year
    g['net'] = g['pnl_pct'] - cost
    return g.pivot_table(index='year', columns=['order', 'scn', 'arm'], values='net', aggfunc='sum').round(1)


def fill_table(counts: pd.DataFrame) -> pd.DataFrame:
    """Signals (the second close beyond the level), orders cancelled because T1 came first, and fills, per scenario (arm A0, so no counter trades)."""
    c = counts[(counts['arm'] == 'A0') & (counts['order'] == 'neutral')]
    g = c.groupby('scn')
    t = pd.DataFrame({'sessions': g.size(), 'signals': g['signals'].sum(), 'cancelled_at_T1': g['cancels'].sum(),
                      'entries': g[list(configs.ABS_SETUPS)].sum().sum(axis=1)})
    t['filled_%_of_signals'] = t['entries'] / t['signals'] * 100
    return t.round(1)


def report(legs: pd.DataFrame, counts: pd.DataFrame) -> str:
    L = ['Thirds with the original stop; entries by the close-armed watcher with a limit at the level (scenarios c1/c5/c15 = 1/5/15-minute closes, realistic retest fill; '
         'l1/l5/l15 = LITERAL fill at the level on the signal close, optimistic reference), touch = the earlier touch design. Re-buy and counter trade as options; no cap on entries.',
         f'PROVISIONAL: entry cut-off {configs.ENTRY_CUTOFF_MIN} min and hard exit {configs.HARD_EXIT_MIN} min before the last bar, no entry in the first minute, '
         'touch fills for targets/re-buys/counter entry (A0x: one tick through), stops fill at the level or at the open if it jumped past. '
         'pnl in percent of price per lot, summed over legs; cost columns are per leg (entry plus exit of one lot).', '',
         'Signals and fills (A0, neutral order)', fill_table(counts).to_string(), '']
    for order in configs.BAR_ORDERS:
        o = legs[legs['order'] == order]
        L += [f'=== bar order: {order} ===', 'Pooled over instruments', headline(o, ['scn', 'arm']).to_string(), '',
              'A0 per instrument', headline(o[o['arm'] == 'A0'], ['scn', 'symbol']).to_string(), '',
              'A0 per family', headline(o[o['arm'] == 'A0'], ['scn', 'family']).to_string(), '']
    pairs = [(s, 'A0') for s in configs.RUN_PLAN]
    L += ['Net by year at 0.02% per leg, A0 (pooled over instruments)', by_year(legs, pairs, 0.02).to_string(), '']
    return '\n'.join(L)


def parity(symbols):
    """First entry per setup per session, A0, adverse order: engine's lot-1 outcome against thirds_feasibility's `a` (T1 / stop / open)."""
    configs.ENTRY_CUTOFF_MIN, configs.HARD_EXIT_MIN = 0, 0          # the feasibility pass had no time rules; switch them off here so the comparison is like for like
    cfg_map = {('fade', 'long'): 'F_L', ('fade', 'short'): 'F_S', ('breakout', 'long'): 'B_L', ('breakout', 'short'): 'B_S'}
    rows = []
    for sym in symbols:
        fe = pd.read_csv(os.path.join(configs.OUTPUT_DIR, f'thirds_trades_{sym}.csv'), parse_dates=['date'])
        fe['setup'] = [cfg_map[(f, s)] for f, s in zip(fe['family'], fe['side'])]
        fe_map = {(d.date(), st): (a, eb, ebs, g) for d, st, a, eb, ebs, g in zip(fe['date'], fe['setup'], fe['a'], fe['entry_bar'], fe['entry_bar_stop'], fe['gap_in_risk'])}
        eng = {}
        for s in data.load_sessions(sym):
            res = engine.simulate_session(s, configs.ARMS['A0'], 'adverse', entry_limit=1)
            for leg in res['legs']:
                if leg['lot'] == 1:
                    eng[(s['date'], leg['setup'])] = ({'T1': 'T1', 'stop': 'stop'}.get(leg['reason'], 'open'), leg['entry_bar'])
        for k in set(fe_map) | set(eng):
            f, e = fe_map.get(k, ('none', -1, False, 0.0)), eng.get(k, ('none', -1))
            rows.append({'symbol': sym, 'feas': f[0], 'engine': e[0], 'f_bar': f[1], 'f_ebs': f[2], 'f_gap': f[3], 'e_bar': e[1]})
    df = pd.DataFrame(rows)
    both = df[(df['feas'] != 'none') & (df['engine'] != 'none')]
    mis = both[both['feas'] != both['engine']]
    only = df[(df['feas'] == 'none') ^ (df['engine'] == 'none')]
    txt = [f'Parity (first entry per setup per session, A0, adverse order): {len(df)} setup-sessions, {len(both)} with an entry in both, '
           f'{(both["feas"] == both["engine"]).mean() * 100:.1f}% same lot-1 outcome among those.',
           pd.crosstab(df['feas'], df['engine'], margins=True).to_string(),
           f'Both entered, different outcome: {len(mis)}; of these the entry bars differ in {(mis["f_bar"] != mis["e_bar"]).sum()}, feasibility entry bar is the first tradable minute (bar 1) in {(mis["f_bar"] == 1).sum()}, feasibility stopped on the entry bar in {mis["f_ebs"].sum()}.',
           f'Residual after removing those two causes: {len(mis[(mis["f_bar"] != 1) & (~mis["f_ebs"])])} different outcomes.',
           f'Entry in only one of the two: {len(only)}; of these the feasibility entry is bar 1 in {(only["f_bar"] == 1).sum()}, the engine entry is bar 1 in {(only["e_bar"] == 1).sum()}; other: {len(only[(only["f_bar"] != 1) & (only["e_bar"] != 1)])}.']
    return '\n'.join(txt)


def main(args):
    os.makedirs(configs.OUTPUT_DIR, exist_ok=True)
    if '--parity' in args:
        out = parity(configs.SYMBOLS)
        with open(os.path.join(configs.OUTPUT_DIR, 'thirds_parity.txt'), 'w') as f:
            f.write(out + '\n')
        print(out)
        return
    if '--report' in args:
        legs = pd.concat([pd.read_csv(os.path.join(configs.OUTPUT_DIR, f'thirds_legs_{sym}.csv')) for sym in configs.SYMBOLS], ignore_index=True)
        counts = pd.concat([pd.read_csv(os.path.join(configs.OUTPUT_DIR, f'thirds_counts_{sym}.csv')) for sym in configs.SYMBOLS], ignore_index=True)
        out = report(legs, counts)
        with open(os.path.join(configs.OUTPUT_DIR, 'thirds_pnl_summary.txt'), 'w') as f:
            f.write(out + '\n')
        print(out)
        return
    for sym in args or configs.SYMBOLS:             # one process per symbol can run in parallel; then run with --report
        legs, counts = run_symbol(sym)
        legs.to_csv(os.path.join(configs.OUTPUT_DIR, f'thirds_legs_{sym}.csv'), index=False)
        counts.to_csv(os.path.join(configs.OUTPUT_DIR, f'thirds_counts_{sym}.csv'), index=False)
        print(sym, len(legs), 'legs')


if __name__ == '__main__':
    main(sys.argv[1:])
