"""
Fake / replay Hestia: the Hestia core (hestia_core/core.py) on simulated ports.

    FakeHestia = HestiaCore + SimKernel (simulated clock) + SimBroker (scripted broker) + ReplayData (replayed 1-minute data)

Purpose (plan section 8, phase P3): let an engine be written and tested against the interface before any live Hestia
exists, and later replay recorded days through the same engine code. The policy is the core's, shared with the live Hestia
(plans/hestia-p4-live-services.md); this module only wires the simulated ports and exposes the scenario knobs tests use.

Time is simulated: call `run_until` / `run_for`; nothing sleeps. Engines run on real threads but only one at a time
(hestia_core.fake_kernel), so every run is repeatable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Callable, Optional

from hestia_core.broker_router import BrokerRouter
from hestia_core.core import Alert, CoreConfig, HestiaCore, _Bucket, _Rec  # noqa: F401 (re-exports used by tests)
from hestia_core.fake_kernel import EngineTask, FakeHestiaDeadlock, SimKernel, WALL_TIMEOUT_S  # noqa: F401
from hestia_core.interface import ContractRef
from hestia_core.paper_broker import PaperBroker
from hestia_core.replay import (BrokerCall, BrokerReply, ContractSpec, PlacedOrder, ReplayData, SimBroker)  # noqa: F401


@dataclass
class FakeConfig(CoreConfig):
    available_cash: float = 10_000_000.0
    slippage_points: float = 0.0
    unconfirmed_after_s: float = 30.0     # the simulated broker's fill-wait timeout: a fill unconfirmed by then is UNCONFIRMED
    ghost_recovery_s: float = 2.0         # an order whose placement call raised is found in the order book after this
    reconcile_latency_s: float = 1.0      # reading the broker's row for an order
    paper_cash: float = 1_000_000.0       # each paper engine's own pool


class FakeHestia(HestiaCore):

    def __init__(self, start: datetime, config: Optional[FakeConfig] = None,
                 broker: Optional[Callable[[BrokerCall], BrokerReply]] = None, holidays: Optional[set] = None,
                 store=None, sizing_provider=None):
        cfg = config or FakeConfig()
        kernel = SimKernel(start)
        data = ReplayData(kernel, holidays)
        self._sim_data = data
        sim = SimBroker(kernel, broker or (lambda call: BrokerReply('fill')), data.price, self._position_margin,
                        cfg.available_cash, cfg.slippage_points, cfg.unconfirmed_after_s, cfg.ghost_recovery_s,
                        cfg.reconcile_latency_s)
        self._sim = sim
        self._paper = PaperBroker(kernel, data.price, self._position_margin, cfg.paper_cash, cfg.slippage_points)
        self.holidays = data.holidays
        super().__init__(kernel, data, BrokerRouter(sim, self._paper), cfg, EngineTask, store=store, sizing_provider=sizing_provider)

    def _position_margin(self, token: str, net: int, avg: float) -> float:
        spec = self._sim_data._contracts[token]
        info = self.data.info(spec.ref)
        return abs(net) * self._margin_per_lot(info, avg)

    # ---- scenario knobs (delegated to the simulated ports) ---------------------------------------------------------

    @property
    def orders(self):
        return self._sim.orders

    @property
    def paper_orders(self):
        return self._paper.orders

    @property
    def rejections(self):
        return self._sim.rejections

    @property
    def broker_behavior(self):
        return self._sim.behavior

    @broker_behavior.setter
    def broker_behavior(self, fn):
        self._sim.behavior = fn

    @property
    def bar_delay(self):
        return self._sim_data.bar_delay

    @property
    def bar_gap(self):
        return self._sim_data.bar_gap

    @property
    def bar_partial(self):
        return self._sim_data.bar_partial

    @property
    def provisional_override(self):
        return self._sim_data.provisional_override

    @property
    def _contracts(self):
        return self._sim_data._contracts

    def _bars_for(self, token: str):
        return self._sim_data._bars_for(token)

    def add_contract(self, spec: ContractSpec) -> None:
        self._sim_data.add_contract(spec)

    def set_price(self, token: str, price: Optional[float]) -> None:
        self._sim_data.set_price(token, price)

    def schedule_price(self, token: str, when: datetime, price: float) -> None:
        self._sim_data.schedule_price(token, when, price)

    def seed_position(self, engine: str, contract: ContractRef, net_lots: int, avg_price: float) -> None:
        self._ledger[(engine, contract.token)] = [net_lots, avg_price, self.now]
        if self.broker.pool_of(engine) == 'live':
            self._sim.seed(contract.token, net_lots, avg_price)
        else:
            self._paper.book(engine)[contract.token] = [net_lots, avg_price]

    def inject_feed_stale(self, engine: str, contract: ContractRef, start: datetime, end: datetime) -> None:
        self._sim_data.inject_feed_stale(engine, contract, start, end)

    def inject_dpl_freeze(self, engine: str, contract: ContractRef, when: datetime, price: float, frozen: bool) -> None:
        self._sim_data.inject_dpl_freeze(engine, contract, when, price, frozen)

    # ---- running ---------------------------------------------------------------------------------------------------

    def run_until(self, t: datetime) -> None:
        self.kernel.run_until(t)

    def run_for(self, seconds: float) -> None:
        self.kernel.run_for(seconds)

    def start_session(self, session_date: date, session_open: Optional[datetime] = None) -> None:
        """Begin a session: the replay data sets the day up, then the core launches each registered engine (a fresh
        instance, as the daily cron does) at the session open."""
        self._sim_data.begin_session(session_date, session_open)
        self.begin_session()
