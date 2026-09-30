"""Missed-flip reconcile and sizing/affordability cases on a stub context, ported from Prometheus's own equivalent test file
(tests/test_prometheus_engine_ports.py) and simplified for Typhon's single-lot shape. The target-check tests at the end
have no Selene/Helios equivalent -- neither of those engines' own decided designs carries a target at all."""
from datetime import datetime, timedelta

import pytest

from hestia_core.interface import AckStatus, Bar, Direction, ExitReason, FlipRequest, CloseRequest, LtpQuote, MarginSnapshot, \
    OpenRequest, RequestAck, SizingConfig, SupertrendPoint
from typhon_engine.engine import TyphonEngine
from typhon_engine.engine_configs import DEFAULT
from typhon_engine.levels import margin_per_unit
from typhon_engine.state import EngineState
from typhon_engine_helpers import CFG, FRONT, SESSION_DATE, SESSION_OPEN, hm


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
    e = TyphonEngine(CFG)
    e.ctx = Ctx(now, series, **kw)
    e.session_date, e.session_open, e.rollover_at, e.contract = SESSION_DATE, SESSION_OPEN, hm(23, 16), FRONT
    e.session_close = hm(23, 30)
    e.state = state or EngineState(status='watching')
    return e


def in_trade(direction='bearish'):
    return EngineState(status='in_trade', direction=direction, units=1, entry_price=99.0, entry_ts='2026-09-03T09:30:00',
                       contract_token=FRONT.token, contract_symbol=FRONT.symbol, contract_expiry=FRONT.expiry.isoformat(), lots=1,
                       trade_counter=3, last_processed_boundary='2026-09-03T09:30:00')


def flip_series():
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
    assert not e.ctx.sent and e.state.pending_missed_flip is not None
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
    assert (e.ctx.sent[0].close_lots, e.ctx.sent[0].open_lots) == (1, 1)


def test_in_trade_with_an_agreeing_flip_does_not_refire():
    e = engine(hm(10, 30), in_trade('bullish'), flip_series())
    e._reconcile_missed_flip()
    assert not e.ctx.sent and e.state.pending_flip is None


def test_in_trade_flip_inside_the_entry_guard_exits_only():
    e = engine(hm(9, 5), in_trade('bearish'), flip_series()[2:3])
    e.state.last_processed_boundary = '2026-09-03T08:59:00'
    e._reconcile_missed_flip()
    assert len(e.ctx.sent) == 1 and isinstance(e.ctx.sent[0], CloseRequest)


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


# ---- sizing and the affordability check --------------------------------------------------------------------------------------

def test_static_sizing_ignores_the_live_margin_entirely():
    e = engine(hm(11, 0), watching(), cash=1.0)
    assert e._current_units() == 1


def test_dynamic_sizing_uses_the_live_margin_and_never_returns_less_than_one_unit():
    # Typhon's own margin_contract_value_divisor/margin_sizing_multiplier (a single observed
    # NATGASMINI quote, engine_configs.py's own docstring) differ from Selene's/Prometheus's 8/4
    # pair -- derive the expected units from CFG directly rather than hardcoding a mismatched
    # formula (a real mechanical-port bug, found and fixed 2026-09-30: sed-porting this test file
    # from Selene's own left the OLD 8/4 formula in place, which happened to still fail loudly
    # here since the raw (uncapped) unit count differs enough to change which side of unit_cap=50
    # it lands on).
    sz = SizingConfig(dynamic=True, static_units=2, unit_cap=50, allocation_rs=None)
    e = engine(hm(11, 0), watching(), ltp=65000.0, cash=1_000_000.0, sizing=sz)
    e.infos = {FRONT.token: type('I', (), {'lot_size': 1})()}
    per_unit = margin_per_unit(65000.0, 1, CFG)
    assert e._current_units() == min(int(1_000_000 // per_unit), 50)
    e.ctx.cash = 10.0
    assert e._current_units() == 1


def test_dynamic_sizing_falls_back_to_the_static_units_when_the_margin_read_fails():
    sz = SizingConfig(dynamic=True, static_units=3, unit_cap=50)
    e = engine(hm(11, 0), watching(), sizing=sz, margin_raises=True)
    assert e._current_units() == 3


def test_the_affordability_check_uses_the_live_margin_even_with_static_sizing():
    e = engine(hm(11, 0), watching(), ltp=65000.0, cash=1_000.0)
    e.infos = {FRONT.token: type('I', (), {'lot_size': 1})()}
    assert e._margin_sufficient(1) is False
    per_unit = margin_per_unit(65000.0, 1, CFG)   # Typhon's own divisor/multiplier, not Selene's 8/4
    e.ctx.cash = per_unit
    assert e._margin_sufficient(1) is True


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
    assert not e.ctx.sent and e.state.last_processed_boundary == '2026-09-03T23:00:00'
    e2 = engine(hm(23, 15), watching('2026-09-03T22:30:00'))
    e2._on_bar(_bar_complete(hm(23, 15)))
    assert len(e2.ctx.sent) == 1 and e2.state.last_processed_boundary == hm(23, 0).isoformat()


# ---- a request in flight is never overlapped -----------------------------------------------------------------------------------

def _in_trade_engine(pending):
    e = TyphonEngine(CFG)
    e.ctx = Ctx(hm(11, 0))
    e.session_date, e.session_open, e.rollover_at = SESSION_DATE, SESSION_OPEN, hm(23, 16)
    e.session_close = hm(23, 30)
    e.contract = FRONT
    e.state = EngineState(status='in_trade', direction='bearish', units=1, entry_price=99.0, contract_token=FRONT.token,
                          lots=1, pending=pending)
    return e


def test_a_flip_signal_never_overlaps_an_outstanding_request():
    e = _in_trade_engine({'typhon-x-t1-exit_all-1': {'purpose': 'exit_all'}})
    assert e._act_on_signal('bullish', True, hm(10, 45), 100.0) is False
    assert not e.ctx.sent and e.state.pending_flip is None


def test_a_flip_signal_in_the_same_direction_does_nothing():
    e = _in_trade_engine({})
    assert e._act_on_signal('bearish', True, hm(10, 45), 100.0) is False and not e.ctx.sent


# ---- the target -- the one thing that has no Selene/Helios equivalent ----------------------------------------------------------

def _priced_engine(direction, sl_price, target_price, ltp):
    e = TyphonEngine(CFG)
    e.ctx = Ctx(hm(11, 0), ltp=ltp)
    e.session_date, e.session_open, e.rollover_at = SESSION_DATE, SESSION_OPEN, hm(23, 16)
    e.session_close = hm(23, 30)
    e.contract = FRONT
    e.state = EngineState(status='in_trade', direction=direction, units=1, entry_price=100.0,
                          entry_ts='2026-09-03T09:30:00', contract_token=FRONT.token, contract_symbol=FRONT.symbol,
                          contract_expiry=FRONT.expiry.isoformat(), lots=1, sl_price=sl_price, target_price=target_price,
                          trade_counter=3)
    return e


def test_a_target_hit_exits_the_whole_position_via_send_exit_all():
    e = _priced_engine('bullish', sl_price=99.2, target_price=115.0, ltp=116.0)
    e._check_stop(hm(11, 0))
    assert len(e.ctx.sent) == 1
    req = e.ctx.sent[0]
    assert req.reason == ExitReason.OTHER, 'a target hit is OTHER, same convention as Prometheus\'s own target legs'
    assert e.state.pending[req.request_id]['reason'] == 'target'


def test_the_stop_is_checked_before_the_target_and_returns_without_evaluating_it():
    """A hit stop exits immediately -- the target branch is never reached on the same call. (A single LTP can never
    cross both levels of a real bullish/bearish position at once, since the target sits on the opposite side of entry
    from the stop; this checks the CODE ORDER matches the backtest's own "stop wins a same-bar tie" convention,
    typhon_backtest/parity_backtest_typhon.py's scan_exit(), for the one case that's actually reachable live.)"""
    e = _priced_engine('bullish', sl_price=99.2, target_price=101.0, ltp=98.0)
    e._check_stop(hm(11, 0))
    assert len(e.ctx.sent) == 1
    req = e.ctx.sent[0]
    assert req.reason == ExitReason.STOP_LOSS
    assert e.state.pending[req.request_id]['reason'] == 'stop_loss'


def test_a_bearish_target_hit_checks_the_correct_direction():
    e = _priced_engine('bearish', sl_price=100.8, target_price=85.0, ltp=84.0)
    e._check_stop(hm(11, 0))
    assert len(e.ctx.sent) == 1 and e.state.pending[e.ctx.sent[0].request_id]['reason'] == 'target'


def test_neither_level_hit_is_a_no_op():
    e = _priced_engine('bullish', sl_price=99.2, target_price=115.0, ltp=105.0)
    e._check_stop(hm(11, 0))
    assert not e.ctx.sent


def test_a_none_target_never_fires_sl_only_shape_still_works():
    """Levels.build_levels() returns target_price=None when target_pct is falsy -- confirms _check_stop degrades cleanly
    to Selene's/Helios's own SL-only behaviour if Typhon's config ever loses its target."""
    e = _priced_engine('bullish', sl_price=99.2, target_price=None, ltp=999.0)
    e._check_stop(hm(11, 0))
    assert not e.ctx.sent, 'no target configured means no target check, whatever the LTP'
