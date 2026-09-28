"""
Fake Hestia, data side of the contract: sessions, bars, Supertrend, provisional bars, gaps, tracking, feed events.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from hestia_fake_helpers import (FRONT, NEXT, NEAR, SESSION_DATE, SESSION_OPEN, SPEC, RecEngine, events_of, factory, world)
from hestia_core.fake import ContractSpec
from hestia_core.indicators import compute_st
from hestia_core.interface import (BarComplete, BarQuality, ContractRef, Direction, EngineContext, FeedRecovered, FeedStale,
                                   ProvisionalBar, SessionStart, TrackFailed, TrackReady)

END = datetime(2026, 9, 3, 23, 40)


@pytest.fixture
def hestias():
    made = []

    def build(*a, **k):
        h = world(*a, **k)
        made.append(h)
        return h
    yield build
    for h in made:
        h.close()


def _run_day(h, until=END):
    h.start_session(SESSION_DATE)
    h.run_until(until)


def test_one_engine_per_instrument(hestias):
    h = hestias([('a', factory(name='a'))])
    with pytest.raises(ValueError, match='one engine per instrument'):
        h.register('b', factory(name='b'))


def test_session_start_arrives_first_with_contract_facts(hestias):
    log = []
    h = hestias([('a', factory(log=log))], contracts=(FRONT, NEXT, NEAR),
               holidays={datetime(2026, 9, 7).date()})      # a full closure inside NEAR's window
    _run_day(h, datetime(2026, 9, 3, 9, 30))
    first = log[0][1]
    assert isinstance(first, SessionStart)
    assert log[0][0] == SESSION_OPEN
    by_token = {c.ref.token: c for c in first.contracts}
    assert by_token['T0'].trading_days_left == 3               # Sep 3, 4, 8 with Monday the 7th closed
    assert by_token['T1'].trading_days_left > 20
    assert {r.token for r in first.seeded} == {'T0', 'T1', 'T2'}
    assert first.evening_only is False
    assert first.rollover_time == datetime(2026, 9, 3, 23, 15)


def test_bar_complete_matches_production_supertrend_and_prev_st(hestias):
    log = []
    h = hestias([('a', factory(log=log))])
    _run_day(h)
    bars = events_of(log, BarComplete)
    assert len(bars) == 58                                      # 870 minutes -> 58 complete 15-minute bars
    assert all(b.quality == BarQuality.COMPLETE and b.minutes_present == 15 for b in bars)
    assert [b.boundary_ts - b.bar.ts for b in bars] == [timedelta(minutes=15)] * len(bars)
    assert all(bars[i + 1].boundary_ts - bars[i].boundary_ts == timedelta(minutes=15) for i in range(len(bars) - 1))

    minutes = h._contracts['T1'].minutes
    frame = pd.DataFrame(h._bars_for('T1'))
    expected = compute_st(frame, SPEC.st_period, SPEC.st_multiplier)
    day = expected[expected['time_stamp'].dt.date == SESSION_DATE].reset_index()
    idx = day['index']
    for b, i in zip(bars, idx):
        assert b.st.value == pytest.approx(expected.loc[i, 'supertrend'])
        prev = expected.loc[i - 1, 'supertrend']
        assert b.prev_st == pytest.approx(prev)
        assert b.st.trend == (Direction.BULLISH if expected.loc[i, 'trend'] else Direction.BEARISH)
    assert sum(b.st.flip for b in bars) > 0, 'the synthetic day must contain a flip or the check is vacuous'
    assert len(minutes) == 3 * 870


def test_bar_events_only_for_the_trading_contract_and_tracked_bars_are_ready_first(hestias):
    seen = []

    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.track(NEXT)
        if isinstance(ev, BarComplete):
            latest = ctx.latest_bar(NEXT)
            seen.append((ev.boundary_ts, latest[0].ts + timedelta(minutes=15) if latest else None))
    log = []
    h = hestias([('a', factory(log=log, act=act))])
    _run_day(h)
    assert all(b.contract == FRONT for b in events_of(log, BarComplete))
    assert len(events_of(log, TrackReady)) == 1
    assert seen and all(boundary == ready for boundary, ready in seen), 'tracked contract bar must be readable at the same boundary'


def test_provisional_bar_then_recovered_real_bar(hestias):
    log = []
    h = hestias([('a', factory(log=log))])
    boundary = datetime(2026, 9, 3, 12, 0)
    h.bar_delay[('T1', boundary)] = 40.0
    _run_day(h)
    prov = [(t, e) for t, e in log if isinstance(e, ProvisionalBar)]
    assert len(prov) == 1 and prov[0][0] == boundary
    real = [(t, e) for t, e in log if isinstance(e, BarComplete) and e.boundary_ts == boundary]
    assert len(real) == 1
    t_real, bar = real[0]
    assert t_real == boundary + timedelta(seconds=40)
    assert bar.quality == BarQuality.RECOVERED and bar.reconciles_provisional
    assert prov[0][1].prev_st == pytest.approx(bar.prev_st)
    assert prov[0][1].bar.close == pytest.approx(bar.bar.close)


def test_provisional_override_can_differ_from_the_real_bar(hestias):
    log = []
    h = hestias([('a', factory(log=log))])
    boundary = datetime(2026, 9, 3, 12, 0)
    h.bar_delay[('T1', boundary)] = 40.0
    h.provisional_override[('T1', boundary)] = {'close': 200.0}
    _run_day(h)
    prov = events_of(log, ProvisionalBar)[0]
    real = [e for e in events_of(log, BarComplete) if e.boundary_ts == boundary][0]
    assert prov.bar.close == 200.0 and prov.bar.high >= 200.0
    assert prov.bar.close != pytest.approx(real.bar.close)
    assert prov.st.value != pytest.approx(real.st.value)


def test_recovered_bar_is_not_readable_until_it_arrives(hestias):
    seen = {}

    def act(eng, ctx, ev):
        if isinstance(ev, ProvisionalBar):
            seen['during'] = ctx.latest_bar(FRONT)[0].ts
            ctx.wait(0)                                          # yield once inside the window
        if isinstance(ev, BarComplete) and ev.reconciles_provisional:
            seen['after'] = ctx.latest_bar(FRONT)[0].ts
    log = []
    h = hestias([('a', factory(log=log, act=act))])
    boundary = datetime(2026, 9, 3, 12, 0)
    h.bar_delay[('T1', boundary)] = 40.0
    _run_day(h)
    assert seen['during'] == boundary - timedelta(minutes=30)     # the previous bar, not the delayed one
    assert seen['after'] == boundary - timedelta(minutes=15)


def test_gap_bar_is_reported_and_leaves_a_hole_in_the_series(hestias):
    log = []
    h = hestias([('a', factory(log=log))])
    boundary = datetime(2026, 9, 3, 12, 0)
    h.bar_gap.add(('T1', boundary))
    _run_day(h)
    bars = events_of(log, BarComplete)
    gap = [b for b in bars if b.quality == BarQuality.GAP]
    assert len(gap) == 1 and gap[0].bar is None and gap[0].st is None and gap[0].boundary_ts == boundary
    assert len(bars) == 58
    after = [b for b in bars if b.boundary_ts == boundary + timedelta(minutes=15)][0]
    before = [b for b in bars if b.boundary_ts == boundary - timedelta(minutes=15)][0]
    assert after.prev_st == pytest.approx(before.st.value), 'ST series skips the missing bar'


def test_partial_bar_quality_is_reported(hestias):
    log = []
    h = hestias([('a', factory(log=log))])
    boundary = datetime(2026, 9, 3, 12, 0)
    h.bar_partial[('T1', boundary)] = 11
    _run_day(h)
    b = [b for b in events_of(log, BarComplete) if b.boundary_ts == boundary][0]
    assert b.quality == BarQuality.PARTIAL and b.minutes_present == 11


def test_track_ready_and_failed(hestias):
    unknown = ContractRef('XX', 'T9', 'XX99', datetime(2027, 1, 1).date())

    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.track(NEXT)
            ctx.track(unknown)
    log = []
    h = hestias([('a', factory(log=log, act=act))])
    _run_day(h, datetime(2026, 9, 3, 9, 10))
    assert [e.contract.token for e in events_of(log, TrackReady)] == ['T2']
    failed = events_of(log, TrackFailed)
    assert [e.contract.token for e in failed] == ['T9'] and failed[0].reason


def test_feed_stale_and_recovered_events_and_ltp_age(hestias):
    ages = []

    def act(eng, ctx, ev):
        if isinstance(ev, FeedStale):
            ages.append(ctx.ltp(FRONT).age_sec)
    log = []
    h = hestias([('a', factory(log=log, act=act))])
    h.inject_feed_stale('a', FRONT, datetime(2026, 9, 3, 10, 0), datetime(2026, 9, 3, 10, 5))
    _run_day(h, datetime(2026, 9, 3, 10, 30))
    assert len(events_of(log, FeedStale)) == 1 and len(events_of(log, FeedRecovered)) == 1
    assert ages == [0.0]


def test_supertrend_uses_each_engines_own_spec(hestias):
    from hestia_core.interface import DataSpec
    log3, log2 = [], []
    spec3 = DataSpec('XX', 15, 10, 3.0)
    h = hestias([('a', factory(log=log3, spec=spec3))])
    _run_day(h)
    frame = h._bars_for('T1')
    exp3 = compute_st(frame, 10, 3.0)
    exp2 = compute_st(frame, 10, 2.0)
    day = exp3[exp3['time_stamp'].dt.date == SESSION_DATE]
    got = [b.st.value for b in events_of(log3, BarComplete)]
    assert got == pytest.approx(list(day['supertrend']))
    assert got != pytest.approx(list(exp2[exp2['time_stamp'].dt.date == SESSION_DATE]['supertrend'])), 'multiplier must matter'


def test_context_satisfies_the_interface_protocol(hestias):
    seen = []

    def act(eng, ctx, ev):
        seen.append(isinstance(ctx, EngineContext))
    h = hestias([('a', factory(act=act))])
    _run_day(h, datetime(2026, 9, 3, 9, 5))
    assert seen and all(seen)
