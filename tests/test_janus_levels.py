import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'janus_backtest'))
from janus_levels import camarilla_levels  # noqa: E402


def test_levels_match_a_hand_computed_example():
    # H=110, L=100, C=105 -> R=10; step_k = 10*1.1/divisor_k = 11/12, 11/6, 11/4, 11/2
    lv = camarilla_levels(110.0, 100.0, 105.0)
    assert lv['R'] == 10.0 and lv['C'] == 105.0
    assert lv['R1'] == pytest.approx(105 + 11 / 12) and lv['S1'] == pytest.approx(105 - 11 / 12)
    assert lv['R2'] == pytest.approx(105 + 11 / 6) and lv['R3'] == pytest.approx(105 + 11 / 4) and lv['R4'] == pytest.approx(105 + 11 / 2)
    assert lv['S4'] == pytest.approx(105 - 11 / 2)
    assert lv['R5'] == pytest.approx(110 / 100 * 105)                 # 115.5
    assert lv['S5'] == pytest.approx(105 - (115.5 - 105))             # 94.5
    assert lv['R6'] == pytest.approx(115.5 + 1.168 * (115.5 - (105 + 5.5)))   # 121.34
    assert lv['S6'] == pytest.approx(105 - (lv['R6'] - 105))
    assert lv['S6'] - lv['S5'] == pytest.approx(-1.168 * (lv['S4'] - lv['S5']))   # mirror form of the same rule


def test_levels_are_symmetric_about_the_close_and_ordered():
    lv = camarilla_levels(250.0, 240.0, 243.0)
    for k in range(1, 7):
        assert lv[f'R{k}'] - 243.0 == pytest.approx(243.0 - lv[f'S{k}'])
    assert lv['C'] < lv['R1'] < lv['R2'] < lv['R3'] < lv['R4'] < lv['R5'] < lv['R6']


def test_a_bad_range_is_refused():
    with pytest.raises(ValueError):
        camarilla_levels(100.0, 110.0, 105.0)
    with pytest.raises(ValueError):
        camarilla_levels(100.0, 0.0, 50.0)
