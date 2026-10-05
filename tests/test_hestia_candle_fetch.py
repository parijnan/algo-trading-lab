"""Candle fetching through the gateway (against the scripted double) and the SharedFeed adapter."""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from hestia_fake_helpers import minutes_frame
from smartapi_double import CandleSmartConnect, FakeClock
from hestia_core.candle_fetch import FetchConfig, fetch_history, fetch_one_minute_window
from hestia_core.feed_port import FeedPort, SharedFeedAdapter
from hestia_core.gateway import BrokerGateway

FRAMES = {'T1': minutes_frame(100.0, seed=3)}
NOW = datetime(2026, 9, 3, 12, 0)


def rig(now=NOW):
    clock = FakeClock()
    sc = CandleSmartConnect(lambda: now, FRAMES)
    return BrokerGateway(sc, clock=clock.monotonic, sleep=clock.sleep), sc, clock


def test_a_window_fetch_returns_naive_rows_within_the_window():
    gw, sc, clock = rig()
    df = fetch_one_minute_window(gw, 'T1', datetime(2026, 9, 3, 11, 55), datetime(2026, 9, 3, 12, 0), FetchConfig(), clock.sleep)
    assert df['time_stamp'].tolist() == [pd.Timestamp(f'2026-09-03 11:{m}') for m in range(55, 60)]
    assert df['time_stamp'].dt.tz is None and df['close'].dtype.kind == 'f'


def test_a_successful_fetch_with_nothing_new_is_an_empty_frame_not_a_failure():
    gw, sc, clock = rig()
    df = fetch_one_minute_window(gw, 'T1', datetime(2026, 9, 3, 12, 30), datetime(2026, 9, 3, 12, 35), FetchConfig(), clock.sleep)
    assert df is not None and df.empty


def test_a_failed_burst_returns_none_after_the_configured_attempts_with_pauses():
    gw, sc, clock = rig()
    sc.fail = lambda *a: True
    cfg = FetchConfig(inner_attempts=5, inner_interval_s=1.0)
    assert fetch_one_minute_window(gw, 'T1', datetime(2026, 9, 3, 11, 55), datetime(2026, 9, 3, 12, 0), cfg, clock.sleep) is None
    assert len(sc.calls) == 5 and clock.t >= 4.0


def test_a_burst_that_recovers_partway_returns_the_data():
    gw, sc, clock = rig()
    n = {'calls': 0}

    def fail(*a):
        n['calls'] += 1
        return n['calls'] <= 2
    sc.fail = fail
    df = fetch_one_minute_window(gw, 'T1', datetime(2026, 9, 3, 11, 55), datetime(2026, 9, 3, 12, 0), FetchConfig(), clock.sleep)
    assert len(df) == 5 and len(sc.calls) == 3


def test_history_is_fetched_in_day_chunks_and_trimmed_to_the_window():
    gw, sc, clock = rig(now=datetime(2026, 9, 4, 0, 0))
    df = fetch_history(gw, 'T1', datetime(2026, 9, 1, 9, 0), datetime(2026, 9, 3, 23, 30), FetchConfig(chunk_days=2), clock.sleep)
    assert [(c[1], c[2]) for c in sc.calls] == [('2026-09-01 09:00', '2026-09-02 23:30'), ('2026-09-03 09:00', '2026-09-03 23:30')]
    assert df['time_stamp'].dt.date.nunique() == 3 and df['time_stamp'].is_monotonic_increasing


def test_history_waits_out_rate_limits_and_a_failed_chunk_leaves_a_hole_for_the_gap_check():
    gw, sc, clock = rig(now=datetime(2026, 9, 4, 0, 0))
    seen = {'n': 0}

    def fail(tok, frm, to, now):
        seen['n'] += 1
        return frm.startswith('2026-09-01') and seen['n'] <= 2                      # the first chunk is rate limited twice
    sc.fail = fail
    df = fetch_history(gw, 'T1', datetime(2026, 9, 1, 9, 0), datetime(2026, 9, 3, 23, 30), FetchConfig(), clock.sleep)
    assert df['time_stamp'].dt.date.nunique() == 3 and clock.t >= 2 * FetchConfig().rate_backoff_s

    class Broken:
        def getCandleData(self, params):
            raise Exception('boom')
    gw2 = BrokerGateway(Broken(), clock=clock.monotonic, sleep=clock.sleep)
    assert fetch_history(gw2, 'T1', datetime(2026, 9, 1, 9, 0), datetime(2026, 9, 3, 23, 30), FetchConfig(), clock.sleep).empty


def test_the_shared_feed_adapter_maps_to_the_shared_feed_api():
    calls = []

    class Shared:
        def subscribe_options(self, tokens, exchange_type=None):
            calls.append(('sub', tokens, exchange_type))

        def unsubscribe_options(self, tokens, exchange_type=None):
            calls.append(('unsub', tokens, exchange_type))

        def get_ltp(self, token):
            return 5.0

        def get_ohlc(self, token):
            return {'open': 1, 'high': 2, 'low': 0, 'close': 1}

        def get_last_tick_age(self, token):
            return 3.0
    a = SharedFeedAdapter(Shared(), 5)
    assert isinstance(a, FeedPort)
    a.subscribe(['T1']), a.unsubscribe(('T1',))
    assert calls == [('sub', ['T1'], 5), ('unsub', ['T1'], 5)]
    assert (a.get_ltp('T1'), a.last_tick_age('T1'), a.get_ohlc('T1')['high']) == (5.0, 3.0, 2)


# ---- the rescue fallback (Phase 2 of plans/hestia-fyers-candle-source.md) -------------------------------------------------------------

W0, W1 = datetime(2026, 9, 3, 11, 55), datetime(2026, 9, 3, 12, 0)
RESCUE = pd.DataFrame({'time_stamp': [pd.Timestamp('2026-09-03 11:59')], 'open': [1.0], 'high': [2.0], 'low': [0.5], 'close': [1.5], 'volume': [9.0]})


def test_the_fallback_is_called_once_after_the_whole_burst_has_failed_and_its_frame_is_the_result():
    gw, sc, clock = rig()
    sc.fail = lambda *a: True
    calls, stats = [], {}
    cfg = FetchConfig(inner_attempts=5, inner_interval_s=1.0)
    out = fetch_one_minute_window(gw, 'T1', W0, W1, cfg, clock.sleep, stats=stats, fallback=lambda: calls.append(1) or RESCUE)
    assert out is RESCUE and calls == [1] and len(sc.calls) == 5
    assert stats == {'attempts': 5, 'exhausted': True, 'rescued': True}


def test_fallback_after_fires_earlier_and_stops_the_angel_attempts():
    gw, sc, clock = rig()
    sc.fail = lambda *a: True
    stats = {}
    out = fetch_one_minute_window(gw, 'T1', W0, W1, FetchConfig(inner_attempts=5), clock.sleep, stats=stats, fallback=lambda: RESCUE, fallback_after=2)
    assert out is RESCUE and len(sc.calls) == 2
    assert stats == {'attempts': 2, 'exhausted': False, 'rescued': True}


def test_a_fallback_after_beyond_the_burst_is_capped_to_the_last_attempt():
    gw, sc, clock = rig()
    sc.fail = lambda *a: True
    out = fetch_one_minute_window(gw, 'T1', W0, W1, FetchConfig(inner_attempts=3), clock.sleep, fallback=lambda: RESCUE, fallback_after=9)
    assert out is RESCUE and len(sc.calls) == 3


def test_a_successful_angel_attempt_is_never_replaced_or_followed_by_a_fallback_call():
    gw, sc, clock = rig()
    calls = []
    out = fetch_one_minute_window(gw, 'T1', W0, W1, FetchConfig(), clock.sleep, fallback=lambda: calls.append(1) or RESCUE, fallback_after=1)
    assert out is not RESCUE and len(out) == 5 and calls == []


def test_a_burst_that_recovers_before_the_fallback_point_never_calls_it():
    gw, sc, clock = rig()
    n = {'c': 0}
    sc.fail = lambda *a: n.__setitem__('c', n['c'] + 1) or n['c'] <= 2
    calls = []
    out = fetch_one_minute_window(gw, 'T1', W0, W1, FetchConfig(inner_attempts=5), clock.sleep, fallback=lambda: calls.append(1) or RESCUE)
    assert len(out) == 5 and calls == []


def test_a_fallback_that_returns_none_leaves_the_burst_exactly_as_it_was_and_still_exhausts():
    gw, sc, clock = rig()
    sc.fail = lambda *a: True
    stats = {}
    assert fetch_one_minute_window(gw, 'T1', W0, W1, FetchConfig(inner_attempts=5), clock.sleep, stats=stats, fallback=lambda: None) is None
    assert len(sc.calls) == 5 and stats == {'attempts': 5, 'exhausted': True, 'rescued': False}


def test_a_fallback_that_raises_is_swallowed_and_the_burst_still_exhausts():
    gw, sc, clock = rig()
    sc.fail = lambda *a: True

    def boom():
        raise RuntimeError('fyers down')
    assert fetch_one_minute_window(gw, 'T1', W0, W1, FetchConfig(inner_attempts=3), clock.sleep, fallback=boom) is None
    assert len(sc.calls) == 3


def test_an_empty_frame_from_the_fallback_still_counts_as_a_result_not_a_failure():
    gw, sc, clock = rig()
    sc.fail = lambda *a: True
    out = fetch_one_minute_window(gw, 'T1', W0, W1, FetchConfig(inner_attempts=3), clock.sleep, fallback=lambda: RESCUE.iloc[0:0])
    assert out is not None and out.empty


def test_without_a_fallback_the_burst_is_unchanged_and_stats_carry_rescued_false():
    gw, sc, clock = rig()
    sc.fail = lambda *a: True
    stats = {}
    assert fetch_one_minute_window(gw, 'T1', W0, W1, FetchConfig(inner_attempts=4), clock.sleep, stats=stats) is None
    assert stats == {'attempts': 4, 'exhausted': True, 'rescued': False}
