"""
Fake Hestia, execution side of the contract: one request per decision, Hestia-owned retries, idempotency, the fill
confirmation invariant, priority, admission, dependencies, restart-facing request_status.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from hestia_fake_helpers import (FRONT, NEXT, NEAR, OTHER_FRONT, SESSION_DATE, SPEC_YY, RecEngine, events_of, factory,
                                 minutes_frame, world)
from hestia_core.fake import BrokerReply, ContractSpec, FakeConfig, _Bucket
from hestia_core.interface import (AckStatus, CloseRequest, Direction, ExitReason, FlattenRequest, FlipRequest, OpenRequest,
                                   OutcomeStatus, RequestKind, RequestOutcome, SessionStart, SizingConfig)

T0 = datetime(2026, 9, 3, 9, 0)


@pytest.fixture
def hestias():
    made = []

    def build(*a, **k):
        h = world(*a, **k)
        h.set_price('T1', 100.0)
        h.set_price('T2', 101.0)
        h.set_price('T0', 99.0)
        made.append(h)
        return h
    yield build
    for h in made:
        h.close()


def go(h, seconds=120):
    h.start_session(SESSION_DATE)
    h.run_until(T0 + timedelta(seconds=seconds))


def submit_on_start(*reqs, acks=None):
    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            for r in reqs:
                a = ctx.submit(r)
                if acks is not None:
                    acks.append(a)
    return act


def outcomes(log):
    return events_of(log, RequestOutcome)


def open_req(rid='r1', lots=3, direction=Direction.BULLISH, contract=FRONT, **kw):
    return OpenRequest(rid, contract, direction, lots, trade_ref=1, **kw)


def test_open_fills_and_updates_the_ledger(hestias):
    log, acks = [], []
    h = hestias([('a', factory(log=log, act=submit_on_start(open_req(), acks=acks)))])
    go(h)
    assert [a.status for a in acks] == [AckStatus.ACCEPTED]
    (o,) = outcomes(log)
    assert o.status == OutcomeStatus.FILLED and o.confirmed and o.kind == RequestKind.OPEN
    assert o.opened.lots == 3 and o.opened.avg_price == 100.0 and o.closed is None
    assert h.held('a', FRONT) == 3
    assert [(x.side, x.lots) for x in h.orders] == [('BUY', 3)]


def test_duplicate_request_id_never_sends_a_second_order(hestias):
    log, acks = [], []

    def act(eng, ctx, ev):
        submit_on_start(open_req(), open_req(), acks=acks)(eng, ctx, ev)
        if isinstance(ev, RequestOutcome) and len(acks) == 2:       # once, after the outcome is final
            acks.append(ctx.submit(open_req()))
    h = hestias([('a', factory(log=log, act=act))])
    go(h)
    assert [a.status for a in acks] == [AckStatus.ACCEPTED, AckStatus.DUPLICATE, AckStatus.DUPLICATE]
    assert 'filled' in acks[2].detail
    assert len(h.orders) == 1 and h.held('a', FRONT) == 3 and len(outcomes(log)) == 1


def test_broker_rejections_are_retried_by_hestia_and_hidden_from_the_engine(hestias):
    log = []
    h = hestias([('a', factory(log=log, act=submit_on_start(open_req())))],
                broker=lambda c: BrokerReply('reject') if c.attempt <= 3 else BrokerReply('fill'))
    go(h)
    (o,) = outcomes(log)
    assert o.status == OutcomeStatus.FILLED
    assert len(h.rejections) == 3 and len(h.orders) == 1 and h.held('a', FRONT) == 3


def test_entry_rejected_after_retries_is_reported_once(hestias):
    log = []
    h = hestias([('a', factory(log=log, act=submit_on_start(open_req())))], broker=lambda c: BrokerReply('reject'))
    go(h)
    (o,) = outcomes(log)
    assert o.status == OutcomeStatus.REJECTED and not o.confirmed
    assert h.orders == [] and h.held('a', FRONT) == 0
    assert any(a.level == 'critical' for a in h.alerts)


def test_order_placement_error_is_recovered_from_the_order_book(hestias):
    log = []
    h = hestias([('a', factory(log=log, act=submit_on_start(open_req())))], broker=lambda c: BrokerReply('ghost'))
    go(h)
    (o,) = outcomes(log)
    assert o.status == OutcomeStatus.FILLED and len(h.orders) == 1 and h.held('a', FRONT) == 3


def test_partial_entry_is_reported_and_never_topped_up(hestias):
    log = []
    h = hestias([('a', factory(log=log, act=submit_on_start(open_req(lots=5))))],
                broker=lambda c: BrokerReply('partial', lots=2))
    go(h)
    (o,) = outcomes(log)
    assert o.status == OutcomeStatus.PARTIAL and o.confirmed and o.opened.lots == 2 and o.requested_lots == 5
    assert len(h.orders) == 1 and h.held('a', FRONT) == 2


def test_partial_exit_is_completed_by_hestia(hestias):
    log = []
    close = CloseRequest('c1', FRONT, Direction.BULLISH, ExitReason.TREND_FLIP)
    h = hestias([('a', factory(log=log, act=submit_on_start(close)))],
                broker=lambda c: BrokerReply('partial', lots=2) if c.attempt == 1 else BrokerReply('fill'))
    h.seed_position('a', FRONT, 5, 98.0)
    go(h)
    (o,) = outcomes(log)
    assert o.status == OutcomeStatus.FILLED and o.closed.lots == 5
    assert [(x.side, x.lots) for x in h.orders] == [('SELL', 5), ('SELL', 3)]
    assert h.held('a', FRONT) == 0


def _unconfirmed_broker(lots=None, resolve_after=None):
    return lambda c: (BrokerReply('unconfirmed', lots=lots, resolve_after=resolve_after)
                      if c.request.request_id == 'r1' else BrokerReply('fill'))


def _react_on_unconfirmed(*follow_ups):
    """Submit `follow_ups` (once) the moment the first request is reported UNCONFIRMED."""
    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.submit(open_req('r1'))
        if isinstance(ev, RequestOutcome) and ev.request_id == 'r1' and ev.status == OutcomeStatus.UNCONFIRMED:
            for r in follow_ups:
                ctx.submit(r)
    return act


def test_unconfirmed_is_settled_against_the_broker_book_before_a_later_request_acts(hestias):
    """User decision 2026-09-28: reconcile against the broker's positions and act on what it shows."""
    log = []
    close = CloseRequest('c1', FRONT, Direction.BULLISH, ExitReason.TREND_FLIP)
    h = hestias([('a', factory(log=log, act=_react_on_unconfirmed(close, open_req('r2'))))], broker=_unconfirmed_broker())
    go(h, 200)
    out = [(o.request_id, o.status) for o in outcomes(log)]
    assert out == [('r1', OutcomeStatus.UNCONFIRMED), ('r1', OutcomeStatus.FILLED), ('c1', OutcomeStatus.FILLED),
                   ('r2', OutcomeStatus.FILLED)]
    by = {o.request_id: o for o in outcomes(log) if o.status != OutcomeStatus.UNCONFIRMED}
    assert 'broker book' in by['r1'].detail and by['r1'].confirmed
    assert by['c1'].closed.lots == 3 and by['c1'].detail != 'already flat', 'the close acted on the reconciled position'
    t = {(e.request_id, e.status): when for when, e in log if isinstance(e, RequestOutcome)}
    assert t[('r1', OutcomeStatus.UNCONFIRMED)] == h.orders[0].ts + timedelta(seconds=30)
    assert t[('r1', OutcomeStatus.FILLED)] == t[('r1', OutcomeStatus.UNCONFIRMED)] + timedelta(seconds=1)
    assert [(o.request_id, o.side, o.lots) for o in h.orders] == [('r1', 'BUY', 3), ('c1', 'SELL', 3), ('r2', 'BUY', 3)]
    assert h.held('a', FRONT) == 3
    assert any(a.level == 'critical' and 'not confirmed' in a.text for a in h.alerts)


def test_stop_loss_flatten_while_unconfirmed_acts_on_the_reconciled_position(hestias):
    log = []
    stop = FlattenRequest('f1', FRONT, ExitReason.STOP_LOSS)
    h = hestias([('a', factory(log=log, act=_react_on_unconfirmed(stop)))], broker=_unconfirmed_broker())
    go(h, 200)
    by = {o.request_id: o for o in outcomes(log) if o.status != OutcomeStatus.UNCONFIRMED}
    assert by['r1'].status == OutcomeStatus.FILLED and by['f1'].status == OutcomeStatus.FILLED
    assert by['f1'].closed.lots == 3 and h.held('a', FRONT) == 0
    assert [o.side for o in h.orders] == ['BUY', 'SELL']


def test_unconfirmed_order_that_never_executed_is_rejected_and_the_close_is_then_genuinely_flat(hestias):
    log = []
    close = CloseRequest('c1', FRONT, Direction.BULLISH, ExitReason.TREND_FLIP)
    h = hestias([('a', factory(log=log, act=_react_on_unconfirmed(close)))], broker=_unconfirmed_broker(lots=0))
    go(h, 200)
    by = {o.request_id: o for o in outcomes(log) if o.status != OutcomeStatus.UNCONFIRMED}
    assert by['r1'].status == OutcomeStatus.REJECTED and 'no fill' in by['r1'].detail
    assert by['c1'].status == OutcomeStatus.FILLED and by['c1'].detail == 'already flat'
    assert [o.request_id for o in h.orders] == ['r1'] and h.held('a', FRONT) == 0


def test_unconfirmed_partial_found_by_reconciliation_is_reported_as_partial(hestias):
    log = []
    h = hestias([('a', factory(log=log, act=submit_on_start(open_req('r1', lots=5))))], broker=_unconfirmed_broker(lots=2))
    go(h, 200)
    final = outcomes(log)[-1]
    assert final.status == OutcomeStatus.PARTIAL and final.opened.lots == 2 and h.held('a', FRONT) == 2


def test_hestia_keeps_reconciling_on_its_own_when_no_request_asks(hestias):
    log = []
    h = hestias([('a', factory(log=log, act=submit_on_start(open_req('r1'))))], broker=_unconfirmed_broker())
    go(h, 300)
    t = {e.status: when for when, e in log if isinstance(e, RequestOutcome)}
    assert t[OutcomeStatus.FILLED] - t[OutcomeStatus.UNCONFIRMED] == timedelta(seconds=61)    # 60 s interval + 1 s read
    assert h.held('a', FRONT) == 3


def test_without_periodic_reconciliation_an_unconfirmed_request_stays_unconfirmed(hestias):
    seen = {}

    def act(eng, ctx, ev):
        submit_on_start(open_req('r1'))(eng, ctx, ev)
        seen['status'] = ctx.request_status('r1').status
    h = hestias([('a', factory(act=act, timeout=100))], broker=_unconfirmed_broker(),
                config=FakeConfig(reconcile_interval_s=None))
    go(h, 600)
    assert h.request_record('a', 'r1').state == 'unconfirmed' and h.held('a', FRONT) == 0


def test_close_when_flat_is_filled_with_zero_lots_and_sends_nothing(hestias):
    log = []
    close = CloseRequest('c1', FRONT, Direction.BULLISH, ExitReason.TREND_FLIP)
    h = hestias([('a', factory(log=log, act=submit_on_start(close, FlattenRequest('f1', FRONT, ExitReason.MANUAL_EXIT))))])
    go(h)
    assert [(o.status, o.closed.lots, o.detail) for o in outcomes(log)] == [(OutcomeStatus.FILLED, 0, 'already flat')] * 2
    assert h.orders == []


def test_close_with_the_wrong_expected_direction_is_rejected(hestias):
    log = []
    close = CloseRequest('c1', FRONT, Direction.BEARISH, ExitReason.TREND_FLIP)
    h = hestias([('a', factory(log=log, act=submit_on_start(close)))])
    h.seed_position('a', FRONT, 3, 98.0)
    go(h)
    (o,) = outcomes(log)
    assert o.status == OutcomeStatus.REJECTED and 'bullish' in o.detail
    assert h.orders == [] and h.held('a', FRONT) == 3


def test_closing_more_lots_than_held_is_rejected(hestias):
    log = []
    h = hestias([('a', factory(log=log, act=submit_on_start(CloseRequest('c1', FRONT, Direction.BULLISH, ExitReason.TREND_FLIP, lots=9))))])
    h.seed_position('a', FRONT, 3, 98.0)
    go(h)
    assert outcomes(log)[0].status == OutcomeStatus.REJECTED and h.orders == []


def test_flip_is_one_netted_order_and_reports_both_halves(hestias):
    log = []
    flip = FlipRequest('f1', FRONT, Direction.BEARISH, close_lots=3, open_lots=3, trade_ref=2)
    h = hestias([('a', factory(log=log, act=submit_on_start(flip)))])
    h.seed_position('a', FRONT, -3, 102.0)
    go(h)
    (o,) = outcomes(log)
    assert o.status == OutcomeStatus.FILLED and o.kind == RequestKind.FLIP
    assert o.closed.lots == 3 and o.opened.lots == 3
    assert [(x.side, x.lots) for x in h.orders] == [('BUY', 6)]
    assert h.held('a', FRONT) == 3


def test_flip_partial_fill_completes_the_close_before_opening(hestias):
    log = []
    flip = FlipRequest('f1', FRONT, Direction.BEARISH, close_lots=3, open_lots=3, trade_ref=2)
    h = hestias([('a', factory(log=log, act=submit_on_start(flip)))],
                broker=lambda c: BrokerReply('partial', lots=2) if c.attempt == 1 else BrokerReply('fill'))
    h.seed_position('a', FRONT, -3, 102.0)
    go(h)
    (o,) = outcomes(log)
    assert o.status == OutcomeStatus.FILLED and o.closed.lots == 3 and o.opened.lots == 3
    assert [x.lots for x in h.orders] == [6, 4]
    assert h.held('a', FRONT) == 3


def test_flip_when_flat_is_rejected(hestias):
    log = []
    h = hestias([('a', factory(log=log, act=submit_on_start(FlipRequest('f1', FRONT, Direction.BEARISH, 3, 3, 2))))])
    go(h)
    assert outcomes(log)[0].status == OutcomeStatus.REJECTED and h.orders == []


def _roll_pair(contract_close=FRONT, contract_open=NEXT):
    close = CloseRequest('roll-c', contract_close, Direction.BULLISH, ExitReason.ROLL)
    reopen = OpenRequest('roll-o', contract_open, Direction.BULLISH, 3, trade_ref=2, roll_reopen=True, depends_on='roll-c')
    return close, reopen


def test_depends_on_holds_the_reopen_until_the_close_fills(hestias):
    log = []
    h = hestias([('a', factory(log=log, act=submit_on_start(*_roll_pair())))],
                broker=lambda c: BrokerReply('fill', latency=5.0) if c.request.request_id == 'roll-c' else BrokerReply('fill'))
    h.seed_position('a', FRONT, 3, 98.0)
    go(h)
    close_fill = [t for t, e in log if isinstance(e, RequestOutcome) and e.request_id == 'roll-c'][0]
    open_order = [o for o in h.orders if o.request_id == 'roll-o'][0]
    assert open_order.ts >= close_fill
    assert [o.status for o in outcomes(log)] == [OutcomeStatus.FILLED, OutcomeStatus.FILLED]
    assert h.held('a', FRONT) == 0 and h.held('a', NEXT) == 3


def test_failed_close_fails_its_dependent_reopen_without_an_order(hestias):
    log = []
    h = hestias([('a', factory(log=log, act=submit_on_start(*_roll_pair())))],
                broker=lambda c: BrokerReply('reject'))
    h.seed_position('a', FRONT, 3, 98.0)
    go(h, 600)
    st = {o.request_id: o.status for o in outcomes(log)}
    assert st == {'roll-c': OutcomeStatus.REJECTED, 'roll-o': OutcomeStatus.DEPENDENCY_FAILED}
    assert h.orders == [] and h.held('a', NEXT) == 0


def test_unknown_dependency_is_invalid(hestias):
    acks = []
    req = OpenRequest('o1', FRONT, Direction.BULLISH, 1, trade_ref=1, depends_on='ghost')
    h = hestias([('a', factory(act=submit_on_start(req, acks=acks)))])
    go(h)
    assert acks[0].status == AckStatus.INVALID and h.orders == []


def test_contract_of_another_instrument_is_invalid(hestias):
    acks = []
    other = ContractSpec(OTHER_FRONT, minutes=minutes_frame(50.0, 3))
    req = OpenRequest('o1', OTHER_FRONT, Direction.BULLISH, 1, trade_ref=1)
    h = hestias([('a', factory(act=submit_on_start(req, acks=acks)))], extra=[other])
    go(h)
    assert acks[0].status == AckStatus.INVALID and h.orders == []


def test_unit_cap_refuses_an_oversized_entry(hestias):
    log = []
    h = hestias([('a', factory(log=log, act=submit_on_start(open_req(lots=3))),
                 {'sizing': SizingConfig(dynamic=False, static_units=1, unit_cap=2)})])
    go(h)
    (o,) = outcomes(log)
    assert o.status == OutcomeStatus.LIMIT_REFUSED and 'unit cap' in o.detail and h.orders == []


def test_unit_cap_counts_lots_per_unit(hestias):
    log = []
    h = hestias([('a', factory(log=log, act=submit_on_start(open_req(lots=4))),
                 {'sizing': SizingConfig(dynamic=False, static_units=1, unit_cap=2), 'lots_per_unit': 2})])
    go(h)
    assert outcomes(log)[0].status == OutcomeStatus.FILLED


def test_entry_inside_the_roll_window_is_refused_but_exits_are_not(hestias):
    log = []
    close = CloseRequest('c1', NEAR, Direction.BULLISH, ExitReason.ROLL)
    h = hestias([('a', factory(log=log, act=submit_on_start(open_req('o1', contract=NEAR), close)))],
                contracts=(FRONT, NEXT, NEAR))
    h.seed_position('a', NEAR, 2, 98.0)
    go(h)
    st = {o.request_id: o for o in outcomes(log)}
    assert st['o1'].status == OutcomeStatus.LIMIT_REFUSED and 'roll window' in st['o1'].detail
    assert st['c1'].status == OutcomeStatus.FILLED and h.held('a', NEAR) == 0


def test_margin_admission_and_release(hestias):
    log = []

    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.submit(open_req('a1', lots=2))
        if isinstance(ev, RequestOutcome):
            if ev.request_id == 'a1':
                ctx.submit(open_req('b1', lots=1))
            if ev.request_id == 'b1':
                ctx.submit(CloseRequest('c1', FRONT, Direction.BULLISH, ExitReason.TREND_FLIP))
            if ev.request_id == 'c1':
                ctx.submit(open_req('d1', lots=1))
    cfg = FakeConfig(available_cash=1000.0)            # margin per lot = 100 x 10 / 2 = 500
    h = hestias([('a', factory(log=log, act=act))], config=cfg)
    go(h)
    st = [(o.request_id, o.status) for o in outcomes(log)]
    assert st == [('a1', OutcomeStatus.FILLED), ('b1', OutcomeStatus.MARGIN_REFUSED), ('c1', OutcomeStatus.FILLED),
                  ('d1', OutcomeStatus.FILLED)]
    assert h.held('a', FRONT) == 1


def test_request_status_distinguishes_unknown_in_flight_and_final(hestias):
    seen = {}

    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.submit(open_req('r1'))
            seen['unknown'] = ctx.request_status('nope')
            seen['inflight'] = ctx.request_status('r1')
        if isinstance(ev, RequestOutcome):
            seen['final'] = ctx.request_status('r1')
            seen['outcome'] = ev
    h = hestias([('a', factory(act=act))])
    go(h)
    assert seen['unknown'] is None
    assert seen['inflight'].status == OutcomeStatus.IN_FLIGHT and not seen['inflight'].confirmed
    assert seen['final'] == seen['outcome'] and seen['final'].status == OutcomeStatus.FILLED


def _two_engines(hestias, act_a, act_b, **kw):
    other = ContractSpec(OTHER_FRONT, lot_size=10, minutes=minutes_frame(50.0, 3))
    log_a, log_b = kw.pop('log_a', None), kw.pop('log_b', None)
    h = hestias([('b', lambda: RecEngine('b', spec=SPEC_YY, trade=OTHER_FRONT, act=act_b, log=log_b)),
                 ('a', lambda: RecEngine('a', act=act_a, log=log_a))], extra=[other], **kw)
    h.set_price('U1', 50.0)
    return h


def test_stop_loss_is_served_before_an_entry_submitted_earlier_at_the_same_instant(hestias):
    stop = CloseRequest('stop', FRONT, Direction.BULLISH, ExitReason.STOP_LOSS)
    entry = OpenRequest('entry', OTHER_FRONT, Direction.BULLISH, 2, trade_ref=1)
    h = _two_engines(hestias, submit_on_start(stop), submit_on_start(entry), config=FakeConfig(workers=1))
    h.seed_position('a', FRONT, 2, 98.0)
    go(h)
    assert [(e, r) for e, r in h.dispatch_log[0][1]] == [('b', 'entry'), ('a', 'stop')], 'both must be queued at one dispatch'
    assert [o.request_id for o in h.orders] == ['stop', 'entry']


def test_priority_order_across_all_five_classes(hestias):
    """Submitted in exactly the reverse of service order, by two engines, all queued in one dispatch."""
    from datetime import date
    from hestia_core.interface import ContractRef
    next2 = ContractRef('XX', 'T3', 'XX29JAN27FUT', date(2027, 1, 29))
    yy_next = ContractRef('YY', 'U2', 'YY30NOV26FUT', date(2026, 11, 30))
    extra = [ContractSpec(OTHER_FRONT, lot_size=10, minutes=minutes_frame(50.0, 3)),
             ContractSpec(yy_next, lot_size=10, minutes=minutes_frame(52.0, 4))]

    def act_b(eng, ctx, ev):                       # engine b: roll re-open (class 5), roll exit (class 3)
        if isinstance(ev, SessionStart):
            ctx.submit(OpenRequest('reopen', yy_next, Direction.BULLISH, 1, trade_ref=2, roll_reopen=True))
            ctx.submit(CloseRequest('roll', OTHER_FRONT, Direction.BULLISH, ExitReason.ROLL))

    def act_a(eng, ctx, ev):                       # engine a: entry (4), close (2), stop (1)
        if isinstance(ev, SessionStart):
            ctx.submit(OpenRequest('open', next2, Direction.BULLISH, 1, trade_ref=1))
            ctx.submit(CloseRequest('close', NEXT, Direction.BULLISH, ExitReason.TREND_FLIP))
            ctx.submit(CloseRequest('stop', FRONT, Direction.BULLISH, ExitReason.STOP_LOSS))
    h = hestias([('b', lambda: RecEngine('b', spec=SPEC_YY, trade=OTHER_FRONT, act=act_b)),
                 ('a', lambda: RecEngine('a', act=act_a))], contracts=(FRONT, NEXT, next2), extra=extra,
                config=FakeConfig(workers=1))
    h.set_price('U1', 50.0)
    h.set_price('U2', 52.0)
    h.set_price('T3', 102.0)
    h.seed_position('a', FRONT, 1, 98.0)
    h.seed_position('a', NEXT, 1, 98.0)
    h.seed_position('b', OTHER_FRONT, 1, 49.0)
    go(h)
    queued_together = {r for _, r in h.dispatch_log[0][1]}
    assert queued_together == {'reopen', 'roll', 'open', 'close', 'stop'}, 'all five must be in the same dispatch'
    assert [o.request_id for o in h.orders] == ['stop', 'close', 'roll', 'open', 'reopen']


def test_a_slow_fill_for_one_engine_does_not_delay_another_engines_stop(hestias):
    log_a, log_b = [], []
    slow = OpenRequest('slow', FRONT, Direction.BULLISH, 1, trade_ref=1)
    stop = CloseRequest('stop', OTHER_FRONT, Direction.BULLISH, ExitReason.STOP_LOSS)
    h = _two_engines(hestias, submit_on_start(slow), submit_on_start(stop), log_a=log_a, log_b=log_b,
                     broker=lambda c: BrokerReply('fill', latency=30.0) if c.request.request_id == 'slow' else BrokerReply('fill'))
    h.seed_position('b', OTHER_FRONT, 1, 49.0)
    go(h)
    t_a = [t for t, e in log_a if isinstance(e, RequestOutcome)][0]
    t_b = [t for t, e in log_b if isinstance(e, RequestOutcome)][0]
    assert t_a - T0 >= timedelta(seconds=30) and t_b - T0 < timedelta(seconds=2)


def test_same_engine_same_contract_requests_are_served_in_submit_order(hestias):
    log = []
    close = CloseRequest('c1', FRONT, Direction.BULLISH, ExitReason.STOP_LOSS)     # higher priority, but submitted second
    h = hestias([('a', factory(log=log, act=submit_on_start(open_req('o1', lots=2), close)))],
                broker=lambda c: BrokerReply('fill', latency=5.0))
    go(h)
    assert [o.request_id for o in h.orders] == ['o1', 'c1']
    assert [o.status for o in outcomes(log)] == [OutcomeStatus.FILLED, OutcomeStatus.FILLED]
    assert h.held('a', FRONT) == 0


def test_order_budget_keeps_a_reserved_slice_for_exits():
    now = datetime(2026, 9, 3, 9, 0)
    b = _Bucket(rate=10.0, reserved=4.0, now=now)
    normal = [b.take(now, high=False) for _ in range(10)]
    assert normal[:6] == [0.0] * 6 and all(w > 0 for w in normal[6:]), 'entries can use only the unreserved 6 of 10'
    assert [b.take(now, high=True) for _ in range(4)] == [0.0] * 4, 'exits still get the reserved 4'
    assert b.take(now, high=True) > 0
    assert b.take(now + timedelta(seconds=1), high=False) == 0.0, 'the budget refills with time'


def test_stop_is_not_queued_behind_saturated_entries_when_workers_are_scarce(hestias):
    """Two workers, one reserved for exits: entries can hold at most one, so a stop starts at once even with entries waiting."""
    log_a, log_b = [], []

    def act_a(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.submit(OpenRequest('slow1', FRONT, Direction.BULLISH, 1, trade_ref=1))
            ctx.submit(OpenRequest('slow2', NEXT, Direction.BULLISH, 1, trade_ref=2))
    stop = CloseRequest('stop', OTHER_FRONT, Direction.BULLISH, ExitReason.STOP_LOSS)
    h = _two_engines(hestias, act_a, submit_on_start(stop), log_a=log_a, log_b=log_b, config=FakeConfig(workers=2),
                     broker=lambda c: BrokerReply('fill', latency=30.0) if c.request.request_id.startswith('slow') else BrokerReply('fill'))
    h.seed_position('b', OTHER_FRONT, 1, 49.0)
    go(h, 200)
    t_stop = [t for t, e in log_b if isinstance(e, RequestOutcome)][0]
    slow = [t for t, e in log_a if isinstance(e, RequestOutcome)]
    assert t_stop - T0 < timedelta(seconds=2)
    assert [round((t - T0).total_seconds()) for t in slow] == [30, 60], 'entries were held to one worker, in submit order'


def _pending_broker(lots=None, pending_for=120.0):
    return lambda c: (BrokerReply('unconfirmed', lots=lots, pending_for=pending_for)
                      if c.request.request_id == 'r1' else BrokerReply('fill'))


def test_order_still_working_at_the_broker_is_never_reported_as_no_fill(hestias):
    """A DPL lock can hold a market order open. While the broker's row says working, the request stays UNCONFIRMED, a later
    close waits, Hestia keeps re-reading, and the close then acts on what the order finally did."""
    log = []
    close = CloseRequest('c1', FRONT, Direction.BULLISH, ExitReason.TREND_FLIP)
    h = hestias([('a', factory(log=log, act=_react_on_unconfirmed(close)))], broker=_pending_broker())
    go(h, 400)
    out = [(o.request_id, o.status) for o in outcomes(log)]
    assert out == [('r1', OutcomeStatus.UNCONFIRMED), ('r1', OutcomeStatus.FILLED), ('c1', OutcomeStatus.FILLED)]
    t = {(e.request_id, e.status): when for when, e in log if isinstance(e, RequestOutcome)}
    placed = h.orders[0].ts
    assert t[('r1', OutcomeStatus.FILLED)] - placed >= timedelta(seconds=120), 'not settled while the order was still working'
    assert h.request_record('a', 'r1').pending_reads >= 15, 'kept re-reading every few seconds'
    assert [(o.request_id, o.side, o.lots) for o in h.orders] == [('r1', 'BUY', 3), ('c1', 'SELL', 3)]
    assert h.held('a', FRONT) == 0
    assert sum('still working' in a.text for a in h.alerts_for('critical')) == 1


def test_a_working_order_that_finally_shows_no_fill_is_rejected_only_when_the_broker_says_so(hestias):
    log = []
    h = hestias([('a', factory(log=log, act=submit_on_start(open_req('r1'))))], broker=_pending_broker(lots=0, pending_for=100.0))
    go(h, 90)
    assert [o.status for o in outcomes(log)] == [OutcomeStatus.UNCONFIRMED], 'still working at 90 s: not a no-fill'
    h.run_until(T0 + timedelta(seconds=300))
    assert [o.status for o in outcomes(log)] == [OutcomeStatus.UNCONFIRMED, OutcomeStatus.REJECTED]
    assert h.held('a', FRONT) == 0


def test_the_simulated_ports_satisfy_the_port_protocols(hestias):
    from hestia_core.ports import BrokerPort, DataPort, Scheduler
    h = hestias([])
    assert isinstance(h.kernel, Scheduler) and isinstance(h.broker, BrokerPort) and isinstance(h.data, DataPort)


def test_no_request_is_admitted_at_or_after_the_session_close(hestias):
    """Production refused every order after the closing time; a stop fired by a tick after the close can never fill."""
    log, acks, seen = [], [], {}

    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.wait((23 * 3600 + 31 * 60) - (9 * 3600))                    # 23:31, past the last minute of data (23:29)
            seen['close'] = ctx.session_open()
            acks.append(ctx.submit(open_req('late')))
            acks.append(ctx.submit(FlattenRequest('late-flat', FRONT, ExitReason.STOP_LOSS)))
    h = hestias([('a', factory(log=log, act=act))])
    h.start_session(SESSION_DATE)
    h.run_until(datetime(2026, 9, 3, 23, 45))
    outs = {o.request_id: o for o in outcomes(log)}
    assert outs['late'].status == OutcomeStatus.LIMIT_REFUSED and 'market closed' in outs['late'].detail
    assert outs['late-flat'].status == OutcomeStatus.LIMIT_REFUSED
    assert not h.orders
