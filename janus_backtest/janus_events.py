"""
Descriptive events for one session: the first touch of each Camarilla level, and what happens next. Pure functions on numpy arrays and a session
dict from janus_data; no data access, no strategy rules.

Everything is computed in an "upper frame": for a lower-side level the prices and levels are negated, so one piece of logic serves both sides.

First passage after a touch: scan from the touch bar for whichever of two levels is reached first. On the touch bar itself only the outward
level is evaluated (an inward level reached inside the same bar cannot be ordered against the touch). A bar that reaches both levels is
'ambiguous' and reported as such, never guessed. A touch that is already true at the first bar's open is flagged `at_open` (a gap, not a touch).
The driftless benchmark for each pair is the gambler's-ruin probability from the level distances, so a result is read as excess over it.
"""

import numpy as np

import janus_configs as configs


def _frame(session: dict, side: int):
    b, lv = session['bars'], session['levels']
    if side == 1:
        h, lo, o, c = b['high'].values, b['low'].values, b['open'].values, b['close'].values
        up = {'C': lv['C'], **{k: lv[f'R{k}'] for k in range(1, 6)}}
    else:
        h, lo, o, c = -b['low'].values, -b['high'].values, -b['open'].values, -b['close'].values
        up = {'C': -lv['C'], **{k: -lv[f'S{k}'] for k in range(1, 6)}}
    return h, lo, o, c, up


def first_passage(h, lo, i0: int, out_level: float, in_level: float):
    """('out' | 'in' | 'ambiguous' | 'none', bar index). See the module docstring for the touch-bar rule."""
    for j in range(i0, len(h)):
        out = h[j] >= out_level
        inn = (lo[j] <= in_level) if j > i0 else False
        if out and inn:
            return 'ambiguous', j
        if out:
            return 'out', j
        if inn:
            return 'in', j
    return 'none', len(h) - 1


def open_zone(session: dict) -> str:
    """Where the session opens relative to the levels: gap beyond S4/R4, between S3/S4 or R3/R4, or inside S3..R3."""
    o, lv = float(session['bars']['open'].iloc[0]), session['levels']
    if o >= lv['R4']:
        return 'above_R4'
    if o >= lv['R3']:
        return 'R3_R4'
    if o > lv['S3']:
        return 'inside_S3_R3'
    if o > lv['S4']:
        return 'S3_S4'
    return 'below_S4'


def _dist(up: dict, a, b) -> float:
    return abs(up[a] - up[b])


def analyse_session(session: dict) -> list:
    """One row per (level, side): touched or not, and for touched levels the timing, excursions, session-close position and first-passage
    outcomes with their driftless benchmarks. Excursions and distances are in units of the previous session's range R."""
    rows = []
    R = session['levels']['R']
    zone = open_zone(session)
    for side in (1, -1):
        h, lo, o, c, up = _frame(session, side)
        for k in configs.TOUCH_LEVELS:
            lvl = up[k]
            hit = np.nonzero(h >= lvl)[0]
            row = {'symbol': session['symbol'], 'date': session['date'], 'year': session['date'].year, 'expiry': session['expiry'],
                   'level': ('R' if side == 1 else 'S') + str(k), 'side': side, 'open_zone': zone,
                   'r_pct': R / session['levels']['C'] * 100, 'touched': bool(len(hit)), 'at_open': False}
            if len(hit):
                i0 = int(hit[0])
                row.update({'touch_bar': i0, 'at_open': bool(o[0] >= lvl),
                            'close_vs_level_R': (c[-1] - lvl) / R,
                            'out_exc_R': (h[i0:].max() - lvl) / R,
                            'in_exc_R': (lvl - lo[i0 + 1:].min()) / R if i0 + 1 < len(lo) else 0.0})
                for out_k, in_k in configs.FIRST_PASSAGE[k]:
                    res, j = first_passage(h, lo, i0, up[out_k], up[in_k])
                    tag = f'{out_k}_{in_k}'
                    d_out, d_in = _dist(up, out_k, k), _dist(up, k, in_k)
                    row[f'fp_{tag}'] = res
                    row[f'bm_{tag}'] = d_out / (d_in + d_out)     # driftless P(inward level first), gambler's ruin: the farther the outward level, the likelier 'in'
            rows.append(row)
    return rows
