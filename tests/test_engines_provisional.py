"""Provisional-boundary trading in the Selene, Helios and Typhon engines (plans/hestia-provisional-all-engines.md), ported from Prometheus's own cases
(tests/test_prometheus_engine.py, tests/test_prometheus_engine_ports.py) and extended: the feed-staleness gate, shadow mode, the close check, the unreconciled-gap
case, and the pins that the feature is OFF by default. One scenario set, run through each engine so the three copies cannot drift apart."""
import dataclasses
import importlib
import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest

from hestia_core.interface import (AckStatus, Bar, BarComplete, BarQuality, Direction, FeedRecovered, FeedStale, LtpQuote, MarginSnapshot, ProvisionalBar,
                                   RequestAck, SizingConfig, SupertrendPoint)

NAMES = ['selene', 'helios', 'typhon']
MARGIN = {'selene': 0.05, 'helios': 0.04, 'typhon': 0.11}           # the mean final-minute range per instrument, rounded up (plan section 7)


@pytest.fixture(params=NAMES)
def eng(request):
    n = request.param
    H = importlib.import_module(f'{n}_engine_helpers')
    mod = importlib.import_module(f'{n}_engine.engine')
    return SimpleNamespace(name=n, H=H, cls=getattr(mod, f'{n.capitalize()}Engine'), State=importlib.import_module(f'{n}_engine.state').EngineState,
                           DEFAULT=importlib.import_module(f'{n}_engine.engine_configs').DEFAULT)


def cfg_of(eng, **kw):
    return dataclasses.replace(eng.H.CFG, **kw)


ON = dict(provisional_enabled=True, provisional_margin_pct=0.05)           # a margin the scripted price path clears comfortably
SHADOW = dict(provisional_enabled=False, provisional_shadow=True, provisional_margin_pct=0.05)


def prov_run(eng, boundary=None, override=None, until=None, stale=None, partial=None, **cfg_kw):
    H = eng.H
    made = []
    h = H.scripted_world(H.FLIP_PATH, cfg=cfg_of(eng, **cfg_kw), made=made)
    b = boundary or H.hm(10, 15)
    h.bar_delay[(H.FRONT.token, b)] = 40.0
    if override:
        h.provisional_override[(H.FRONT.token, b)] = override
    if partial:
        h.bar_partial[(H.FRONT.token, b)] = partial
    if stale:
        h.inject_feed_stale(eng.name, H.FRONT, *stale)
    h.start_session(H.SESSION_DATE, H.SESSION_OPEN)
    h.run_until(until or b + timedelta(minutes=3))
    return h, made


def order_times(h):
    return [(o.ts.hour, o.ts.minute, o.ts.second) for o in h.orders]


# ---------------------------------------------------------------------------------------------------------------------------------
# Off by default, and the spec follows the flags
# ---------------------------------------------------------------------------------------------------------------------------------

def test_the_feature_is_on_by_default_with_the_typical_size_margin(eng):
    d = eng.DEFAULT
    assert d.provisional_enabled is True and d.provisional_shadow is False
    assert d.provisional_margin_pct == MARGIN[eng.name]
    assert eng.cls(d).spec.provisional.enabled is True


def test_the_data_spec_asks_hestia_for_provisional_bars_only_when_a_flag_is_on(eng):
    assert eng.cls(cfg_of(eng, provisional_enabled=False)).spec.provisional.enabled is False
    assert eng.cls(cfg_of(eng, provisional_enabled=True)).spec.provisional.enabled is True
    assert eng.cls(cfg_of(eng, provisional_shadow=True)).spec.provisional.enabled is True


def test_with_the_feature_switched_off_nothing_acts_before_the_real_bar(eng):
    h, made = prov_run(eng, provisional_enabled=False)
    assert order_times(h) == [(10, 15, 40)]                                          # the real bar, 40 s late, makes the entry
    assert made[-1].provisional_seen is None and made[-1].provisional_pending is None


# ---------------------------------------------------------------------------------------------------------------------------------
# Acting, confirming, disagreeing
# ---------------------------------------------------------------------------------------------------------------------------------

def test_a_provisional_flip_acts_at_the_boundary_and_is_confirmed_by_the_real_bar(eng):
    h, made = prov_run(eng, **ON)
    assert order_times(h) == [(10, 15, 0)]                                           # once, at the boundary, not at the late real bar
    texts = eng.H.alert_texts(h)
    assert any('PROVISIONAL flip -> bearish' in t for t in texts) and any('CONFIRMED by the real bar' in t for t in texts)
    e = made[-1]
    assert e.provisional_pending is None and not e.provisional_disabled and e.state.last_processed_boundary == '2026-09-03T10:00:00'


def test_a_disagreement_alerts_with_the_real_bars_quality_and_latches_provisional_off(eng):
    h, made = prov_run(eng, boundary=eng.H.hm(9, 45), override={'close': 90.0}, until=eng.H.hm(9, 50), **ON)
    e = made[-1]
    assert len(h.orders) == 1 and e.state.status == 'in_trade'                       # no automated reversal
    texts = [t for t in eng.H.alert_texts(h) if 'DISAGREES' in t]
    assert e.provisional_disabled and len(texts) == 1
    assert 'real bar RECOVERED' in texts[0] and 'minutes present' in texts[0]


def test_a_real_bar_built_at_the_cutoff_from_fewer_minutes_is_named_in_the_disagreement(eng):
    h, made = prov_run(eng, boundary=eng.H.hm(9, 45), override={'close': 90.0}, partial=14, until=eng.H.hm(9, 50), **ON)
    t = [t for t in eng.H.alert_texts(h) if 'DISAGREES' in t][0]
    assert 'real bar PARTIAL' in t and '14 minutes present' in t                      # the operator can see the real bar was itself short
    assert made[-1].provisional_disabled


def test_once_latched_a_later_provisional_flip_does_nothing(eng):
    e = unit_engine(eng, **ON)
    e.provisional_disabled = True
    e._on_provisional(prov_bar(eng, close=100.0, prev_st=100.30, st_value=101.0, direction=Direction.BEARISH))
    assert not e.ctx.sent


def test_the_margin_is_measured_against_the_previous_supertrend(eng):
    h, made = prov_run(eng, until=eng.H.hm(10, 15) + timedelta(seconds=20), provisional_enabled=True, provisional_margin_pct=50.0)
    assert not h.orders                                                              # the provisional close did not clear a 50% margin
    h.run_until(eng.H.hm(10, 20))
    assert order_times(h) == [(10, 15, 40)]


@pytest.mark.parametrize('close, prev_st, st_value, acts', [
    (100.0, 99.9, 90.0, False),     # clears the CURRENT supertrend by 10% but the PREVIOUS one by only 0.1%: no action
    (100.0, 90.0, 99.95, True),     # clears the PREVIOUS supertrend by 11%, the current one by 0.05%: acts
])
def test_the_margin_guard_unit_cases(eng, close, prev_st, st_value, acts):
    e = unit_engine(eng, provisional_enabled=True, provisional_margin_pct=0.15)
    e._on_provisional(prov_bar(eng, close=close, prev_st=prev_st, st_value=st_value, direction=Direction.BEARISH))
    assert bool(e.ctx.sent) is acts


def test_a_thin_cross_is_held_back_and_a_clear_cross_acts_for_an_entry(eng):
    e = unit_engine(eng, provisional_enabled=True, provisional_margin_pct=0.15)
    e._on_provisional(prov_bar(eng, close=100.0, prev_st=100.10, st_value=101.0, direction=Direction.BEARISH))     # 0.10% below the previous ST
    assert not e.ctx.sent
    e._on_provisional(prov_bar(eng, close=100.0, prev_st=100.30, st_value=101.0, direction=Direction.BEARISH))     # 0.30%
    assert len(e.ctx.sent) == 1 and e.ctx.sent[0].direction == Direction.BEARISH


def test_a_provisional_bar_that_does_not_flip_never_acts(eng):
    e = unit_engine(eng, **ON)
    e._on_provisional(prov_bar(eng, close=100.0, prev_st=90.0, st_value=99.0, direction=Direction.BEARISH, flip=False))
    assert not e.ctx.sent


# ---------------------------------------------------------------------------------------------------------------------------------
# The staleness gate
# ---------------------------------------------------------------------------------------------------------------------------------

def test_a_stale_feed_blocks_the_provisional_action_and_the_real_bar_acts_instead(eng):
    H = eng.H
    h, made = prov_run(eng, stale=(H.hm(10, 10), H.hm(10, 15) + timedelta(seconds=20)), until=H.hm(10, 25), **ON)     # stale across the boundary, recovered before the real bar
    assert order_times(h) == [(10, 15, 40)]                                          # nothing at the boundary, the late real bar enters
    assert made[-1].provisional_pending is None and made[-1].feed_stale == set()     # and the recovery cleared the flag


def test_feed_stale_and_recovered_events_are_tracked_per_contract(eng):
    e = unit_engine(eng, **ON)
    e.started = True
    e._on_event(FeedStale(eng.H.FRONT, 45.0))
    assert e.feed_stale == {eng.H.FRONT.token}
    e._on_provisional(prov_bar(eng, close=100.0, prev_st=100.30, st_value=101.0, direction=Direction.BEARISH))
    assert not e.ctx.sent
    e._on_event(FeedRecovered(eng.H.FRONT))
    assert e.feed_stale == set()
    e._on_provisional(prov_bar(eng, close=100.0, prev_st=100.30, st_value=101.0, direction=Direction.BEARISH))
    assert len(e.ctx.sent) == 1


# ---------------------------------------------------------------------------------------------------------------------------------
# Shadow mode: evaluate and log, never act
# ---------------------------------------------------------------------------------------------------------------------------------

def test_shadow_mode_sends_no_order_at_the_boundary_and_reports_the_real_bar_confirming(eng):
    h, made = prov_run(eng, **SHADOW)
    assert order_times(h) == [(10, 15, 40)]                                          # only the real bar acts, as with the feature off
    texts = eng.H.alert_texts(h)
    assert any('PROVISIONAL (shadow, no action) flip -> bearish' in t for t in texts)
    assert any('PROVISIONAL (shadow) bearish flip at 10:00' in t and 'CONFIRMS it' in t for t in texts)
    e = made[-1]
    assert e.provisional_pending is None and e.provisional_shadow_pending is None and not e.provisional_disabled


def test_shadow_mode_reports_a_would_be_disagreement_without_latching_or_acting(eng):
    h, made = prov_run(eng, boundary=eng.H.hm(9, 45), override={'close': 90.0}, until=eng.H.hm(9, 50), **SHADOW)
    assert not h.orders                                                              # the real bar does not flip, and shadow sent nothing
    assert any('DISAGREES (nothing was done)' in t for t in eng.H.alert_texts(h))
    assert not made[-1].provisional_disabled


def test_enabled_wins_over_shadow_when_both_are_set(eng):
    h, made = prov_run(eng, provisional_enabled=True, provisional_shadow=True, provisional_margin_pct=0.05)
    assert order_times(h) == [(10, 15, 0)]


# ---------------------------------------------------------------------------------------------------------------------------------
# The close check (the data that tells whether the margin can be relaxed)
# ---------------------------------------------------------------------------------------------------------------------------------

def test_every_evaluated_provisional_bar_is_checked_against_the_real_close_in_the_log(eng, caplog):
    with caplog.at_level(logging.INFO):
        prov_run(eng, **SHADOW)
    lines = [r.getMessage() for r in caplog.records if 'provisional close check' in r.getMessage()]
    assert len(lines) == 1 and 'diff=' in lines[0] and 'provisional close=' in lines[0] and 'real close=' in lines[0] and 'real bar quality=RECOVERED' in lines[0]


def test_no_close_check_is_logged_with_the_feature_off(eng, caplog):
    with caplog.at_level(logging.INFO):
        prov_run(eng, provisional_enabled=False)
    assert not [r for r in caplog.records if 'provisional' in r.getMessage()]


# ---------------------------------------------------------------------------------------------------------------------------------
# Skips and the bar that never comes
# ---------------------------------------------------------------------------------------------------------------------------------

def test_a_provisional_bar_at_the_close_is_ignored(eng):
    e = unit_engine(eng, **ON)
    b = Bar(eng.H.hm(23, 15), 100.0, 100.0, 100.0, 100.0, 1.0)
    e._on_provisional(ProvisionalBar(eng.H.FRONT, eng.H.hm(23, 30), b, SupertrendPoint(101.0, Direction.BEARISH, True), 100.5))
    assert not e.ctx.sent and e.provisional_seen is None


def test_a_provisional_bar_with_no_previous_supertrend_is_skipped(eng):
    e = unit_engine(eng, **ON)
    e._on_provisional(prov_bar(eng, close=100.0, prev_st=None, st_value=101.0, direction=Direction.BEARISH))
    assert not e.ctx.sent and e.provisional_seen is None


def test_an_armed_roll_or_a_flip_in_progress_blocks_the_provisional_action(eng):
    e = unit_engine(eng, **ON)
    e.state.roll_target = {'token': eng.H.NEXT.token, 'symbol': eng.H.NEXT.symbol, 'expiry': eng.H.NEXT.expiry.isoformat(), 'flatten_only': False}
    e._on_provisional(prov_bar(eng, close=100.0, prev_st=100.30, st_value=101.0, direction=Direction.BEARISH))
    assert not e.ctx.sent
    e2 = unit_engine(eng, **ON)
    e2.state.pending_flip = {'direction': 'bearish'}
    e2._on_provisional(prov_bar(eng, close=100.0, prev_st=100.30, st_value=101.0, direction=Direction.BEARISH))
    assert not e2.ctx.sent


def test_an_acted_provisional_flip_whose_real_bar_never_arrives_is_a_critical_alert_and_latches_off(eng):
    e = unit_engine(eng, **ON)
    e.provisional_pending = {'boundary': eng.H.hm(10, 15).isoformat(), 'direction': 'bearish', 'window_start': eng.H.hm(10, 0), 'pre': ('watching', None)}
    e._on_bar(BarComplete(eng.H.FRONT, eng.H.hm(10, 15), None, None, None, BarQuality.GAP, 0))
    assert e.provisional_pending is None and e.provisional_disabled
    assert any(level == 'critical' and 'could NOT be reconciled' in text for level, text in e.ctx.alerts)


def test_a_gap_for_a_different_boundary_leaves_a_pending_flip_alone(eng):
    e = unit_engine(eng, **ON)
    e.provisional_pending = {'boundary': eng.H.hm(10, 15).isoformat(), 'direction': 'bearish', 'window_start': eng.H.hm(10, 0), 'pre': ('watching', None)}
    e._on_bar(BarComplete(eng.H.FRONT, eng.H.hm(10, 30), None, None, None, BarQuality.GAP, 0))
    assert e.provisional_pending is not None and not e.provisional_disabled


def test_a_gap_clears_a_shadow_verdict_and_the_seen_record(eng):
    e = unit_engine(eng, **SHADOW)
    e._on_provisional(prov_bar(eng, close=100.0, prev_st=100.30, st_value=101.0, direction=Direction.BEARISH, boundary=eng.H.hm(10, 15)))
    assert e.provisional_shadow_pending is not None and e.provisional_seen is not None
    e._on_bar(BarComplete(eng.H.FRONT, eng.H.hm(10, 15), None, None, None, BarQuality.GAP, 0))
    assert e.provisional_shadow_pending is None and e.provisional_seen is None


# ---------------------------------------------------------------------------------------------------------------------------------
# Unit-level builders (a stub context, as in the engines' own ports files)
# ---------------------------------------------------------------------------------------------------------------------------------

class Ctx:
    def __init__(self, now):
        self._now, self.sent, self.alerts = now, [], []

    def now(self):
        return self._now

    def submit(self, request):
        self.sent.append(request)
        return RequestAck(request.request_id, AckStatus.ACCEPTED)

    def alert(self, level, text, channel=None, emoji=None, log_locally=True):
        self.alerts.append((level, text))

    def save_state(self, blob):
        pass

    def ltp(self, ref):
        return LtpQuote(100.0, self._now, 0.0)

    def margin(self):
        return MarginSnapshot(10_000_000.0, self._now)

    def sizing(self):
        return SizingConfig(dynamic=False, static_units=1, unit_cap=50)


def unit_engine(eng, **cfg_kw):
    H = eng.H
    e = eng.cls(cfg_of(eng, **cfg_kw))
    e.ctx = Ctx(H.hm(11, 0))
    e.session_date, e.session_open, e.rollover_at, e.contract = H.SESSION_DATE, H.SESSION_OPEN, H.hm(23, 16), H.FRONT
    e.session_close = H.hm(23, 30)
    e.state = eng.State(status='watching')
    return e


def prov_bar(eng, close, prev_st, st_value, direction, flip=True, boundary=None):
    H = eng.H
    b = Bar(H.hm(10, 45), close, close, close, close, 1.0)
    return ProvisionalBar(H.FRONT, boundary or H.hm(11, 0), b, SupertrendPoint(st_value, direction, flip), prev_st)
