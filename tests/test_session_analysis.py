"""Tests for research/session_analysis: clock buckets and the per-trade profit attribution (synthetic bars)."""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'research', 'session_analysis'))
import session_configs as cfg  # noqa: E402
from session_lib import attribute_path, session_of, two_way_of  # noqa: E402


def bars(rows):
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame({'open': [r[1] for r in rows], 'close': [r[2] for r in rows]}, index=idx)


def test_buckets_cover_the_trading_day_without_overlap():
    assert [session_of(h) for h in (9, 12, 13, 16, 17, 23)] == ['Morning', 'Morning', 'Afternoon', 'Afternoon', 'Evening', 'Evening']
    assert session_of(3) == 'Other'
    assert [two_way_of(h) for h in (9, 16, 17, 23)] == ['Day', 'Day', 'Evening', 'Evening']


def test_attribution_sums_to_exit_minus_entry_and_books_each_move_to_its_hour():
    b = bars([('2026-10-06 16:58', 100, 101), ('2026-10-06 16:59', 101, 103), ('2026-10-06 17:00', 103, 102), ('2026-10-06 17:01', 102, 104)])
    p = attribute_path(b, entry_px=100.0, exit_px=104.0, direction_sign=1.0)
    assert abs(p['pts'].sum() - 4.0) < 1e-9
    by = p.groupby('bucket')['pts'].sum()
    assert abs(by['Afternoon'] - 3.0) < 1e-9 and abs(by['Evening'] - 1.0) < 1e-9        # +1 +2 in the afternoon, -1 in the evening, then the exit leg +2 in the evening


def test_short_trade_profit_is_the_price_fall():
    b = bars([('2026-10-06 10:00', 100, 99), ('2026-10-06 10:01', 99, 97), ('2026-10-06 10:02', 97, 96)])
    p = attribute_path(b, entry_px=100.0, exit_px=97.0, direction_sign=-1.0)
    assert abs(p['pts'].sum() - 3.0) < 1e-9 and (p['bucket'] == 'Morning').all()


def test_the_overnight_jump_is_booked_to_the_gap_not_to_a_session():
    b = bars([('2026-10-06 23:28', 100, 101), ('2026-10-06 23:29', 101, 102), ('2026-10-07 09:00', 105, 106), ('2026-10-07 09:01', 106, 106)])
    p = attribute_path(b, entry_px=100.0, exit_px=107.0, direction_sign=1.0)
    gap = p.loc[p['bucket'] == cfg.OVERNIGHT, 'pts'].sum()
    assert abs(gap - 3.0) < 1e-9                                                          # 102 -> 105 open
    assert abs(p['pts'].sum() - 7.0) < 1e-9
    assert abs(p.loc[p['bucket'] == 'Morning', 'pts'].sum() - 2.0) < 1e-9                 # 105 -> 106 close, then 106 -> 107 exit leg is in the exit bar's bucket too


def test_a_flip_exit_at_the_open_of_the_last_bar_does_not_earn_that_bars_own_move():
    b = bars([('2026-10-06 16:58', 100, 101), ('2026-10-06 16:59', 101, 102), ('2026-10-06 17:00', 103, 110)])      # exits at the 17:00 open (103); the 17:00 bar's rise to 110 is not held
    p = attribute_path(b, entry_px=100.0, exit_px=103.0, direction_sign=1.0)
    assert abs(p['pts'].sum() - 3.0) < 1e-9
    assert abs(p.loc[p['bucket'] == 'Evening', 'pts'].sum() - 1.0) < 1e-9                  # only the 102 -> 103 exit leg lands in the evening


def test_weight_scales_every_piece():
    b = bars([('2026-10-06 10:00', 100, 102), ('2026-10-06 10:01', 102, 102)])
    p = attribute_path(b, entry_px=100.0, exit_px=104.0, direction_sign=1.0, weight=0.5)
    assert abs(p['pts'].sum() - 2.0) < 1e-9


def test_a_trade_closed_at_the_next_days_open_books_the_jump_to_the_overnight_gap():
    b = bars([('2026-10-06 23:28', 100, 101), ('2026-10-06 23:29', 101, 102), ('2026-10-07 09:00', 105, 106)])      # exits at the 09:00 open, 105
    p = attribute_path(b, entry_px=100.0, exit_px=105.0, direction_sign=1.0)
    assert abs(p.loc[p['bucket'] == cfg.OVERNIGHT, 'pts'].sum() - 3.0) < 1e-9
    assert abs(p['pts'].sum() - 5.0) < 1e-9 and p.loc[p['bucket'] == 'Morning', 'pts'].sum() == 0.0
