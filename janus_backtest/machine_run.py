"""
Run the Janus state machine (janus_machine.py) on one symbol and write its logs as CSV (plans/janus-state-machine.md).

    python janus_backtest/machine_run.py                              # CRUDEOILM, range-setter levels, plain thirds (rebuy, counter, TSL off)
    python janus_backtest/machine_run.py --rebuy --tsl                # toggles
    python janus_backtest/machine_run.py --source previous_session    # the control levels
    python janus_backtest/machine_run.py --from 2026-09-01 --to 2026-10-16
    python janus_backtest/machine_run.py --parity [--last 150]        # machine versus janus_engine.py (A0/A1/A2/A3, touch), leg for leg, both bar orders

Output goes to outputs/machine/<run name>/ and the folder is emptied at the start of every run, so a rule change never leaves stale per-trade files behind:
    trades.csv    one row per trade (levels and range record, entry, stop, targets, exits, P&L, MFE/MAE with minutes, order sensitivity)
    legs.csv      one row per lot leg (own fill, own exit, own MFE/MAE with timestamps)
    events.csv    the ordered event log of every session (first touch of each level, entries, lot exits, stops and stop moves, re-buys)
    sessions.csv  one row per session: the range record and the full level set
    minute/<trade_key>.csv   one row per minute from the entry bar to the exit bar of that trade
    run_info.txt  the settings the run used
The logs are for the bar order janus_configs.MACHINE_BAR_ORDER; the other order is simulated alongside and only marks which trades change.
"""

import argparse
import os
import shutil
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import janus_configs as configs  # noqa: E402
import janus_data as data        # noqa: E402
import janus_engine as engine    # noqa: E402
import janus_machine as machine  # noqa: E402

pd.set_option('display.width', 200)
pd.set_option('display.max_columns', 40)


def run_name(symbol, source, opts) -> str:
    tsl = ('on' if opts.get('tsl_reentry', True) else 'on-noreentry') if opts['tsl'] else 'off'
    if opts.get('stop_scale', configs.STOP_SCALE) != 1.0:
        tsl_tail = f"_stop-{round(opts['stop_scale'] * 100)}"
    else:
        tsl_tail = ''
    rules = opts.get('tsl_rules') or configs.TSL_RULES
    if opts['tsl'] and rules != configs.TSL_RULES:   # the default rules keep the plain name, any other rule set is spelled out so folders never collide
        tsl += f"_t1-{rules.get(1) or 'stay'}_t2-{rules.get(2) or 'stay'}"
    tsl += tsl_tail
    if opts.get('max_entries', configs.MAX_ENTRIES_PER_SETUP):
        tsl += f"_max-{opts['max_entries']}"
    return f"{symbol}_{source}_rebuy-{'on' if opts['rebuy'] else 'off'}_counter-{'on' if opts['counter'] else 'off'}_tsl-{tsl}"


def _other_order(order):
    return next(x for x in configs.BAR_ORDERS if x != order)


def run_symbol(symbol, source, opts, date_from=None, date_to=None, live_tail=None):
    """Simulate every session under the logged bar order and the other one. Returns dict of DataFrames/lists ready to write."""
    order = configs.MACHINE_BAR_ORDER
    other = _other_order(order)
    trades, legs, events, sessions, minute = [], [], [], [], {}
    adverse = {}
    n = 0
    live_tail = configs.MACHINE_LIVE_TAIL if live_tail is None else live_tail
    for s in data.load_sessions(symbol, level_source=source, live_tail=live_tail):
        if (date_from and s['date'] < date_from) or (date_to and s['date'] > date_to):
            continue
        s['level_source'] = source
        main = machine.simulate_session(s, opts, order)
        alt = machine.simulate_session(s, opts, other)
        for t in alt['trades']:
            adverse[(t['date'], t['setup'], t['entry_ts'])] = t
        trades.extend(main['trades'])
        legs.extend(main['legs'])
        events.extend(main['events'])
        minute.update(main['minute_logs'])
        rng = s.get('range') or {}
        sessions.append({'symbol': symbol, 'date': s['date'], 'expiry': s['expiry'], 'level_source': source, 'data_source': s['data_source'], 'prev_date': s['prev']['date'], 'prev_close': s['prev']['close'],
                         'range_setter_date': rng.get('setter_date'), 'range_age': rng.get('age'), 'range_extensions': rng.get('extensions'),
                         'range_high': rng.get('high'), 'range_low': rng.get('low'), 'R_control': s['levels_prev']['R'], 'trades': len(main['trades']),
                         **{k: v for k, v in s['levels'].items()}})
        n += 1
    tdf = pd.DataFrame(trades)
    if len(tdf):
        keys = list(zip(tdf['date'], tdf['setup'], tdf['entry_ts']))
        match = [adverse.get(k) for k in keys]
        tdf['order_sensitive'] = [m is None or m['reasons'] != r or abs(m['pnl_pts'] - p) > 1e-9 for m, r, p in zip(match, tdf['reasons'], tdf['pnl_pts'])]
        tdf['other_order_pnl_pts'] = [m['pnl_pts'] if m else None for m in match]
        main_keys = set(keys)
        tdf.attrs['other_order_only'] = sum(1 for k in adverse if k not in main_keys)
    return {'trades': tdf, 'legs': pd.DataFrame(legs), 'events': pd.DataFrame(events), 'sessions': pd.DataFrame(sessions), 'minute': minute, 'n_sessions': n,
            'order': order, 'other': other}


def write_run(res, symbol, source, opts, out_root=None) -> str:
    out = os.path.join(out_root or configs.MACHINE_OUTPUT_DIR, run_name(symbol, source, opts))
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(os.path.join(out, 'minute'))
    for name in ('trades', 'legs', 'events', 'sessions'):
        res[name].to_csv(os.path.join(out, f'{name}.csv'), index=False)
    for key, rows in res['minute'].items():
        pd.DataFrame(rows).to_csv(os.path.join(out, 'minute', f'{key}.csv'), index=False)
    with open(os.path.join(out, 'run_info.txt'), 'w') as f:
        f.write(f"symbol {symbol}\nlevel_source {source}\nlast_session {res['sessions']['date'].max()} (data sources: {dict(res['sessions']['data_source'].value_counts())})\nopts {opts}\nlogged_bar_order {res['order']} (other order marks order_sensitive: {res['other']})\n"
                f"sessions {res['n_sessions']}\nTSL_RULES {opts.get('tsl_rules') or configs.TSL_RULES}\nENTRY_CUTOFF_MIN {configs.ENTRY_CUTOFF_MIN} HARD_EXIT_MIN {configs.HARD_EXIT_MIN} "
                f"AT_OPEN_MINUTES {configs.AT_OPEN_MINUTES}\n")
    return out


def summary(res) -> str:
    t, g = res['trades'], res['legs']
    if not len(t):
        return 'no trades'
    lines = [f"{res['n_sessions']} sessions, {len(t)} trades, {len(g)} legs; logged order {res['order']}"
             f" ({int(t['order_sensitive'].sum())} trades change under {res['other']}; {t.attrs.get('other_order_only', 0)} trades exist only under {res['other']})",
             f"gross P&L: {g['pnl_pct'].sum():,.1f}% of price per lot summed over legs; {g['pnl_r'].sum():,.1f} R; mean per leg {g['pnl_r'].mean():+.3f} R"]
    by = t.groupby('setup').agg(trades=('pnl_pts', 'size'), pnl_r=('pnl_r', 'sum'), mean_mfe_r=('mfe_r', 'mean'), mean_mae_r=('mae_r', 'mean'),
                                median_minutes=('minutes_held', 'median'))
    lines.append(by.round(2).to_string())
    return '\n'.join(lines)


def parity(symbol, source, last, date_from=None, live_tail=None):
    """The machine with every toggle off/on against janus_engine.simulate_session (touch): same legs (setup, lot, entry/exit bar and price, reason)."""
    sessions = data.load_sessions(symbol, level_source=source, live_tail=configs.MACHINE_LIVE_TAIL if live_tail is None else live_tail)
    if date_from:
        sessions = [s for s in sessions if s['date'] >= date_from]
    sessions = sessions[-last:] if last else sessions
    arms = {'A0': {'rebuy': False, 'counter': False}, 'A1': {'rebuy': True, 'counter': False}, 'A2': {'rebuy': False, 'counter': True},
            'A3': {'rebuy': True, 'counter': True}}
    bad, total, legs_n = 0, 0, 0
    for s in sessions:
        s['level_source'] = source
        for order in configs.BAR_ORDERS:
            for name, arm in arms.items():
                ref = engine.simulate_session(s, arm, order, scn='touch')['legs']
                got = machine.simulate_session(s, {**arm, 'tsl': False}, order)['legs']
                f = lambda legs: sorted((x['setup'], x['trade'], x['lot'], x['entry_bar'], round(x['entry'], 6), x['exit_bar'], round(x['exit'], 6), x['reason']) for x in legs)  # noqa: E731
                total += 1
                legs_n += len(ref)
                if f(ref) != f(got):
                    bad += 1
                    if bad <= 5:
                        print('MISMATCH', s['date'], order, name, len(ref), len(got))
    return f'parity {symbol} {source}: {len(sessions)} sessions x 2 orders x 4 arms = {total} runs, {legs_n} reference legs, {bad} runs differ'


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument('--symbol', default=configs.MACHINE_SYMBOL)
    ap.add_argument('--source', default=configs.MACHINE_LEVEL_SOURCE, choices=configs.LEVEL_SOURCES)
    ap.add_argument('--rebuy', action='store_true', default=configs.MACHINE_REBUY)
    ap.add_argument('--counter', action='store_true', default=configs.MACHINE_COUNTER)
    ap.add_argument('--tsl', action='store_true', default=configs.MACHINE_TSL)
    ap.add_argument('--no-tsl-reentry', action='store_true', help='with --tsl: no new entry on a setup after a trailed stop closed its trade, for the rest of the session')
    ap.add_argument('--tsl-rules', help="with --tsl: where the stop goes after T1 and after T2, e.g. entry,stay or stay,T1 (places: entry, T1, T2, stay)")
    ap.add_argument('--stop-scale', type=float, default=configs.STOP_SCALE, help='tighter stop: share of the original stop distance (1.0 = the Camarilla stop level, 0.9 = 90 percent of the way)')
    ap.add_argument('--max-entries', type=int, default=configs.MAX_ENTRIES_PER_SETUP, help='entries one setup may take per session; 0 = no limit')
    ap.add_argument('--from', dest='date_from')
    ap.add_argument('--to', dest='date_to')
    ap.add_argument('--no-tail', action='store_true', help='Fyers history only (no Angel One tail for unexpired contracts)')
    ap.add_argument('--parity', action='store_true')
    ap.add_argument('--last', type=int, default=0)
    a = ap.parse_args(argv)
    d0 = pd.Timestamp(a.date_from).date() if a.date_from else None
    d1 = pd.Timestamp(a.date_to).date() if a.date_to else None
    if a.parity:
        print(parity(a.symbol, a.source, a.last, d0, live_tail=False if a.no_tail else None))
        return
    opts = {'rebuy': a.rebuy, 'counter': a.counter, 'tsl': a.tsl, 'tsl_reentry': configs.TSL_REENTRY and not a.no_tsl_reentry}
    opts['stop_scale'] = a.stop_scale
    opts['max_entries'] = a.max_entries
    if a.tsl_rules:
        p1, p2 = [None if x.lower() == 'stay' else x for x in a.tsl_rules.split(',')]
        opts['tsl_rules'] = {1: p1, 2: p2}
    res = run_symbol(a.symbol, a.source, opts, d0, d1, live_tail=False if a.no_tail else None)
    out = write_run(res, a.symbol, a.source, opts)
    print(summary(res))
    print('written:', out)


if __name__ == '__main__':
    main(sys.argv[1:])
