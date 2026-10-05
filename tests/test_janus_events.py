import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'janus_backtest'))
import janus_events as ev  # noqa: E402
from janus_levels import camarilla_levels  # noqa: E402

# Previous H=110 L=100 C=105 (R=10): R2=106.8333 R3=107.75 R4=110.5 R5=115.5 ; S2=103.1667 S3=102.25 S4=99.5 S5=94.5
LV = camarilla_levels(110.0, 100.0, 105.0)


def session(bars):
    """bars: list of (open, high, low, close)."""
    df = pd.DataFrame(bars, columns=['open', 'high', 'low', 'close'])
    df['volume'] = 1
    return {'symbol': 'T', 'date': date(2026, 1, 5), 'expiry': date(2026, 1, 20), 'bars': df, 'prev': {}, 'levels': LV}


def row(rows, level):
    return next(r for r in rows if r['level'] == level)


def test_untouched_levels_are_reported_as_untouched():
    rows = ev.analyse_session(session([(105, 106, 104, 105)] * 5))
    assert all(not r['touched'] for r in rows) and len(rows) == 6        # R3,R4,R5,S3,S4,S5


def test_a_fade_that_reaches_the_inward_level_first():
    # touch R3 (107.75) on bar 1, then fall to R2 (106.83) on bar 2 before R4 (110.5)
    rows = ev.analyse_session(session([(105, 106, 104.5, 105.5), (105.5, 108.0, 105.5, 107.0), (107.0, 107.2, 106.5, 106.6)]))
    r = row(rows, 'R3')
    assert r['touched'] and r['touch_bar'] == 1 and not r['at_open']
    assert r['fp_4_2'] == 'in'
    assert r['fp_4_C'] == 'none'                                         # C (105) never reached, R4 never reached


def test_a_breakout_continues_to_the_outward_level():
    rows = ev.analyse_session(session([(105, 106, 104.5, 105.5), (105.5, 108.0, 105.5, 107.0), (107.0, 111.0, 107.0, 110.9)]))
    r = row(rows, 'R3')
    assert r['fp_4_2'] == 'out'
    assert row(rows, 'R4')['touched'] and row(rows, 'R4')['touch_bar'] == 2


def test_the_touch_bar_does_not_count_its_own_inward_excursion():
    # one bar touches R3 and also dips to R2: cannot be ordered against the touch, so it is not scored 'in'
    rows = ev.analyse_session(session([(105, 106, 104.5, 105.5), (105.5, 108.0, 106.0, 107.0), (107.0, 107.5, 107.0, 107.2)]))
    assert row(rows, 'R3')['fp_4_2'] == 'none'


def test_a_bar_reaching_both_levels_is_ambiguous_not_guessed():
    rows = ev.analyse_session(session([(105, 106, 104.5, 105.5), (105.5, 108.0, 105.5, 107.0), (107.0, 111.0, 106.0, 108.0)]))
    assert row(rows, 'R3')['fp_4_2'] == 'ambiguous'


def test_a_gap_open_beyond_a_level_is_flagged_at_open():
    rows = ev.analyse_session(session([(111.0, 111.5, 110.8, 111.2), (111.2, 111.4, 110.0, 110.2)]))
    r = row(rows, 'R4')
    assert r['touched'] and r['at_open'] and r['touch_bar'] == 0
    assert ev.open_zone(session([(111.0, 111.5, 110.8, 111.2)])) == 'above_R4'


def test_the_lower_side_mirrors_the_upper_side_exactly():
    up = [(105, 106, 104.5, 105.5), (105.5, 108.0, 105.5, 107.0), (107.0, 107.2, 106.5, 106.6)]
    mirrored = [(2 * 105 - o, 2 * 105 - lo, 2 * 105 - h, 2 * 105 - c) for o, h, lo, c in up]   # reflect about the previous close
    lv_mirror = camarilla_levels(110.0, 100.0, 105.0)                    # symmetric levels about C, so reflection maps R_k to S_k
    s_up, s_dn = session(up), session(mirrored)
    s_dn['levels'] = lv_mirror
    ru, rd = row(ev.analyse_session(s_up), 'R3'), row(ev.analyse_session(s_dn), 'S3')
    for key in ('touched', 'touch_bar', 'fp_4_2', 'fp_4_C'):
        assert ru[key] == rd[key]
    assert ru['out_exc_R'] == pytest.approx(rd['out_exc_R']) and ru['in_exc_R'] == pytest.approx(rd['in_exc_R'])


def test_the_driftless_benchmark_is_the_gamblers_ruin_probability():
    r = row(ev.analyse_session(session([(105, 108.0, 104.5, 107.0)])), 'R3')
    d_out, d_in = LV['R4'] - LV['R3'], LV['R3'] - LV['R2']
    assert r['bm_4_2'] == pytest.approx(d_out / (d_in + d_out))
    assert r['bm_4_C'] == pytest.approx(d_out / ((LV['R3'] - LV['C']) + d_out))
    assert r['bm_4_C'] == pytest.approx(0.5)                             # C->R3 and R3->R4 are both 0.275 R


def test_open_zones():
    for open_px, zone in [(105.0, 'inside_S3_R3'), (108.5, 'R3_R4'), (111.0, 'above_R4'), (101.0, 'S3_S4'), (99.0, 'below_S4')]:
        assert ev.open_zone(session([(open_px, open_px, open_px, open_px)])) == zone
