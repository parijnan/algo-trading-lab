import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'selene_backtest'))
import selene_configs as configs  # noqa: E402
import exit_single_target_selene as st  # noqa: E402
from exit_calib_selene import PriceSeries  # noqa: E402


def series(bars):
    """bars: list of (open, high, low) one per minute starting 2026-02-02 09:00; the first bar of the session is exempt from stop and target checks."""
    idx = pd.date_range('2026-02-02 09:00', periods=len(bars), freq='min')
    df = pd.DataFrame(bars, columns=['open', 'high', 'low'], index=idx)
    df['close'] = df['open']
    return PriceSeries(df), idx


def trade(idx, bull, entry, lo, end, flip=np.nan):
    return {'trade_id': 1, 'entry_ts': idx[lo], 'bull': bull, 'entry': entry, 'lo': lo, 'end': end, 'flip_price': flip}


def test_one_lot_target_hit_pays_the_target_once_not_twice():
    p, idx = series([(100, 100, 100), (100, 101, 99.5), (101, 104, 100.5), (103, 103, 103)])
    sim = st.run_one_lot([trade(idx, True, 100.0, 1, 3)], p, sl=3.0, target=3.0)
    assert sim['pnl_pts'].iloc[0] == pytest.approx(3.0)             # 3% of 100, one lot (the two-lot simulator would say 6)
    assert sim['pnl_pct'].iloc[0] == pytest.approx(3.0)
    assert sim['lot1_reason'].iloc[0] == 'target1'


def test_disabled_target_is_the_stop_only_control():
    p, idx = series([(100, 100, 100), (100, 101, 99.5), (101, 150, 100.5), (103, 103, 103)])
    sim = st.run_one_lot([trade(idx, True, 100.0, 1, 3, flip=102.0)], p, sl=3.0, target=configs.DISABLED_PCT)
    assert sim['pnl_pts'].iloc[0] == pytest.approx(2.0) and sim['lot1_reason'].iloc[0] == 'trend_flip'      # no target, no stop: the flip exits
    p2, idx2 = series([(100, 100, 100), (100, 101, 96), (96, 97, 95), (96, 96, 96)])
    sim2 = st.run_one_lot([trade(idx2, True, 100.0, 1, 3, flip=96.0)], p2, sl=3.0, target=configs.DISABLED_PCT)
    assert sim2['pnl_pts'].iloc[0] == pytest.approx(-3.0) and sim2['lot1_reason'].iloc[0] == 'stop_loss'


def test_a_bearish_trade_mirrors_the_bullish_one():
    p, idx = series([(100, 100, 100), (100, 100.5, 99), (99, 99.2, 96), (97, 97, 97)])
    sim = st.run_one_lot([trade(idx, False, 100.0, 1, 3)], p, sl=3.0, target=3.0)
    assert sim['pnl_pts'].iloc[0] == pytest.approx(3.0)


def grid_frame():
    """Four cells: two stop-only, two with a target; pre-split and full-window winners deliberately differ."""
    rows = [
        {'sl_pct': 1.0, 'target_pct': 1000.0, 'target_on': False, 'pre_calmar_pct': 5.0, 'pre_calmar': 1.0, 'post_pnl_rs': 10.0},
        {'sl_pct': 3.0, 'target_pct': 1000.0, 'target_on': False, 'pre_calmar_pct': 4.0, 'pre_calmar': 9.0, 'post_pnl_rs': 99.0},
        {'sl_pct': 1.0, 'target_pct': 8.0, 'target_on': True, 'pre_calmar_pct': 7.0, 'pre_calmar': 2.0, 'post_pnl_rs': 20.0},
        {'sl_pct': 3.0, 'target_pct': 8.0, 'target_on': True, 'pre_calmar_pct': 6.0, 'pre_calmar': 8.0, 'post_pnl_rs': 88.0},
    ]
    for r in rows:
        r.update({'pre_pnl_rs': 1.0, 'post_dd_rs': -1.0, 'post_calmar': 1.0, 'post_calmar_pct': 1.0, 'post_win_pct': 1.0, 'target_hit_rate_pct': 0.0,
                  'total_pnl_rs': 1.0, 'calmar': 1.0, 'calmar_pct': 1.0})
    return pd.DataFrame(rows)


def test_pick_chooses_the_best_cell_within_the_requested_shape():
    df = grid_frame()
    assert st.pick(df, 'pre_calmar_pct', with_target=False)['sl_pct'] == 1.0
    assert st.pick(df, 'pre_calmar_pct', with_target=True)['sl_pct'] == 1.0 and st.pick(df, 'pre_calmar_pct', with_target=True)['target_pct'] == 8.0
    assert st.pick(df, 'pre_calmar', with_target=False)['sl_pct'] == 3.0


def test_walkforward_reads_the_post_period_of_the_cell_chosen_on_the_pre_period():
    rows = st.walkforward(grid_frame(), 2.5)
    by = {(r['selected_by'], r['shape']): r for r in rows}
    r = by[('pre_calmar_pct', 'stop only')]
    assert r['sl_pct'] == 1.0 and r['post_pnl_rs'] == 10.0          # chosen on pre (SL 1.0), NOT the cell that is best out of sample (SL 3.0, 99)
    r = by[('pre_calmar', 'stop + one target')]
    assert (r['sl_pct'], r['target_pct'], r['post_pnl_rs']) == (3.0, 8.0, 88.0)
    assert by[('pre_calmar', 'stop only')]['target_pct'] is None
    assert len(rows) == 4
