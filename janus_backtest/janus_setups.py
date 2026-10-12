"""
Entry detection and first-passage path of one Janus trade, on one session's 1-minute bars. Pure functions on numpy arrays; no strategy
tuning, no costs. Everything is in the LONG frame: a short is the same logic on negated prices (frame(), where S and R swap).

Conventions (plans/janus-camarilla-research.md items 1, 3, 6): a touch entry fires at the first bar whose high reaches the level after price
was seen below it in an EARLIER bar (or the session opened below it), so a bar that spans the level cannot arm and trigger itself. The fill is
the level, or the bar's open if the bar opened above it. Inside a bar the order of high and low is unknown, so a bar that reaches both the
upward and the downward level counts as the downward one (pessimistic) and is flagged ambiguous.
"""

import numpy as np

import janus_configs as configs

_LEVEL_NUMBERS = range(1, 8)


def frame(session: dict, direction: int):
    """(high, low, open, close, levels) arrays/dict in the long frame. direction -1 negates prices and swaps S and R."""
    b, lv = session['bars'], session['levels']
    if direction == 1:
        return (b['high'].values, b['low'].values, b['open'].values, b['close'].values,
                {**{f'R{k}': lv[f'R{k}'] for k in _LEVEL_NUMBERS}, **{f'S{k}': lv[f'S{k}'] for k in _LEVEL_NUMBERS}, 'C': lv['C']})
    levels = {'C': -lv['C']}
    for k in _LEVEL_NUMBERS:
        levels[f'R{k}'], levels[f'S{k}'] = -lv[f'S{k}'], -lv[f'R{k}']
    return -b['low'].values, -b['high'].values, -b['open'].values, -b['close'].values, levels


def find_entry(h, l, o, level: float, min_bar: int = 0):
    """(bar, fill) of the first touch of `level` from below after price was below it in an earlier bar, or None. No entry before bar `min_bar`
    (the first minute of a session is an opening print, not a tradable touch; bars before it still count as having been below the level)."""
    lowest = np.minimum.accumulate(l)
    for j in range(min_bar, len(h)):
        was_below = (o[0] < level) if j == 0 else (lowest[j - 1] < level)
        if h[j] >= level and was_below:
            return j, max(level, float(o[j]))
    return None


def scan(h, l, start_up: int, start_dn: int, up: float, dn: float):
    """First of: high reaches `up` (checked from bar start_up) or low reaches `dn` (checked from bar start_dn). Returns (outcome, bar, ambiguous)
    with outcome 'up', 'dn' or 'none'; a bar reaching both is 'dn' with ambiguous True."""
    for j in range(min(start_up, start_dn), len(h)):
        d = j >= start_dn and l[j] <= dn
        u = j >= start_up and h[j] >= up
        if d:
            return 'dn', j, bool(u)
        if u:
            return 'up', j, False
    return 'none', len(h) - 1, False


def trade_path(h, l, bar: int, fill: float, stop: float, targets) -> dict:
    """What happens after an entry at `fill` in bar `bar`, as separate first-passage questions (all with the ORIGINAL stop in force until T2):
      a            'T1' | 'stop' | 'open' : first of T1 and the stop (the stop is tested on the entry bar too)
      b            after T1: 'T2' | 'retrace' (price back to the fill before T2) | 'open'
      t2_vs_stop   after T1: 'T2' | 'stop' | 'open' (the fixed-stop question; a retrace to the fill does not stop anything)
      reentry      after a retrace to the fill: 'win' (T1 again before the stop) | 'loss' | 'open'
      c_fixed / c_cost / c_t1   after T2: 'T3' | 'stopped' | 'open', with the stop left at the original / moved to the fill / moved to T1
    """
    t1, t2, t3 = targets
    trail_from = (lambda b: b) if configs.TRAIL_SAME_BAR == 'test' else (lambda b: b + 1)
    out = {'a': 'open', 'a_ambiguous': False, 'entry_bar_stop': False, 'b': None, 'retrace_bar': None, 'reentry': None, 'reentry_ambiguous': False,
           't2_vs_stop': None, 'c_fixed': None, 'c_cost': None, 'c_t1': None}
    res, b1, amb = scan(h, l, bar, bar, t1, stop)
    out['a_ambiguous'] = amb
    if res == 'dn':
        out['a'] = 'stop'
        out['entry_bar_stop'] = b1 == bar
        return out
    if res == 'none':
        return out
    out['a'] = 'T1'
    res, rb, _ = scan(h, l, b1, b1 + 1, t2, fill)
    out['b'] = {'up': 'T2', 'dn': 'retrace', 'none': 'open'}[res]
    if res == 'dn':
        out['retrace_bar'] = rb
        r2, _, amb2 = scan(h, l, rb + 1, rb, t1, stop)
        out['reentry'] = {'up': 'win', 'dn': 'loss', 'none': 'open'}[r2]
        out['reentry_ambiguous'] = amb2
    res, b2, _ = scan(h, l, b1, b1 + 1, t2, stop)
    out['t2_vs_stop'] = {'up': 'T2', 'dn': 'stop', 'none': 'open'}[res]
    if res != 'up':
        return out
    for key, level in (('c_fixed', stop), ('c_cost', fill), ('c_t1', t1)):
        r3, _, _ = scan(h, l, b2, trail_from(b2) if key != 'c_fixed' else b2 + 1, t3, level)
        out[key] = {'up': 'T3', 'dn': 'stopped', 'none': 'open'}[r3]
    return out
