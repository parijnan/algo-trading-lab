"""
BrokerRouter: one BrokerPort in front of a live port and a paper port, choosing per engine (plan: paper versus live is set
per engine, not per process, so a Selene DRY_RUN can run inside the same Hestia as live Prometheus).
"""

from __future__ import annotations

from typing import Dict

from hestia_core.ports import BrokerPort, OrderRead, OrderSpec, PlaceResult


class BrokerRouter:

    def __init__(self, live: BrokerPort, paper: BrokerPort):
        self.live, self.paper = live, paper
        self._paper_engines: set = set()
        self._owner: Dict[str, BrokerPort] = {}

    def _port(self, engine: str) -> BrokerPort:
        return self.paper if engine in self._paper_engines else self.live

    def register_engine(self, name: str, paper: bool) -> None:
        if paper:
            self._paper_engines.add(name)
        self._port(name).register_engine(name, paper)

    def pool_of(self, engine: str) -> str:
        return self._port(engine).pool_of(engine)

    def free_cash(self, engine: str) -> float:
        return self._port(engine).free_cash(engine)

    def set_order_listener(self, fn) -> None:
        self.live.set_order_listener(fn)
        self.paper.set_order_listener(fn)

    def place(self, spec: OrderSpec, on_result, on_placed=None) -> None:
        port = self._port(spec.engine)

        def remember(res: PlaceResult) -> None:
            if res.order_id:
                self._owner[res.order_id] = port
            on_result(res)
        port.place(spec, remember, on_placed)

    def read_order(self, order_id: str, on_result) -> None:
        self._owner.get(order_id, self.live).read_order(order_id, on_result)

    def read_positions(self, on_result) -> None:
        self.live.read_positions(on_result)
