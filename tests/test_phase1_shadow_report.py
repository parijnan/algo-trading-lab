"""research/fyers_mcx_validation/phase1_shadow_report.py: the log parser, the latency and head-to-head statistics, and the flip comparison against
the boundary lines an engine acted on, on synthetic data."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'research' / 'fyers_mcx_validation'))
import phase1_shadow_report as rpt                                          # noqa: E402
from hestia_core.history import resample_1m                                 # noqa: E402
from hestia_core.indicators import compute_st                               # noqa: E402
from hestia_core.mcx_market import closing_time_str                         # noqa: E402

LOG = """2026-10-06 09:15:04,172 INFO hestia_live_data: CRUDEOILM19OCT26FUT: 15m boundary 09:00 close=8822.00 ST=8923.50 trend=bearish flip=True
2026-10-06 09:30:04,101 INFO hestia_live_data: CRUDEOILM19OCT26FUT: 15m boundary 09:15 close=8800.00 ST=8900.25 trend=bearish flip=False
2026-10-06 09:30:09,499 INFO hestia_live_data: SILVERMIC30NOV26FUT: 15m boundary 09:15 close=228250.00 ST=None trend=bullish flip=False
2026-10-06 09:30:09,500 INFO hestia_alerts: [selene] something unrelated close=1 ST=2 trend=x flip=True
"""


def test_the_boundary_lines_the_engines_acted_on_are_parsed_and_noise_is_ignored():
    b = rpt.parse_boundaries(LOG)
    assert list(b['symbol']) == ['CRUDEOILM19OCT26FUT', 'CRUDEOILM19OCT26FUT', 'SILVERMIC30NOV26FUT']
    assert list(b['bar_start']) == [pd.Timestamp('2026-10-06 09:00'), pd.Timestamp('2026-10-06 09:15'), pd.Timestamp('2026-10-06 09:15')]
    assert list(b['flip']) == [True, False, False] and b['st'].iloc[0] == 8923.5 and np.isnan(b['st'].iloc[2])


def test_angel_symbols_map_to_fyers_symbols():
    assert rpt.angel_to_fyers('SILVERMIC30NOV26FUT') == 'MCX:SILVERMIC26NOVFUT'
    assert rpt.angel_to_fyers('NATGASMINI27OCT26FUT') == 'MCX:NATGASMINI26OCTFUT'
    assert rpt.angel_to_fyers('NIFTY') is None


def poll(tick, token, side, boundary, present, after, attempts=1, exhausted=''):
    return dict(date='2026-10-06', tick=tick, token=token, symbol='MCX:X', side=side, boundary=boundary, ok=int(present), kind='ok',
                attempts=attempts, after_s=after, rows=5, expected_present=int(present), exhausted=exhausted, note='')


def test_latency_percentiles_are_per_side_and_boundary_minutes_are_split_out():
    rows = [poll(f'10:{m:02d}', 't', 'angel', int(m % 15 == 0), True, 1.0 + m / 100) for m in range(0, 30)]
    rows += [poll(f'10:{m:02d}', 't', 'fyers', int(m % 15 == 0), True, 0.5) for m in range(0, 30)]
    out = rpt.latency_summary(pd.DataFrame(rows)).set_index(['side', 'minutes'])
    assert out.loc[('fyers', 'all'), 'p50_s'] == 0.5 and out.loc[('fyers', 'all'), 'polls'] == 30
    assert out.loc[('angel', 'boundary'), 'polls'] == 2 and out.loc[('angel', 'all'), 'got_minute_pct'] == 100.0
    assert out.loc[('angel', 'all'), 'p50_s'] > out.loc[('fyers', 'all'), 'p50_s']


def test_head_to_head_counts_rescues_losses_and_who_was_earlier():
    rows = [poll('t1', 'a', 'angel', 1, True, 2.0), poll('t1', 'a', 'fyers', 1, True, 1.0),            # fyers earlier
            poll('t2', 'a', 'angel', 0, False, None, attempts=5, exhausted=1), poll('t2', 'a', 'fyers', 0, True, 1.2),   # a rescue
            poll('t3', 'a', 'angel', 0, True, 1.0), poll('t3', 'a', 'fyers', 0, False, None),            # fyers missed it
            poll('t4', 'a', 'angel', 0, True, 1.0), poll('t4', 'a', 'fyers', 0, True, 3.0)]              # angel earlier
    h = rpt.head_to_head(pd.DataFrame(rows))
    assert h['ticks_compared'] == 4 and h['angel_exhausted'] == 1 and h['angel_missing_minute'] == 1
    assert h['fyers_had_it_when_angel_did_not'] == 1 and h['fyers_missing_when_angel_had_it'] == 1
    assert h['fyers_earlier_pct'] == 50.0, 'of the two ticks both sources had, Fyers was earlier on one'


def synthetic_minutes(days=22, seed=7):
    rng = np.random.default_rng(seed)
    idx = [pd.Timestamp('2026-09-14') + pd.Timedelta(days=d, hours=9, minutes=m) for d in range(days) for m in range(0, 14 * 60 + 30)]
    idx = [t for t in idx if t.dayofweek < 5]
    close = 100 + np.cumsum(rng.normal(0, 0.15, len(idx)))
    return pd.DataFrame({'time_stamp': idx, 'open': close - 0.02, 'high': close + 0.1, 'low': close - 0.1, 'close': close, 'volume': 10})


def engine_boundaries(minutes, period, mult, day):
    """What an engine would have logged for `day` from this very series: the reference the comparison is against."""
    end = minutes['time_stamp'].max() + timedelta(minutes=1)
    bars = compute_st(resample_1m(minutes, 15, end, '09:00', closing_time_str), period, mult)
    b = bars[bars['time_stamp'] >= day].dropna(subset=['supertrend'])
    return pd.DataFrame({'symbol': 'X', 'bar_start': b['time_stamp'], 'close': b['close'], 'st': b['supertrend'],
                         'trend': b['trend'].map(lambda v: 'bullish' if v else 'bearish'), 'flip': b['trend_flip'].astype(bool)})


def test_identical_series_produce_zero_mismatches_and_the_same_flips():
    m = synthetic_minutes()
    day = pd.Timestamp('2026-10-05')
    b = engine_boundaries(m, 10, 2.0, day)
    out = rpt.compare_flips(m, b, 10, 2.0, day)
    assert out['boundaries_compared'] == len(b) > 20
    assert out['trend_mismatch'] == 0 and out['close_max_abs_diff'] == pytest.approx(0, abs=1e-9) and out['st_max_abs_diff'] == pytest.approx(0, abs=1e-9)
    assert out['flips_on_same_bar'] == out['engine_flips'] == out['fyers_flips'] and not out['engine_flips_missing_in_fyers']
    assert out['engine_flips'] > 0, 'the day must contain a flip or the comparison is vacuous'


def test_a_flip_the_engine_acted_on_but_the_fyers_series_lacks_is_named():
    m = synthetic_minutes()
    day = pd.Timestamp('2026-10-05')
    b = engine_boundaries(m, 10, 2.0, day)
    acted = b[b['flip']].iloc[0]['bar_start']
    b.loc[b['bar_start'] == acted + pd.Timedelta(minutes=45), 'flip'] = True            # the engine "flipped" where Fyers did not
    out = rpt.compare_flips(m, b, 10, 2.0, day)
    assert str(acted + pd.Timedelta(minutes=45)) in out['engine_flips_missing_in_fyers']


def test_a_trend_disagreement_is_counted():
    m = synthetic_minutes()
    day = pd.Timestamp('2026-10-05')
    b = engine_boundaries(m, 10, 2.0, day)
    b.loc[b.index[3], 'trend'] = 'bearish' if b.loc[b.index[3], 'trend'] == 'bullish' else 'bullish'
    assert rpt.compare_flips(m, b, 10, 2.0, day)['trend_mismatch'] == 1


def test_the_settled_value_of_a_minute_is_its_last_sighting_and_the_provisional_rate_is_measured():
    df = pd.DataFrame([
        {'time_stamp': pd.Timestamp('2026-10-05 10:00'), 'open': 1, 'high': 2, 'low': 0, 'close': 1.5, 'volume': 5, 'seen_at': '2026-10-05T10:01:00.07'},
        {'time_stamp': pd.Timestamp('2026-10-05 10:00'), 'open': 1, 'high': 2, 'low': 0, 'close': 1.9, 'volume': 9, 'seen_at': '2026-10-05T10:02:00.05'},
        {'time_stamp': pd.Timestamp('2026-10-05 10:01'), 'open': 3, 'high': 4, 'low': 3, 'close': 3.5, 'volume': 7, 'seen_at': '2026-10-05T10:02:00.06'}])
    s = rpt.settled_minutes(df)
    assert list(s['close']) == [1.9, 3.5] and list(s['volume']) == [9, 7]
    p = rpt.provisional_stats(df)
    assert p == {'minutes': 2, 'changed_after_first_seen': 1, 'changed_pct': 50.0, 'close_changed_pct': 50.0}
