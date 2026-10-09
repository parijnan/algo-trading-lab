import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'research' / 'early_mfe'))
import early_mfe_study as S  # noqa: E402


def rec(e=100.0, o=None, h=None, l=None, i_x=50, pct=1.0, lot_exits=None, lot_pts=None):
    n = 10
    o = np.full(n + 1, e) if o is None else np.asarray(o, float)
    h = np.full(n, e) if h is None else np.asarray(h, float)
    l = np.full(n, e) if l is None else np.asarray(l, float)
    return S.make_record(e, pd.Timestamp('2026-01-05'), o, h, l, i_x, pct, lot_exits=lot_exits, lot_pts=lot_pts)


def test_a_trade_already_out_before_the_checkpoint_is_excluded():
    assert S.alive_at(rec(i_x=5), 5) and not S.alive_at(rec(i_x=4), 5)          # exit at bar 5 happens after the checkpoint at the open of bar 5


def test_features_use_only_bars_before_the_checkpoint():
    h = [101, 102, 103, 120, 120, 120, 120, 120, 120, 120]      # a spike at bar 3
    r = rec(h=h, l=[99] * 10, o=[100, 100, 100, 100, 100.5] + [100] * 6)
    mfe, mae, u = S.checkpoint_features(r, 3)
    assert mfe == pytest.approx(3.0) and mae == pytest.approx(1.0) and u == pytest.approx(0.0)       # the spike at bar 3 is not known yet
    assert S.checkpoint_features(r, 4)[0] == pytest.approx(20.0)                                      # known one checkpoint later
    assert S.checkpoint_features(r, 4)[2] == pytest.approx(0.5)


def test_rule_exits_at_the_open_of_the_checkpoint_bar_and_leaves_other_trades_alone():
    r = rec(o=[100, 100, 100, 99, 99] + [100] * 6, pct=2.0)       # at checkpoint 4 the price is 99: underwater
    assert S.rule_outcome(r, 4, 'u', 0) == (pytest.approx(-1.0), True)
    up = rec(o=[100, 100, 100, 101, 101] + [100] * 6, pct=2.0)
    assert S.rule_outcome(up, 4, 'u', 0) == (2.0, False)
    gone = rec(o=[100, 100, 100, 99, 99] + [100] * 6, i_x=2, pct=-3.0)
    assert S.rule_outcome(gone, 4, 'u', 0) == (-3.0, False)       # already out: the baseline result stands


def test_mfe_rule_triggers_below_the_threshold_only():
    low = rec(h=[100.2] * 10, o=[100] * 11, pct=1.5)
    assert S.rule_outcome(low, 5, 'mfe', 0.5)[1] and not S.rule_outcome(low, 5, 'mfe', 0.1)[1]


def test_prometheus_lot_already_booked_keeps_its_result_and_the_other_exits():
    r = rec(o=[100, 100, 100, 99, 99] + [100] * 6, pct=1.0, lot_exits=[2, 40], lot_pts=[1.25, 4.0])      # lot 1 booked its target before bar 4
    pct, trig = S.rule_outcome(r, 4, 'u', 0)
    assert trig and pct == pytest.approx((1.25 + (99 - 100)) / 2 / 100 * 100)


def test_partial_correlation_removes_the_current_pnl_effect():
    rng = np.random.default_rng(1)
    z = rng.normal(size=500)
    x = z + rng.normal(scale=0.1, size=500)            # x is almost z
    y = z + rng.normal(scale=0.5, size=500)            # y depends on z only
    assert abs(S.partial_corr(x, z, y)) < 0.15 and np.corrcoef(x, y)[0, 1] > 0.7
