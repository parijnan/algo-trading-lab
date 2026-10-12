import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'janus_backtest'))
import janus_configs as configs  # noqa: E402
import janus_setups as st        # noqa: E402
from janus_levels import camarilla_levels  # noqa: E402

# Path tests use plain numbers: fill 100, stop 98 (risk 2), T1 102, T2 104, T3 106.
FILL, STOP, TG = 100.0, 98.0, (102.0, 104.0, 106.0)


def arr(bars):
    """bars: list of (open, high, low, close) -> (h, l, o)."""
    a = np.array(bars, dtype=float)
    return a[:, 1], a[:, 2], a[:, 0]


def path(bars, bar=0):
    h, l, _ = arr(bars)
    return st.trade_path(h, l, bar, FILL, STOP, TG)


# ---- find_entry --------------------------------------------------------------------------------------------------------------------------
def test_entry_when_the_session_opens_below_and_trades_up_through_the_level():
    h, l, o = arr([(98, 99, 97, 98.5), (98.5, 101, 98.5, 100.5)])
    assert st.find_entry(h, l, o, 100.0) == (1, 100.0)


def test_no_entry_if_the_session_opens_above_and_never_dips_below():
    h, l, o = arr([(102, 103, 101, 102), (102, 104, 101, 103)])
    assert st.find_entry(h, l, o, 100.0) is None


def test_entry_needs_a_dip_below_in_an_earlier_bar_then_a_recross():
    h, l, o = arr([(102, 103, 101, 102), (102, 102.5, 99, 99.5), (99.5, 100.4, 99.2, 100.2)])
    assert st.find_entry(h, l, o, 100.0) == (2, 100.0)


def test_a_bar_that_spans_the_level_cannot_arm_and_trigger_itself():
    h, l, o = arr([(102, 103, 101, 102), (102, 103, 99, 102.5)])
    assert st.find_entry(h, l, o, 100.0) is None


def test_the_bar_after_a_spanning_bar_is_a_late_entry_filled_at_its_open():
    h, l, o = arr([(102, 103, 101, 102), (102, 103, 99, 102.5), (102.5, 104, 101, 103)])
    assert st.find_entry(h, l, o, 100.0) == (2, 102.5)


def test_a_bar_that_opens_above_the_level_after_a_dip_fills_at_its_open():
    h, l, o = arr([(98, 99, 97, 98.5), (100.6, 101, 100.4, 100.8)])
    assert st.find_entry(h, l, o, 100.0) == (1, 100.6)


def test_entries_in_the_first_minute_are_not_allowed_but_still_arm():
    h, l, o = arr([(99, 101, 98, 100.5), (100.5, 101, 100.2, 100.8)])
    assert st.find_entry(h, l, o, 100.0, min_bar=1) == (1, 100.5)      # armed by bar 0's low, fills at bar 1's open
    assert st.find_entry(h, l, o, 100.0, min_bar=0) == (0, 100.0)


# ---- trade_path --------------------------------------------------------------------------------------------------------------------------
def test_stop_on_the_entry_bar_is_a_loss_and_flagged_ambiguous_when_the_bar_also_reaches_t1():
    r = path([(99, 102.5, 97.5, 100)])
    assert r['a'] == 'stop' and r['entry_bar_stop'] and r['a_ambiguous']


def test_stop_before_t1():
    r = path([(100, 100.5, 99.5, 100), (100, 100.2, 97.9, 98)])
    assert r['a'] == 'stop' and not r['entry_bar_stop'] and r['b'] is None


def test_neither_level_reached_is_open():
    assert path([(100, 100.5, 99.5, 100)] * 3)['a'] == 'open'


def test_t1_then_retrace_to_the_fill_then_the_rebought_lot_hits_t1_again():
    r = path([(100, 100.5, 99.5, 100), (100, 102.2, 100.1, 102), (102, 102.5, 99.9, 100.2), (100.2, 102.1, 100.1, 102)])
    assert r['a'] == 'T1' and r['b'] == 'retrace' and r['retrace_bar'] == 2 and r['reentry'] == 'win'
    assert r['t2_vs_stop'] == 'open'                      # a retrace to the fill does not stop anything while the stop is the original one


def test_rebought_lot_that_falls_to_the_stop_is_a_loss():
    r = path([(100, 100.5, 99.5, 100), (100, 102.2, 100.1, 102), (102, 102.5, 99.9, 100.2), (100.2, 100.5, 97.8, 98)])
    assert r['reentry'] == 'loss' and r['t2_vs_stop'] == 'stop'


def test_the_retrace_bar_counts_its_own_low_against_the_stop_but_not_its_high_for_t1():
    # the retrace bar reaches the fill and the stop; its high is not credited toward T1 (the order inside the bar is unknown)
    r = path([(100, 100.5, 99.5, 100), (100, 102.2, 100.1, 102), (102, 102.8, 97.5, 100)])
    assert r['b'] == 'retrace' and r['reentry'] == 'loss' and not r['reentry_ambiguous']


def test_t2_before_any_retrace_has_no_reentry_opportunity():
    r = path([(100, 100.5, 99.5, 100), (100, 104.5, 100.1, 104), (104, 104.2, 103.5, 104)])
    assert r['b'] == 'T2' and r['reentry'] is None and r['t2_vs_stop'] == 'T2'


def test_after_t2_each_stop_choice_is_scored_separately():
    # T2 at bar 1; bar 2 falls back to the fill (101 > T1? no: low 99.9 touches cost and T1, not the original stop); bar 3 reaches T3
    r = path([(100, 100.5, 99.5, 100), (100, 104.5, 100.1, 104), (104, 104.2, 99.9, 103), (103, 106.5, 102.9, 106)])
    assert r['c_fixed'] == 'T3' and r['c_cost'] == 'stopped' and r['c_t1'] == 'stopped'


def test_a_pullback_that_holds_above_t1_stops_nothing():
    r = path([(100, 100.5, 99.5, 100), (100, 104.5, 102.5, 104), (104, 104.2, 102.5, 103), (103, 106.5, 102.9, 106)])
    assert r['c_fixed'] == 'T3' and r['c_cost'] == 'T3' and r['c_t1'] == 'T3'


def test_a_pullback_to_t1_stops_the_t1_trail_but_not_the_cost_trail():
    r = path([(100, 100.5, 99.5, 100), (100, 104.5, 102.5, 104), (104, 104.2, 101.9, 103), (103, 106.5, 102.9, 106)])
    assert r['c_cost'] == 'T3' and r['c_t1'] == 'stopped' and r['c_fixed'] == 'T3'


def test_trail_same_bar_test_versus_skip(monkeypatch):
    bars = [(100, 100.5, 99.5, 100), (100, 104.5, 101.0, 104), (104, 106.5, 103, 106)]
    # bar 1 reaches T2 and its low (101.0) is below T1 (102): the moved stop is hit in the same bar under 'test', ignored under 'skip'
    monkeypatch.setattr(configs, 'TRAIL_SAME_BAR', 'test')
    assert path(bars)['c_t1'] == 'stopped'
    monkeypatch.setattr(configs, 'TRAIL_SAME_BAR', 'skip')
    assert path(bars)['c_t1'] == 'T3'


# ---- frame -------------------------------------------------------------------------------------------------------------------------------
def test_short_frame_is_the_negated_mirror():
    lv = camarilla_levels(110.0, 100.0, 105.0)
    df = pd.DataFrame([(105, 106, 104, 105.5)], columns=['open', 'high', 'low', 'close'])
    s = {'symbol': 'T', 'date': date(2026, 1, 5), 'expiry': date(2026, 1, 20), 'bars': df, 'prev': {}, 'levels': lv}
    h, l, o, c, L = st.frame(s, -1)
    assert (h[0], l[0], o[0], c[0]) == (-104, -106, -105, -105.5)
    assert L['S3'] == -lv['R3'] and L['R4'] == -lv['S4'] and L['R5'] == -lv['S5'] and L['C'] == -lv['C']
    assert st.frame(s, 1)[4]['S3'] == lv['S3']


def test_a_dip_to_the_fill_inside_the_t1_bar_is_not_counted_as_a_retrace():
    # the order of the low and the high inside the T1 bar is unknown, so the retrace question starts at the next bar
    r = path([(100, 100.5, 99.5, 100), (100, 102.2, 99.6, 102), (102, 102.4, 101, 102.1)])
    assert r['a'] == 'T1' and r['b'] == 'open' and r['reentry'] is None
