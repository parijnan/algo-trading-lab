"""Broker gateway: endpoint budgets with a reserved lane, one HTTP call at a time, priority at the lock, counters after."""
import threading
import time

import pytest

from smartapi_double import FakeClock, ScriptedSmartConnect
from hestia_core.gateway import (BrokerGateway, DataException, EndpointBudget, is_rate_limit, is_session_failure)


def gateway(clock=None, budgets=None):
    clock = clock or FakeClock()
    sc = ScriptedSmartConnect(clock)
    return BrokerGateway(sc, budgets, clock=clock.monotonic, sleep=clock.sleep), sc, clock


PARAMS = {'tradingsymbol': 'X', 'symboltoken': '1', 'transactiontype': 'BUY', 'quantity': '10'}


def test_normal_traffic_is_held_below_the_reserved_slice_and_high_priority_is_not():
    gw, sc, clock = gateway()
    for _ in range(6):
        gw.place_order(PARAMS)
    assert clock.t == 0.0, 'the first six of ten per second are free for normal traffic'
    gw.place_order(PARAMS)
    assert clock.t == pytest.approx(0.1), 'the seventh must wait: the last four tokens are reserved'
    t = clock.t
    for _ in range(4):
        gw.place_order(PARAMS, high=True)
    assert clock.t == t, 'risk-reducing calls spend the reserved tokens without waiting'
    assert gw.counts['orders'] == 11


def test_each_endpoint_has_its_own_budget():
    gw, sc, clock = gateway()
    sc.getCandleData = lambda p: {'data': []}
    for _ in range(3):
        gw.candles({})
    assert clock.t == 0.0
    gw.candles({})
    assert clock.t == pytest.approx(1 / 3), 'candles are capped at 3 a second'
    t = clock.t
    gw.rms()
    assert clock.t == t, 'other endpoints are not slowed by the candle budget'


def test_the_counter_moves_after_the_request_and_even_when_it_raises():
    gw, sc, clock = gateway()
    sc.place_script = ['ratelimit']
    seen = {}
    real = sc.placeOrderFullResponse

    def spy(params):
        seen['count_during'] = gw.counts['orders']
        return real(params)
    sc.placeOrderFullResponse = spy
    with pytest.raises(DataException):
        gw.place_order(PARAMS)
    assert seen['count_during'] == 0 and gw.counts['orders'] == 1


def test_http_calls_never_overlap():
    clock = FakeClock()
    active, worst = [0], [0]

    class Slow:
        def orderBook(self):
            active[0] += 1
            worst[0] = max(worst[0], active[0])
            time.sleep(0.01)
            active[0] -= 1
            return {'data': []}
    gw = BrokerGateway(Slow(), {'orderbook': EndpointBudget(1000.0)}, clock=time.monotonic, sleep=time.sleep)
    threads = [threading.Thread(target=gw.order_book) for _ in range(8)]
    [t.start() for t in threads]
    [t.join(5) for t in threads]
    assert worst[0] == 1 and gw.counts['orderbook'] == 8


def test_a_waiting_high_priority_call_takes_the_lock_before_a_waiting_normal_one():
    gate, order = threading.Event(), []

    class Held:
        def __init__(self):
            self.first = True

        def orderBook(self):
            if self.first:
                self.first = False
                gate.wait(5)
            return {'data': []}
    obj = Held()
    gw = BrokerGateway(obj, {'orderbook': EndpointBudget(1000.0)}, clock=time.monotonic, sleep=time.sleep)
    holder = threading.Thread(target=gw.order_book)
    holder.start()
    time.sleep(0.05)                                           # the holder now owns the lock

    def caller(name, high):
        gw.order_book(high=high)
        order.append(name)
    normal = threading.Thread(target=caller, args=('normal', False))
    normal.start()
    time.sleep(0.05)
    high = threading.Thread(target=caller, args=('high', True))
    high.start()
    time.sleep(0.05)
    gate.set()
    for t in (holder, normal, high):
        t.join(5)
    assert order == ['high', 'normal']


def test_error_classifiers():
    assert is_rate_limit(DataException('Access denied because of exceeding access rate'))
    assert is_rate_limit(Exception('AB1021'))
    assert not is_rate_limit(Exception('Connection reset'))
    assert is_session_failure(Exception('Invalid Token')) and is_session_failure(Exception('AB1007'))
    assert not is_session_failure(Exception('Connection reset'))
