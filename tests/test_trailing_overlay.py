import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'research' / 'trailing_profit'))
import trailing_overlay as T  # noqa: E402


def series(bars):
    """bars: (open, high, low) per minute; the first bar of the session is guarded (exempt from stop checks)."""
    a = np.array(bars, float)
    guarded = np.zeros(len(bars), bool)
    guarded[0] = True
    return SimpleNamespace(open=a[:, 0], high=a[:, 1], low=a[:, 2], guarded=guarded)


def trade(bull=True, entry=100.0, n=6, flip=np.nan):
    return {'trade_id': 1, 'entry_ts': None, 'bull': bull, 'entry': entry, 'lo': 1, 'end': n, 'flip_price': flip}


def test_no_rule_is_the_plain_decided_stop_and_a_flip_exit_otherwise():
    p = series([(100, 100, 100), (100, 101, 99.5), (101, 102, 100.5), (102, 103, 101), (103, 104, 102), (104, 105, 103)])
    assert T.simulate_trail(trade(flip=104.0), p, 3.0, None, ('none',)) == (4.0, 'flip')
    p2 = series([(100, 100, 100), (100, 101, 99.5), (99, 99.2, 96.0), (97, 97, 97), (97, 97, 97), (97, 97, 97)])
    assert T.simulate_trail(trade(), p2, 3.0, None, ('none',)) == (pytest.approx(-3.0), 'stop')


RISING = [(100, 100, 100), (100, 101, 100), (101, 102.5, 101.2), (102.5, 104, 102.6), (104, 105.5, 104.2)]       # peak 105.5 after bar 4


def test_a_trail_follows_the_peak_and_exits_at_its_level():
    held = series(RISING + [(104.6, 105, 103.5)])                                       # trail = 105.5 x 0.98 = 103.39; the low 103.5 holds
    assert T.simulate_trail(trade(flip=105.0, n=6), held, 3.0, None, ('trail', 2.0)) == (5.0, 'flip')
    broken = series(RISING + [(104.6, 105, 103.3)])                                     # the low 103.3 <= 103.39: stopped at the trail level
    pts, why = T.simulate_trail(trade(flip=105.0, n=6), broken, 3.0, None, ('trail', 2.0))
    assert why == 'stop' and pts == pytest.approx(105.5 * 0.98 - 100.0)


def test_a_bar_whose_own_range_exceeds_the_trail_stops_itself_out():
    """The intrabar order is unknown at one-minute resolution; the overlay takes the pessimistic reading (the high comes first)."""
    wide = series([(100, 100, 100), (100, 106, 100), (106, 106, 106), (106, 106, 106), (106, 106, 106), (106, 106, 106)])
    pts, why = T.simulate_trail(trade(flip=106.0), wide, 3.0, None, ('trail', 2.0))
    assert why == 'stop' and pts == pytest.approx(106 * 0.98 - 100.0)


def test_an_activation_rule_does_nothing_until_the_gain_is_reached():
    p = series([(100, 100, 100), (100, 101, 99.2), (100, 101.5, 99.0), (99.5, 100, 99.0), (99, 99, 99), (99, 99, 99)])
    assert T.simulate_trail(trade(flip=99.0), p, 3.0, None, ('act', 3.0, 0.5)) == (-1.0, 'flip')        # peak 101.5 < +3%: only the decided stop applies


def test_breakeven_moves_the_stop_to_the_entry_after_the_gain():
    p = series([(100, 100, 100), (100, 102, 100), (102, 102, 99.9), (99, 99, 99), (99, 99, 99), (99, 99, 99)])
    pts, why = T.simulate_trail(trade(flip=99.0), p, 3.0, None, ('be', 2.0, None))
    assert why == 'stop' and pts == pytest.approx(0.0)
    assert T.simulate_trail(trade(flip=99.0), p, 3.0, None, ('none',)) == (-1.0, 'flip')


def test_an_adverse_gap_fills_at_the_open_not_the_level():
    p = series([(100, 100, 100), (100, 105, 104.5), (101, 101.5, 100.8), (101, 101, 101), (101, 101, 101), (101, 101, 101)])
    pts, why = T.simulate_trail(trade(flip=101.0), p, 3.0, None, ('trail', 2.0))      # trail 105 x 0.98 = 102.9, the next bar opens at 101
    assert why == 'stop' and pts == pytest.approx(1.0)


def reflect(bars, about=100.0):
    return [(2 * about - o, 2 * about - lo, 2 * about - h) for o, h, lo in bars]


def test_a_short_is_the_mirror_of_a_long():
    bars = RISING + [(104.6, 105, 103.3)]
    a = T.simulate_trail(trade(bull=True, flip=105.0, n=6), series(bars), 3.0, None, ('trail', 2.0))
    b = T.simulate_trail(trade(bull=False, flip=95.0, n=6), series(reflect(bars)), 3.0, None, ('trail', 2.0))
    assert a[1] == b[1] == 'stop' and a[0] == pytest.approx(b[0])


def test_the_target_wins_when_reached_first_and_the_stop_wins_a_same_bar_tie():
    p = series([(100, 100, 100), (100, 116, 100), (116, 116, 116), (116, 116, 116), (116, 116, 116), (116, 116, 116)])
    assert T.simulate_trail(trade(), p, 3.0, 15.0, ('none',)) == (pytest.approx(15.0), 'target')
    tie = series([(100, 100, 100), (100, 116, 96), (116, 116, 116), (116, 116, 116), (116, 116, 116), (116, 116, 116)])
    assert T.simulate_trail(trade(), tie, 3.0, 15.0, ('none',))[1] == 'stop'
