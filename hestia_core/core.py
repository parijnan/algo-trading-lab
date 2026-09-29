"""
The Hestia core: every policy an engine depends on, independent of how time passes, how the broker is reached and where
market data comes from (hestia_core/ports.py). The fake Hestia and the live Hestia are this class on different ports.

What lives here: the request registry (idempotent ids), admission (ledger checks, unit cap, roll window, margin),
priority dispatch with reserved exit capacity, per-engine-per-contract submit order, `depends_on`, the ledger, the
UNCONFIRMED -> reconciling -> settled state machine, engine supervision (crash containment with auto-resume, silence
alerts, KILL, session end) and the per-engine EngineContext.

Single-threaded by contract: every entry point runs on the scheduler's dispatcher (see ports.py), except the engine
threads' own context calls, which the task layer serialises with it.
"""

from __future__ import annotations

import itertools
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from hestia_core.ledger import apply_fill
from hestia_core.ports import BrokerPort, DataPort, OrderRead, OrderSpec, PlaceResult, PositionRow, Scheduler
from hestia_core.interface import (
    AckStatus, CommandEvent, CommandKind, ContractInfo, ContractRef, Direction, Engine, Fill, FillSummary,
    LedgerPosition, MarginSnapshot, OutcomeStatus, PriorityClass, RequestAck, RequestKind, RequestOutcome, SessionStart,
    RUNNING_ROW_COLUMNS, SizingConfig, Stop, StopReason, TRADE_RECORD_COLUMNS, priority_class,
)

log = logging.getLogger('hestia_core')


# ---------------------------------------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------------------------------------

@dataclass
class CoreConfig:
    dispatch_window_s: float = 0.05       # submits within this window are dispatched together, ordered by priority class
    workers: int = 4                      # concurrent requests being executed against the broker
    reserved_workers: int = 1             # of those, kept free of entries (OPEN, roll re-open) so a stop never queues behind them
    order_rate_per_s: float = 10.0        # the account-wide order budget (repo cap)
    reserved_rate_per_s: float = 4.0      # slice of it only stop/close/roll-exit traffic may use
    reject_retry_attempts: int = 3        # entries: broker rejections retried this many times, then REJECTED
    reject_retry_cooldown_s: float = 1.0
    exit_max_attempts: int = 8            # exits are completed, but not forever
    reconcile_interval_s: Optional[float] = 60.0   # Hestia re-reads an UNCONFIRMED order this often on its own (None: only when asked)
    reconcile_retry_s: float = 5.0        # re-read interval while the broker still shows the order as working
    ledger_reconcile_interval_s: Optional[float] = 300.0   # ledger versus the broker's position book (live pool only)
    roll_window_days: int = 5             # no new entries into a contract with this many trading days left or fewer
    restart_limit: int = 3                # auto-resumes allowed per restart_window_s, then the engine is left failed
    restart_window_s: float = 1800.0
    restart_backoff_s: Tuple[float, ...] = (5.0, 30.0, 120.0)
    silence_warn_s: float = 180.0
    silence_critical_s: float = 300.0
    critical_repeat_s: float = 300.0
    monitor_period_s: float = 30.0
    rollover_time: time = time(23, 15)
    margin_per_lot: Optional[Callable[[ContractInfo, float], float]] = None   # default: price x lot_size / 2


@dataclass
class Alert:
    ts: datetime
    level: str
    engine: Optional[str]
    text: str
    channel: Optional[str] = None
    emoji: Optional[str] = None   # explicit per-event emoji (e.g. an engine's own session-start message); None falls back
                                   # to AlertRouter's severity-based default, same as before this field existed


class _Bucket:
    """Account-wide order budget: `rate` orders per second with a burst of one second; `reserved` of it is kept for high priority."""

    def __init__(self, rate: float, reserved: float, now: datetime):
        self.rate, self.reserved = rate, reserved
        self.tokens, self.last = rate, now

    def take(self, now: datetime, high: bool) -> float:
        self.tokens = min(self.rate, self.tokens + (now - self.last).total_seconds() * self.rate)
        self.last = now
        floor = 0.0 if high else self.reserved
        if self.tokens - 1.0 >= floor - 1e-9:
            self.tokens -= 1.0
            return 0.0
        return (floor + 1.0 - self.tokens) / self.rate


# ---------------------------------------------------------------------------------------------------------------------
# Request registry
# ---------------------------------------------------------------------------------------------------------------------

@dataclass
class _Rec:
    engine: str
    request: object
    seq: int
    pclass: int
    submitted: datetime
    state: str = 'queued'                # queued | running | unconfirmed | reconciling | done
    outcome: Optional[RequestOutcome] = None
    close_target: int = 0
    open_target: int = 0
    closed_lots: int = 0
    opened_lots: int = 0
    closed_fills: List[Fill] = field(default_factory=list)
    opened_fills: List[Fill] = field(default_factory=list)
    attempts: int = 0
    rejects: int = 0
    reserved: float = 0.0
    depends_on: Optional[str] = None
    pending: Optional[tuple] = None      # (order_id, order_lots, side) of an order whose fill is unconfirmed
    pending_attempt: Optional[int] = None
    restored: bool = False               # rebuilt from the journal after a restart: settled without touching the ledger
    jl_lots: int = 0                     # what the journal recorded of the original request, for a restored one
    jl_close: int = 0
    jl_open: int = 0
    pending_reads: int = 0

    @property
    def request_id(self) -> str:
        return self.request.request_id


def _summary(fills: List[Fill]) -> Optional[FillSummary]:
    if not fills:
        return None
    lots = sum(f.lots for f in fills)
    return FillSummary(lots=lots, avg_price=sum(f.lots * f.price for f in fills) / lots, fills=tuple(fills))


# ---------------------------------------------------------------------------------------------------------------------
# The core
# ---------------------------------------------------------------------------------------------------------------------

class HestiaCore:

    def __init__(self, kernel: Scheduler, data: DataPort, broker: BrokerPort, config: CoreConfig, task_factory,
                 store=None, sizing_provider: Optional[Callable[[str], SizingConfig]] = None):
        self.cfg = config
        self.kernel = kernel
        self.data = data
        self.broker = broker
        self._task_factory = task_factory
        self.store = store                                           # StateStore or None (in memory only)
        self.sizing_provider = sizing_provider                       # live-read sizing (SizingStore.get) or None
        self.alert_sinks: List[Callable[[Alert], None]] = []
        self.trade_sinks: List[Callable[[str, dict], None]] = []
        self.running_row_sinks: List[Callable[[str, dict], None]] = []
        broker.set_order_listener(self._on_order_update)
        data.attach(self)
        # registration
        self._factories: Dict[str, Callable[[], Engine]] = {}
        self._specs: Dict[str, object] = {}
        self._lots_per_unit: Dict[str, int] = {}
        self._sizing: Dict[str, SizingConfig] = {}
        self._by_instrument: Dict[str, str] = {}
        # tasks
        self._tasks: Dict[str, object] = {}
        self._gen = itertools.count(1)
        self.engine_state: Dict[str, str] = {}                       # running | killed | failed | ended
        self._crashes: Dict[str, List[datetime]] = defaultdict(list)
        self._saved_state: Dict[str, str] = {}
        self._monitor_started = False
        self._stopping = False
        # execution
        self._registry: Dict[Tuple[str, str], _Rec] = {}
        self._by_order: Dict[str, _Rec] = {}
        self._queued: List[_Rec] = []
        self._running: int = 0
        self._running_entries: int = 0
        self._seq = itertools.count(1)
        self._pump_handle = None
        self._bucket = _Bucket(self.cfg.order_rate_per_s, self.cfg.reserved_rate_per_s, kernel.now)
        self._ledger: Dict[Tuple[str, str], List] = {}               # (engine, token) -> [net_lots, avg_price, ts]
        self._ledger_rev = 0                                         # bumped by every ledger change
        # observations
        self.alerts: List[Alert] = []
        self.trades: List[Tuple[str, dict]] = []
        self.running_rows: List[Tuple[str, dict]] = []
        self.warnings: List[str] = []
        self.event_log: List[Tuple[datetime, str, str]] = []
        self.dispatch_log: List[Tuple[datetime, Tuple[Tuple[str, str], ...]]] = []
        self.outcome_log: List[Tuple[datetime, str, RequestOutcome]] = []
        self.ledger_mismatches: Dict[str, Tuple[int, int]] = {}
        self.last_reconciled: Optional[datetime] = None

    @property
    def now(self) -> datetime:
        return self.kernel.now

    # ---- registration ----------------------------------------------------------------------------------------------

    def register(self, name: str, factory: Callable[[], Engine], *, lots_per_unit: int = 1,
                 sizing: Optional[SizingConfig] = None, paper: bool = False) -> None:
        probe = factory()
        instrument = probe.spec.instrument
        if instrument in self._by_instrument:
            raise ValueError(f'instrument {instrument} is already owned by engine {self._by_instrument[instrument]!r}; '
                             f'one engine per instrument')
        if name in self._factories:
            raise ValueError(f'engine {name!r} already registered')
        self._factories[name] = factory
        self._specs[name] = probe.spec
        self._by_instrument[instrument] = name
        self._lots_per_unit[name] = lots_per_unit
        self._sizing[name] = sizing or SizingConfig(dynamic=False, static_units=1, unit_cap=50)
        self.engine_state[name] = 'ended'
        self.broker.register_engine(name, paper)

    def set_sizing(self, name: str, sizing: SizingConfig) -> None:
        self._sizing[name] = sizing

    def _sizing_for(self, name: str) -> SizingConfig:
        """The engine's sizing, read live when a provider is set. The unit cap is a hard limit and always the registered one."""
        base = self._sizing[name]
        if self.sizing_provider is None:
            return base
        live = self.sizing_provider(name)
        return live if live.unit_cap == base.unit_cap else SizingConfig(live.dynamic, live.static_units, base.unit_cap,
                                                                        live.allocation_rs)

    # ---- sessions and engine lifecycle -----------------------------------------------------------------------------

    def begin_session(self) -> None:
        """Schedule the engines' launch at the data side's session open (the data side has already set its session)."""
        self._stopping = False
        self.kernel.at(self.data.session_open, self._launch_all)
        if not self._monitor_started:
            self._monitor_started = True
            self.kernel.after(self.cfg.monitor_period_s, self._monitor)
            if self.cfg.ledger_reconcile_interval_s is not None:
                self.kernel.after(self.cfg.ledger_reconcile_interval_s, self._periodic_reconcile)

    def end_session(self, at: Optional[datetime] = None) -> None:
        self.kernel.at(at or self.now, lambda: self.stop_all(StopReason.SESSION_END))

    def stop_all(self, reason: StopReason, leave_position: bool = True) -> None:
        """Tell every running engine to stop and stop bringing engines back. Orders already at the broker are not touched."""
        self._stopping = True
        for name in list(self._tasks):
            if self.engine_state.get(name) == 'running':
                self._send_stop(name, reason, leave_position)

    def tasks_snapshot(self) -> Dict[str, object]:
        return dict(self._tasks)

    def in_flight_count(self) -> int:
        """Requests still being worked: queued, at the broker, or being reconciled (UNCONFIRMED ones are reported separately)."""
        return sum(1 for r in self._registry.values() if r.state in ('queued', 'running', 'reconciling'))

    def unconfirmed_requests(self) -> List[Tuple[str, str]]:
        return [(r.engine, r.request_id) for r in self._registry.values() if r.state == 'unconfirmed']

    def host_kill(self) -> None:
        for name in list(self._tasks):
            self._abandon_queued(name)
            self.engine_state[name] = 'killed'
            self._send_stop(name, StopReason.HOST_KILL, leave_position=True)

    def send_command(self, engine: str, kind: CommandKind) -> None:
        if kind == CommandKind.KILL:
            self._abandon_queued(engine)
            self.engine_state[engine] = 'killed'
            self._send_stop(engine, StopReason.KILL, leave_position=True)
        else:
            self.deliver_to(engine, CommandEvent(kind))

    def close(self) -> None:
        for task in list(self._tasks.values()):
            task.abort()

    def _launch_all(self) -> None:
        if self._stopping:
            return
        for name in self._factories:
            if self.engine_state.get(name) in ('killed',):
                continue
            self._launch(name)

    def _launch(self, name: str) -> None:
        old = self._tasks.get(name)
        if old is not None and old.state != 'done':
            old.abort()
        engine = self._factories[name]()
        task = self._task_factory(self, name, engine, next(self._gen), lambda t: CoreContext(self, t))
        self._tasks[name] = task
        self.engine_state[name] = 'running'
        task.deliver(self._session_start_event(name))
        task.start()

    def _session_start_event(self, name: str) -> SessionStart:
        spec = self._specs[name]
        first, close = self.data.session_open, self.data.session_close
        evening_only = first.time() >= time(17, 0)
        late = close is not None and close.time() >= time(23, 55)
        rollover = datetime.combine(self.data.session_date, time(23, 40) if late else self.cfg.rollover_time)
        return SessionStart(self.data.session_date, first, rollover, evening_only, self.data.infos(spec.instrument),
                            self.data.seeded(spec.instrument))

    def _send_stop(self, name: str, reason: StopReason, leave_position: bool) -> None:
        task = self._tasks.get(name)
        if task is None or task.state == 'done':
            return
        task.stop_delivered = True
        self.deliver(task, Stop(reason, leave_position))

    # ---- delivery (also used by the data side) ---------------------------------------------------------------------

    def deliver(self, task, event) -> None:
        self.event_log.append((self.now, task.name, repr(event)))
        task.deliver(event)

    def deliver_to(self, name: str, event) -> None:
        task = self._tasks.get(name)
        if task is not None and task.state != 'done':
            self.deliver(task, event)

    def traders_of(self, token: str) -> list:
        return [t for t in self._tasks.values() if t.trading_token == token and t.state != 'done']

    # ---- request intake --------------------------------------------------------------------------------------------

    def _submit(self, task, request) -> RequestAck:
        name = task.name
        rid = request.request_id
        key = (name, rid)
        if key in self._registry:
            rec = self._registry[key]
            state = rec.outcome.status.value if rec.outcome else rec.state
            return RequestAck(rid, AckStatus.DUPLICATE, f'duplicate; {state}')
        if not self.data.knows(request.contract.token):
            return RequestAck(rid, AckStatus.INVALID, 'unknown contract')
        if request.contract.instrument != self._specs[name].instrument:
            return RequestAck(rid, AckStatus.INVALID, 'contract is not this engine\'s instrument')
        dep = getattr(request, 'depends_on', None)
        if dep is not None and (name, dep) not in self._registry:
            return RequestAck(rid, AckStatus.INVALID, f'unknown depends_on {dep}')
        rec = _Rec(engine=name, request=request, seq=next(self._seq), pclass=int(priority_class(request)),
                   submitted=self.now, depends_on=dep)
        self._registry[key] = rec
        self._queued.append(rec)
        self._journal({'t': 'submit', 'engine': name, 'request_id': rid, 'kind': request.kind.value,
                       'token': request.contract.token, 'pclass': rec.pclass,
                       'lots': getattr(request, 'lots', None) or getattr(request, 'open_lots', 0) or 0,
                       'close_lots': getattr(request, 'close_lots', 0) or 0, 'open_lots': getattr(request, 'open_lots', 0) or 0,
                       'ts': self.now})
        self._schedule_pump(self.cfg.dispatch_window_s)
        return RequestAck(rid, AckStatus.ACCEPTED)

    def _schedule_pump(self, delay: float) -> None:
        if self._pump_handle is not None and not self._pump_handle.cancelled:
            return
        self._pump_handle = self.kernel.after(delay, self._pump)

    def _pump(self) -> None:
        self._pump_handle = None
        if self._queued:
            self.dispatch_log.append((self.now, tuple((r.engine, r.request_id) for r in self._queued)))
        progressed = True
        while progressed:
            progressed = False
            for rec in sorted(self._queued, key=lambda r: (r.pclass, r.seq)):
                verdict = self._eligibility(rec)
                if verdict == 'wait':
                    continue
                if verdict == 'dependency_failed':
                    self._queued.remove(rec)
                    self._finish(rec, OutcomeStatus.DEPENDENCY_FAILED, f'{rec.depends_on} did not fill')
                    progressed = True
                    break
                if self._running >= self.cfg.workers:
                    continue
                if self._is_entry(rec) and self._running_entries >= max(1, self.cfg.workers - self.cfg.reserved_workers):
                    continue
                self._queued.remove(rec)
                self._start(rec)
                progressed = True
                break

    @staticmethod
    def _is_entry(rec: _Rec) -> bool:
        return rec.pclass > int(PriorityClass.ROLL_EXIT)

    def _run_inc(self, rec: _Rec) -> None:
        self._running += 1
        self._running_entries += self._is_entry(rec)

    def _run_dec(self, rec: _Rec) -> None:
        self._running -= 1
        self._running_entries -= self._is_entry(rec)

    def _eligibility(self, rec: _Rec) -> str:
        if rec.depends_on is not None:
            dep = self._registry[(rec.engine, rec.depends_on)]
            if dep.state != 'done':
                self._begin_reconcile(dep)
                return 'wait'
            if dep.outcome.status != OutcomeStatus.FILLED:
                return 'dependency_failed'
        for other in self._registry.values():                  # per engine+contract: strictly in submit order
            if (other is not rec and other.engine == rec.engine and other.state in ('running', 'unconfirmed', 'reconciling')
                    and other.request.contract.token == rec.request.contract.token):
                self._begin_reconcile(other)                   # an unconfirmed order is settled against the broker's book first
                return 'wait'
        for other in self._queued:
            if (other is not rec and other.engine == rec.engine and other.seq < rec.seq
                    and other.request.contract.token == rec.request.contract.token):
                return 'wait'
        return 'go'

    # ---- ledger and margin -----------------------------------------------------------------------------------------

    def _held(self, engine: str, token: str) -> int:
        return self._ledger.get((engine, token), [0, None, None])[0]

    def _apply_fill(self, engine: str, contract: ContractRef, signed_lots: int, price: float) -> None:
        pos = self._ledger.setdefault((engine, contract.token), [0, None, self.now])
        pos[0], pos[1] = apply_fill(pos[0], pos[1], signed_lots, price)
        pos[2] = self.now
        self._ledger_rev += 1
        self._persist_ledger()

    def _persist_ledger(self) -> None:
        if self.store is not None:
            try:
                self.store.save_ledger(self._ledger)
            except Exception as exc:                                 # noqa: BLE001
                self._alert('critical', None, f'could not persist the ledger: {exc!r}')

    def _journal(self, record: dict) -> None:
        if self.store is not None:
            try:
                self.store.journal(record, self.now)
            except Exception as exc:                                 # noqa: BLE001
                self._alert('critical', None, f'could not write the request journal: {exc!r}')

    def restore(self) -> None:
        """After a restart, before the engines launch: reload the persisted ledger and the request journal. A request with a
        `submit` and no `final` was in flight when the process died. It is never re-sent and never reported unknown: it comes back
        UNCONFIRMED ("in doubt") and a critical alert asks for the broker to be checked. `settle_restored()` then finishes each one
        after the broker's book has been taken as the ledger's truth (`bootstrap_ledger(authoritative=True)`): a request whose order
        id was journalled (a `placed` line with no `attempt_done`) is settled by reading that very order, outcome only, never touching
        the ledger a second time; one with no order id on record ends ABANDONED."""
        if self.store is None:
            return
        for key, row in self.store.load_ledger().items():
            self._ledger[key] = row
        submits, finals, placed, done = {}, {}, {}, {}
        for rec in self.store.load_journal(self.now.date()):
            key = (rec['engine'], rec['request_id'])
            t = rec['t']
            if t == 'submit':
                submits[key] = rec
            elif t == 'final':
                finals[key] = rec
            elif t == 'placed':
                placed.setdefault(key, []).append(rec)
            elif t == 'attempt_done':
                done.setdefault(key, set()).add(rec['attempt'])
        from types import SimpleNamespace
        from hestia_core.state_store import decode_outcome
        doubt = []
        self._restored_no_order = []
        for key, sub in submits.items():
            kind = RequestKind(sub['kind'])
            stub = SimpleNamespace(request_id=key[1], kind=kind, contract=SimpleNamespace(token=sub.get('token')))
            rec = _Rec(engine=key[0], request=stub, seq=next(self._seq), pclass=int(sub.get('pclass', 4)), submitted=self.now)
            rec.restored, rec.jl_lots = True, int(sub.get('lots', 0))
            rec.jl_close, rec.jl_open = int(sub.get('close_lots', 0)), int(sub.get('open_lots', 0))
            rec.state = 'done'
            if key in finals:
                rec.outcome = decode_outcome(finals[key]['outcome'])
            else:
                rec.outcome = RequestOutcome(key[1], OutcomeStatus.UNCONFIRMED, kind, rec.jl_lots, self.now, None, None,
                                             'in doubt: the process restarted while this request was in flight; check the broker')
                open_orders = [p for p in placed.get(key, []) if p['attempt'] not in done.get(key, set())]
                if open_orders:
                    p = open_orders[-1]
                    rec.state, rec.pending, rec.pending_attempt = 'unconfirmed', (p['order_id'], int(p['lots']), p['side']), p['attempt']
                    self._by_order[p['order_id']] = rec
                else:
                    self._restored_no_order.append(key)
                doubt.append(key)
            self._registry[key] = rec
        if doubt:
            self._alert('critical', None, f'{len(doubt)} request(s) were in flight when the previous process ended and are IN DOUBT '
                                          f'(never re-sent): {doubt}. Check the broker book.')

    _restored_no_order: list = []

    def settle_restored(self) -> None:
        """Finish the in-doubt requests `restore()` found. Call after `bootstrap_ledger(authoritative=True)` has completed."""
        for key in self._restored_no_order:
            rec = self._registry[key]
            self._emit(rec, RequestOutcome(rec.request_id, OutcomeStatus.ABANDONED, rec.request.kind, rec.jl_lots, self.now, None,
                                           None, 'in doubt after a restart with no order id on record; the ledger was taken from '
                                                 'the broker book'))
        self._restored_no_order = []
        for rec in list(self._registry.values()):
            if rec.restored and rec.state == 'unconfirmed' and rec.pending is not None:
                if self.broker.pool_of(rec.engine) == 'live':
                    self._begin_reconcile(rec)
                else:                                          # a paper order has no broker row to read
                    rec.pending, rec.state = None, 'done'
                    self._emit(rec, RequestOutcome(rec.request_id, OutcomeStatus.ABANDONED, rec.request.kind, rec.jl_lots, self.now,
                                                   None, None, 'a paper request was in flight at the restart; the ledger is the '
                                                               'persisted one'))

    def _settle_restored(self, rec: _Rec, read: OrderRead, source: str) -> None:
        """Outcome only: the ledger was already taken from the broker's book, so filling it here would count the order twice."""
        order_id, order_lots, side = rec.pending
        rec.pending = None
        self._by_order.pop(order_id, None)
        lots = max(0, min(read.lots, order_lots))
        status = (OutcomeStatus.REJECTED if lots == 0 else OutcomeStatus.FILLED if lots >= order_lots else OutcomeStatus.PARTIAL)
        kind = rec.request.kind
        closed = opened = None
        if lots:
            if kind == RequestKind.OPEN:
                opened = FillSummary(lots, read.price, ())
            elif kind == RequestKind.FLIP:
                c = min(lots, rec.jl_close)
                closed = FillSummary(c, read.price, ()) if c else None
                opened = FillSummary(lots - c, read.price, ()) if lots - c else None
            else:
                closed = FillSummary(lots, read.price, ())
        rec.state = 'done'
        self._journal({'t': 'attempt_done', 'engine': rec.engine, 'request_id': rec.request_id, 'attempt': rec.pending_attempt})
        self._alert('info' if lots else 'warning', rec.engine,
                    f'restored request {rec.request_id}: {source} shows {lots} of {order_lots} lots filled')
        self._emit(rec, RequestOutcome(rec.request_id, status, kind, rec.jl_lots or order_lots, self.now, closed, opened,
                                       f'settled from {source} after a restart (the ledger comes from the broker book)'))
        self._schedule_pump(0)

    def _margin_per_lot(self, info: ContractInfo, price: float) -> float:
        if self.cfg.margin_per_lot is not None:
            return self.cfg.margin_per_lot(info, price)
        return price * info.lot_size / 2.0

    def available_cash(self, engine: Optional[str] = None) -> float:
        """Cash of the engine's pool (the live account unless the engine is paper) less the margin reserved for entries in
        flight in the same pool."""
        pool = self.broker.pool_of(engine) if engine else 'live'
        reserved = sum(r.reserved for r in self._registry.values() if self.broker.pool_of(r.engine) == pool)
        return self.broker.free_cash(engine or '') - reserved            # no engine named: the live account, never a paper pool

    # ---- admission -------------------------------------------------------------------------------------------------

    def _admit(self, rec: _Rec) -> Optional[Tuple[OutcomeStatus, str]]:
        """Returns None to proceed, or the refusal (status, detail). 'flat' is signalled as (FILLED, 'already flat')."""
        r, eng, token = rec.request, rec.engine, rec.request.contract.token
        held = self._held(eng, token)
        info = self.data.info(r.contract)
        kind = r.kind
        close = getattr(self.data, 'session_close', None)
        if close is not None and self.now >= close:
            # production refused every order after the closing time; a stop fired by a tick after the close, or a request from the bar
            # that completes at the close, can never fill and must not be sent
            return OutcomeStatus.LIMIT_REFUSED, f'market closed: {self.now:%H:%M:%S} is at or after the session close {close:%H:%M}'

        def dirn(n):
            return Direction.BULLISH if n > 0 else Direction.BEARISH

        if kind in (RequestKind.CLOSE, RequestKind.FLATTEN):
            if held == 0:
                return OutcomeStatus.FILLED, 'already flat'
            if kind == RequestKind.CLOSE:
                if dirn(held) != r.expected_direction:
                    return OutcomeStatus.REJECTED, f'ledger holds {dirn(held).value}, request expected {r.expected_direction.value}'
                lots = r.lots if r.lots is not None else abs(held)
                if lots > abs(held):
                    return OutcomeStatus.REJECTED, f'closing {lots} lots but only {abs(held)} held'
                rec.close_target = lots
            else:
                rec.close_target = abs(held)
            return None
        if kind == RequestKind.FLIP:
            if held == 0:
                return OutcomeStatus.REJECTED, 'nothing to flip: ledger is flat'
            if dirn(held) != r.from_direction:
                return OutcomeStatus.REJECTED, f'ledger holds {dirn(held).value}, flip expected {r.from_direction.value}'
            if r.close_lots > abs(held):
                return OutcomeStatus.REJECTED, f'closing {r.close_lots} lots but only {abs(held)} held'
            rec.close_target, rec.open_target = r.close_lots, r.open_lots
            after = abs(held) - r.close_lots + r.open_lots
            price = self.data.price(token) or 0.0
            need = r.open_lots * self._margin_per_lot(info, price) - r.close_lots * self._margin_per_lot(info, price)
            return self._entry_limits(rec, info, after, need)
        # OPEN
        if held != 0 and dirn(held) != r.direction:
            return OutcomeStatus.REJECTED, f'would net against an existing {dirn(held).value} position'
        rec.open_target = r.lots
        after = abs(held) + r.lots
        price = self.data.price(token) or 0.0
        return self._entry_limits(rec, info, after, r.lots * self._margin_per_lot(info, price))

    def _entry_limits(self, rec: _Rec, info: ContractInfo, lots_after: int,
                      margin_needed: float) -> Optional[Tuple[OutcomeStatus, str]]:
        cap_lots = self._sizing[rec.engine].unit_cap * self._lots_per_unit[rec.engine]      # the registered cap, never an override's
        if lots_after > cap_lots:
            return OutcomeStatus.LIMIT_REFUSED, f'unit cap: {lots_after} lots would exceed {cap_lots}'
        if info.trading_days_left <= self.cfg.roll_window_days:
            return OutcomeStatus.LIMIT_REFUSED, (f'roll window: {info.ref.symbol} has {info.trading_days_left} trading days '
                                                 f'left (<= {self.cfg.roll_window_days})')
        cash = self.available_cash(rec.engine)
        if margin_needed > 0 and cash < margin_needed:
            return OutcomeStatus.MARGIN_REFUSED, f'needs {margin_needed:,.0f}, available {cash:,.0f}'
        rec.reserved = max(margin_needed, 0.0)
        return None

    # ---- execution -------------------------------------------------------------------------------------------------

    def _start(self, rec: _Rec) -> None:
        refusal = self._admit(rec)
        if refusal is not None:
            status, detail = refusal
            self._finish(rec, status, detail, zero_fill=(detail == 'already flat'))
            return
        rec.state = 'running'
        self._run_inc(rec)
        self._attempt(rec)

    def _side(self, rec: _Rec) -> str:
        r = rec.request
        if r.kind == RequestKind.OPEN:
            return 'BUY' if r.direction == Direction.BULLISH else 'SELL'
        if r.kind == RequestKind.FLIP:
            return 'BUY' if r.to_direction == Direction.BULLISH else 'SELL'
        return 'SELL' if self._held(rec.engine, r.contract.token) > 0 else 'BUY'

    def _attempt(self, rec: _Rec) -> None:
        if rec.state != 'running':
            return
        rem_close, rem_open = rec.close_target - rec.closed_lots, rec.open_target - rec.opened_lots
        order_lots = rem_close + rem_open
        if order_lots <= 0:
            return self._complete(rec)
        wait = self._bucket.take(self.now, high=rec.pclass <= int(PriorityClass.ROLL_EXIT))
        if wait > 0:
            self.kernel.after(wait, lambda: self._attempt(rec))
            return
        rec.attempts += 1
        spec = OrderSpec(rec.engine, rec.request_id, rec.request.contract, self._side(rec), order_lots, rec.attempts,
                         rec.pclass, rem_close, rem_open, rec.request)
        self.broker.place(spec, lambda res: self._on_place_result(rec, spec, res), lambda oid: self._on_placed(rec, spec, oid))

    def _on_placed(self, rec: _Rec, spec: OrderSpec, order_id: str) -> None:
        """The order exists at the broker (or in the paper book). Journal its id before the fill is known, so a restart in the
        middle of the wait can read this very order instead of guessing."""
        self._journal({'t': 'placed', 'engine': rec.engine, 'request_id': rec.request_id, 'order_id': order_id,
                       'side': spec.side, 'lots': spec.lots, 'attempt': spec.attempt, 'ts': self.now})

    def _on_place_result(self, rec: _Rec, spec: OrderSpec, res: PlaceResult) -> None:
        if rec.state != 'running':
            return
        try:
            self._handle_place_result(rec, spec, res)
        finally:
            if res.kind != 'unconfirmed':                    # an unconfirmed attempt stays open in the journal until settled
                self._journal({'t': 'attempt_done', 'engine': rec.engine, 'request_id': rec.request_id, 'attempt': spec.attempt})

    def _handle_place_result(self, rec: _Rec, spec: OrderSpec, res: PlaceResult) -> None:
        if res.kind == 'rejected':
            rec.rejects += 1
            limit = self.cfg.exit_max_attempts if spec.close_lots > 0 else self.cfg.reject_retry_attempts
            if rec.rejects > limit:
                self._give_up(rec)
            else:
                self.kernel.after(self.cfg.reject_retry_cooldown_s, lambda: self._attempt(rec))
            return
        if res.kind == 'unconfirmed':
            self._go_unconfirmed(rec, spec, res)
            return
        lots = spec.lots if res.kind == 'filled' else max(0, min(res.lots, spec.lots))
        if lots:
            self._fill_lots(rec, spec.side, res.order_id, lots, res.price)
        self._after_fill(rec)

    def _fill_lots(self, rec: _Rec, side: str, order_id: Optional[str], lots: int, price: float) -> None:
        if price is None:
            raise RuntimeError(f'no fill price for {rec.request.contract.symbol} at {self.now}')
        r = rec.request
        sign = 1 if side == 'BUY' else -1
        to_close = min(lots, rec.close_target - rec.closed_lots)
        to_open = lots - to_close
        if to_close:
            rec.closed_fills.append(Fill(to_close, price, self.now, order_id or ''))
            rec.closed_lots += to_close
            self._apply_fill(rec.engine, r.contract, sign * to_close, price)
        if to_open:
            rec.opened_fills.append(Fill(to_open, price, self.now, order_id or ''))
            rec.opened_lots += to_open
            self._apply_fill(rec.engine, r.contract, sign * to_open, price)

    def _after_fill(self, rec: _Rec) -> None:
        rem_close, rem_open = rec.close_target - rec.closed_lots, rec.open_target - rec.opened_lots
        if rem_close == 0 and rem_open == 0:
            return self._complete(rec)
        if rem_close > 0:                                   # exits are completed, entries are not topped up
            if rec.attempts >= self.cfg.exit_max_attempts:
                return self._give_up(rec)
            self.kernel.after(0, lambda: self._attempt(rec))
            return
        self._finish(rec, OutcomeStatus.PARTIAL, f'{rec.opened_lots} of {rec.open_target} lots filled')

    def _complete(self, rec: _Rec) -> None:
        self._finish(rec, OutcomeStatus.FILLED, '')

    def _give_up(self, rec: _Rec) -> None:
        if rec.state != 'running':
            return
        self._alert('critical', rec.engine, f'request {rec.request_id} REJECTED after {rec.rejects} broker rejections '
                                            f'({rec.closed_lots}/{rec.close_target} closed, {rec.opened_lots}/{rec.open_target} opened)')
        self._finish(rec, OutcomeStatus.REJECTED, f'broker rejected after {rec.rejects} attempts')

    # -- unconfirmed orders: settled from the broker's own row for the order -------------------------------------------

    def _go_unconfirmed(self, rec: _Rec, spec: OrderSpec, res: PlaceResult) -> None:
        rec.state = 'unconfirmed'
        self._run_dec(rec)
        rec.pending = (res.order_id, spec.lots, spec.side)
        rec.pending_attempt = spec.attempt
        if res.order_id:
            self._by_order[res.order_id] = rec
        self._alert('critical', rec.engine, f'request {rec.request_id}: fill not confirmed; position status unknown')
        self._emit(rec, self._outcome(rec, OutcomeStatus.UNCONFIRMED, 'order placed, fill not confirmed'))
        if self.cfg.reconcile_interval_s is not None:
            self.kernel.after(self.cfg.reconcile_interval_s, lambda: self._begin_reconcile(rec))
        self._schedule_pump(0)

    def _begin_reconcile(self, rec: _Rec) -> None:
        """Read the broker's row for the unconfirmed order. Requests behind it on the same engine and contract wait for
        the answer, then act on the reconciled ledger."""
        if rec.state != 'unconfirmed' or rec.pending is None:
            return
        rec.state = 'reconciling'
        self.broker.read_order(rec.pending[0], lambda read: self._on_read(rec, read, 'broker book'))

    def _on_order_update(self, order_id: str, read: OrderRead) -> None:
        rec = self._by_order.get(order_id)
        if rec is not None:
            self._on_read(rec, read, 'order-update feed')

    def _on_read(self, rec: _Rec, read: OrderRead, source: str) -> None:
        if rec.state not in ('running', 'unconfirmed', 'reconciling') or rec.pending is None:
            return
        if read.status == 'pending':                        # still working at the broker: never 'no fill', keep reading
            rec.pending_reads += 1
            if rec.state == 'reconciling':
                rec.state = 'unconfirmed'
            if rec.pending_reads == 1:
                self._alert('critical', rec.engine, f'request {rec.request_id}: order still working at the broker '
                                                    f'(open or validation pending); will keep re-reading it')
            self.kernel.after(self.cfg.reconcile_retry_s, lambda: self._begin_reconcile(rec))
            return
        if rec.restored:
            self._settle_restored(rec, read, source)
            return
        order_id, order_lots, side = rec.pending
        rec.pending = None
        self._journal({'t': 'attempt_done', 'engine': rec.engine, 'request_id': rec.request_id, 'attempt': rec.pending_attempt})
        was_waiting = rec.state in ('unconfirmed', 'reconciling')
        lots = max(0, min(read.lots, order_lots))
        if was_waiting:
            rec.state = 'running'
            self._run_inc(rec)
        if lots == 0 and rec.closed_lots + rec.opened_lots == 0:
            self._alert('warning', rec.engine, f'request {rec.request_id}: {source} shows no fill; the order did not execute')
            self._finish(rec, OutcomeStatus.REJECTED, f'{source} shows no fill')
            return
        if lots:
            self._fill_lots(rec, side, order_id, lots, read.price)
        if was_waiting:
            self._alert('info', rec.engine, f'request {rec.request_id}: {source} shows {lots} lots filled')
        rem_close, rem_open = rec.close_target - rec.closed_lots, rec.open_target - rec.opened_lots
        if rem_close == 0 and rem_open == 0:
            self._finish(rec, OutcomeStatus.FILLED, f'resolved by {source}')
        elif rem_close > 0 and rec.attempts < self.cfg.exit_max_attempts:
            self.kernel.after(0, lambda: self._attempt(rec))
        else:
            self._finish(rec, OutcomeStatus.PARTIAL, f'resolved by {source}')

    def _outcome(self, rec: _Rec, status: OutcomeStatus, detail: str, zero_fill: bool = False) -> RequestOutcome:
        r = rec.request
        kind = r.kind
        requested = (rec.close_target + rec.open_target) if kind == RequestKind.FLIP else (rec.close_target or rec.open_target)
        closed = _summary(rec.closed_fills)
        if zero_fill:
            closed = FillSummary(0, None, ())
        return RequestOutcome(rec.request_id, status, kind, requested, self.now, closed, _summary(rec.opened_fills), detail)

    def _finish(self, rec: _Rec, status: OutcomeStatus, detail: str, zero_fill: bool = False) -> None:
        if rec.state == 'running':
            self._run_dec(rec)
        rec.state = 'done'
        rec.reserved = 0.0
        self._emit(rec, self._outcome(rec, status, detail, zero_fill))
        self._schedule_pump(0)

    def _emit(self, rec: _Rec, outcome: RequestOutcome) -> None:
        rec.outcome = outcome
        self.outcome_log.append((self.now, rec.engine, outcome))
        if self.store is not None:
            from hestia_core.state_store import encode_outcome
            kind = 'unconfirmed' if outcome.status == OutcomeStatus.UNCONFIRMED else 'final'
            self._journal({'t': kind, 'engine': rec.engine, 'request_id': rec.request_id, 'outcome': encode_outcome(outcome),
                           'ts': self.now})
        self.deliver_to(rec.engine, outcome)

    def _abandon_queued(self, name: str) -> None:
        for rec in [r for r in self._queued if r.engine == name]:
            self._queued.remove(rec)
            self._finish(rec, OutcomeStatus.ABANDONED, 'engine stopped before the request was worked')

    def _status(self, name: str, request_id: str) -> Optional[RequestOutcome]:
        rec = self._registry.get((name, request_id))
        if rec is None:
            return None
        if rec.outcome is not None and rec.state in ('done', 'unconfirmed', 'reconciling'):
            return rec.outcome
        return RequestOutcome(request_id, OutcomeStatus.IN_FLIGHT, rec.request.kind,
                              rec.close_target + rec.open_target, self.now)

    # ---- ledger versus the broker's position book ------------------------------------------------------------------

    def _live_engines(self) -> List[str]:
        return [e for e in self._factories if self.broker.pool_of(e) == 'live']

    def _expected_live_net(self) -> Dict[str, int]:
        live = set(self._live_engines())
        out: Dict[str, int] = defaultdict(int)
        for (eng, token), (net, _, _) in self._ledger.items():
            if eng in live and net:
                out[token] += net
        return dict(out)

    def _unsettled_tokens(self) -> set:
        return {r.request.contract.token for r in self._registry.values() if r.state in ('running', 'unconfirmed', 'reconciling')}

    def _periodic_reconcile(self) -> None:
        self.reconcile_ledger()
        self.kernel.after(self.cfg.ledger_reconcile_interval_s, self._periodic_reconcile)

    def reconcile_ledger(self) -> None:
        """Compare the live engines' ledger with the broker's position book. Alert-only, like Prometheus's reconcile: a
        mismatch is a finding for the user, never auto-corrected. Paper engines are excluded (they have no broker position),
        and tokens with a request still in flight are skipped this round."""
        rev = self._ledger_rev
        self.broker.read_positions(lambda book: self._on_positions(book, rev))

    def _on_positions(self, book: Optional[Dict[str, PositionRow]], rev: int) -> None:
        if rev != self._ledger_rev:                          # a fill landed while the book was being read: the snapshot is
            return                                           # older than the ledger, so comparing them proves nothing
        if book is None:
            self._alert('warning', None, 'ledger reconciliation: could not read the broker position book')
            return
        expected, busy = self._expected_live_net(), self._unsettled_tokens()
        mismatches = {}
        for token in set(expected) | {t for t, row in book.items() if row.net_lots}:
            if token in busy:
                continue
            want, have = expected.get(token, 0), (book[token].net_lots if token in book else 0)
            if want != have:
                mismatches[token] = (want, have)
        self.ledger_mismatches = mismatches
        for token, (want, have) in sorted(mismatches.items()):
            ref = self.data.ref_for(token)
            name = ref.symbol if ref else token
            self._alert('critical', None, f'ledger mismatch on {name}: engines hold {want:+d} lots, broker book shows '
                                          f'{have:+d}; verify before trading continues')
        if not mismatches:
            self.last_reconciled = self.now

    def bootstrap_ledger(self, authoritative: bool = False) -> None:
        """At start, adopt what the broker's book shows (a position carried overnight) for any live engine whose ledger is
        empty: the position goes to the engine that owns the token's instrument. Anything that cannot be attributed is a
        critical alert. With `authoritative=True` (a restart, where the persisted ledger may be stale because a request was in
        flight) the broker's book wins for every live engine's ledger, and each correction is alerted."""
        self._bootstrap_authoritative = authoritative
        self.broker.read_positions(self._on_bootstrap)

    _bootstrap_authoritative = False

    bootstrap_done = False

    def _on_bootstrap(self, book: Optional[Dict[str, PositionRow]]) -> None:
        self.bootstrap_done = True
        if book is None:
            self._alert('warning', None, 'ledger bootstrap: could not read the broker position book')
            return
        if self._bootstrap_authoritative:
            live = set(self._live_engines())
            for (eng, token), row in list(self._ledger.items()):
                if eng not in live or not row[0]:
                    continue
                have = book[token].net_lots if token in book else 0
                if have != row[0]:
                    self._alert('critical', eng, f'restart: persisted ledger held {row[0]:+d} lots of token {token}, the broker '
                                                 f'book shows {have:+d}; the broker book is taken as the truth')
                    row[0], row[1] = have, (book[token].avg_price if token in book else None)
                    self._ledger_rev += 1
            self._persist_ledger()
        for token, row in sorted(book.items()):
            if not row.net_lots:
                continue
            ref = self.data.ref_for(token)
            owner = self._by_instrument.get(ref.instrument) if ref else None
            if owner is None or self.broker.pool_of(owner) != 'live':
                self._alert('critical', None, f'broker holds {row.net_lots:+d} lots of token {token} that no live engine owns')
                continue
            if self._held(owner, token) == 0:
                self._ledger[(owner, token)] = [row.net_lots, row.avg_price, self.now]
                self._ledger_rev += 1
                self._persist_ledger()
                self._alert('warning', owner, f'ledger adopted {row.net_lots:+d} lots of {ref.symbol} from the broker book')

    # ---- monitoring and crashes ------------------------------------------------------------------------------------

    def _alert(self, level: str, engine: Optional[str], text: str, channel: Optional[str] = None,
              emoji: Optional[str] = None) -> None:
        alert = Alert(self.now, level, engine, text, channel, emoji)
        self.alerts.append(alert)
        for sink in self.alert_sinks:
            try:
                sink(alert)
            except Exception:                                        # noqa: BLE001 - a failing sink must never stop trading
                log.exception('alert sink failed')

    def _monitor(self) -> None:
        for name, task in self._tasks.items():
            if task.state == 'done' or self.engine_state.get(name) != 'running':
                continue
            age = (self.now - task.last_heartbeat).total_seconds()
            if age >= self.cfg.silence_critical_s:
                due = (task.last_critical_ts is None
                       or (self.now - task.last_critical_ts).total_seconds() >= self.cfg.critical_repeat_s)
                if due:
                    self._alert('critical', name, f'engine silent for {age:.0f}s')
                    task.last_critical_ts = self.now
                task.silence_level = 2
            elif age >= self.cfg.silence_warn_s and task.silence_level < 1:
                self._alert('warning', name, f'engine silent for {age:.0f}s')
                task.silence_level = 1
            elif age < self.cfg.silence_warn_s and task.silence_level:
                self._alert('info', name, 'engine responsive again')
                task.silence_level, task.last_critical_ts = 0, None
        self.kernel.after(self.cfg.monitor_period_s, self._monitor)

    def _on_task_done(self, task) -> None:
        name = task.name
        if self._tasks.get(name) is not task:
            return
        if task.error is not None:
            self._crashed(task)
        elif self.engine_state.get(name) == 'running':
            self.engine_state[name] = 'ended'
            if not task.stop_delivered:
                self._alert('warning', name, 'engine returned without a Stop')

    def _crashed(self, task) -> None:
        name = task.name
        now = self.now
        window = self.cfg.restart_window_s
        self._crashes[name] = [t for t in self._crashes[name] if (now - t).total_seconds() < window] + [now]
        n = len(self._crashes[name])
        self._alert('error', name, f'engine crashed: {task.error!r}')
        if self.engine_state.get(name) == 'killed':
            return
        if self._stopping:                                  # shutting down: no restart, and nothing to alarm about beyond the crash
            self.engine_state[name] = 'ended'
            return
        if n > self.cfg.restart_limit:
            self.engine_state[name] = 'failed'
            self._alert('critical', name, f'engine FAILED: {n} crashes within {window / 60:.0f} min, auto-resume limit '
                                          f'{self.cfg.restart_limit} exhausted; any open position is unmanaged')
            self._repeat_failed_alert(name)
            return
        backoff = self.cfg.restart_backoff_s[min(n - 1, len(self.cfg.restart_backoff_s) - 1)]
        self._alert('warning', name, f'auto-resuming engine in {backoff:.0f}s (restart {n}/{self.cfg.restart_limit})')
        self.kernel.after(backoff, lambda: self._resume_engine(name))

    def _resume_engine(self, name: str) -> None:
        if self._stopping or self.engine_state.get(name) not in ('running',):
            return
        self._launch(name)

    def _repeat_failed_alert(self, name: str) -> None:
        if self.engine_state.get(name) != 'failed':
            return
        holding = any(net for (eng, _), (net, _, _) in self._ledger.items() if eng == name)
        if holding:
            self._alert('critical', name, 'engine still failed and holds an open position')
        self.kernel.after(self.cfg.critical_repeat_s, lambda: self._repeat_failed_alert(name))

    # ---- observations ----------------------------------------------------------------------------------------------

    def alerts_for(self, level: Optional[str] = None, engine: Optional[str] = None) -> List[Alert]:
        return [a for a in self.alerts if (level is None or a.level == level) and (engine is None or a.engine == engine)]

    def held(self, engine: str, contract: ContractRef) -> int:
        return self._held(engine, contract.token)

    def request_record(self, engine: str, request_id: str) -> Optional[_Rec]:
        return self._registry.get((engine, request_id))


# ---------------------------------------------------------------------------------------------------------------------
# The per-engine EngineContext
# ---------------------------------------------------------------------------------------------------------------------

class CoreContext:
    def __init__(self, core: HestiaCore, task):
        self._h, self._t = core, task

    def now(self):
        return self._h.now

    def wait(self, seconds):
        self._t.wait(seconds)

    def next_event(self, timeout):
        return self._t.next_event(timeout)

    def contracts(self, instrument):
        return self._h.data.infos(instrument)

    def set_trading_contract(self, contract):
        self._t.trading_token = contract.token
        hook = getattr(self._h.data, 'trading_contract_set', None)   # a live data source starts serving the contract
        if hook is not None:
            hook(self._t, contract)

    def track(self, contract):
        self._h.data.track(self._t, contract)

    def untrack(self, contract):
        self._h.data.untrack(self._t, contract)

    def ltp(self, contract):
        return self._h.data.ltp_quote(contract.token)

    def latest_bar(self, contract):
        return self._h.data.latest_bar(contract.token, self._t.engine.spec)

    def st_series(self, contract, last_n):
        return self._h.data.st_series(contract.token, self._t.engine.spec, last_n)

    def price_near(self, contract, ts, tolerance_min):
        return self._h.data.price_near(contract.token, ts, tolerance_min)

    def session_open(self):
        return self._h.data.session_open

    def position(self, contract):
        h = self._h
        net, avg, ts = h._ledger.get((self._t.name, contract.token), [0, None, h.now])
        return LedgerPosition(contract, net, avg, ts)

    def margin(self):
        return MarginSnapshot(self._h.available_cash(self._t.name), self._h.now)

    def sizing(self):
        return self._h._sizing_for(self._t.name)

    def submit(self, request):
        return self._h._submit(self._t, request)

    def request_status(self, request_id):
        return self._h._status(self._t.name, request_id)

    def save_state(self, blob):
        self._h._saved_state[self._t.name] = blob
        if self._h.store is not None:
            self._h.store.save_engine_state(self._t.name, blob)

    def load_state(self):
        blob = self._h._saved_state.get(self._t.name)
        if blob is None and self._h.store is not None:
            blob = self._h.store.load_engine_state(self._t.name)
            if blob is not None:
                self._h._saved_state[self._t.name] = blob
        return blob

    def alert(self, level, text, channel=None, emoji=None):
        self._h._alert(level, self._t.name, text, channel, emoji)

    def report_trade(self, record):
        extra = sorted(set(record) - set(TRADE_RECORD_COLUMNS))
        if extra:
            self._h.warnings.append(f'{self._t.name}: trade record keys dropped: {extra}')
        row = {c: record.get(c) for c in TRADE_RECORD_COLUMNS}
        self._h.trades.append((self._t.name, row))
        for sink in self._h.trade_sinks:
            try:
                sink(self._t.name, row)
            except Exception:                                        # noqa: BLE001 - reporting never stops trading
                log.exception('trade sink failed')

    def report_running_row(self, record):
        extra = sorted(set(record) - set(RUNNING_ROW_COLUMNS))
        if extra:
            self._h.warnings.append(f'{self._t.name}: running-row keys dropped: {extra}')
        row = {c: record.get(c) for c in RUNNING_ROW_COLUMNS}
        self._h.running_rows.append((self._t.name, row))
        for sink in self._h.running_row_sinks:
            try:
                sink(self._t.name, row)
            except Exception:                                        # noqa: BLE001 - reporting never stops trading
                log.exception('running-row sink failed')
