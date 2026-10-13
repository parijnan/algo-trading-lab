"""Range-based Camarilla levels: the setter-candle range state and its use in the session records (plans/janus-range-levels.md)."""

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'janus_backtest'))
import janus_data  # noqa: E402
from janus_levels import camarilla_levels, range_states  # noqa: E402


def day(n, high, low, close):
    return {'date': date(2026, 1, n), 'high': high, 'low': low, 'close': close}


def test_first_session_is_the_setter():
    r = range_states([day(1, 110, 100, 105)])[0]
    assert (r['high'], r['low'], r['setter_date'], r['age'], r['extensions']) == (110, 100, date(2026, 1, 1), 1, 0)


def test_close_inside_extends_the_range_with_the_days_wicks_and_never_narrows():
    s = range_states([day(1, 110, 100, 105), day(2, 112, 101, 106), day(3, 108, 98, 104), day(4, 107, 103, 105)])
    assert [(r['high'], r['low']) for r in s] == [(110, 100), (112, 100), (112, 98), (112, 98)]
    assert [r['age'] for r in s] == [1, 2, 3, 4] and [r['extensions'] for r in s] == [0, 1, 2, 2]
    assert all(r['setter_date'] == date(2026, 1, 1) for r in s)


def test_close_strictly_above_the_range_starts_a_new_range_from_that_days_own_bar():
    s = range_states([day(1, 110, 100, 105), day(2, 120, 106, 118)])
    assert (s[1]['high'], s[1]['low'], s[1]['setter_date'], s[1]['age']) == (120, 106, date(2026, 1, 2), 1)


def test_close_strictly_below_the_range_starts_a_new_range():
    s = range_states([day(1, 110, 100, 105), day(2, 104, 90, 92)])
    assert (s[1]['high'], s[1]['low'], s[1]['setter_date']) == (104, 90, date(2026, 1, 2))


def test_close_exactly_on_a_bound_stays_inside():
    s = range_states([day(1, 110, 100, 105), day(2, 115, 104, 110), day(3, 108, 95, 100)])
    assert s[1]['setter_date'] == date(2026, 1, 1) and s[1]['high'] == 115
    assert s[2]['setter_date'] == date(2026, 1, 1) and s[2]['low'] == 95


def test_close_is_judged_against_the_range_before_the_days_own_extension():
    # high 120 sits above the old range (110) but the close 109 is inside it: an extension, not a setter
    ext = range_states([day(1, 110, 100, 105), day(2, 120, 104, 109)])[1]
    assert ext['setter_date'] == date(2026, 1, 1) and ext['high'] == 120
    # the close 118 is above the OLD high 110 even though it is inside the day's own new range: a setter
    brk = range_states([day(1, 110, 100, 105), day(2, 120, 104, 118)])[1]
    assert brk['setter_date'] == date(2026, 1, 2)


def test_after_a_breakout_the_next_day_extends_the_single_bar_range():
    s = range_states([day(1, 110, 100, 105), day(2, 120, 106, 118), day(3, 119, 112, 115)])
    assert (s[2]['high'], s[2]['low'], s[2]['setter_date'], s[2]['age']) == (120, 106, date(2026, 1, 2), 2)


def test_levels_from_the_range_use_range_width_and_the_previous_close():
    s = range_states([day(1, 110, 100, 105), day(2, 112, 101, 106)])
    lv = camarilla_levels(s[1]['high'], s[1]['low'], 106.0)
    assert lv['R'] == pytest.approx(12.0) and lv['C'] == 106.0
    assert lv['R3'] == pytest.approx(106 + 12 * 1.1 / 4)
    assert lv['R5'] == pytest.approx(112 / 100 * 106)


def test_a_single_bar_range_gives_the_previous_session_levels():
    d = day(1, 110, 100, 105)
    r = range_states([d])[0]
    assert camarilla_levels(r['high'], r['low'], d['close']) == camarilla_levels(d['high'], d['low'], d['close'])


def _frame(rows, vol=1):
    idx = pd.date_range('2026-01-05 09:00', periods=rows, freq='min')
    return pd.DataFrame({'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.5, 'volume': vol}, index=idx)


def test_range_history_skips_thin_and_no_volume_days(monkeypatch):
    monkeypatch.setattr(janus_data.configs, 'RANGE_MIN_BARS', 120)
    days = {date(2026, 1, 5): _frame(300), date(2026, 1, 6): _frame(50), date(2026, 1, 7): _frame(300, vol=0), date(2026, 1, 8): _frame(300)}
    out = janus_data._range_by_day(days)
    assert sorted(out) == [date(2026, 1, 5), date(2026, 1, 8)]
    assert out[date(2026, 1, 8)]['age'] == 2


def test_range_history_uses_daily_high_low_and_the_last_close(monkeypatch):
    monkeypatch.setattr(janus_data.configs, 'RANGE_MIN_BARS', 10)
    f = _frame(20)
    f.iloc[5, f.columns.get_loc('high')] = 108.0
    f.iloc[7, f.columns.get_loc('low')] = 95.0
    f.iloc[-1, f.columns.get_loc('close')] = 103.0
    out = janus_data._range_by_day({date(2026, 1, 5): f})[date(2026, 1, 5)]
    assert (out['high'], out['low']) == (108.0, 95.0)


def test_unknown_level_source_is_refused():
    with pytest.raises(ValueError):
        janus_data.load_sessions('CRUDEOILM', level_source='nonsense')
