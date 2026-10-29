"""
The Oleus state machine against the research machine (janus_backtest/janus_machine.py with re-buy, counter and trailing stop off): driven along
the same assumed price paths with the research fills, every leg must be identical, and the machine must never hold two trades at once.
"""
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'janus_backtest'))
import janus_configs as configs   # noqa: E402
import janus_engine as eng        # noqa: E402
import janus_machine as mach      # noqa: E402
from janus_levels import camarilla_levels  # noqa: E402

from oleus_engine import levels as oleus_levels                      # noqa: E402
from oleus_engine.engine_configs import DEFAULT, SETUPS             # noqa: E402
from oleus_engine.machine import Machine, ENTRY, STOP, TARGET        # noqa: E402

LV = camarilla_levels(110.0, 100.0, 105.0)
OFF = {'rebuy': False, 'counter': False, 'tsl': False}


def _session(rows):
    idx = pd.date_range('2026-01-05 09:00', periods=len(rows), freq='min')
    df = pd.DataFrame(rows, columns=['open', 'high', 'low', 'close'], index=idx)
    df['volume'] = 1
    return {'symbol': 'CRUDEOILM', 'date': date(2026, 1, 5), 'expiry': date(2026, 1, 20), 'bars': df, 'prev': {}, 'levels': LV}


def _random_rows(seed, n=700, gap_prob=0.1, sigma=0.25):
    rs = np.random.RandomState(seed)
    px, rows = 105.0, []
    for _ in range(n):
        o = px + (rs.normal(0, sigma) if rs.rand() < gap_prob else 0.0)
        c = o + rs.normal(0, sigma)
        rows.append((o, max(o, c) + abs(rs.normal(0, 0.12)), min(o, c) - abs(rs.normal(0, 0.12)), c))
        px = c
    return rows


def drive(session, order):
    """The research bar walk with the Oleus Machine in place of janus_machine's state: returns the legs as comparable tuples."""
    setups = oleus_levels.build_setups(session['levels'])
    m = Machine(setups)
    b = session['bars']
    o, h, l, c = (b[k].tolist() for k in ('open', 'high', 'low', 'close'))
    ts = b.index.values
    cut_ts = ts[-1] - np.timedelta64(DEFAULT.entry_cutoff_min, 'm')
    hard_ts = ts[-1] - np.timedelta64(DEFAULT.hard_exit_min, 'm')
    legs, entry_info = [], {}
    prev = o[0]

    def leg(key, j, exit_bar, exit_px, reason):
        e = entry_info[key]
        legs.append((key, j + 1, e['bar'], round(e['px'], 6), exit_bar, round(exit_px, 6), reason))

    for i in range(len(o)):
        if ts[i] >= hard_ts:
            for key, lots in m.close_all().items():
                for j in lots:
                    leg(key, j, i, o[i], 'hard_exit')
            break
        entries_open = i >= DEFAULT.at_open_minutes and ts[i] < cut_ts
        net = m.net_side_lots()
        pts = eng._bar_path(o[i], h[i], l[i], c[i], order, (net > 0) - (net < 0))
        if prev != o[i]:
            pts = [prev] + pts
        for si in range(len(pts) - 1):
            a, bb = pts[si], pts[si + 1]
            jump = prev != o[i] and si == 0
            cur, just, skipped = a, None, set()
            while True:
                ev = m.find_next(a, bb, entries_open, cur, just, skipped)
                if ev is None:
                    break
                cur, just = ev.trigger, ev.trigger
                if ev.kind == 'stop':
                    px = bb if jump else ev.level
                    reason = 'stop'
                    for j in m.apply_stop(ev.key):
                        leg(ev.key, j, i, px, reason)
                elif ev.kind == 'target':
                    for j in m.apply_target(ev.key, ev.idx):
                        leg(ev.key, j, i, ev.level, 'T3_flat')
                    leg(ev.key, ev.idx, i, ev.level, f'T{ev.idx + 1}')
                else:
                    px = bb if jump else ev.level
                    if not m.entry_ok(ev.key, px):
                        skipped.add(ev.fill_key)
                        continue
                    m.apply_entry(ev.key, px)
                    entry_info[ev.key] = {'bar': i, 'px': px}
                assert len(m.open_trades()) <= 1, 'two trades open at once'
        prev = c[i]
        if i == len(o) - 1:
            for key, lots in m.close_all().items():
                for j in lots:
                    leg(key, j, i, c[i], 'session_end')
    return sorted(legs)


def _sig(legs):
    return sorted((x['setup'], x['lot'], x['entry_bar'], round(x['entry'], 6), x['exit_bar'], round(x['exit'], 6), x['reason']) for x in legs)


@pytest.mark.parametrize('order', configs.BAR_ORDERS)
@pytest.mark.parametrize('gap_prob,sigma', [(0.1, 0.25), (0.4, 0.6)])
def test_machine_reproduces_the_research_legs_one_for_one(order, gap_prob, sigma):
    total = 0
    for seed in range(40):
        s = _session(_random_rows(seed, gap_prob=gap_prob, sigma=sigma))
        ref = mach.simulate_session(s, OFF, order)['legs']
        got = drive(s, order)
        assert got == _sig(ref), (seed, order)
        total += len(ref)
    assert total > 300


def test_the_levels_and_setups_match_the_research_code():
    for hi, lo, cl in [(110.0, 100.0, 105.0), (9800.0, 9450.0, 9712.0), (96.4, 91.2, 92.0)]:
        ref = camarilla_levels(hi, lo, cl)
        got = oleus_levels.camarilla_levels(hi, lo, cl, DEFAULT)
        assert got.keys() == ref.keys() and all(abs(got[k] - ref[k]) < 1e-9 for k in ref)
        a = oleus_levels.build_setups(got)
        b = eng.build_setups(ref)
        assert a.keys() == b.keys()
        for k in a:
            assert (a[k]['side'], a[k]['entry'], a[k]['stop'], a[k]['targets'], a[k]['risk']) == \
                   (b[k]['side'], b[k]['entry'], b[k]['stop'], b[k]['targets'], b[k]['risk'])


def test_a_stop_that_is_another_setups_entry_reverses_on_the_same_move():
    setups = oleus_levels.build_setups(LV)
    m = Machine(setups)
    m.apply_entry('F_L', LV['S3'])
    ev = m.find_next(LV['S3'] + 0.5, LV['S4'] - 0.5, True)
    assert (ev.kind, ev.key, ev.trigger) == ('stop', 'F_L', LV['S4'])
    m.apply_stop('F_L')
    nxt = m.find_next(LV['S3'] + 0.5, LV['S4'] - 0.5, True, cur=ev.trigger, just=ev.trigger)
    assert (nxt.kind, nxt.key) == ('entry', 'B_S'), 'F_L stops at S4, which is B_S\'s entry level'


def test_nothing_changes_until_an_outcome_is_applied_and_the_state_round_trips():
    m = Machine(oleus_levels.build_setups(LV))
    assert m.find_next(100.0, 103.0, True).kind == 'entry'
    assert m.open_trades() == []
    m.apply_entry('F_L', LV['S3'])
    m.apply_target('F_L', 0)
    m2 = Machine(oleus_levels.build_setups(LV))
    m2.load(m.to_dict())
    assert m2.trades == m.trades and m2.open_lot_indices('F_L') == [1, 2]


def test_the_setup_table_is_the_research_one_without_the_counter_trade():
    for k, v in SETUPS.items():
        r = configs.ABS_SETUPS[k]
        assert {kk: v[kk] for kk in ('side', 'family', 'entry', 'stop', 'targets')} == {kk: r[kk] for kk in ('side', 'family', 'entry', 'stop', 'targets')}
