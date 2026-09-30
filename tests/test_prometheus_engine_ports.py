"""Ports of the engine-relevant cases of prometheus_production's own tests (missed-flip reconcile, provisional margin guard, sizing and
the affordability check) onto the engine, on a stub context. The host-level ones (market-close arithmetic, evening-only sessions, the
Slack worker, stale pid, session report, DPL fetch) are Hestia's and are covered by the P4 tests; DPL handling in the engine is
detect-and-alert only, as in production (tests/test_prometheus_engine.py checks no order results)."""
from datetime import datetime, timedelta

import pytest

from hestia_core.interface import (AckStatus, Bar, Direction, FlipRequest, CloseRequest, LtpQuote, MarginSnapshot, OpenRequest,
                                   ProvisionalBar, RequestAck, SizingConfig, SupertrendPoint)
from prometheus_engine.engine import PrometheusEngine
from prometheus_engine.engine_configs import DEFAULT
from prometheus_engine.state import EngineState
from prometheus_engine_helpers import CFG, FRONT, SESSION_DATE, SESSION_OPEN, hm


class Ctx:
    def __init__(self, now, series=(), ltp=100.0, cash=10_000_000.0, sizing=None, margin_raises=False):
        self._now, self.series, self._ltp, self.cash = now, list(series), ltp, cash
        self._sizing = sizing or SizingConfig(dynamic=False, static_units=1, unit_cap=50)
        self.margin_raises = margin_raises
        self.sent, self.alerts = [], []

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
        return None if self._ltp is None else LtpQuote(self._ltp, self._now, 0.0)

    def margin(self):
        if self.margin_raises:
            raise RuntimeError('rms down')
        return MarginSnapshot(self.cash, self._now)

    def sizing(self):
        return self._sizing

    def st_series(self, ref, n):
        return tuple(self.series[-n:])


def bar(ts, close, st, trend, flip):
    return (Bar(ts, close, close, close, close, 1.0), SupertrendPoint(st, trend, flip))


def engine(now, state=None, series=(), **kw):
    e = PrometheusEngine(CFG)
    e.ctx = Ctx(now, series, **kw)
    e.session_date, e.session_open, e.rollover_at, e.contract = SESSION_DATE, SESSION_OPEN, hm(23, 14), FRONT
    e.session_close = hm(23, 29) + timedelta(minutes=1)
    e.state = state or EngineState(status='watching')
    return e


def in_trade(direction='bearish'):
    return EngineState(status='in_trade', direction=direction, units=1, entry_price=99.0, entry_ts='2026-09-03T09:30:00',
                       contract_token=FRONT.token, contract_symbol=FRONT.symbol, contract_expiry=FRONT.expiry.isoformat(),
                       lot1_lots=1, lot1_status='open', lot2_lots=1, lot2_status='open', trade_counter=3,
                       last_processed_boundary='2026-09-03T09:30:00')


def flip_series():
    """Bars after the watermark 09:30: a bullish flip at 09:45, nothing at 10:00."""
    return [bar(hm(9, 15), 99, 100, Direction.BEARISH, False), bar(hm(9, 30), 99, 100, Direction.BEARISH, False),
            bar(hm(9, 45), 102, 100, Direction.BULLISH, True), bar(hm(10, 0), 103, 100.5, Direction.BULLISH, False)]


def watching(wm='2026-09-03T09:30:00'):
    return EngineState(status='watching', trade_counter=3, last_processed_boundary=wm)


# ---- missed flip -------------------------------------------------------------------------------------------------------------

def test_no_watermark_baselines_off_the_last_bar_without_acting():
    e = engine(hm(10, 30), EngineState(status='watching'), flip_series())
    e._reconcile_missed_flip()
    assert e.state.last_processed_boundary == hm(10, 0).isoformat() and not e.ctx.sent


def test_watching_with_an_unprocessed_flip_and_clear_guards_enters():
    e = engine(hm(10, 30), watching(), flip_series())
    e._reconcile_missed_flip()
    assert len(e.ctx.sent) == 1 and isinstance(e.ctx.sent[0], OpenRequest) and e.ctx.sent[0].direction == Direction.BULLISH
    assert e.state.last_processed_boundary == hm(9, 45).isoformat()


def test_watching_blocked_by_the_entry_guard_defers_and_the_retry_fires_once_the_guard_clears():
    e = engine(hm(9, 5), watching('2026-09-03T08:59:00'), flip_series()[2:3])
    e._reconcile_missed_flip()
    assert not e.ctx.sent and e.state.pending_missed_flip['direction'] == 'bullish'
    e._retry_missed_flip(hm(9, 10))
    assert not e.ctx.sent and e.state.pending_missed_flip is not None                       # still inside the first 15 minutes
    e.ctx._now = hm(9, 15)
    e._retry_missed_flip(hm(9, 15))
    assert len(e.ctx.sent) == 1 and e.state.pending_missed_flip is None


def test_a_deferred_missed_flip_is_dropped_if_the_state_moved_on():
    e = engine(hm(9, 20), in_trade())
    e.state.pending_missed_flip = {'direction': 'bullish', 'window_start': hm(9, 45).isoformat(), 'close': 102.0,
                                   'boundary_ts': hm(9, 45).isoformat()}
    e._retry_missed_flip(hm(9, 20))
    assert not e.ctx.sent and e.state.pending_missed_flip is None


def test_in_trade_with_an_opposing_unprocessed_flip_flips_once():
    e = engine(hm(10, 30), in_trade('bearish'), flip_series())
    e._reconcile_missed_flip()
    assert len(e.ctx.sent) == 1 and isinstance(e.ctx.sent[0], FlipRequest)
    assert (e.ctx.sent[0].close_lots, e.ctx.sent[0].open_lots) == (2, 2)


def test_in_trade_with_an_agreeing_flip_does_not_refire():
    e = engine(hm(10, 30), in_trade('bullish'), flip_series())
    e._reconcile_missed_flip()
    assert not e.ctx.sent and e.state.pending_flip is None


def test_in_trade_flip_inside_the_entry_guard_exits_only():
    e = engine(hm(9, 5), in_trade('bearish'), flip_series()[2:3])
    e.state.last_processed_boundary = '2026-09-03T08:59:00'
    e._reconcile_missed_flip()
    assert len(e.ctx.sent) == 1 and isinstance(e.ctx.sent[0], CloseRequest)                  # Rule 7 with no re-entry


def test_no_unprocessed_flip_is_a_no_op():
    e = engine(hm(10, 30), watching('2026-09-03T10:00:00'), flip_series())
    e._reconcile_missed_flip()
    assert not e.ctx.sent


def test_a_multi_day_gap_coalesces_to_the_latest_flip_only():
    series = [bar(hm(9, 15), 99, 100, Direction.BEARISH, True), bar(hm(9, 30), 102, 100, Direction.BULLISH, True),
              bar(hm(9, 45), 98, 101, Direction.BEARISH, True)]
    e = engine(hm(10, 30), watching('2026-09-02T23:00:00'), series)
    e._reconcile_missed_flip()
    assert len(e.ctx.sent) == 1 and e.ctx.sent[0].direction == Direction.BEARISH


# ---- provisional margin guard ------------------------------------------------------------------------------------------------

def prov(e, close, prev_st, st_value, direction, flip=True):
    b = Bar(hm(10, 45), close, close, close, close, 1.0)
    e._on_provisional(ProvisionalBar(FRONT, hm(11, 0), b, SupertrendPoint(st_value, direction, flip), prev_st))


def test_thin_cross_is_held_back_and_a_clear_cross_acts_for_an_entry():
    e = engine(hm(11, 0), watching())
    prov(e, 100.0, 100.10, 101.0, Direction.BEARISH)                # 0.10% below the previous ST: inside the 0.15% margin
    assert not e.ctx.sent
    prov(e, 100.0, 100.30, 101.0, Direction.BEARISH)                # 0.30%: acts
    assert len(e.ctx.sent) == 1 and e.ctx.sent[0].direction == Direction.BEARISH


def test_thin_cross_is_held_back_and_a_clear_cross_acts_for_a_flip():
    e = engine(hm(11, 0), in_trade('bullish'))
    prov(e, 100.0, 100.10, 101.0, Direction.BEARISH)
    assert not e.ctx.sent
    prov(e, 100.0, 100.30, 101.0, Direction.BEARISH)
    assert len(e.ctx.sent) == 1 and isinstance(e.ctx.sent[0], FlipRequest)


def test_a_bullish_flip_from_a_downtrend_acts_on_a_clear_cross():
    e = engine(hm(11, 0), in_trade('bearish'))
    prov(e, 100.0, 99.70, 99.0, Direction.BULLISH)
    assert len(e.ctx.sent) == 1 and e.ctx.sent[0].to_direction == Direction.BULLISH


def test_a_warmup_previous_bar_is_skipped():
    e = engine(hm(11, 0), watching())
    prov(e, 100.0, None, 101.0, Direction.BEARISH)
    assert not e.ctx.sent


# ---- sizing and the affordability check --------------------------------------------------------------------------------------

def test_static_sizing_ignores_the_live_margin_entirely():
    e = engine(hm(11, 0), watching(), cash=1.0)
    assert e._current_units() == 1


def test_dynamic_sizing_uses_the_live_margin_and_never_returns_less_than_one_unit():
    sz = SizingConfig(dynamic=True, static_units=2, unit_cap=50, allocation_rs=None)
    e = engine(hm(11, 0), watching(), ltp=6000.0, cash=1_000_000.0, sizing=sz)
    e.infos = {FRONT.token: type('I', (), {'lot_size': 10})()}
    assert e._current_units() == int(1_000_000 // (6000 * 10 / 3 * 4))                       # 12
    e.ctx.cash = 10.0
    assert e._current_units() == 1


def test_dynamic_sizing_falls_back_to_the_static_units_when_the_margin_read_fails():
    sz = SizingConfig(dynamic=True, static_units=3, unit_cap=50)
    e = engine(hm(11, 0), watching(), sizing=sz, margin_raises=True)
    assert e._current_units() == 3


def test_the_affordability_check_uses_the_live_margin_even_with_static_sizing():
    e = engine(hm(11, 0), watching(), ltp=6000.0, cash=1_000.0)
    e.infos = {FRONT.token: type('I', (), {'lot_size': 10})()}
    assert e._margin_sufficient(1) is False
    e.ctx.cash = 80_000.0
    assert e._margin_sufficient(1) is True                                                  # 6000 x 10 / 3 x 4 = 80,000


def test_no_price_means_no_entry():
    e = engine(hm(11, 0), watching(), ltp=None)
    e._send_entry('bullish', hm(10, 45), 100.0)
    assert not e.ctx.sent and any('no price' in t for _, t in e.ctx.alerts)


# ---- the last boundary is the close ------------------------------------------------------------------------------------------

def _bar_complete(boundary, flip=True, direction=Direction.BULLISH):
    from hestia_core.interface import BarComplete, BarQuality
    b = Bar(boundary - timedelta(minutes=15), 102.0, 102.0, 102.0, 102.0, 1.0)
    return BarComplete(FRONT, boundary, b, SupertrendPoint(100.0, direction, flip), 99.0, BarQuality.COMPLETE, 15)


def test_the_bar_that_completes_at_the_close_is_never_traded_and_leaves_the_watermark_alone():
    e = engine(hm(23, 30), watching('2026-09-03T23:00:00'))
    e._on_bar(_bar_complete(hm(23, 30)))
    assert not e.ctx.sent and e.state.last_processed_boundary == '2026-09-03T23:00:00'      # the next start's missed-flip reconcile owns it
    e2 = engine(hm(23, 15), watching('2026-09-03T22:30:00'))
    e2._on_bar(_bar_complete(hm(23, 15)))                                                    # the bar before it is an ordinary bar
    assert len(e2.ctx.sent) == 1 and e2.state.last_processed_boundary == hm(23, 0).isoformat()


def test_a_provisional_bar_at_the_close_is_ignored_too():
    e = engine(hm(23, 30), watching())
    b = Bar(hm(23, 15), 100.0, 100.0, 100.0, 100.0, 1.0)
    e._on_provisional(ProvisionalBar(FRONT, hm(23, 30), b, SupertrendPoint(101.0, Direction.BEARISH, True), 100.5))
    assert not e.ctx.sent
