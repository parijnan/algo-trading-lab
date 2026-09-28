"""
Paper broker: BrokerPort that fills at the live price and touches no real account, one instance per Hestia, serving every
engine registered as paper (plans/hestia-p4-live-services.md, constraint 4). Each paper engine has a pool of its own
(cash and position book), so a paper engine's margin never competes with the live account and its position never appears in
the account-book reconciliation.
"""

from __future__ import annotations

import itertools
from typing import Callable, Dict, Optional

from hestia_core.ledger import apply_fill
from hestia_core.ports import OrderRead, OrderSpec, PlaceResult


class PaperBroker:

    def __init__(self, kernel, price_fn: Callable[[str], Optional[float]], margin_fn: Callable[[str, int, float], float],
                 cash: float = 1_000_000.0, slippage: float = 0.0, latency_s: float = 0.2):
        self.kernel, self.price_fn, self.margin_fn = kernel, price_fn, margin_fn
        self.cash, self.slippage, self.latency_s = cash, slippage, latency_s
        self.orders = []
        self._books: Dict[str, Dict[str, list]] = {}            # engine -> token -> [net_lots, avg_price]
        self._seq = itertools.count(1)
        self._done: Dict[str, OrderRead] = {}

    def register_engine(self, name: str, paper: bool) -> None:
        self._books.setdefault(name, {})

    def pool_of(self, engine: str) -> str:
        return f'paper:{engine}'

    def set_order_listener(self, fn) -> None:
        pass

    def read_positions(self, on_result) -> None:
        on_result({})                                            # no account book to compare a paper position against

    def free_cash(self, engine: str) -> float:
        book = self._books.get(engine, {})
        return self.cash - sum(self.margin_fn(t, net, avg) for t, (net, avg) in book.items() if net and avg is not None)

    def book(self, engine: str) -> Dict[str, list]:
        return self._books.setdefault(engine, {})

    def place(self, spec: OrderSpec, on_result, on_placed=None) -> None:
        order_id = f'PAPER{next(self._seq):05d}'
        self.orders.append((self.kernel.now, order_id, spec))
        if on_placed is not None:
            on_placed(order_id)

        def fill():
            price = self.price_fn(spec.contract.token)
            if price is None:
                on_result(PlaceResult('rejected', detail='paper broker has no live price'))
                return
            sign = 1 if spec.side == 'BUY' else -1
            px = price + sign * self.slippage
            book = self.book(spec.engine)
            net, avg = book.get(spec.contract.token, [0, None])
            book[spec.contract.token] = list(apply_fill(net, avg, sign * spec.lots, px))
            self._done[order_id] = OrderRead('complete', spec.lots, px)
            on_result(PlaceResult('filled', spec.lots, px, order_id))
        self.kernel.after(self.latency_s, fill)

    def read_order(self, order_id: str, on_result) -> None:
        self.kernel.after(0, lambda: on_result(self._done.get(order_id, OrderRead('complete', 0, None))))
