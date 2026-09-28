"""
AngelBrokerPort against the scripted SmartConnect double (never a real login): per-contract lot size, freeze chunking,
rejection and partial handling, ghost-order recovery with the shared placed-id guard, rate-limit and lost-response
handling, unconfirmed orders settled by their own order-book row, the position book, cash, and the whole thing under the core.
"""
import json
from datetime import date, datetime, timedelta

import pytest

from smartapi_double import FakeClock, ScriptedSmartConnect
from hestia_fake_helpers import FRONT, NEXT, SESSION_DATE, SPEC, RecEngine, events_of, minutes_frame
from hestia_core.angel_broker import AngelBrokerPort, AngelConfig
from hestia_core.core import CoreConfig, HestiaCore
from hestia_core.executors import InlineExecutor
from hestia_core.fake_kernel import EngineTask, SimKernel
from hestia_core.gateway import BrokerGateway
from hestia_core.interface import (CloseRequest, ContractInfo, Direction, ExitReason, OpenRequest, OutcomeStatus,
                                   RequestOutcome, SessionStart)
from hestia_core.order_feed import OrderUpdateFeed
from hestia_core.ports import BrokerPort, OrderSpec, PositionRow
from hestia_core.replay import ContractSpec, ReplayData

LOT = 10


class Rig:
    def __init__(self, freeze=10, cfg=None, feed=None, closing=None):
        self.clock = FakeClock()
        self.sc = ScriptedSmartConnect(self.clock)
        self.gw = BrokerGateway(self.sc, clock=self.clock.monotonic, sleep=self.clock.sleep)
        self.kernel = SimKernel(datetime(2026, 9, 3, 12, 0))
        self.alerts = []
        info = ContractInfo(FRONT, LOT, 0.5, freeze, 40)
        self.port = AngelBrokerPort(self.gw, self.kernel, InlineExecutor(), lambda tok: LOT if tok == 'T1' else None,
                                    lambda ref: info if ref.token == 'T1' else None, cfg, feed, self.clock.wall_now,
                                    lambda: closing, self.clock.sleep, self.clock.monotonic,
                                    alert=lambda level, text: self.alerts.append((level, text)))

    def place(self, lots=3, side='BUY', priority=4):
        out = []
        self.port.place(OrderSpec('a', 'r1', FRONT, side, lots, 1, priority), out.append)
        self.kernel.run_for(0)
        return out[0]

    def read(self, order_id):
        out = []
        self.port.read_order(order_id, out.append)
        self.kernel.run_for(0)
        return out[0]

    def placed(self):
        return [c for c in self.sc.calls if c[0] == 'place']


def test_the_adapter_satisfies_the_broker_port():
    assert isinstance(Rig().port, BrokerPort)


def test_a_fill_uses_the_contracts_own_lot_size_and_reports_lots_and_price():
    r = Rig()
    res = r.place(3)
    assert (res.kind, res.lots, res.price) == ('filled', 3, 100.0) and res.order_id
    (call,) = r.placed()
    assert call[1]['quantity'] == '30' and call[1]['exchange'] == 'MCX' and call[1]['producttype'] == 'CARRYFORWARD'
    assert call[1]['tradingsymbol'] == FRONT.symbol and call[1]['symboltoken'] == 'T1' and call[1]['transactiontype'] == 'BUY'


def test_a_large_order_is_chunked_to_the_freeze_quantity_and_aggregated():
    r = Rig(freeze=2)
    res = r.place(5)
    assert [c[1]['quantity'] for c in r.placed()] == ['20', '20', '10']
    assert (res.kind, res.lots) == ('filled', 5) and res.order_id.count(',') == 2


def test_a_rejected_later_chunk_settles_what_was_placed_as_partial_and_sends_no_more():
    r = Rig(freeze=2)
    r.sc.place_script = ['ok', 'reject']
    res = r.place(5)
    assert (res.kind, res.lots) == ('partial', 2) and len(r.placed()) == 2


def test_a_broker_refusal_is_reported_rejected_and_not_retried_here():
    r = Rig()
    r.sc.place_script = ['reject']
    res = r.place(3)
    assert res.kind == 'rejected' and 'Margin' in res.detail and len(r.placed()) == 1, 'the core owns rejection retries'


def test_an_order_the_exchange_cancelled_after_a_partial_fill_reports_the_partial():
    r = Rig()
    r.sc.place_script = [('partial', 20)]
    res = r.place(3)
    assert (res.kind, res.lots, res.price) == ('partial', 2, 100.0)


def test_an_order_the_exchange_rejected_with_nothing_filled_is_rejected():
    r = Rig()
    r.sc.place_script = [('partial', 0)]
    assert r.place(3).kind == 'rejected'


def test_rate_limit_errors_are_waited_out_and_the_order_is_sent_once():
    r = Rig()
    r.sc.place_script = ['ratelimit', 'ratelimit', 'ok']
    res = r.place(3)
    assert res.kind == 'filled' and len(r.placed()) == 3 and len(r.sc.orders) == 1
    assert r.clock.t >= 2 * AngelConfig().rate_limit_cooldown_s


def test_a_lost_response_is_recovered_from_the_order_book_without_a_second_order():
    r = Rig()
    r.sc.place_script = ['ghost']
    res = r.place(3)
    assert (res.kind, res.lots) == ('filled', 3) and len(r.placed()) == 1 and len(r.sc.orders) == 1
    assert res.order_id == r.sc.orders[0]['orderid']


def test_ghost_recovery_never_claims_an_order_this_process_already_owns_or_a_stale_one():
    r = Rig()
    old = r.clock.wall_now() - timedelta(minutes=10)
    stale = r.sc._new_order({'tradingsymbol': FRONT.symbol, 'symboltoken': 'T1', 'transactiontype': 'BUY', 'quantity': '30'},
                            'complete', 30, ts=old)
    other = r.sc._new_order({'tradingsymbol': FRONT.symbol, 'symboltoken': 'T1', 'transactiontype': 'BUY', 'quantity': '30'},
                            'complete', 30)
    r.port._placed_ids.add(other['orderid'])               # another engine's identical order, placed by this process
    r.sc.place_script = ['ghost']
    res = r.place(3)
    assert res.order_id not in (stale['orderid'], other['orderid'])
    assert res.order_id == r.sc.orders[-1]['orderid'] and len(r.sc.orders) == 3


def test_a_lost_response_with_no_order_is_re_sent_after_the_ghost_check():
    r = Rig()
    r.sc.place_script = ['network', 'ok']
    res = r.place(3)
    assert res.kind == 'filled' and len(r.placed()) == 2 and len(r.sc.orders) == 1


def test_endless_lost_responses_end_unconfirmed_without_blind_resends_and_alert():
    r = Rig()
    r.sc.place_script = ['network'] * 20
    res = r.place(3)
    cfg = AngelConfig()
    assert res.kind == 'unconfirmed' and res.order_id is None
    assert len(r.placed()) == cfg.max_transport_retries + 1 and r.sc.orders == []
    assert any(level == 'critical' and 'not re-sending' in text for level, text in r.alerts)


def test_a_session_failure_is_reported_and_flagged_and_never_retried_inside_the_adapter():
    r = Rig()
    r.sc.place_script = ['session']
    res = r.place(3)
    assert res.kind == 'rejected' and 'session failure' in res.detail and r.port.session_failed
    assert len(r.placed()) == 1 and any(level == 'critical' for level, _ in r.alerts)


def test_nothing_is_placed_at_or_after_the_session_close():
    r = Rig(closing=datetime(2026, 9, 3, 12, 0, 0))
    res = r.place(3)
    assert res.kind == 'rejected' and 'session close' in res.detail and r.sc.calls == []


def test_an_order_still_working_after_the_timeout_is_unconfirmed_then_settled_by_its_own_row():
    r = Rig()
    r.sc.place_script = ['hold']
    res = r.place(3)
    assert res.kind == 'unconfirmed' and res.order_id and r.clock.t >= AngelConfig().order_timeout_s
    assert r.read(res.order_id).status == 'pending', 'still open at the broker: never "no fill"'
    r.sc.complete_open_orders()
    read = r.read(res.order_id)
    assert (read.status, read.lots, read.price) == ('complete', 3, 100.0)


def test_an_unreadable_or_unknown_order_is_pending_not_no_fill():
    r = Rig()
    assert r.read(None).status == 'pending'
    assert r.read('does-not-exist').status == 'pending'
    r.sc.orderBook = lambda: (_ for _ in ()).throw(Exception('boom'))
    assert r.read('x').status == 'pending'


def test_a_multi_chunk_unconfirmed_order_is_read_as_one():
    r = Rig(freeze=2)
    r.sc.place_script = ['hold', 'hold', 'hold']
    res = r.place(5)
    assert res.kind == 'unconfirmed' and len(res.order_id.split(',')) == 3
    r.sc.complete_open_orders()
    assert (r.read(res.order_id).status, r.read(res.order_id).lots) == ('complete', 5)


def test_an_order_that_completes_within_the_wait_is_confirmed_by_polling_the_book():
    r = Rig()
    r.sc.place_script = [('open', 3)]
    res = r.place(3)
    assert (res.kind, res.lots) == ('filled', 3) and r.sc.book_reads >= 3


def test_the_websocket_fast_path_avoids_the_order_book_when_the_socket_is_ready():
    feed = OrderUpdateFeed()
    feed.handle(json.dumps({'order-status': 'AB00'}))
    r = Rig(feed=feed)
    real_place = r.sc.placeOrderFullResponse

    def place_and_push(params):
        resp = real_place(params)
        oid = resp['data']['orderid']
        feed.handle(json.dumps({'order-status': 'AB05', 'orderData': {'orderid': oid, 'filledshares': params['quantity'],
                                                                     'averageprice': 101.5, 'symboltoken': 'T1'}}))
        return resp
    r.sc.placeOrderFullResponse = place_and_push
    res = r.place(3)
    assert (res.kind, res.lots, res.price) == ('filled', 3, 101.5) and r.sc.book_reads == 0


def test_a_late_order_update_settles_an_unconfirmed_attempt_through_the_listener():
    feed = OrderUpdateFeed()
    feed.handle(json.dumps({'order-status': 'AB00'}))
    r = Rig(feed=feed)
    r.sc.place_script = ['hold']
    heard = []
    r.port.set_order_listener(lambda oid, read: heard.append((oid, read)))
    res = r.place(3)
    assert res.kind == 'unconfirmed'
    feed.handle(json.dumps({'order-status': 'AB05', 'orderData': {'orderid': res.order_id, 'filledshares': '30',
                                                                 'averageprice': 100.0, 'symboltoken': 'T1'}}))
    r.kernel.run_for(0)
    assert [(o, x.status, x.lots) for o, x in heard] == [(res.order_id, 'complete', 3)]


def test_the_position_book_is_converted_to_lots_by_each_contracts_lot_size():
    r = Rig()
    r.sc.position_rows = [{'symboltoken': 'T1', 'netqty': '-30', 'netprice': '99.5'},
                          {'symboltoken': 'T1x', 'netqty': '7', 'netprice': '0'}, {'netqty': '5'}]
    out = []
    r.port.read_positions(out.append)
    r.kernel.run_for(0)
    assert out[0] == {'T1': PositionRow(-3, 99.5), 'T1x': PositionRow(7, None)}
    r.sc.position_error = Exception('down')
    out.clear()
    r.port.read_positions(out.append)
    r.kernel.run_for(0)
    assert out == [None]


def test_free_cash_fails_closed_when_the_balance_is_missing_or_stale():
    r = Rig()
    assert r.port.free_cash('a') == 0.0
    r.port.refresh_cash_blocking()
    assert r.port.free_cash('a') == 1_000_000.0
    r.clock.sleep(AngelConfig().cash_max_age_s + 1)
    assert r.port.free_cash('a') == 0.0


def test_a_cash_read_is_retried_once_and_a_second_failure_alerts_and_keeps_the_old_state():
    r = Rig()
    r.sc.rms_failures = 1
    r.port.refresh_cash_blocking()
    assert r.port.free_cash('a') == 1_000_000.0
    r2 = Rig()
    r2.sc.rms_failures = 2
    r2.port.refresh_cash_blocking()
    assert r2.port.free_cash('a') == 0.0 and any(level == 'warning' for level, _ in r2.alerts)


def test_cash_refresh_runs_now_and_on_a_timer():
    r = Rig()
    r.port.start_cash_refresh()
    r.kernel.run_until(r.kernel.now + timedelta(seconds=95))
    assert len([c for c in r.sc.calls if c[0] == 'rms']) == 4                     # now, 30, 60, 90


def test_paper_engines_are_refused_and_pool_is_live():
    r = Rig()
    assert r.port.pool_of('a') == 'live'
    with pytest.raises(ValueError):
        r.port.register_engine('p', True)


# ---- the adapter under the real core ---------------------------------------------------------------------------------

def angel_core(script, engines, core_cfg=None):
    clock = FakeClock()
    sc = ScriptedSmartConnect(clock)
    sc.place_script = list(script)
    gw = BrokerGateway(sc, clock=clock.monotonic, sleep=clock.sleep)
    kernel = SimKernel(datetime(2026, 9, 3, 8, 50))
    data = ReplayData(kernel)
    for k, ref in enumerate((FRONT, NEXT)):
        data.add_contract(ContractSpec(ref, lot_size=LOT, tick_size=0.5, freeze_qty_lots=20,
                                       minutes=minutes_frame(100.0 + 5 * k, seed=11 + k)))
    data.set_price('T1', 100.0)
    data.set_price('T2', 101.0)
    port = AngelBrokerPort(gw, kernel, InlineExecutor(), lambda tok: LOT, data.info, AngelConfig(), None,
                           clock.wall_now, lambda: None, clock.sleep, clock.monotonic, alert=lambda *a: None)
    port.refresh_cash_blocking()
    core = HestiaCore(kernel, data, port, core_cfg or CoreConfig(reconcile_interval_s=10.0, ledger_reconcile_interval_s=None),
                      EngineTask)
    for name, factory in engines:
        core.register(name, factory)
    data.begin_session(SESSION_DATE)
    core.begin_session()
    return core, sc, kernel, clock


def _acts(log, *follow):
    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.submit(OpenRequest('r1', FRONT, Direction.BULLISH, 3, trade_ref=1))
        if isinstance(ev, RequestOutcome) and ev.request_id == 'r1' and ev.status == OutcomeStatus.UNCONFIRMED:
            for r in follow:
                ctx.submit(r)
    return lambda: RecEngine('a', act=act, log=log)


def test_the_core_runs_on_the_angel_adapter_end_to_end():
    log = []
    core, sc, kernel, clock = angel_core([], [('a', _acts(log))])
    try:
        kernel.run_until(datetime(2026, 9, 3, 9, 2))
        (o,) = events_of(log, RequestOutcome)
        assert o.status == OutcomeStatus.FILLED and o.opened.lots == 3 and o.opened.avg_price == 100.0
        assert core.held('a', FRONT) == 3 and len(sc.orders) == 1 and sc.orders[0]['quantity'] == '30'
    finally:
        core.close()


def test_an_order_held_open_is_unconfirmed_never_no_fill_and_a_later_close_acts_on_the_reconciled_position():
    log = []
    close = CloseRequest('c1', FRONT, Direction.BULLISH, ExitReason.TREND_FLIP)
    core, sc, kernel, clock = angel_core([('open', 40)], [('a', _acts(log, close))])
    try:
        kernel.run_until(datetime(2026, 9, 3, 9, 30))
        out = [(o.request_id, o.status) for o in events_of(log, RequestOutcome)]
        assert out == [('r1', OutcomeStatus.UNCONFIRMED), ('r1', OutcomeStatus.FILLED), ('c1', OutcomeStatus.FILLED)]
        assert [(r['transactiontype'], r['quantity']) for r in sc.orders] == [('BUY', '30'), ('SELL', '30')]
        assert core.held('a', FRONT) == 0
        assert sum('still working' in a.text for a in core.alerts_for('critical')) == 1
    finally:
        core.close()
