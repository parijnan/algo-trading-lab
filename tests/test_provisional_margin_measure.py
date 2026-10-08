import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'research' / 'provisional_margin'))
import margin_configs as cfg  # noqa: E402
import measure_margins as mm  # noqa: E402


def minutes(start, n, price=100.0, volume=10, per_minute=None):
    """n one-minute rows from `start`; open = close = price, high = price + 1, low = price - 1 unless overridden per minute index."""
    idx = pd.date_range(start, periods=n, freq='min')
    df = pd.DataFrame({'time_stamp': idx, 'open': price, 'high': price + 1.0, 'low': price - 1.0, 'close': price, 'volume': volume})
    for i, kw in (per_minute or {}).items():
        for col, v in kw.items():
            df.loc[i, col] = v
    return df


# ---- the bound U ------------------------------------------------------------------------------------------------------------------

def test_final_minute_range_is_the_bound_when_it_exceeds_the_gap():
    m = minutes('2026-02-02 09:00', 16, 200.0, per_minute={14: {'high': 205.0, 'low': 199.0, 'close': 201.0}, 15: {'open': 201.5, 'high': 202.0, 'low': 201.0, 'close': 201.5}})
    b = mm.build_bars(m)
    row = b.loc[pd.Timestamp('2026-02-02 09:00')]
    assert bool(row['final_traded']) and row['final_range'] == 6.0 and row['gap_next'] == pytest.approx(0.5)
    assert row['U_pct'] == pytest.approx(6.0 / 201.0 * 100)             # max(range 6, gap 0.5) as a percent of the bar's close


def test_the_move_to_the_next_open_is_the_bound_when_it_exceeds_the_range():
    m = minutes('2026-02-02 09:00', 16, 200.0, per_minute={14: {'high': 200.5, 'low': 199.5, 'close': 200.0}, 15: {'open': 203.0, 'high': 203.5, 'low': 202.5, 'close': 203.0}})
    row = mm.build_bars(m).loc[pd.Timestamp('2026-02-02 09:00')]
    assert row['final_range'] == 1.0 and row['gap_next'] == 3.0
    assert row['U_pct'] == pytest.approx(3.0 / 200.0 * 100)


def test_a_next_row_far_after_the_boundary_does_not_count_as_a_gap():
    m = pd.concat([minutes('2026-02-02 09:00', 15, 200.0), minutes('2026-02-02 09:30', 3, 230.0)], ignore_index=True)        # next row 15 minutes late
    row = mm.build_bars(m).loc[pd.Timestamp('2026-02-02 09:00')]
    assert row['gap_next'] == 0.0 and row['U_pct'] == pytest.approx(2.0 / 200.0 * 100)


def test_a_final_minute_with_no_trade_is_excluded_from_the_bound():
    m = minutes('2026-02-02 09:00', 16, 200.0, per_minute={14: {'volume': 0}})
    row = mm.build_bars(m).loc[pd.Timestamp('2026-02-02 09:00')]
    assert not bool(row['final_traded']) and np.isnan(row['U_pct'])


def test_a_missing_final_minute_row_is_excluded_too():
    m = minutes('2026-02-02 09:00', 16, 200.0).drop(index=14)
    row = mm.build_bars(m).loc[pd.Timestamp('2026-02-02 09:00')]
    assert not bool(row['final_traded']) and np.isnan(row['U_pct']) and row['n_min'] == 14


def test_bars_sit_on_the_clock():
    m = minutes('2026-02-02 09:07', 30, 100.0)
    assert [t.minute for t in mm.build_bars(m).index] == [0, 15, 30]


# ---- r, d and the populations ------------------------------------------------------------------------------------------------------

def table(rows):
    """Hand-made bar table with the columns `rule` and `grid_table` read."""
    t = pd.DataFrame(rows)
    for c, v in {'risk_bar': False, 'flip_bar': False, 'r_pct': np.nan, 'dist_pct': np.nan}.items():
        if c not in t:
            t[c] = v
    t['risk_bar'], t['flip_bar'] = t['risk_bar'].eq(True), t['flip_bar'].eq(True)
    t['start'] = pd.to_datetime(t['start'])
    return t


def test_r_is_u_minus_d_floored_at_zero_on_non_flip_bars_only():
    n = 40
    idx = pd.date_range('2026-02-02 09:00', periods=n, freq='15min')
    close = np.linspace(100, 110, n)
    bars = pd.DataFrame({'open': close - 0.1, 'high': close + 0.5, 'low': close - 0.5, 'close': close, 'volume': 100, 'n_min': 15, 'final_traded': True,
                         'final_range': 1.0, 'gap_next': 0.0, 'U_pct': 0.4}, index=idx)
    bars.index.name = 'win'
    bars.loc[idx[30], 'close'] = 80.0                                            # a crash bar: the real bar flips the trend
    b = mm.add_signal(bars, 10, 2.0)
    flip = b[b['flip']]
    assert len(flip) >= 1 and not flip['risk_bar'].any() and flip['r_pct'].isna().all()      # real flips are never in the risk population
    ok = b[b['risk_bar']]
    assert len(ok) > 10
    expected = (ok['U_pct'] - ok['dist_pct']).clip(lower=0)
    assert np.allclose(ok['r_pct'], expected)
    assert (ok['r_pct'] >= 0).all()
    assert (ok['dist_pct'] > ok['U_pct']).any() and (ok['r_pct'] == 0).any()       # far from the line: zero margin needed


def test_a_bar_without_a_traded_final_minute_is_not_a_risk_bar():
    n = 40
    idx = pd.date_range('2026-02-02 09:00', periods=n, freq='15min')
    close = np.linspace(100, 110, n)
    bars = pd.DataFrame({'open': close, 'high': close + 0.5, 'low': close - 0.5, 'close': close, 'volume': 100, 'n_min': 15, 'final_traded': True,
                         'final_range': 1.0, 'gap_next': 0.0, 'U_pct': 0.4}, index=idx)
    bars.index.name = 'win'
    bars.loc[idx[20], ['final_traded', 'U_pct']] = [False, np.nan]
    b = mm.add_signal(bars, 10, 2.0)
    assert not bool(b.loc[20, 'risk_bar'])


# ---- the rule ----------------------------------------------------------------------------------------------------------------------

def test_ceil_to_grid():
    assert mm.ceil_to_grid(0.0) == 0.0
    assert mm.ceil_to_grid(0.05 + 1e-13) == 0.05                  # float noise does not push it up a step
    assert mm.ceil_to_grid(0.0500001) == 0.06                      # a real excess does
    assert mm.ceil_to_grid(0.051) == 0.06
    assert mm.ceil_to_grid(0.3) == 0.3


def test_m_star_is_the_fit_period_maximum_rounded_up_and_checked_out_of_sample():
    t = table([
        {'start': '2025-06-01', 'risk_bar': True, 'r_pct': 0.031}, {'start': '2025-07-01', 'risk_bar': True, 'r_pct': 0.12},
        {'start': '2025-08-01', 'risk_bar': True, 'r_pct': 0.0},   {'start': '2026-02-01', 'risk_bar': True, 'r_pct': 0.10},
        {'start': '2025-06-02', 'flip_bar': True, 'dist_pct': 0.5}, {'start': '2026-03-02', 'flip_bar': True, 'dist_pct': 0.05},
        {'start': '2026-04-02', 'flip_bar': True, 'dist_pct': 0.4},
    ])
    r = mm.rule(t)
    assert r['r_fit_max'] == pytest.approx(0.12) and r['m_star'] == pytest.approx(0.12) and r['oos_violations_at_m_star'] == 0
    assert r['m_star_basis'] == 'fit period'
    assert r['coverage_at_m_star'] == pytest.approx(2 / 3)                  # clearance 0.5 and 0.4 exceed 0.12, 0.05 does not
    assert r['coverage_at_m_star_oos'] == pytest.approx(0.5)


def test_an_out_of_sample_violation_widens_the_margin_to_all_history_and_says_so():
    t = table([{'start': '2025-06-01', 'risk_bar': True, 'r_pct': 0.05}, {'start': '2026-02-01', 'risk_bar': True, 'r_pct': 0.20}])
    r = mm.rule(t)
    assert r['m_star_fit'] == pytest.approx(0.05) and r['oos_violations_at_m_star'] == 1
    assert r['m_star'] == pytest.approx(0.20) and 'out-of-sample check failed' in r['m_star_basis']


def test_a_flip_never_enters_the_margin_even_with_a_large_r():
    t = table([{'start': '2025-06-01', 'risk_bar': True, 'r_pct': 0.02}, {'start': '2025-06-02', 'flip_bar': True, 'r_pct': 9.0, 'dist_pct': 0.3}])
    assert mm.rule(t)['m_star'] == pytest.approx(0.02)


def test_grid_table_counts_coverage_and_risk_bars_over_each_margin():
    t = table([{'start': '2025-06-01', 'risk_bar': True, 'r_pct': 0.03}, {'start': '2025-06-02', 'risk_bar': True, 'r_pct': 0.07},
               {'start': '2025-06-03', 'flip_bar': True, 'dist_pct': 0.05}, {'start': '2025-06-04', 'flip_bar': True, 'dist_pct': 0.20}])
    g = mm.grid_table(t, step=0.01, top=0.10).set_index('margin_pct')
    assert g.loc[0.0, 'risk_bars_over'] == 2 and g.loc[0.03, 'risk_bars_over'] == 1 and g.loc[0.07, 'risk_bars_over'] == 0
    assert g.loc[0.0, 'coverage'] == 1.0 and g.loc[0.05, 'coverage'] == 0.5 and g.loc[0.20 if 0.20 in g.index else 0.10, 'coverage'] == 0.5


def test_the_measurement_uses_each_engines_own_supertrend_settings():
    assert cfg.INSTRUMENTS['SILVERMIC'][1:] == (10, 2.5) and cfg.INSTRUMENTS['GOLDPETAL'][1:] == (10, 3.5)
    assert cfg.INSTRUMENTS['NATGASMINI'][1:] == (10, 3.0) and cfg.INSTRUMENTS['CRUDEOILM'][1:] == (10, 2.5)


def test_distance_to_the_line_is_positive_in_a_downtrend_too():
    n = 40
    idx = pd.date_range('2026-02-02 09:00', periods=n, freq='15min')
    close = np.linspace(110, 100, n)                                              # falling: the supertrend sits ABOVE the close
    bars = pd.DataFrame({'open': close + 0.1, 'high': close + 0.5, 'low': close - 0.5, 'close': close, 'volume': 100, 'n_min': 15, 'final_traded': True,
                         'final_range': 1.0, 'gap_next': 0.0, 'U_pct': 0.4}, index=idx)
    bars.index.name = 'win'
    b = mm.add_signal(bars, 10, 2.0)
    ok = b[b['risk_bar']]
    after = ok[ok['start'] > b[b['flip']]['start'].iloc[0]]                       # after the first flip the trend is bearish
    assert len(after) > 10 and (after['prev_st'] > after['close']).all()           # the line is above the price, so a signed gap would be negative
    assert (ok['dist_pct'] > 0).all()
    assert np.allclose(ok['r_pct'], (ok['U_pct'] - ok['dist_pct']).clip(lower=0))
