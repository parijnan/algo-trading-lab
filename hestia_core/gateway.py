"""
Broker gateway: the one door to Angel One's SmartConnect object (plan section 2).

- Every call takes an endpoint budget first (a token bucket per endpoint, account-wide because there is one gateway) and
  then the single HTTP lock, so calls from many workers never overlap on the one `requests.Session` inside `obj`.
- Two lanes. Risk-reducing calls (`high=True`: stops, exits, flattens) may spend an endpoint's reserved share of tokens and
  take the HTTP lock ahead of waiting normal calls, so a burst of entries or data polls cannot starve a stop-out.
- Counters are incremented after the request, as elsewhere in this repo, and only when it was actually made.
- Nothing here retries or interprets responses: retry policy belongs to the callers (the order adapter, the data service).
  The classifiers below only recognise the error families Angel One is known to raise.

The rates are the caps already in use in this repo for candles (3/s), LTP (10/s) and orders (10/s, 4 of them reserved for
risk-reducing traffic); the order-book, position and margin endpoints are set conservatively (1/s each) and are UNVERIFIED
against Angel One's published limits.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

try:                                                        # the SmartApi package is optional for tests
    from SmartApi.smartExceptions import DataException, NetworkException
except Exception:                                           # pragma: no cover - only without the package installed
    class DataException(Exception):
        pass

    class NetworkException(Exception):
        pass

TRANSPORT_ERRORS = (DataException, NetworkException)


class SessionError(Exception):  # reserved for the host (slice 3); adapters report session failures through alerts
    """The broker session is no longer valid (token or session failure). Never retried; it needs the host's attention."""


def is_rate_limit(exc: BaseException) -> bool:
    text = str(exc).lower()
    return 'access rate' in text or 'exceeding' in text or 'ab1021' in text


def is_session_failure(exc: BaseException) -> bool:
    text = str(exc).lower()
    return 'token' in text or 'invalid' in text or 'ab1007' in text


@dataclass(frozen=True)
class EndpointBudget:
    rate: float                 # calls per second, burst of one second
    reserved: float = 0.0       # of that rate, kept for high-priority callers


DEFAULT_BUDGETS: Dict[str, EndpointBudget] = {
    'orders': EndpointBudget(10.0, 4.0),
    'candles': EndpointBudget(3.0),
    'ltp': EndpointBudget(10.0),
    'orderbook': EndpointBudget(1.0),
    'positions': EndpointBudget(1.0),
    'rms': EndpointBudget(1.0),
}


class _Bucket:
    def __init__(self, budget: EndpointBudget, now: float):
        self.rate, self.reserved = budget.rate, budget.reserved
        self.tokens, self.last = budget.rate, now

    def take(self, now: float, high: bool) -> float:
        self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
        self.last = now
        floor = 0.0 if high else self.reserved
        if self.tokens - 1.0 >= floor - 1e-9:
            self.tokens -= 1.0
            return 0.0
        return (floor + 1.0 - self.tokens) / self.rate


class _PriorityLock:
    """A mutex where waiting high-priority callers go before waiting normal ones."""

    def __init__(self):
        self._cond = threading.Condition()
        self._busy = False
        self._waiting_high = 0

    def acquire(self, high: bool) -> None:
        with self._cond:
            if high:
                self._waiting_high += 1
            try:
                while self._busy or (not high and self._waiting_high):
                    self._cond.wait()
                self._busy = True
            finally:
                if high:
                    self._waiting_high -= 1

    def release(self) -> None:
        with self._cond:
            self._busy = False
            self._cond.notify_all()


class BrokerGateway:

    def __init__(self, obj, budgets: Optional[Dict[str, EndpointBudget]] = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self._obj = obj
        self._clock, self._sleep = clock, sleep
        self._budgets = dict(DEFAULT_BUDGETS if budgets is None else budgets)
        self._buckets = {k: _Bucket(b, clock()) for k, b in self._budgets.items()}
        self._bucket_lock = threading.Lock()
        self._http = _PriorityLock()
        self.counts: Dict[str, int] = {k: 0 for k in self._budgets}

    def _wait_for_budget(self, endpoint: str, high: bool) -> None:
        while True:
            with self._bucket_lock:
                wait = self._buckets[endpoint].take(self._clock(), high)
            if wait <= 0:
                return
            self._sleep(wait)

    def _call(self, endpoint: str, fn: Callable, high: bool):
        self._wait_for_budget(endpoint, high)
        self._http.acquire(high)
        try:
            result = fn()
        finally:
            self._http.release()
            self.counts[endpoint] += 1                       # after the request, whether or not it succeeded
        return result

    # -- the endpoints ------------------------------------------------------------------------------------------------

    def place_order(self, params: dict, high: bool = False):
        return self._call('orders', lambda: self._obj.placeOrderFullResponse(params), high)

    def order_book(self, high: bool = False):
        return self._call('orderbook', lambda: self._obj.orderBook(), high)

    def positions(self, high: bool = False):
        return self._call('positions', lambda: self._obj.position(), high)

    def rms(self, high: bool = False):
        return self._call('rms', lambda: self._obj.rmsLimit(), high)

    def ltp(self, exchange: str, symbol: str, token: str, high: bool = False):
        return self._call('ltp', lambda: self._obj.ltpData(exchange, symbol, token), high)

    def candles(self, params: dict, high: bool = False):
        return self._call('candles', lambda: self._obj.getCandleData(params), high)
