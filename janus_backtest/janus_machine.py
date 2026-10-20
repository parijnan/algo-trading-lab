"""
Janus state machine: one session at a time, minute by minute, touch entries (plans/janus-state-machine.md). Same four setups and same event walk as
janus_engine.py (which stays untouched as the control: with re-buy, counter and TSL off the legs equal its A0 touch run leg for leg), plus what the
engine does not keep: a stop that belongs to the TRADE (so it can be trailed), the excursions (MFE, MAE) of every lot and trade with the minute each extreme
was reached, a per-minute log for every trade, and an ordered event log of the day (first touch of each level, entries, targets, stops, moves).

Rules (all parameters in janus_configs): three equal lots per trade, lot k exits at target k, one stop for all open lots, no entry in the first minute,
no entry/re-buy/counter in the last ENTRY_CUTOFF_MIN minutes, everything open exits HARD_EXIT_MIN minutes before the last bar. A fade enters as price moves
back through S3 (long) or R3 (short), a breakout as price moves through R4 (long) or S4 (short); the stop of one setup is the entry of another, so a stop
followed by the opposite entry at the same price (stop-and-reverse) comes from the same event loop. Toggles (off by default): `rebuy`, `counter`, `tsl`.

Excursions are measured from the fill. A lot's window is its fill to its exit; a trade's window is its first fill to its last exit. Inside a bar only
the part of the assumed price path after the fill (and before the exit) counts; a gap between bars counts for a position that is open across it. The
minute of an extreme is the bar in which it was first reached. The order of high and low inside a bar is unknown, so every bar is walked along an
assumed path (neutral or adverse, see janus_engine._bar_path); the runner reports which trades change when the other order is assumed.
"""

import numpy as np
import pandas as pd

import janus_configs as configs
from janus_engine import _bar_path, build_setups

_STOP, _TARGET, _REBUY, _ENTRY = 0, 1, 2, 3          # priority at the same price
_NAMES = {1: 'T1', 2: 'T2', 3: 'T3'}


def _ts(sim, bar):
    return pd.Timestamp(sim['ts'][bar])


def _ev(sim, kind, key=None, tr=None, level=None, price=None, lot=None, detail=''):
    row = {'symbol': sim['symbol'], 'date': sim['date'], 'ts': _ts(sim, sim['i']), 'bar': sim['i'], 'event': kind, 'setup': key,
           'trade_key': tr['key'] if tr else None, 'level': level, 'price': price, 'lot': lot, 'detail': detail}
    sim['events'].append(row)
    if tr is not None:
        sim['bar_events'].setdefault(tr['id'], []).append(f'{kind}' + (f' {level}' if level else '') + (f' lot{lot}' if lot else '') + (f' @{price:.2f}' if price is not None else ''))


def _fresh_extremes(obj, price, bar):
    obj.update({'pos': price, 'hi': price, 'hi_bar': bar, 'lo': price, 'lo_bar': bar})


def _adv(obj, price, bar):
    """Move the tracked position of a lot or trade to `price`, widening its high/low (and the bar they were first reached in)."""
    a, b = (obj['pos'], price) if obj['pos'] <= price else (price, obj['pos'])
    if b > obj['hi']:
        obj['hi'], obj['hi_bar'] = b, bar
    if a < obj['lo']:
        obj['lo'], obj['lo_bar'] = a, bar
    obj['pos'] = price


def _advance(sim, price):
    for tr in sim['state'].values():
        if tr is not None:
            _adv(tr, price, sim['i'])
            for x in tr['lots']:
                if x['state'] == 'open':
                    _adv(x, price, sim['i'])


def _excursion(obj, side, entry):
    """(mfe, mae, mfe_bar, mae_bar): favourable and adverse excursion in points (both >= 0) from `entry`, and the bars they were first reached."""
    if side > 0:
        return obj['hi'] - entry, entry - obj['lo'], obj['hi_bar'], obj['lo_bar']
    return entry - obj['lo'], obj['hi'] - entry, obj['lo_bar'], obj['hi_bar']


def _record(sim, tr, j, price, i, reason):
    lot = tr['lots'][j]
    s = sim['setups'][tr['setup']]
    pts = s['side'] * (price - lot['entry'])
    mfe, mae, mfe_bar, mae_bar = _excursion(lot, s['side'], lot['entry'])
    leg = {'symbol': sim['symbol'], 'date': sim['date'], 'setup': tr['setup'], 'family': s['family'], 'side': s['side'], 'trade': tr['id'],
           'trade_key': tr['key'], 'kind': tr['kind'], 'lot': j + 1, 'rebuy': lot['rebuy'], 'cycle': lot['cycle'], 'entry': lot['entry'],
           'entry_bar': lot['bar'], 'entry_ts': _ts(sim, lot['bar']), 'exit': price, 'exit_bar': i, 'exit_ts': _ts(sim, i), 'reason': reason,
           'risk': s['risk'], 'pnl_pts': pts, 'pnl_r': pts / s['risk'], 'pnl_pct': pts / lot['entry'] * 100,
           'mfe_pts': mfe, 'mfe_ts': _ts(sim, mfe_bar), 'mae_pts': mae, 'mae_ts': _ts(sim, mae_bar)}
    sim['legs'].append(leg)
    tr['legs'].append(leg)
    tr['realized'] += pts
    _ev(sim, 'lot_exit', tr['setup'], tr, price=price, lot=j + 1, detail=f'{reason} pnl_pts={pts:.2f}')


def _end_trade(sim, tr):
    key = tr['setup']
    sim['state'][key] = None
    tr['closed'], tr['exit_bar'] = True, sim['i']
    _summarise(sim, tr)


def _summarise(sim, tr):
    s = sim['setups'][tr['setup']]
    legs = tr['legs']
    mfe, mae, mfe_bar, mae_bar = _excursion(tr, s['side'], tr['entry'])
    sess, lv = sim['session'], sim['session']['levels']
    rng = sess.get('range') or {}
    control = sess.get('levels_prev') or lv
    last = max(legs, key=lambda x: x['exit_bar'])
    row = {'symbol': sim['symbol'], 'date': sim['date'], 'trade_key': tr['key'], 'setup': tr['setup'], 'family': s['family'], 'side': s['side'], 'kind': tr['kind'],
           'entry_ts': _ts(sim, tr['entry_bar']), 'entry': tr['entry'], 'stop_initial': tr['stop_initial'], 'stop_final': tr['stop'], 'stop_moves': len(tr['stop_moves']),
           'risk_pts': s['risk'], 'risk_pct_of_price': s['risk'] / tr['entry'] * 100,
           'T1': s['targets'][0], 'T2': s['targets'][1], 'T3': s['targets'][2], 'legs': len(legs),
           'rebuy_cycles': tr['cycles'], 'exit_ts': last['exit_ts'], 'minutes_held': (last['exit_bar'] - tr['entry_bar']),
           'reasons': ','.join(x['reason'] for x in sorted(legs, key=lambda x: (x['exit_bar'], x['lot']))),
           'pnl_pts': sum(x['pnl_pts'] for x in legs), 'pnl_r': sum(x['pnl_r'] for x in legs), 'pnl_pct': sum(x['pnl_pct'] for x in legs),
           'mfe_pts': mfe, 'mfe_ts': _ts(sim, mfe_bar), 'mfe_r': mfe / s['risk'], 'mae_pts': mae, 'mae_ts': _ts(sim, mae_bar), 'mae_r': mae / s['risk'],
           'minutes_to_mfe': mfe_bar - tr['entry_bar'], 'minutes_to_mae': mae_bar - tr['entry_bar'],
           'level_source': sim['level_source'], 'data_source': sess.get('data_source', 'fyers'), 'prev_close': lv['C'], 'range_high': rng.get('high'), 'range_low': rng.get('low'),
           'range_setter_date': rng.get('setter_date'), 'range_age': rng.get('age'), 'range_extensions': rng.get('extensions'),
           'R_levels': lv['R'], 'R_control': control['R'], 'R_ratio': lv['R'] / control['R'] if control['R'] else None,
           'bar_order': sim['order'], 'rebuy_on': sim['opts']['rebuy'], 'counter_on': sim['opts']['counter'], 'tsl_on': sim['opts']['tsl'], 'tsl_reentry': sim['opts']['tsl_reentry']}
    sim['trade_rows'].append(row)


def _unique_key(sim, key, bar):
    base = f"{sim['date']:%Y-%m-%d}_{key}_{_ts(sim, bar):%H%M}"
    k, n = base, 1
    while k in sim['keys']:
        n += 1
        k = f'{base}_{n}'
    sim['keys'].add(k)
    return k


def _open_trade(sim, key, price, i, kind) -> bool:
    s = sim['setups'][key]
    if (price - s['targets'][0]) * s['side'] >= 0:          # filled at or past T1: nothing left to trade
        return False
    tr = {'id': sim['n_trades'], 'setup': key, 'kind': kind, 'cycles': 0, 'entry': price, 'entry_bar': i, 'stop': s['stop'], 'stop_initial': s['stop'],
          'stop_moves': [], 'realized': 0.0, 'legs': [], 'closed': False, 'rows': [], 'key': _unique_key(sim, key, i),
          'lots': [{'state': 'open', 'entry': price, 'bar': i, 'rebuy': False, 'cycle': 0} for _ in range(3)]}
    _fresh_extremes(tr, price, i)
    for x in tr['lots']:
        _fresh_extremes(x, price, i)
    sim['state'][key] = tr
    sim['trades'].append(tr)
    sim['touched'][tr['id']] = tr
    sim['n_trades'] += 1
    _ev(sim, 'entry' if kind == 'entry' else 'counter_entry', key, tr, price=price, detail=f"stop={tr['stop']:.2f}")
    return True


def _candidates(sim, d):
    """Every trigger that price moving in direction d could fire now: (trigger price, priority, key, idx, fill price)."""
    out = []
    through = configs.TICK_THROUGH * sim['tick']
    for key, tr in sim['state'].items():
        s = sim['setups'][key]
        side = s['side']
        if tr is not None:
            lots = tr['lots']
            if d == -side and any(x['state'] == 'open' for x in lots):
                out.append((tr['stop'], _STOP, key, None, tr['stop']))
            if d == side:
                for j, x in enumerate(lots):
                    if x['state'] == 'open':
                        out.append((s['targets'][j] + side * through, _TARGET, key, j, s['targets'][j]))
            if d == -side and sim['opts']['rebuy'] and sim['entries_open'] and any(x['state'] == 'booked' for x in lots):
                out.append((s['entry'] - side * through, _REBUY, key, None, s['entry']))
        elif d == side and sim['entries_open'] and key not in sim['blocked']:
            out.append((s['entry'], _ENTRY, key, None, s['entry']))
    return out


def _move_stop(sim, tr, k):
    """TSL: after target k the stop moves to the place the rules name (opts['tsl_rules'], default configs.TSL_RULES), never backwards."""
    place = sim['opts']['tsl_rules'].get(k)
    s = sim['setups'][tr['setup']]
    if place is None:
        return
    new = tr['entry'] if place == 'entry' else s['targets'][int(place[1:]) - 1]
    if (new - tr['stop']) * s['side'] > 0:
        tr['stop_moves'].append((sim['i'], tr['stop'], new))
        _ev(sim, 'stop_moved', tr['setup'], tr, price=new, detail=f"after T{k}: {tr['stop']:.2f} -> {new:.2f} ({place})")
        tr['stop'] = new


def _walk_segment(sim, a, b, jump, i):
    if a == b:
        return
    if jump:
        _advance(sim, b)                                     # a position open across a gap has been through it
    d = 1 if b > a else -1
    cur, just, skipped, events = a, None, set(), 0
    while True:
        best = None
        for t, pr, key, idx, fill in _candidates(sim, d):
            dist = (t - cur) * d
            if (dist > 0 or (dist == 0 and just == t)) and (b - t) * d >= 0 and (key, pr, idx) not in skipped:
                if best is None or (dist, pr) < (best[0], best[1]):
                    best = (dist, pr, t, key, idx, fill)
        if best is None:
            break
        events += 1
        if events > configs.MAX_EVENTS_PER_BAR:
            raise RuntimeError(f'event loop exceeded {configs.MAX_EVENTS_PER_BAR} events in one bar ({sim["symbol"]} {sim["date"]} bar {i})')
        _, pr, t, key, idx, fill = best
        if not jump:
            _advance(sim, t)
        cur, just = t, t
        s, tr = sim['setups'][key], sim['state'][key]
        if pr == _STOP:
            px = b if jump else fill                          # a stop crossed in the jump fills at the open
            _ev(sim, 'stop', key, tr, price=px, detail='gap' if jump else '')
            for j, x in enumerate(tr['lots']):
                if x['state'] == 'open':
                    _record(sim, tr, j, px, i, 'stop' if not tr['stop_moves'] else 'stop_trailed')
                    x['state'] = 'closed'
            if tr['stop_moves'] and not sim['opts']['tsl_reentry']:
                sim['blocked'].add(key)                       # a trailed stop closed this setup's trade: no new entry on it this session
            _end_trade(sim, tr)
        elif pr == _TARGET:
            _ev(sim, 'target', key, tr, level=_NAMES[idx + 1], price=fill, lot=idx + 1)
            _record(sim, tr, idx, fill, i, _NAMES[idx + 1])
            tr['lots'][idx]['state'] = 'booked'
            if idx == 2:
                for j, x in enumerate(tr['lots']):
                    if x['state'] == 'open':
                        _record(sim, tr, j, fill, i, 'T3_flat')
                        x['state'] = 'closed'
                _end_trade(sim, tr)
                ck = s['counter']
                if sim['opts']['counter'] and ck and sim['state'][ck] is None and sim['entries_open'] and ck not in sim['blocked']:
                    _open_trade(sim, ck, fill, i, 'counter')
            elif sim['opts']['tsl']:
                _move_stop(sim, tr, idx + 1)
        elif pr == _REBUY:
            tr['cycles'] += 1
            for x in tr['lots']:
                if x['state'] == 'booked':
                    x.update({'state': 'open', 'entry': fill, 'bar': i, 'rebuy': True, 'cycle': tr['cycles']})
                    _fresh_extremes(x, fill, i)
            _ev(sim, 'rebuy', key, tr, price=fill, detail=f"{sum(1 for x in tr['lots'] if x['state'] == 'open')} lots open")
        else:
            px = b if jump else fill                          # a touch entry crossed in the jump fills at the open
            if not _open_trade(sim, key, px, i, 'entry'):
                skipped.add((key, pr, idx))
    if not jump:
        _advance(sim, b)


def _close_all(sim, price, i, reason):
    _advance(sim, price)
    for key, tr in list(sim['state'].items()):
        if tr is not None:
            _ev(sim, reason, key, tr, price=price)
            for j, x in enumerate(tr['lots']):
                if x['state'] == 'open':
                    _record(sim, tr, j, price, i, reason)
                    x['state'] = 'closed'
            _end_trade(sim, tr)


def _level_touches(sim, pts):
    """Log the first touch of each tracked level in the order the assumed path reaches them."""
    for a, b in zip(pts[:-1], pts[1:]):
        lo, hi = (a, b) if a <= b else (b, a)
        hit = [(abs(v - a), name, v) for name, v in sim['level_values'] if name not in sim['touched_levels'] and lo <= v <= hi]
        for _, name, v in sorted(hit):
            sim['touched_levels'].add(name)
            sim['events'].append({'symbol': sim['symbol'], 'date': sim['date'], 'ts': _ts(sim, sim['i']), 'bar': sim['i'], 'event': 'level_touch', 'setup': None,
                                  'trade_key': None, 'level': name, 'price': v, 'lot': None, 'detail': ''})


def _write_rows(sim, i, o, h, l, c, v):
    for tid, tr in sim['touched'].items():
        s = sim['setups'][tr['setup']]
        side = s['side']
        open_lots = [x for x in tr['lots'] if x['state'] == 'open']
        booked = sum(1 for x in tr['lots'] if x['state'] == 'booked')
        closed = sum(1 for x in tr['lots'] if x['state'] == 'closed')
        unreal = sum(side * (c - x['entry']) for x in open_lots)
        mfe, mae, mfe_bar, mae_bar = _excursion(tr, side, tr['entry'])
        nxt = next((s['targets'][j] for j, x in enumerate(tr['lots']) if x['state'] == 'open'), None)
        tr['rows'].append({
            'ts': _ts(sim, i), 'bar': i, 'minute_in_trade': i - tr['entry_bar'], 'open': o, 'high': h, 'low': l, 'close': c, 'volume': v,
            'lots_open': len(open_lots), 'lots_booked': booked, 'lots_closed': closed, 'stop': tr['stop'],
            'next_target': nxt, 'dist_to_next_target': side * (nxt - c) if nxt is not None and not tr['closed'] else None,
            'dist_to_stop': side * (c - tr['stop']) if not tr['closed'] else None,
            'realized_pts': tr['realized'], 'unrealized_pts': unreal, 'total_pts': tr['realized'] + unreal,
            'total_r': (tr['realized'] + unreal) / s['risk'],
            'mfe_pts': mfe, 'mfe_ts': _ts(sim, mfe_bar), 'mae_pts': mae, 'mae_ts': _ts(sim, mae_bar),
            'events': '; '.join(sim['bar_events'].get(tid, [])), 'trade_closed': tr['closed']})


def simulate_session(session: dict, opts: dict = None, order: str = None) -> dict:
    """Run one session. `opts` = {'rebuy', 'counter', 'tsl'} booleans (defaults: the MACHINE_* settings in janus_configs); `order` a bar order from
    configs.BAR_ORDERS. Returns {'legs': [...], 'trades': [summary rows], 'minute_logs': {trade_key: [rows]}, 'events': [...]}."""
    opts = {'rebuy': configs.MACHINE_REBUY, 'counter': configs.MACHINE_COUNTER, 'tsl': configs.MACHINE_TSL, 'tsl_reentry': configs.TSL_REENTRY, 'tsl_rules': configs.TSL_RULES, **(opts or {})}
    order = order or configs.MACHINE_BAR_ORDER
    b = session['bars']
    o, h, l, c = (b[k].tolist() for k in ('open', 'high', 'low', 'close'))
    vol = b['volume'].tolist() if 'volume' in b else [None] * len(o)
    ts = b.index.values
    setups = build_setups(session['levels'])
    lv = session['levels']
    sim = {'setups': setups, 'state': {k: None for k in setups}, 'legs': [], 'trade_rows': [], 'trades': [], 'events': [], 'n_trades': 0, 'keys': set(),
           'opts': opts, 'order': order, 'symbol': session['symbol'], 'date': session['date'], 'session': session, 'ts': ts, 'i': 0,
           'entries_open': False, 'blocked': set(), 'tick': configs.TICK_SIZE[session['symbol']], 'touched': {}, 'bar_events': {}, 'touched_levels': set(),
           'level_values': [(n, lv[n]) for n in configs.MACHINE_LEVELS], 'level_source': session.get('level_source', configs.LEVEL_SOURCE)}
    cut_ts = ts[-1] - np.timedelta64(configs.ENTRY_CUTOFF_MIN, 'm')
    hard_ts = ts[-1] - np.timedelta64(configs.HARD_EXIT_MIN, 'm')
    prev = o[0]
    for i in range(len(o)):
        sim['i'] = i
        sim['touched'] = {tr['id']: tr for tr in sim['state'].values() if tr is not None}
        sim['bar_events'] = {}
        if ts[i] >= hard_ts:
            _close_all(sim, o[i], i, 'hard_exit')
            _write_rows(sim, i, o[i], h[i], l[i], c[i], vol[i])
            break
        sim['entries_open'] = i >= configs.AT_OPEN_MINUTES and ts[i] < cut_ts
        net = sum(setups[k]['side'] * sum(1 for x in tr['lots'] if x['state'] == 'open') for k, tr in sim['state'].items() if tr is not None)
        pts = _bar_path(o[i], h[i], l[i], c[i], order, (net > 0) - (net < 0))
        if prev != o[i]:
            pts = [prev] + pts
        _level_touches(sim, pts)
        live = any(tr is not None for tr in sim['state'].values())
        lo, hi = min(prev, l[i]), max(prev, h[i])
        if live or (sim['entries_open'] and any(lo <= s['entry'] <= hi for s in setups.values())):
            for si in range(len(pts) - 1):
                _walk_segment(sim, pts[si], pts[si + 1], jump=(prev != o[i] and si == 0), i=i)
        prev = c[i]
        if i == len(o) - 1:
            _close_all(sim, c[i], i, 'session_end')
        _write_rows(sim, i, o[i], h[i], l[i], c[i], vol[i])
    return {'legs': sim['legs'], 'trades': sim['trade_rows'], 'minute_logs': {tr['key']: tr['rows'] for tr in sim['trades']}, 'events': sim['events']}
