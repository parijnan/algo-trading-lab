"""Prometheus production-parity backtest (prometheus_backtest/phase3_fyers/parity_backtest_prometheus.py, 2026-10-16)."""

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'prometheus_backtest' / 'phase3_fyers'))
sys.path.insert(0, str(ROOT))
import configs_p3 as configs  # noqa: E402
import parity_backtest_prometheus as pb  # noqa: E402
from prometheus_engine.engine_configs import DEFAULT  # noqa: E402


def test_the_parity_parameters_are_the_live_engines():
    assert configs.PARITY_ST_MULTIPLIER == DEFAULT.st_multiplier and configs.ST_PERIOD == DEFAULT.st_period
    assert (configs.PARITY_SL_PCT, configs.PARITY_T1_PCT, configs.PARITY_T2_PCT) == (DEFAULT.sl_pct, DEFAULT.target1_pct, DEFAULT.target2_flat_pct)
    assert configs.LOTS_PER_LEG == DEFAULT.lots_per_leg and configs.MIN_ENTRY_BUFFER_MIN == DEFAULT.min_entry_buffer_min
    assert configs.NO_EXIT_BEFORE_BUFFER_MIN == DEFAULT.no_exit_before_buffer_min and configs.BASIS_MAX_GAP_MIN == DEFAULT.basis_tolerance_min
    assert pb._p3.TENDER_ROLL_TRADING_DAYS == DEFAULT.roll_window_days


def contract(rows):
    """rows: (minute offset from 09:00 on 2026-01-05, open, high, low, close)."""
    base = pd.Timestamp('2026-01-05 09:00')
    df = pd.DataFrame([{'time_stamp': base + pd.Timedelta(minutes=m), 'open': o, 'high': h, 'low': l, 'close': c, 'volume': 1} for m, o, h, l, c in rows])
    return pb.Contract(pd.Timestamp('2026-01-30').date(), df)


T0 = pd.Timestamp('2026-01-05 09:00')
END = pd.Timestamp('2026-01-05 12:00')


def test_a_stop_in_the_same_bar_as_a_target_wins():
    c = contract([(0, 100, 100, 100, 100), (1, 100, 106, 94, 100)])               # bar 1 reaches both the stop (95) and T1 (105)
    assert c.scan_exit('bullish', 95, 105, 110, T0, END)[2] == 'stop_loss'


def test_the_sessions_first_minute_never_triggers_anything():
    c = contract([(0, 100, 120, 80, 100), (1, 100, 101, 99, 100)])
    assert c.scan_exit('bullish', 95, 105, 110, T0, END) is None


def test_target_one_comes_before_target_two_and_the_scan_can_resume_for_the_second():
    c = contract([(0, 100, 100, 100, 100), (1, 100, 112, 99, 111)])               # one bar reaches T1 (105) and T2 (110)
    first = c.scan_exit('bullish', 95, 105, 110, T0, END)
    assert first[2] == 'target1' and first[1] == 105
    second = c.scan_exit('bullish', 95, None, 110, first[0], END)                    # lot 1 is booked, resume from the same bar
    assert second[2] == 'target2' and second[1] == 110


def test_fills_gap_through_the_level_at_the_bars_open():
    c = contract([(0, 100, 100, 100, 100), (1, 90, 91, 89, 90), (2, 112, 113, 111, 112)])
    stop = c.scan_exit('bullish', 95, 105, 110, T0, END)
    assert stop[2] == 'stop_loss' and stop[1] == 90                                  # adverse gap: the open, not the level
    tgt = c.scan_exit('bearish', 120, 95, 90, T0, pd.Timestamp('2026-01-05 09:02'))
    assert tgt[2] == 'target1' and tgt[1] == 90                                      # favourable gap: the open


def test_a_bearish_position_mirrors_the_levels():
    c = contract([(0, 100, 100, 100, 100), (1, 100, 101, 94, 95)])
    assert c.scan_exit('bearish', 105, 96, 90, T0, END)[2] == 'target1'
