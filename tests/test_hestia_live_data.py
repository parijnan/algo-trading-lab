"""
The live data service against a candle double on the simulated kernel: it must produce the same bars and Supertrend as the
replay data for the same minutes, and behave under degraded conditions (AB1021 stretches, partial windows, gaps, provisional
bars, restarts from the private cache, tracked contracts, feed and DPL events).
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from hestia_data_helpers import DAY, LiveWorld
from hestia_fake_helpers import FRONT, NEXT, SPEC, RecEngine, events_of, factory, world
from smartapi_double import FakeFeed
from hestia_core.interface import (BarComplete, BarQuality, ContractRef, DplFrozen, FeedRecovered, FeedStale, ProvisionalBar,
                                   SessionStart, TrackFailed, TrackReady)
from hestia_core.live_data import LiveDataConfig

END = datetime(2026, 9, 3, 23, 40)


def STRETCH_CFG():
    """A 3-minute deferred-bar cutoff, so a 70 s AB1021 stretch is waited out instead of building a partial bar (the default
    cutoff is 1 minute, as in Prometheus, which builds the bar at 12:01 from 14 of 15 minutes)."""
    return LiveDataConfig(seed_retry_attempts=1, ltp_refresh_s=0, deferred_bar_cutoff_min=3.0)


@pytest.fixture
def worlds(tmp_path):
    made = []

    def build(engines, **kw):
        w = LiveWorld(tmp_path / f'w{len(made)}', engines, **kw)
        made.append(w)
        return w
    yield build
    for w in made:
        w.close()


def summary(events):
    """Everything an engine sees, with floats compared to 1e-9 (the pipeline CSV round trip costs one ulp of a synthetic price;
    real broker prices have two decimals and survive it exactly)."""
    out = []
    for e in events:
        if e.bar is None:
            continue
        out.append((e.boundary_ts, e.bar.ts, pytest.approx(e.bar.open, rel=1e-9), pytest.approx(e.bar.high, rel=1e-9),
                    pytest.approx(e.bar.low, rel=1e-9), pytest.approx(e.bar.close, rel=1e-9), e.bar.volume,
                    pytest.approx(e.st.value, rel=1e-9) if e.st.value is not None else None, e.st.trend, e.st.flip,
                    pytest.approx(e.prev_st, rel=1e-9) if e.prev_st is not None else None, e.quality, e.minutes_present))
    return out


def test_live_bars_and_supertrend_equal_the_replay_data_for_the_same_minutes(worlds):
    log_live, log_replay = [], []
    w = worlds([('a', factory(log=log_live))]).start()
    w.run_until(END)
    r = world([('a', factory(log=log_replay))])
    r.start_session(DAY)
    r.run_until(END)
    r.close()
    live, replay = events_of(log_live, BarComplete), events_of(log_replay, BarComplete)
    assert len(live) == len(replay) == 58
    assert summary(live) == summary(replay), 'same boundaries, bars, ST, prev ST, quality and minutes'
    assert sum(b.st.flip for b in live) > 0, 'the day must contain a flip or the comparison is vacuous'
    assert all(b.quality == BarQuality.COMPLETE and not b.reconciles_provisional for b in live)


def test_the_seed_is_reported_and_the_first_bars_continue_the_seeded_series(worlds):
    log = []
    w = worlds([('a', factory(log=log))])
    assert w.prepared == {FRONT.symbol: True, NEXT.symbol: True}
    w.start()
    w.run_until(datetime(2026, 9, 3, 9, 20))
    start = events_of(log, SessionStart)[0]
    assert {r.token for r in start.seeded} == {'T1', 'T2'} and start.session_open == datetime(2026, 9, 3, 9, 0)
    first = events_of(log, BarComplete)[0]
    assert first.boundary_ts == datetime(2026, 9, 3, 9, 15) and first.st.value is not None, 'seeded history gives a real ST at once'
    assert first.prev_st is not None


def test_the_trading_contracts_st_is_logged_at_every_15m_boundary_not_just_on_a_flip(worlds, caplog):
    """User, 2026-09-30: the log was noisy with the trade-update ticker while missing the ST value at every 15m
    boundary -- more useful information than a flip-only log line, since a flip is rare and this is the actual signal
    driving every decision. Only the trading contract's own bar is logged, not a merely-tracked next-contract one."""
    log = []
    with caplog.at_level('INFO', logger='hestia_live_data'):
        w = worlds([('a', factory(log=log))]).start()
        w.run_until(datetime(2026, 9, 3, 9, 20))
    first = events_of(log, BarComplete)[0]
    assert first.boundary_ts == datetime(2026, 9, 3, 9, 15)
    boundary_lines = [r.message for r in caplog.records if 'XX30OCT26FUT' in r.message and '15m boundary 09:00' in r.message]
    assert len(boundary_lines) == 1, 'exactly one boundary log line for the trading contract, not the tracked next one'
    assert f'ST={first.st.value:.2f}' in boundary_lines[0]
    assert f"trend={first.st.trend.value}" in boundary_lines[0]
    assert f'flip={first.st.flip}' in boundary_lines[0]


def test_an_ab1021_stretch_defers_the_bar_provisional_first_then_the_recovered_real_bar(worlds):
    feed = FakeFeed()
    log = []
    w = worlds([('a', factory(log=log))], feed=feed, cfg=STRETCH_CFG())
    boundary = datetime(2026, 9, 3, 12, 0)
    w.sc.fail = lambda tok, f, t, now: boundary <= now < boundary + timedelta(seconds=70)
    w.start()
    w.run_until(datetime(2026, 9, 3, 11, 59, 30))
    feed.ohlc['T1'] = {'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.5}       # ticks accumulated since the last read
    w.run_until(datetime(2026, 9, 3, 12, 30))
    prov = [(t, e) for t, e in log if isinstance(e, ProvisionalBar)]
    assert len(prov) == 1 and prov[0][1].boundary_ts == boundary and prov[0][1].bar.close == 100.5
    real = [(t, e) for t, e in log if isinstance(e, BarComplete) and e.boundary_ts == boundary]
    assert len(real) == 1 and real[0][1].reconciles_provisional and real[0][1].quality == BarQuality.RECOVERED
    assert real[0][0] > boundary + timedelta(seconds=60), 'the real bar arrived only after the stretch ended'
    assert prov[0][1].prev_st == pytest.approx(real[0][1].prev_st)


def test_without_tick_data_a_deferred_bar_is_still_delivered_without_a_provisional(worlds):
    log = []
    w = worlds([('a', factory(log=log))], feed=FakeFeed(), cfg=STRETCH_CFG())
    boundary = datetime(2026, 9, 3, 12, 0)
    w.sc.fail = lambda tok, f, t, now: boundary <= now < boundary + timedelta(seconds=70)
    w.start()
    w.run_until(datetime(2026, 9, 3, 12, 30))
    assert events_of(log, ProvisionalBar) == []
    (b,) = [e for e in events_of(log, BarComplete) if e.boundary_ts == boundary]
    assert b.quality == BarQuality.RECOVERED and not b.reconciles_provisional


def test_a_window_still_incomplete_at_the_cutoff_is_built_from_what_is_on_hand_as_partial(worlds):
    log = []
    w = worlds([('a', factory(log=log))], cfg=LiveDataConfig(seed_retry_attempts=1, ltp_refresh_s=0, deferred_bar_cutoff_min=1.0))
    missing = [datetime(2026, 9, 3, 12, m) for m in (3, 4, 5)]
    for ts in missing:
        w.sc.missing.add(('T1', ts))
    w.start()
    w.run_until(datetime(2026, 9, 3, 12, 40))
    (b,) = [e for e in events_of(log, BarComplete) if e.boundary_ts == datetime(2026, 9, 3, 12, 15)]
    assert b.quality == BarQuality.PARTIAL and b.minutes_present == 12
    assert any('still incomplete' in a.text for a in w.core.alerts_for('warning'))


def test_a_missing_minute_that_arrives_late_completes_the_bar_as_recovered(worlds):
    log = []
    w = worlds([('a', factory(log=log))], cfg=LiveDataConfig(seed_retry_attempts=1, ltp_refresh_s=0, deferred_bar_cutoff_min=3.0))
    w.sc.missing.add(('T1', datetime(2026, 9, 3, 12, 14)))
    w.start()
    w.run_until(datetime(2026, 9, 3, 12, 15, 30))
    assert [e for e in events_of(log, BarComplete) if e.boundary_ts == datetime(2026, 9, 3, 12, 15)] == []
    w.sc.missing.clear()
    w.run_until(datetime(2026, 9, 3, 12, 40))
    (b,) = [e for e in events_of(log, BarComplete) if e.boundary_ts == datetime(2026, 9, 3, 12, 15)]
    assert b.quality == BarQuality.RECOVERED and b.minutes_present == 15


def test_a_window_with_no_data_at_all_is_a_gap_event_and_a_critical_alert(worlds):
    log = []
    w = worlds([('a', factory(log=log))])
    for m in range(0, 15):
        w.sc.missing.add(('T1', datetime(2026, 9, 3, 13, m)))
    w.start()
    w.run_until(datetime(2026, 9, 3, 13, 45))
    (g,) = [e for e in events_of(log, BarComplete) if e.quality == BarQuality.GAP]
    assert g.bar is None and g.st is None and g.boundary_ts == datetime(2026, 9, 3, 13, 15)
    assert any('no 1-minute data' in a.text for a in w.core.alerts_for('critical'))
    later = [e for e in events_of(log, BarComplete) if e.boundary_ts == datetime(2026, 9, 3, 13, 30)][0]
    assert later.prev_st is not None


def test_a_restart_mid_day_reuses_the_private_cache_and_fetches_only_the_gap(worlds, tmp_path):
    log1 = []
    w1 = worlds([('a', factory(log=log1))])
    w1.start()
    w1.run_until(datetime(2026, 9, 3, 11, 0))
    w1.core.close()
    log2 = []
    w2 = LiveWorld(tmp_path / 'w0', [('a', factory(log=log2))], start=datetime(2026, 9, 3, 11, 20), seed_first=False)
    try:
        w2.sc.calls.clear()
        w2.data.prepare(['XX'])
        gap_calls = [c for c in w2.sc.calls if c[0] == 'T1']
        assert len(gap_calls) == 1 and gap_calls[0][1] == '2026-09-03 11:00', 'only the minutes the cache lacks are fetched'
        w2.start()
        w2.run_until(datetime(2026, 9, 3, 12, 16))
        b = [e for e in events_of(log2, BarComplete) if e.boundary_ts == datetime(2026, 9, 3, 12, 0)][0]
        ref = [e for e in events_of(log1, BarComplete) if e.boundary_ts == datetime(2026, 9, 3, 10, 30)]
        assert b.st.value is not None and ref, 'the resumed series is contiguous'
    finally:
        w2.close()


def test_seeding_refuses_a_series_with_a_hole(tmp_path):
    w = LiveWorld(tmp_path, [], seed_first=False)
    df = w.frames['T1']
    hole = df[(df['time_stamp'].dt.date == pd.Timestamp('2026-09-02').date())
              & (df['time_stamp'] >= '2026-09-02 12:00') & (df['time_stamp'] < '2026-09-02 12:15')]
    from hestia_core.history import merge_and_save
    import os
    path = w.catalog.row('T1').filepath
    kept = df[df['time_stamp'].dt.date.isin([pd.Timestamp('2026-09-01').date(), pd.Timestamp('2026-09-02').date()])]
    os.remove(path)
    merge_and_save(path, kept.drop(hole.index))
    assert w.data.prepare(['XX']) == {FRONT.symbol: False, NEXT.symbol: True}
    assert any('could not seed' in a.text for a in w.core.alerts_for('critical'))
    assert w.data.seeded('XX') == (NEXT,)
    w.close()


def test_a_contract_already_rolled_past_is_never_seeded_even_with_days_left_before_its_own_expiry(tmp_path):
    """Found 2026-09-30: prepare() used to take the raw front-N-by-expiry ('not yet expired'), which kept trying to seed
    an already-rolled-off, about-to-expire contract right up through its own expiry date -- and a genuine data hole on
    that stale contract then blocked the WHOLE host's startup (prepare() is a hard blocking call every engine's
    session-start waits on), not just the one engine that used to trade it. DAY (2026-09-03, Thursday) is inside
    CLOSE_FRONT's own 5-trading-day roll window (it expires the very next trading day), so the effective contract is
    already FAR_NEXT -- a hole in CLOSE_FRONT's own data must never be attempted or alerted on."""
    CLOSE_FRONT = ContractRef('XX', 'T1', 'XX04SEP26FUT', pd.Timestamp('2026-09-04').date())
    FAR_NEXT = ContractRef('XX', 'T2', 'XX30NOV26FUT', pd.Timestamp('2026-11-30').date())
    w = LiveWorld(tmp_path, [], refs=(CLOSE_FRONT, FAR_NEXT), seed_first=False)
    df = w.frames['T1']
    hole = df[(df['time_stamp'].dt.date == pd.Timestamp('2026-09-02').date())
              & (df['time_stamp'] >= '2026-09-02 12:00') & (df['time_stamp'] < '2026-09-02 12:15')]
    from hestia_core.history import merge_and_save
    import os
    path = w.catalog.row('T1').filepath
    kept = df[df['time_stamp'].dt.date.isin([pd.Timestamp('2026-09-01').date(), pd.Timestamp('2026-09-02').date()])]
    os.remove(path)
    merge_and_save(path, kept.drop(hole.index))
    assert w.data.prepare(['XX']) == {FAR_NEXT.symbol: True}, 'CLOSE_FRONT is never attempted, holed or not'
    assert not w.core.alerts_for('critical'), 'no alert for a contract that was never even supposed to be tracked'
    assert w.data.seeded('XX') == (FAR_NEXT,)
    w.close()


def test_tracked_contract_bar_is_ready_before_the_trading_contract_bar_is_delivered(worlds):
    seen = []

    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.track(NEXT)
        if isinstance(ev, BarComplete):
            latest = ctx.latest_bar(NEXT)
            seen.append((ev.boundary_ts, latest[0].ts + timedelta(minutes=15) if latest else None))
    log = []
    w = worlds([('a', factory(log=log, act=act))], cfg=LiveDataConfig(seed_retry_attempts=1, ltp_refresh_s=0, poll_stagger_s=20.0))
    w.start()
    w.run_until(datetime(2026, 9, 3, 12, 5))
    assert len(events_of(log, TrackReady)) == 1
    boundaries = [b for b, _ in seen]
    assert len(boundaries) == 12 and all(b == ready for b, ready in seen), \
        'even with the tracked contract polled 20 s after the trading one, its bar is readable at delivery'
    assert set(w.data.streams) == {'T1', 'T2'}


def test_track_of_an_unknown_contract_fails_and_a_stale_tracked_bar_is_released_with_a_warning(worlds):
    from hestia_core.interface import ContractRef
    unknown = ContractRef('XX', 'T9', 'XX99', FRONT.expiry)

    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.track(unknown)
            ctx.track(NEXT)
    log = []
    w = worlds([('a', factory(log=log, act=act))])
    original = w.data._cycle
    # the tracked contract's polling stops dead at 10:00 (its stream never finalizes another boundary)
    w.data._cycle = lambda token, boundary: (None if token == 'T2' and boundary >= datetime(2026, 9, 3, 10, 0)
                                             else original(token, boundary))
    w.start()
    w.run_until(datetime(2026, 9, 3, 11, 0))
    assert [e.contract.token for e in events_of(log, TrackFailed)] == ['T9']
    boundaries = [(t, e.boundary_ts) for t, e in log if isinstance(e, BarComplete) and e.boundary_ts > datetime(2026, 9, 3, 10, 0)]
    assert len(boundaries) >= 3, 'the trading contract keeps getting bars'
    assert all(t - b >= timedelta(minutes=3) for t, b in boundaries), 'each was held until the release time'
    assert any('without tracked contract' in a.text for a in w.core.alerts_for('warning'))


def test_price_prefers_the_feed_then_the_rest_price_then_the_last_bar(worlds):
    feed = FakeFeed()
    w = worlds([('a', factory())], feed=feed, cfg=LiveDataConfig(seed_retry_attempts=1, ltp_refresh_s=5.0))
    w.start()
    w.run_until(datetime(2026, 9, 3, 9, 30))
    last_close = float(w.data.streams['T1'].raw_all.iloc[-1]['close'])
    assert w.data.price('T1') == last_close and w.data.ltp_quote('T1').age_sec > 0
    feed.ltp['T1'], feed.age['T1'] = 123.0, 2.0
    q = w.data.ltp_quote('T1')
    assert q.price == 123.0 and q.age_sec == 2.0
    feed.age['T1'] = 500.0                                   # the feed went quiet: fall back
    w.sc.ltps['T1'] = 111.0
    w.data.streams['T1']                                     # the REST refresher runs on its own timer for uncovered tokens
    w.run_until(datetime(2026, 9, 3, 9, 32))
    assert w.data.price('T1') == 111.0 and w.sc.ltp_calls > 0


def test_a_silent_feed_raises_stale_and_recovered_events(worlds):
    feed = FakeFeed()
    log = []
    w = worlds([('a', factory(log=log))], feed=feed)
    feed.age['T1'] = 1.0
    w.start()
    w.run_until(datetime(2026, 9, 3, 10, 0))
    feed.age['T1'] = 90.0
    w.run_until(datetime(2026, 9, 3, 10, 2))
    feed.age['T1'] = 1.0
    w.run_until(datetime(2026, 9, 3, 10, 4))
    assert len(events_of(log, FeedStale)) == 1 and events_of(log, FeedStale)[0].age_sec == 90.0
    assert len(events_of(log, FeedRecovered)) == 1


def test_a_price_pinned_at_the_circuit_limit_raises_dpl_frozen_and_unfrozen(worlds):
    feed = FakeFeed()
    log = []
    w = worlds([('a', factory(log=log))], feed=feed)
    w.sc.circuit_limits['T1'] = (110.0, 90.0)
    feed.age['T1'] = 1.0
    feed.ltp['T1'] = 100.0
    w.start()
    w.run_until(datetime(2026, 9, 3, 10, 0))
    feed.ltp['T1'] = 110.0
    w.run_until(datetime(2026, 9, 3, 10, 3))
    feed.ltp['T1'] = 108.0
    w.run_until(datetime(2026, 9, 3, 10, 6))
    frozen = events_of(log, DplFrozen)
    assert [(e.frozen, e.price) for e in frozen] == [(True, 110.0), (False, 110.0)]


def test_a_minute_still_forming_is_never_stored_or_used(worlds):
    """A broker that returns the in-progress candle must not get it into the keep-first cache: it would seed a wrong bar."""
    w = worlds([('a', factory())])
    real_rows = w.sc._rows

    def with_forming(token, frm, to):
        rows = real_rows(token, frm, to)
        now = w.kernel.now
        forming = w.frames[token][w.frames[token]['time_stamp'] == pd.Timestamp(now).floor('min')]
        for r in forming.itertuples():
            rows.append([r.time_stamp.strftime('%Y-%m-%dT%H:%M:%S') + '+05:30', r.open, r.high, r.low, 999.0, r.volume])
        return rows
    w.sc._rows = with_forming
    w.start()
    w.run_until(datetime(2026, 9, 3, 9, 40))
    cached = w.data.cache.read(datetime(2026, 9, 3, 9, 40), 'T1')
    assert (cached['close'] != 999.0).all() and cached['time_stamp'].max() == datetime(2026, 9, 3, 9, 39)
    assert (w.data.streams['T1'].raw_today['close'] != 999.0).all()


def test_history_the_pipeline_lacks_is_backfilled_into_a_private_file_and_the_pipeline_file_is_never_written(tmp_path):
    import hashlib
    from hestia_core.history import merge_and_save
    w = LiveWorld(tmp_path, [], seed_first=False)
    path = w.catalog.row('T1').filepath
    df = w.frames['T1']
    only_second_day = df[df['time_stamp'].dt.date == pd.Timestamp('2026-09-02').date()]
    import os
    os.remove(path)
    merge_and_save(path, only_second_day)                         # the pipeline is missing 09-01, which a 2-day seed needs
    before = hashlib.sha1(open(path, 'rb').read()).hexdigest()
    w.data.cfg.seed_days = 3
    w.sc.calls.clear()
    assert w.data.prepare(['XX'])[FRONT.symbol] is True
    assert hashlib.sha1(open(path, 'rb').read()).hexdigest() == before, 'the pipeline file is never written'
    assert w.data._backfill_path('T1').exists() and any(c[0] == 'T1' for c in w.sc.calls)
    days = sorted({d for d in w.data.streams['T1'].raw_past['time_stamp'].dt.date})
    assert pd.Timestamp('2026-09-01').date() in days and pd.Timestamp('2026-09-02').date() in days
    w.close()


def test_an_evening_only_session_opens_at_1700_and_the_window_before_it_is_not_a_gap(worlds, tmp_path):
    from hestia_core.mcx_market import MarketCalendar
    holidays = tmp_path / 'holidays.csv'
    holidays.write_text('date,morning_session_closed,evening_session_closed,holiday_name\n2026-09-03,True,False,Special\n')
    log = []
    w = worlds([('a', factory(log=log))], calendar=MarketCalendar(holidays), today_from='17:00', start=datetime(2026, 9, 3, 16, 55))
    w.start()
    w.run_until(datetime(2026, 9, 3, 17, 40))
    start = events_of(log, SessionStart)[0]
    assert start.evening_only and start.session_open == datetime(2026, 9, 3, 17, 0)
    bars = events_of(log, BarComplete)
    assert [b.boundary_ts for b in bars] == [datetime(2026, 9, 3, 17, 15), datetime(2026, 9, 3, 17, 30)]
    assert not [b for b in bars if b.quality == BarQuality.GAP] and not w.core.alerts_for('critical')
    assert bars[0].prev_st is not None, 'the seed from the previous days still gives a real supertrend'
