"""Weekend-flatten overlay: calendar, flatten fills, dropped entries, gap split, slippage, the re-entry arm, and Prometheus lot handling."""

import os
import sys

import numpy as np
import pandas as pd
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'research', 'weekend_flatten'))
import flatten_configs as cfg  # noqa: E402
import weekend_flatten as WF  # noqa: E402

OFF_USER = cfg.OFFSET_VARIANTS['user']
OFF_ENGINE = cfg.OFFSET_VARIANTS['engine']


class FakeP:
    def __init__(self, index, o, h, l):
        self.index, self.open, self.high, self.low = index, np.asarray(o, float), np.asarray(h, float), np.asarray(l, float)
        day = index.normalize()
        first = pd.Series(index, index=index).groupby(day).transform('min')
        self.guarded = (index == first.to_numpy())


def sessions(dates, last_min='23:29', first='09:00'):
    parts = [pd.date_range(f'{d} {first}', f'{d} {last_min}', freq='min') for d in dates]
    return parts[0].append(parts[1:]) if len(parts) > 1 else parts[0]


def series(dates, price=100.0, last_min='23:29'):
    idx = sessions(dates, last_min)
    n = len(idx)
    return FakeP(idx, np.full(n, price), np.full(n, price), np.full(n, price))


WEEK1 = ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09']
WEEK2 = ['2026-10-12', '2026-10-13']


def trade(p, entry_ts, end_ts, entry, bull=True, flip_price=None, tid=1):
    lo = p.index.searchsorted(pd.Timestamp(entry_ts))
    end = p.index.searchsorted(pd.Timestamp(end_ts))
    return {'trade_id': tid, 'entry_ts': pd.Timestamp(entry_ts), 'bull': bull, 'entry': entry, 'lo': lo, 'end': end,
            'flip_price': entry if flip_price is None else flip_price}


def test_calendar_flatten_times_user_and_engine_variants():
    p = series(WEEK1 + WEEK2)
    cal = WF.build_calendar(p.index, OFF_USER)
    assert list(cal['date']) == [pd.Timestamp('2026-10-09'), pd.Timestamp('2026-10-13')]
    assert cal['flatten_ts'].iloc[0] == pd.Timestamp('2026-10-09 23:15')
    late = series(WEEK1, last_min='23:54')
    assert WF.build_calendar(late.index, OFF_USER)['flatten_ts'].iloc[0] == pd.Timestamp('2026-10-09 23:45')
    assert WF.build_calendar(late.index, OFF_ENGINE)['flatten_ts'].iloc[0] == pd.Timestamp('2026-10-09 23:40')


def test_calendar_friday_holiday_makes_thursday_the_last_day_and_next_open_is_next_session():
    p = series(['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-12'])
    cal = WF.build_calendar(p.index, OFF_USER)
    assert cal['date'].iloc[0] == pd.Timestamp('2026-10-08')
    assert p.index[int(cal['next_open_idx'].iloc[0])] == pd.Timestamp('2026-10-12 09:00')


def test_calendar_flags_weekday_of_last_session():
    p = series(['2026-10-05', '2026-10-06', '2026-10-12'])
    assert WF.build_calendar(p.index, OFF_USER)['weekday'].iloc[0] == 1


def test_trade_spanning_weekend_is_flattened_at_flatten_bar_open_and_gap_is_split_out():
    p = series(WEEK1 + WEEK2)
    f = pd.Timestamp('2026-10-09 23:15')
    fi = p.index.searchsorted(f)
    p.open[fi:] = 103.0
    p.high[fi:] = 103.0
    p.low[fi:] = 103.0
    mon = p.index.searchsorted(pd.Timestamp('2026-10-12 09:00'))
    p.open[mon:] = 106.0
    p.high[mon:] = 106.0
    p.low[mon:] = 106.0
    t = trade(p, '2026-10-06 10:00', '2026-10-13 12:00', 100.0, flip_price=106.0)
    cal = WF.build_calendar(p.index, OFF_USER)
    r = WF.flatten_single(t, p, cal, sl_pct=50, target_pct=None, slip_pct=0.0)
    assert r['flattened'] and r['flat_pts'] == pytest.approx(3.0) and r['gap_pts'] == pytest.approx(3.0) and r['base_pts'] == pytest.approx(6.0)


def test_short_trade_mirrors_and_slippage_reduces_the_flatten_fill():
    p = series(WEEK1 + WEEK2)
    fi = p.index.searchsorted(pd.Timestamp('2026-10-09 23:15'))
    p.open[fi:], p.high[fi:], p.low[fi:] = 97.0, 97.0, 97.0
    t = trade(p, '2026-10-06 10:00', '2026-10-13 12:00', 100.0, bull=False, flip_price=97.0)
    cal = WF.build_calendar(p.index, OFF_USER)
    r0 = WF.flatten_single(t, p, cal, 50, None, 0.0)
    r1 = WF.flatten_single(t, p, cal, 50, None, 0.10)
    assert r0['flat_pts'] == pytest.approx(3.0)
    assert r1['flat_pts'] == pytest.approx(3.0 - 97.0 * 0.001)


def test_short_trade_gap_is_positive_when_price_falls_over_the_weekend():
    p = series(WEEK1 + WEEK2)
    mon = p.index.searchsorted(pd.Timestamp('2026-10-12 09:00'))
    p.open[mon:], p.high[mon:], p.low[mon:] = 96.0, 96.0, 96.0
    t = trade(p, '2026-10-06 10:00', '2026-10-13 12:00', 100.0, bull=False, flip_price=96.0)
    cal = WF.build_calendar(p.index, OFF_USER)
    r = WF.flatten_single(t, p, cal, 50, None, 0.0)
    assert r['flattened'] and r['gap_pts'] == pytest.approx(4.0) and r['flat_pts'] == pytest.approx(0.0)


def test_stop_before_flatten_leaves_trade_unchanged():
    p = series(WEEK1 + WEEK2)
    hit = p.index.searchsorted(pd.Timestamp('2026-10-07 12:00'))
    p.low[hit] = 90.0
    t = trade(p, '2026-10-06 10:00', '2026-10-13 12:00', 100.0)
    cal = WF.build_calendar(p.index, OFF_USER)
    r = WF.flatten_single(t, p, cal, sl_pct=5, target_pct=None, slip_pct=0.0)
    assert not r['flattened'] and r['flat_pts'] == r['base_pts'] == pytest.approx(-5.0)


def test_trade_ending_before_flatten_unchanged_and_trade_after_weekend_unchanged():
    p = series(WEEK1 + WEEK2)
    cal = WF.build_calendar(p.index, OFF_USER)
    inside = trade(p, '2026-10-06 10:00', '2026-10-08 12:00', 100.0)
    after = trade(p, '2026-10-12 10:00', '2026-10-13 12:00', 100.0)
    assert not WF.flatten_single(inside, p, cal, 5, None, 0.0)['flattened']
    assert not WF.flatten_single(after, p, cal, 5, None, 0.0)['flattened']


def test_entry_at_or_after_flatten_on_last_day_is_dropped_but_one_minute_earlier_is_kept():
    p = series(WEEK1 + WEEK2)
    cal = WF.build_calendar(p.index, OFF_USER)
    late = trade(p, '2026-10-09 23:15', '2026-10-13 12:00', 100.0)
    ok = trade(p, '2026-10-09 23:14', '2026-10-13 12:00', 100.0)
    assert WF.flatten_single(late, p, cal, 5, None, 0.0)['dropped']
    r = WF.flatten_single(ok, p, cal, 5, None, 0.0)
    assert not r['dropped'] and r['flattened']


def test_first_weekend_flattens_a_trade_that_spans_two_weekends():
    p = series(WEEK1 + WEEK2 + ['2026-10-14', '2026-10-15', '2026-10-16', '2026-10-19'])
    cal = WF.build_calendar(p.index, OFF_USER)
    t = trade(p, '2026-10-06 10:00', '2026-10-19 12:00', 100.0)
    fi = p.index.searchsorted(pd.Timestamp('2026-10-09 23:15'))
    p.open[fi:] = 104.0
    p.high[fi:] = 104.0
    p.low[fi:] = 104.0
    r = WF.flatten_single(t, p, cal, 50, None, 0.0)
    assert r['flattened'] and r['flat_pts'] == pytest.approx(4.0)


def test_reentry_arm_enters_after_the_buffer_with_a_fresh_entry_and_exits_at_the_flip():
    p = series(WEEK1 + WEEK2)
    fi = p.index.searchsorted(pd.Timestamp('2026-10-09 23:15'))
    p.open[fi:], p.high[fi:], p.low[fi:] = 100.0, 100.0, 100.0
    mon = p.index.searchsorted(pd.Timestamp('2026-10-12 09:15'))
    p.open[mon:], p.high[mon:], p.low[mon:] = 110.0, 110.0, 110.0
    t = trade(p, '2026-10-06 10:00', '2026-10-13 12:00', 100.0, flip_price=115.0)
    cal = WF.build_calendar(p.index, OFF_USER)
    r = WF.reentry_single(t, p, cal, sl_pct=50, target_pct=None, slip_pct=0.0)
    assert r == pytest.approx((115.0 - 110.0) / 110.0 * 100)
    r2 = WF.reentry_single(t, p, cal, sl_pct=50, target_pct=None, slip_pct=0.10)
    assert r2 == pytest.approx((115.0 - 110.0 - 0.11) / 110.0 * 100)


def test_reentry_arm_skips_when_flip_comes_before_the_buffer_ends():
    p = series(WEEK1 + WEEK2)
    t = trade(p, '2026-10-06 10:00', '2026-10-12 09:10', 100.0)
    cal = WF.build_calendar(p.index, OFF_USER)
    assert WF.reentry_single(t, p, cal, 50, None, 0.0) is None


def _prom_row(entry_ts, direction, entry, e1, e2, p1, p2):
    return pd.DataFrame([{'trade_id': 1, 'entry_ts': pd.Timestamp(entry_ts), 'direction': direction, 'entry_price': entry,
                          'lot1_exit_ts': pd.Timestamp(e1), 'lot2_exit_ts': pd.Timestamp(e2), 'lot1_pnl_points': p1, 'lot2_pnl_points': p2,
                          'lot1_exit_reason': 'target1', 'lot2_exit_reason': 'trend_flip'}])


def test_prometheus_only_open_lots_are_flattened():
    p = series(WEEK1 + WEEK2)
    fi = p.index.searchsorted(pd.Timestamp('2026-10-09 23:15'))
    p.open[fi:] = 102.0
    cal = WF.build_calendar(p.index, OFF_USER)
    s = _prom_row('2026-10-06 10:00', 'bullish', 100.0, '2026-10-07 10:00', '2026-10-13 10:00', 1.25, 9.0)
    out = WF.run_prometheus(s, pd.Series(p.open, index=p.index), cal, 0.0).iloc[0]
    assert out['flattened'] and out['base_pct'] == pytest.approx((1.25 + 9.0) / 2) and out['flat_pct'] == pytest.approx((1.25 + 2.0) / 2)


def test_prometheus_both_lots_closed_before_flatten_is_untouched_and_late_entry_dropped():
    p = series(WEEK1 + WEEK2)
    cal = WF.build_calendar(p.index, OFF_USER)
    s = _prom_row('2026-10-06 10:00', 'bearish', 100.0, '2026-10-07 10:00', '2026-10-08 10:00', 1.25, 2.0)
    o = WF.run_prometheus(s, pd.Series(p.open, index=p.index), cal, 0.0).iloc[0]
    assert not o['flattened'] and o['flat_pct'] == pytest.approx(o['base_pct'])
    late = _prom_row('2026-10-09 23:20', 'bearish', 100.0, '2026-10-12 10:00', '2026-10-13 10:00', 1.0, 2.0)
    assert WF.run_prometheus(late, pd.Series(p.open, index=p.index), cal, 0.0).iloc[0]['dropped']


def test_summarise_counts_and_dropped_trades_leave_the_flat_series():
    df = pd.DataFrame({'trade_id': [1, 2, 3], 'entry_ts': pd.to_datetime(['2025-03-01', '2025-04-01', '2026-02-01']), 'entry': [100.0] * 3,
                       'base_pct': [1.0, -2.0, 3.0], 'flat_pct': [0.5, None, 3.0], 'flattened': [True, False, False], 'dropped': [False, True, False],
                       'gap_pct': [0.2, 0.0, 0.0], 'base_reason': ['flip'] * 3})
    cal = pd.DataFrame({'weekday': [4, 1]})
    s = WF.summarise('X', df, cal)
    assert s['flattened'] == 1 and s['dropped'] == 1 and s['weeks_suspect'] == 1 and s['base_total'] == pytest.approx(2.0) and s['flat_total'] == pytest.approx(3.5)
    assert s['dropped_base_sum'] == pytest.approx(-2.0) and s['flattened_flat_sum'] == pytest.approx(0.5)
