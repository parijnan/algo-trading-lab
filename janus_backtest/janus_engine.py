"""
Day simulation for the Janus fade and breakout setups on 1-minute bars (plan section 9a; items 3, 6, 8, 11 as decided 2026-10-11).

Four setups run side by side on one session with no cap on entries: fade long (reclaim of S3), fade short (reject of R3), breakout long (break of
R4), breakout short (break of S4). Each trade is three lots with one stop: lot 1 exits at T1, lot 2 at T2, lot 3 at T3. There is no trail. With
the 'rebuy' option, booked lots are bought back at the original entry level when price returns to it, for the same targets. With the 'counter'
option, a fade's T3 opens its sister setup at that level. Because the stop of one setup is the entry of another (R3 and S3 for the breakouts and
fades, R4 and S4 the other way), a stop followed by the opposite entry at the same price (stop-and-reverse) falls out of the same event loop.

Price path. Inside a bar the order of high and low is unknown, so each bar is walked along an assumed path (janus_configs.BAR_ORDERS); the jump
from the previous close to the open is a segment of its own, and a stop or an entry crossed in the jump fills at the open, while limit-type
fills (targets, re-buys, the counter entry) fill at the level. Events inside a segment are processed in price order, stops first, then targets,
then re-buys, then entries; an event may trigger another at the same price (the cascade), and a hard cap on events per bar raises instead of stopping.

Time rules (provisional, plan item 9): no entry in the first minute; no entry, re-buy or counter trade after the entry cut-off; everything open
exits at the hard exit. Costs are not applied here; leg records carry gross points and percent.
"""

import numpy as np

import janus_configs as configs

_STOP, _TARGET, _REBUY, _ENTRY = 0, 1, 2, 3          # priority at the same price
_KIND = {_STOP: 'stop', _TARGET: 'target', _REBUY: 'rebuy', _ENTRY: 'entry'}


def build_setups(lv: dict) -> dict:
    out = {}
    for key, spec in configs.ABS_SETUPS.items():
        e, x = lv[spec['entry']], lv[spec['stop']]
        out[key] = {'key': key, 'side': spec['side'], 'family': spec['family'], 'counter': spec['counter'], 'entry': e, 'stop': x,
                    'targets': [lv[t] for t in spec['targets']], 'risk': abs(e - x)}
    return out


def _bar_path(o, h, l, c, order: str, net_side: int):
    low_first = (c >= o) if (order == 'neutral' or net_side == 0) else (net_side > 0)
    return [o, l, h, c] if low_first else [o, h, l, c]


def _record(sim, trade, j, price, i, reason):
    lot = trade['lots'][j]
    s = sim['setups'][trade['setup']]
    pts = s['side'] * (price - lot['entry'])
    sim['rec'].append({'symbol': sim['symbol'], 'date': sim['date'], 'setup': trade['setup'], 'family': s['family'], 'side': s['side'],
                       'trade': trade['id'], 'kind': trade['kind'], 'lot': j + 1, 'rebuy': lot['rebuy'], 'cycle': lot['cycle'],
                       'entry': lot['entry'], 'entry_bar': lot['bar'], 'exit': price, 'exit_bar': i, 'reason': reason, 'risk': s['risk'],
                       'pnl_pts': pts, 'pnl_r': pts / s['risk'], 'pnl_pct': pts / lot['entry'] * 100})


def _end_trade(sim, key):
    sim['state'][key] = None
    sim['watch'][key] = 'IDLE'


def _entry_allowed(sim, key) -> bool:
    lim = sim['limit']
    return sim['entries_open'] and (lim is None or sim['setup_entries'][key] < lim)


def _open_trade(sim, key, price, i, kind) -> bool:
    s = sim['setups'][key]
    if (price - s['targets'][0]) * s['side'] >= 0:          # filled at or past T1: nothing left to trade
        return False
    sim['state'][key] = {'id': sim['n_trades'], 'setup': key, 'kind': kind, 'cycles': 0,
                         'lots': [{'state': 'open', 'entry': price, 'bar': i, 'rebuy': False, 'cycle': 0} for _ in range(3)]}
    sim['n_trades'] += 1
    sim['setup_entries'][key] += 1
    sim['watch'][key] = 'IDLE'
    return True


def _candidates(sim, d):
    """Every trigger that price moving in direction d could fire now: (trigger price, priority, key, idx, fill price, kind)."""
    out = []
    through = configs.TICK_THROUGH * sim['tick']
    for key, tr in sim['state'].items():
        s = sim['setups'][key]
        side = s['side']
        if tr is not None:
            lots = tr['lots']
            if d == -side and any(x['state'] == 'open' for x in lots):
                out.append((s['stop'], _STOP, key, None, s['stop']))
            if d == side:
                for j, x in enumerate(lots):
                    if x['state'] == 'open':
                        out.append((s['targets'][j] + side * through, _TARGET, key, j, s['targets'][j]))
            if (d == -side and sim['arm']['rebuy'] and s['family'] in configs.REENTRY_FAMILIES and sim['entries_open']
                    and any(x['state'] == 'booked' for x in lots)):
                out.append((s['entry'] - side * through, _REBUY, key, None, s['entry']))
        elif sim['close_mode']:
            if sim['watch'][key] == 'PENDING' and _entry_allowed(sim, key):
                if d == -side:                                              # price comes back down (up) to the limit
                    out.append((s['entry'], _ENTRY, key, None, s['entry']))
                if d == side and configs.CANCEL_AT_T1:                      # the move went without us
                    out.append((s['targets'][0], _STOP, key, 'cancel', s['targets'][0]))
        elif d == side and _entry_allowed(sim, key):
            out.append((s['entry'], _ENTRY, key, None, s['entry']))
    return out


def _walk_segment(sim, a, b, jump, i):
    if a == b:
        return
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
            return
        events += 1
        if events > configs.MAX_EVENTS_PER_BAR:
            raise RuntimeError(f'event loop exceeded {configs.MAX_EVENTS_PER_BAR} events in one bar ({sim["symbol"]} {sim["date"]} bar {i})')
        _, pr, t, key, idx, fill = best
        cur, just = t, t
        s, tr = sim['setups'][key], sim['state'][key]
        if idx == 'cancel':
            sim['watch'][key] = 'IDLE'
            sim['cancels'] += 1
            continue
        if pr == _STOP:
            px = b if jump else fill                      # a stop crossed in the jump fills at the open
            for j, x in enumerate(tr['lots']):
                if x['state'] == 'open':
                    _record(sim, tr, j, px, i, 'stop')
                    x['state'] = 'closed'
            _end_trade(sim, key)
        elif pr == _TARGET:
            _record(sim, tr, idx, fill, i, f'T{idx + 1}')
            tr['lots'][idx]['state'] = 'booked'
            if idx == 2:
                for j, x in enumerate(tr['lots']):
                    if x['state'] == 'open':
                        _record(sim, tr, j, fill, i, 'T3_flat')
                        x['state'] = 'closed'
                _end_trade(sim, key)
                ck = s['counter']
                if sim['arm']['counter'] and ck and sim['state'][ck] is None and _entry_allowed(sim, ck):
                    _open_trade(sim, ck, fill, i, 'counter')
        elif pr == _REBUY:
            tr['cycles'] += 1
            for x in tr['lots']:
                if x['state'] == 'booked':
                    x.update({'state': 'open', 'entry': fill, 'bar': i, 'rebuy': True, 'cycle': tr['cycles']})
        else:
            px = fill if sim['close_mode'] else (b if jump else fill)      # a touch entry crossed in the jump fills at the open; a limit fills at its level
            if not _open_trade(sim, key, px, i, 'entry'):
                skipped.add((key, pr, idx))


def _close_all(sim, price, i, reason):
    for key, tr in sim['state'].items():
        if tr is not None:
            for j, x in enumerate(tr['lots']):
                if x['state'] == 'open':
                    _record(sim, tr, j, price, i, reason)
                    x['state'] = 'closed'
            _end_trade(sim, key)


def _watch_update(sim, cl, i):
    """Close of a signal bar: advance every flat setup's watcher (IDLE -> ARMED on a close on the arming side, ARMED -> PENDING on a later close on the
    entry side). With fill 'at_signal' the trade opens at the level straight away (the optimistic literal reading)."""
    for key, s in sim['setups'].items():
        if sim['state'][key] is not None:
            continue
        side, e, w = s['side'], s['entry'], sim['watch'][key]
        arming, entry_side = (cl - e) * side < 0, (cl - e) * side > 0
        if w == 'IDLE' and arming:
            sim['watch'][key] = 'ARMED'
        elif w == 'ARMED' and entry_side:
            sim['signals'] += 1
            if sim['scn']['fill'] == 'at_signal':
                if _entry_allowed(sim, key):
                    _open_trade(sim, key, e, i, 'entry')
            else:
                sim['watch'][key] = 'PENDING'


def simulate_session(session: dict, arm: dict, order: str, entry_limit=None, scn: str = 'touch') -> dict:
    """Run one session. Returns {'legs': [leg records], 'trades': n, 'setup_entries': {setup: n}}. `entry_limit` caps entries per setup per
    session (None = no cap; used only by the parity check against thirds_feasibility). `scn` names an entry mode in janus_configs.ENTRY_SCENARIOS."""
    b = session['bars']
    o, h, l, c = (b[k].tolist() for k in ('open', 'high', 'low', 'close'))
    ts = b.index.values
    setups = build_setups(session['levels'])
    sim = {'setups': setups, 'state': {k: None for k in setups}, 'rec': [], 'n_trades': 0, 'setup_entries': {k: 0 for k in setups},
           'arm': arm, 'limit': entry_limit, 'symbol': session['symbol'], 'date': session['date'], 'entries_open': False,
           'tick': configs.TICK_SIZE[session['symbol']], 'scn': configs.ENTRY_SCENARIOS[scn], 'signals': 0, 'cancels': 0}
    sim['close_mode'] = sim['scn']['mode'] == 'close_limit'
    sim['watch'] = {k: 'IDLE' for k in setups}
    if sim['close_mode']:
        minute = ts.astype('datetime64[m]').astype(np.int64) // sim['scn']['minutes']
        bucket_end = [i == len(o) - 1 or minute[i + 1] != minute[i] for i in range(len(o))]
    entry_levels = [s['entry'] for s in setups.values()]
    cut_ts = ts[-1] - np.timedelta64(configs.ENTRY_CUTOFF_MIN, 'm')
    hard_ts = ts[-1] - np.timedelta64(configs.HARD_EXIT_MIN, 'm')
    prev = o[0]
    for i in range(len(o)):
        if ts[i] >= hard_ts:
            _close_all(sim, o[i], i, 'hard_exit')
            break
        sim['entries_open'] = i >= configs.AT_OPEN_MINUTES and ts[i] < cut_ts
        live = any(tr is not None for tr in sim['state'].values())
        lo, hi = min(prev, l[i]), max(prev, h[i])
        pending = sim['close_mode'] and sim['entries_open'] and any(w == 'PENDING' for w in sim['watch'].values())
        if live or pending or (not sim['close_mode'] and sim['entries_open'] and any(lo <= e <= hi for e in entry_levels)):
            net = sum(setups[k]['side'] * sum(1 for x in tr['lots'] if x['state'] == 'open') for k, tr in sim['state'].items() if tr is not None)
            pts = _bar_path(o[i], h[i], l[i], c[i], order, (net > 0) - (net < 0))
            if prev != o[i]:
                pts = [prev] + pts
            for si in range(len(pts) - 1):
                _walk_segment(sim, pts[si], pts[si + 1], jump=(prev != o[i] and si == 0), i=i)
        prev = c[i]
        if sim['close_mode'] and bucket_end[i]:
            _watch_update(sim, c[i], i)
    else:
        _close_all(sim, c[-1], len(o) - 1, 'session_end')
    return {'legs': sim['rec'], 'trades': sim['n_trades'], 'setup_entries': sim['setup_entries'], 'signals': sim['signals'], 'cancels': sim['cancels']}
