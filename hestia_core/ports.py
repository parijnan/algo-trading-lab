"""
The seams of the Hestia core (plans/hestia-p4-live-services.md).

The core (hestia_core/core.py) holds every policy the engines depend on: request registry, admission, priority dispatch,
ledger, reconciliation, supervision. It talks to the outside world only through the three ports below, so the fake
Hestia (simulated clock, scripted broker, replayed data) and the live Hestia (real-time reactor, Angel One adapter, live
data service) are the same policy on different plumbing.

Threading contract: the core is single-threaded. Every callback into it (a scheduled function, a broker result, an
order-update, a data event) runs on the scheduler's dispatcher, one at a time, so the core needs no locks. A live broker
adapter does its blocking I/O elsewhere and posts the completion back with `Scheduler.after(0, ...)`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Optional, Protocol, Tuple, runtime_checkable

from hestia_core.interface import Bar, ContractInfo, ContractRef, LtpQuote, SupertrendPoint


@runtime_checkable
class Scheduler(Protocol):
    now: datetime

    def at(self, when: datetime, fn: Callable[[], None]): ...
    def after(self, seconds: float, fn: Callable[[], None]): ...      # returns a handle with .cancel() and .cancelled
    def post(self, fn: Callable[[], None]): ...                       # thread-safe in the live reactor: how workers report back


# ---------------------------------------------------------------------------------------------------------------------
# Broker port
# ---------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class OrderSpec:
    """One order attempt. `lots` is the whole order (a netted flip is close + open in one order). The adapter owns
    chunking to the freeze quantity, ghost recovery and the market-hours refusal; the core owns retries of rejections."""
    engine: str
    request_id: str
    contract: ContractRef
    side: str                 # BUY | SELL
    lots: int
    attempt: int
    priority: int             # PriorityClass value
    close_lots: int = 0
    open_lots: int = 0
    request: object = None    # the originating request, for scripted test brokers


@dataclass(frozen=True)
class PlaceResult:
    """What became of one attempt.
    filled       every lot filled                  (`lots` == order lots, `price` = average)
    partial      some lots filled, the rest will not (`lots` may be 0)
    rejected     the broker refused it; no order exists
    unconfirmed  an order exists (`order_id`) but its fill could not be confirmed in time; the core settles it by reading it"""
    kind: str
    lots: int = 0
    price: Optional[float] = None
    order_id: Optional[str] = None
    detail: str = ''


@dataclass(frozen=True)
class OrderRead:
    """The broker's own row for one order.
    complete   terminal: `lots` filled at `price` (0 lots means it did not execute: rejected or cancelled)
    pending    still working (`open`, `validation pending`): a DPL lock can hold a market order open. Never means 'no fill'."""
    status: str
    lots: int = 0
    price: Optional[float] = None


@dataclass(frozen=True)
class PositionRow:
    """One row of the broker's position book, in lots (the adapter converts from shares with the contract's lot size)."""
    net_lots: int
    avg_price: Optional[float] = None


@runtime_checkable
class BrokerPort(Protocol):
    """Paper versus live is decided per engine, not per process: `register_engine(name, paper)` says which side an engine's
    orders go to, `pool_of` names the money and position pool it draws on ('live' is the one real account; a paper engine
    has a pool of its own), and the account-book reconciliation covers only the live pool."""
    def register_engine(self, name: str, paper: bool) -> None: ...
    def pool_of(self, engine: str) -> str: ...
    def place(self, spec: OrderSpec, on_result: Callable[[PlaceResult], None],
              on_placed: Optional[Callable[[str], None]] = None) -> None: ...   # on_placed(order_id): the order exists, before its fill is known
    def read_order(self, order_id: str, on_result: Callable[[OrderRead], None]) -> None: ...
    def read_positions(self, on_result: Callable[[Optional[Dict[str, PositionRow]]], None]) -> None: ...   # live pool; None on failure
    def free_cash(self, engine: str) -> float: ...                       # cash of the engine's pool, net of margin already in use
    def set_order_listener(self, fn: Callable[[str, OrderRead], None]) -> None: ...   # the order-update feed


# ---------------------------------------------------------------------------------------------------------------------
# Data port
# ---------------------------------------------------------------------------------------------------------------------

@runtime_checkable
class DataPort(Protocol):
    def attach(self, core) -> None: ...                      # gives the data side `core.traders_of` and `core.deliver`
    def knows(self, token: str) -> bool: ...                 # data exists for this contract
    def infos(self, instrument: str) -> Tuple[ContractInfo, ...]: ...
    def info(self, ref: ContractRef) -> Optional[ContractInfo]: ...
    def ref_for(self, token: str) -> Optional[ContractRef]: ...
    def price(self, token: str) -> Optional[float]: ...
    def ltp_quote(self, token: str) -> Optional[LtpQuote]: ...
    def latest_bar(self, token: str, spec) -> Optional[Tuple[Bar, SupertrendPoint]]: ...
    def st_series(self, token: str, spec, last_n: int) -> Tuple[Tuple[Bar, SupertrendPoint], ...]: ...
    def price_near(self, token: str, ts: datetime, tolerance_min: int) -> Optional[float]: ...
    def seeded(self, instrument: str) -> Tuple[ContractRef, ...]: ...
    def track(self, task, contract: ContractRef) -> None: ...
    def untrack(self, task, contract: ContractRef) -> None: ...
    session_date: Optional[object]
    session_open: Optional[datetime]
    session_close: Optional[datetime]
