"""
Fake / replay Hestia: a deterministic simulator that implements hestia_core.interface.EngineContext for real engine code.

Purpose (plan section 8, phase P3): let an engine be written and tested against the interface before any live Hestia
exists, and later replay recorded days through the same engine code. It models what the live Hestia will guarantee, not
how it does it: one request per decision, Hestia-owned retries, idempotent request ids, priority classes, account-wide
order-rate budget, margin admission, hard limits, crash containment with auto-resume, silence alerts, KILL semantics.

Time is simulated (hestia_core.fake_kernel.SimKernel): call `run_until` / `run_for`; nothing sleeps. Market data comes from
1-minute frames handed to `add_contract`; 15-minute bars are built from them anchored at each day's first minute, the
same way production builds them, and Supertrend is computed once per (contract, engine spec) with hestia_core.indicators
(bars are revealed at their boundary; Supertrend is causal, so precomputing changes nothing an engine can see).

Modelling notes, deliberately simple and stated so nobody mistakes them for market truth:
- price at time t is the minute's open in the first half of the minute and its close in the second half; `set_price` and
  `schedule_price` override it for scripted tests;
- a PARTIAL bar reports fewer minutes but keeps the true OHLC and Supertrend; a RECOVERED bar is the true bar delivered late;
- a provisional bar takes its OHLC from the true bar unless `provisional_override` says otherwise;
- fills happen at the price source's price at the fill instant, plus `slippage_points` against the trader.
"""

from __future__ import annotations

import copy
import itertools
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from hestia_core import calendar as hcal
from hestia_core.fake_kernel import EngineTask, FakeHestiaDeadlock, SimKernel, WALL_TIMEOUT_S  # noqa: F401 (re-export)
from hestia_core.indicators import compute_st
from hestia_core.interface import (
    AckStatus, Bar, BarComplete, BarQuality, CommandEvent, CommandKind, ContractInfo, ContractRef, Direction, DplFrozen,
    Engine, ExitReason, FeedRecovered, FeedStale, Fill, FillSummary, FlattenRequest, FlipRequest, LedgerPosition, LtpQuote,
    MarginSnapshot, OpenRequest, OutcomeStatus, PriorityClass, ProvisionalBar, RequestAck, RequestKind, RequestOutcome,
    SessionStart, SizingConfig, Stop, StopReason, SupertrendPoint, TRADE_RECORD_COLUMNS, TrackFailed, TrackReady,
    CloseRequest, priority_class,
)

log = logging.getLogger('hestia_fake')

BAR_MIN = 15


# ---------------------------------------------------------------------------------------------------------------------
# Configuration and injectable behaviour
# ---------------------------------------------------------------------------------------------------------------------

@dataclass
class FakeConfig:
    dispatch_window_s: float = 0.05       # submits within this window are dispatched together, ordered by priority class
    workers: int = 4                      # concurrent requests being executed against the broker
    reserved_workers: int = 1             # of those, kept free of entries (OPEN, roll re-open) so a stop never queues behind them
    order_rate_per_s: float = 10.0        # the account-wide order budget (repo cap)
    reserved_rate_per_s: float = 4.0      # slice of it only stop/close/roll-exit traffic may use
    reject_retry_attempts: int = 3        # entries: broker rejections retried this many times, then REJECTED
    reject_retry_cooldown_s: float = 1.0
    exit_max_attempts: int = 8            # exits are completed, but not forever
    ghost_recovery_s: float = 2.0         # an order whose placement call raised is found in the order book after this
    unconfirmed_after_s: float = 30.0     # a fill not confirmed by then becomes an UNCONFIRMED outcome
    reconcile_latency_s: float = 1.0      # reading the broker's position book to settle an UNCONFIRMED request
    reconcile_interval_s: Optional[float] = 60.0   # Hestia re-reads the book this often on its own (None: only when asked)
    roll_window_days: int = 5             # no new entries into a contract with this many trading days left or fewer
    restart_limit: int = 3                # auto-resumes allowed per restart_window_s, then the engine is left failed
    restart_window_s: float = 1800.0
    restart_backoff_s: Tuple[float, ...] = (5.0, 30.0, 120.0)
    silence_warn_s: float = 180.0
    silence_critical_s: float = 300.0
    critical_repeat_s: float = 300.0
    monitor_period_s: float = 30.0
    available_cash: float = 10_000_000.0
    slippage_points: float = 0.0
    rollover_time: time = time(23, 15)
    margin_per_lot: Optional[Callable[[ContractInfo, float], float]] = None   # default: price x lot_size / 2


@dataclass
class BrokerCall:
    """What the simulated broker is asked to do on one attempt; a behaviour function turns it into a BrokerReply."""
    engine: str
    request: object
    attempt: int
    order_lots: int
    close_lots: int
    open_lots: int


@dataclass
class BrokerReply:
    kind: str = 'fill'                  # fill | partial | reject | ghost | unconfirmed
    lots: Optional[int] = None          # partial: lots filled; unconfirmed: lots the broker's book will show (None = all, 0 = none)
    latency: float = 0.2                # seconds until the reply
    resolve_after: Optional[float] = None   # unconfirmed only: seconds after placement when the order-update feed reports it late


@dataclass
class PlacedOrder:
    order_id: str
    ts: datetime
    engine: str
    request_id: str
    contract: ContractRef
    side: str
    lots: int
    attempt: int
    priority: int


@dataclass
class Alert:
    ts: datetime
    level: str
    engine: Optional[str]
    text: str
    channel: Optional[str] = None


@dataclass
class ContractSpec:
    ref: ContractRef
    lot_size: int = 1
    tick_size: float = 1.0
    freeze_qty_lots: int = 10
    minutes: Optional[pd.DataFrame] = None      # columns: time_stamp, open, high, low, close, volume


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
    hidden: Optional[tuple] = None       # (order, reply) of an unconfirmed order: what the broker's book will show
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

    @property
    def request_id(self) -> str:
        return self.request.request_id


def _summary(fills: List[Fill]) -> Optional[FillSummary]:
    if not fills:
        return None
    lots = sum(f.lots for f in fills)
    return FillSummary(lots=lots, avg_price=sum(f.lots * f.price for f in fills) / lots, fills=tuple(fills))


# ---------------------------------------------------------------------------------------------------------------------
# The fake Hestia
# ---------------------------------------------------------------------------------------------------------------------

class FakeHestia:

    def __init__(self, start: datetime, config: Optional[FakeConfig] = None,
                 broker: Optional[Callable[[BrokerCall], BrokerReply]] = None,
                 holidays: Optional[set] = None):
        self.cfg = config or FakeConfig()
        self.kernel = SimKernel(start)
        self.broker_behavior = broker or (lambda call: BrokerReply('fill'))
        self.holidays = set(holidays or ())
        # registration
        self._factories: Dict[str, Callable[[], Engine]] = {}
        self._specs: Dict[str, object] = {}
        self._lots_per_unit: Dict[str, int] = {}
        self._sizing: Dict[str, SizingConfig] = {}
        self._by_instrument: Dict[str, str] = {}
        # contracts and data
        self._contracts: Dict[str, ContractSpec] = {}
        self._bars: Dict[str, pd.DataFrame] = {}
        self._st_cache: Dict[Tuple[str, int, float], pd.DataFrame] = {}
        self._minute_idx: Dict[str, Optional[pd.DataFrame]] = {}
        self._visible: Dict[str, int] = {}
        self._prices: Dict[str, float] = {}
        self._stale: Dict[str, datetime] = {}
        self.bar_delay: Dict[Tuple[str, datetime], float] = {}       # (token, boundary) -> seconds the true bar arrives late
        self.bar_gap: set = set()                                    # (token, boundary) with no bar at all
        self.bar_partial: Dict[Tuple[str, datetime], int] = {}       # (token, boundary) -> minutes present
        self.provisional_override: Dict[Tuple[str, datetime], dict] = {}   # e.g. {'close': 101.0}
        # tasks
        self._tasks: Dict[str, EngineTask] = {}
        self._gen = itertools.count(1)
        self.engine_state: Dict[str, str] = {}                       # running | killed | failed | ended
        self._crashes: Dict[str, List[datetime]] = defaultdict(list)
        self._saved_state: Dict[str, str] = {}
        self._session_date: Optional[date] = None
        self._session_open: Optional[datetime] = None
        self._session_close: Optional[datetime] = None
        self._monitor_started = False
        # execution
        self._registry: Dict[Tuple[str, str], _Rec] = {}
        self._queued: List[_Rec] = []
        self._running: int = 0
        self._running_entries: int = 0
        self._seq = itertools.count(1)
        self._order_seq = itertools.count(1)
        self._pump_handle = None
        self._bucket = _Bucket(self.cfg.order_rate_per_s, self.cfg.reserved_rate_per_s, start)
        self._ledger: Dict[Tuple[str, str], List] = {}               # (engine, token) -> [net_lots, avg_price, ts]
        # observations for tests
        self.orders: List[PlacedOrder] = []
        self.rejections: List[Tuple[datetime, str, str]] = []
        self.alerts: List[Alert] = []
        self.trades: List[Tuple[str, dict]] = []
        self.warnings: List[str] = []
        self.event_log: List[Tuple[datetime, str, str]] = []
        self.dispatch_log: List[Tuple[datetime, Tuple[Tuple[str, str], ...]]] = []
        self.outcome_log: List[Tuple[datetime, str, RequestOutcome]] = []

    # ---- setup -----------------------------------------------------------------------------------------------------

    @property
    def now(self) -> datetime:
        return self.kernel.now

    def add_contract(self, spec: ContractSpec) -> None:
        self._contracts[spec.ref.token] = spec
        self._visible.setdefault(spec.ref.token, 0)

    def register(self, name: str, factory: Callable[[], Engine], *, lots_per_unit: int = 1,
                 sizing: Optional[SizingConfig] = None) -> None:
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

    def set_sizing(self, name: str, sizing: SizingConfig) -> None:
        self._sizing[name] = sizing

    def set_price(self, token: str, price: Optional[float]) -> None:
        if price is None:
            self._prices.pop(token, None)
        else:
            self._prices[token] = price

    def schedule_price(self, token: str, when: datetime, price: float) -> None:
        self.kernel.at(when, lambda: self.set_price(token, price))

    def seed_position(self, engine: str, contract: ContractRef, net_lots: int, avg_price: float) -> None:
        self._ledger[(engine, contract.token)] = [net_lots, avg_price, self.now]

    def inject_feed_stale(self, engine: str, contract: ContractRef, start: datetime, end: datetime) -> None:
        def go_stale():
            self._stale[contract.token] = start
            self._deliver_to(engine, FeedStale(contract, age_sec=(self.now - start).total_seconds()))

        def recover():
            self._stale.pop(contract.token, None)
            self._deliver_to(engine, FeedRecovered(contract))
        self.kernel.at(start, go_stale)
        self.kernel.at(end, recover)

    def inject_dpl_freeze(self, engine: str, contract: ContractRef, when: datetime, price: float, frozen: bool) -> None:
        self.kernel.at(when, lambda: self._deliver_to(engine, DplFrozen(contract, price, frozen)))

    # ---- running ---------------------------------------------------------------------------------------------------

    def run_until(self, t: datetime) -> None:
        self.kernel.run_until(t)

    def run_for(self, seconds: float) -> None:
        self.kernel.run_for(seconds)

    def close(self) -> None:
        for task in list(self._tasks.values()):
            task.abort()

    # ---- sessions --------------------------------------------------------------------------------------------------

    def start_session(self, session_date: date, session_open: Optional[datetime] = None) -> None:
        """Begin a session: launch each registered engine (a fresh instance, as the daily cron does), deliver SessionStart,
        and schedule the day's bar boundaries."""
        self._session_date = session_date
        tokens = self._tokens_with_data()
        opens = [self._first_minute(t, session_date) for t in tokens]
        opens = [o for o in opens if o is not None]
        self._session_open = session_open or (min(opens) if opens else datetime.combine(session_date, time(9, 0)))
        for token in tokens:
            bars = self._bars_for(token)
            self._visible[token] = int((bars['time_stamp'] + timedelta(minutes=BAR_MIN) <= self._session_open).sum())
        self.kernel.at(self._session_open, self._launch_all)
        for token in tokens:
            bars = self._bars_for(token)
            day = bars[bars['time_stamp'].dt.date == session_date]
            for _, row in day.iterrows():
                boundary = row['time_stamp'] + timedelta(minutes=BAR_MIN)
                self.kernel.at(boundary, lambda tok=token, b=boundary: self._boundary(tok, b))
        for token, boundary in sorted(self.bar_gap):
            if boundary.date() == session_date:
                self.kernel.at(boundary, lambda tok=token, b=boundary: self._gap(tok, b))
        closes = [self._contracts[t].minutes['time_stamp'].max() for t in tokens]
        self._session_close = max(closes).to_pydatetime() if closes else None
        if not self._monitor_started:
            self._monitor_started = True
            self.kernel.after(self.cfg.monitor_period_s, self._monitor)

    def end_session(self, at: Optional[datetime] = None) -> None:
        def fire():
            for name in list(self._tasks):
                if self.engine_state.get(name) == 'running':
                    self._send_stop(name, StopReason.SESSION_END, leave_position=True)
        self.kernel.at(at or self.now, fire)

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
            self._deliver_to(engine, CommandEvent(kind))

    def _launch_all(self) -> None:
        for name in self._factories:
            if self.engine_state.get(name) in ('killed',):
                continue
            self._launch(name)

    def _launch(self, name: str) -> None:
        old = self._tasks.get(name)
        if old is not None and old.state != 'done':
            old.abort()
        engine = self._factories[name]()
        task = EngineTask(self, name, engine, next(self._gen), lambda t: _Ctx(self, t))
        self._tasks[name] = task
        self.engine_state[name] = 'running'
        task.deliver(self._session_start_event(name))
        task.start()

    def _session_start_event(self, name: str) -> SessionStart:
        spec = self._specs[name]
        infos = self._infos(spec.instrument)
        seeded = tuple(i.ref for i in infos if self._visible.get(i.ref.token, 0) > 0)
        first = self._session_open
        evening_only = first.time() >= time(17, 0)
        late = self._session_close is not None and self._session_close.time() >= time(23, 55)
        rollover = datetime.combine(self._session_date, time(23, 40) if late else self.cfg.rollover_time)
        return SessionStart(self._session_date, first, rollover, evening_only, infos, seeded)

    def _send_stop(self, name: str, reason: StopReason, leave_position: bool) -> None:
        task = self._tasks.get(name)
        if task is None or task.state == 'done':
            return
        task.stop_delivered = True
        self._deliver(task, Stop(reason, leave_position))

    # ---- delivery --------------------------------------------------------------------------------------------------

    def _deliver(self, task: EngineTask, event) -> None:
        self.event_log.append((self.now, task.name, repr(event)))
        task.deliver(event)

    def _deliver_to(self, name: str, event) -> None:
        task = self._tasks.get(name)
        if task is not None and task.state != 'done':
            self._deliver(task, event)

    # ---- data ------------------------------------------------------------------------------------------------------

    def _tokens_with_data(self) -> List[str]:
        return [t for t, s in self._contracts.items() if s.minutes is not None and len(s.minutes)]

    def _first_minute(self, token: str, d: date) -> Optional[datetime]:
        m = self._contracts[token].minutes
        day = m[m['time_stamp'].dt.date == d]
        return None if day.empty else day['time_stamp'].min().to_pydatetime()

    def _bars_for(self, token: str) -> pd.DataFrame:
        if token in self._bars:
            return self._bars[token]
        m = self._contracts[token].minutes.sort_values('time_stamp').copy()
        m['day'] = m['time_stamp'].dt.date
        first = m.groupby('day')['time_stamp'].transform('min')
        m['bucket'] = ((m['time_stamp'] - first).dt.total_seconds() // (BAR_MIN * 60)).astype(int)
        first_ts = m.groupby('day')['time_stamp'].min()
        bars = (m.groupby(['day', 'bucket'], sort=True)
                .agg(open=('open', 'first'), high=('high', 'max'), low=('low', 'min'), close=('close', 'last'),
                     volume=('volume', 'sum'), minutes=('open', 'count')).reset_index())
        bars['time_stamp'] = [first_ts[d] + timedelta(minutes=BAR_MIN * int(k)) for d, k in zip(bars['day'], bars['bucket'])]
        bars = bars.drop(columns=['day', 'bucket'])
        keep = [(token, ts + timedelta(minutes=BAR_MIN)) not in self.bar_gap for ts in bars['time_stamp']]
        self._bars[token] = bars[keep].reset_index(drop=True)
        return self._bars[token]

    def _st_for(self, token: str, spec) -> pd.DataFrame:
        key = (token, spec.st_period, spec.st_multiplier)
        if key not in self._st_cache:
            self._st_cache[key] = compute_st(self._bars_for(token), spec.st_period, spec.st_multiplier)
        return self._st_cache[key]

    @staticmethod
    def _point(row) -> SupertrendPoint:
        v = row['supertrend']
        if pd.isna(v):
            return SupertrendPoint(None, None, False)
        return SupertrendPoint(float(v), Direction.BULLISH if bool(row['trend']) else Direction.BEARISH,
                               bool(row['trend_flip']))

    @staticmethod
    def _bar(row) -> Bar:
        return Bar(ts=row['time_stamp'].to_pydatetime(), open=float(row['open']), high=float(row['high']),
                   low=float(row['low']), close=float(row['close']), volume=float(row['volume']))

    def _minute_frame(self, token: str) -> Optional[pd.DataFrame]:
        if token not in self._minute_idx:
            spec = self._contracts.get(token)
            self._minute_idx[token] = (None if spec is None or spec.minutes is None
                                       else spec.minutes.sort_values('time_stamp').set_index('time_stamp'))
        return self._minute_idx[token]

    def _price_at(self, token: str, t: datetime) -> Optional[float]:
        if token in self._prices:
            return self._prices[token]
        frame = self._minute_frame(token)
        if frame is None:
            return None
        minute = pd.Timestamp(t).floor('min')
        pos = frame.index.searchsorted(minute, side='right') - 1
        if pos < 0:
            return None
        r = frame.iloc[pos]
        if frame.index[pos] == minute and t.second < 30:
            return float(r['open'])
        return float(r['close'])

    def _infos(self, instrument: str) -> Tuple[ContractInfo, ...]:
        today = (self._session_date or self.now.date())
        out = []
        for spec in sorted(self._contracts.values(), key=lambda s: s.ref.expiry):
            if spec.ref.instrument != instrument:
                continue
            left = hcal.count_trading_days_inclusive(today, spec.ref.expiry, self.holidays)
            out.append(ContractInfo(spec.ref, spec.lot_size, spec.tick_size, spec.freeze_qty_lots, left))
        return tuple(out)

    def _info(self, ref: ContractRef) -> Optional[ContractInfo]:
        for i in self._infos(ref.instrument):
            if i.ref.token == ref.token:
                return i
        return None

    # ---- boundaries ------------------------------------------------------------------------------------------------

    def _boundary(self, token: str, boundary: datetime) -> None:
        """Bar `boundary - 15min .. boundary` of `token` closes. Every contract's bar becomes readable before any event
        for the trading contract is delivered (the tracked-contracts-first guarantee), because each token's own boundary
        callbacks at the same instant run in scheduling order and readers only ever query `_visible`."""
        bars = self._bars_for(token)
        idx = int((bars['time_stamp'] + timedelta(minutes=BAR_MIN) <= boundary).sum())   # bars ending by this boundary
        delay = self.bar_delay.get((token, boundary), 0.0)
        partial = self.bar_partial.get((token, boundary))
        if delay > 0:
            self._send_provisional(token, boundary, idx)
            self.kernel.after(delay, lambda: self._release_bar(token, boundary, idx, BarQuality.RECOVERED, True))
            return
        self._release_bar(token, boundary, idx, BarQuality.PARTIAL if partial else BarQuality.COMPLETE, False)

    def _gap(self, token: str, boundary: datetime) -> None:
        for task in self._traders_of(token):
            self._deliver(task, BarComplete(self._contracts[token].ref, boundary, None, None, None, BarQuality.GAP, 0))

    def _traders_of(self, token: str) -> List[EngineTask]:
        return [t for t in self._tasks.values() if t.trading_token == token and t.state != 'done']

    def _release_bar(self, token: str, boundary: datetime, idx: int, quality: BarQuality, reconciles: bool) -> None:
        self._visible[token] = max(self._visible.get(token, 0), idx)
        bars = self._bars_for(token)
        row_pos = idx - 1
        for task in self._traders_of(token):
            spec = task.engine.spec
            st = self._st_for(token, spec)
            row, prev = st.iloc[row_pos], (st.iloc[row_pos - 1] if row_pos >= 1 else None)
            prev_st = None if prev is None or pd.isna(prev['supertrend']) else float(prev['supertrend'])
            partial = self.bar_partial.get((token, boundary))
            minutes = int(partial if partial else bars.iloc[row_pos]['minutes'])
            self._deliver(task, BarComplete(self._contracts[token].ref, boundary, self._bar(bars.iloc[row_pos]),
                                            self._point(row), prev_st, quality, minutes, reconciles))

    def _send_provisional(self, token: str, boundary: datetime, idx: int) -> None:
        bars = self._bars_for(token)
        true = bars.iloc[idx - 1].copy()
        override = self.provisional_override.get((token, boundary), {})
        for k, v in override.items():
            true[k] = v
        true['high'] = max(true['high'], true['open'], true['close'])
        true['low'] = min(true['low'], true['open'], true['close'])
        for task in self._traders_of(token):
            spec = task.engine.spec
            if not spec.provisional.enabled:
                continue
            visible = bars.iloc[:idx - 1]
            combined = pd.concat([visible, pd.DataFrame([true])], ignore_index=True)
            st = compute_st(combined, spec.st_period, spec.st_multiplier)
            prev_st = None
            if len(st) >= 2 and not pd.isna(st.iloc[-2]['supertrend']):
                prev_st = float(st.iloc[-2]['supertrend'])
            self._deliver(task, ProvisionalBar(self._contracts[token].ref, boundary, self._bar(true),
                                               self._point(st.iloc[-1]), prev_st))

    # ---- request execution -----------------------------------------------------------------------------------------

    def _submit(self, task: EngineTask, request) -> RequestAck:
        name = task.name
        rid = request.request_id
        key = (name, rid)
        if key in self._registry:
            rec = self._registry[key]
            state = rec.outcome.status.value if rec.outcome else rec.state
            return RequestAck(rid, AckStatus.DUPLICATE, f'duplicate; {state}')
        if request.contract.token not in self._contracts:
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

    # -- ledger / margin --------------------------------------------------------------------------------------------

    def _held(self, engine: str, token: str) -> int:
        return self._ledger.get((engine, token), [0, None, None])[0]

    def _apply_fill(self, engine: str, contract: ContractRef, signed_lots: int, price: float) -> None:
        pos = self._ledger.setdefault((engine, contract.token), [0, None, self.now])
        net, avg = pos[0], pos[1]
        new = net + signed_lots
        if net == 0 or (net > 0) == (signed_lots > 0):
            pos[1] = price if net == 0 else (abs(net) * avg + abs(signed_lots) * price) / abs(new)
        elif new == 0:
            pos[1] = None
        elif (new > 0) != (net > 0):
            pos[1] = price
        pos[0], pos[2] = new, self.now

    def _margin_per_lot(self, info: ContractInfo, price: float) -> float:
        if self.cfg.margin_per_lot is not None:
            return self.cfg.margin_per_lot(info, price)
        return price * info.lot_size / 2.0

    def margin_used(self) -> float:
        used = 0.0
        for (engine, token), (net, avg, _) in self._ledger.items():
            if net == 0 or avg is None:
                continue
            info = self._info(self._contracts[token].ref)
            used += abs(net) * self._margin_per_lot(info, avg)
        used += sum(r.reserved for r in self._registry.values())
        return used

    def available_cash(self) -> float:
        return self.cfg.available_cash - self.margin_used()

    # -- admission --------------------------------------------------------------------------------------------------

    def _admit(self, rec: _Rec) -> Optional[Tuple[OutcomeStatus, str]]:
        """Returns None to proceed, or the refusal (status, detail). 'flat' is signalled as (FILLED, 'already flat')."""
        r, eng, token = rec.request, rec.engine, rec.request.contract.token
        held = self._held(eng, token)
        info = self._info(r.contract)
        kind = r.kind

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
            price = self._price_at(token, self.now) or 0.0
            need = r.open_lots * self._margin_per_lot(info, price) - r.close_lots * self._margin_per_lot(info, price)
            return self._entry_limits(rec, info, after, need, price, opening_new_side=True)
        # OPEN
        if held != 0 and dirn(held) != r.direction:
            return OutcomeStatus.REJECTED, f'would net against an existing {dirn(held).value} position'
        rec.open_target = r.lots
        after = abs(held) + r.lots
        price = self._price_at(token, self.now) or 0.0
        return self._entry_limits(rec, info, after, r.lots * self._margin_per_lot(info, price), price, False)

    def _entry_limits(self, rec: _Rec, info: ContractInfo, lots_after: int, margin_needed: float, price: float,
                      opening_new_side: bool) -> Optional[Tuple[OutcomeStatus, str]]:
        cap_lots = self._sizing[rec.engine].unit_cap * self._lots_per_unit[rec.engine]
        if lots_after > cap_lots:
            return OutcomeStatus.LIMIT_REFUSED, f'unit cap: {lots_after} lots would exceed {cap_lots}'
        if info.trading_days_left <= self.cfg.roll_window_days:
            return OutcomeStatus.LIMIT_REFUSED, (f'roll window: {info.ref.symbol} has {info.trading_days_left} trading days '
                                                 f'left (<= {self.cfg.roll_window_days})')
        if margin_needed > 0 and self.available_cash() < margin_needed:
            return OutcomeStatus.MARGIN_REFUSED, f'needs {margin_needed:,.0f}, available {self.available_cash():,.0f}'
        rec.reserved = max(margin_needed, 0.0)
        return None

    # -- execution --------------------------------------------------------------------------------------------------

    def _start(self, rec: _Rec) -> None:
        refusal = self._admit(rec)
        if refusal is not None:
            status, detail = refusal
            self._finish(rec, status, detail, zero_fill=(detail == 'already flat'))
            return
        rec.state = 'running'
        self._run_inc(rec)
        self._attempt(rec)

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
        reply = self.broker_behavior(BrokerCall(rec.engine, rec.request, rec.attempts, order_lots, rem_close, rem_open))
        if reply.kind == 'reject':
            rec.rejects += 1
            self.rejections.append((self.now, rec.engine, rec.request_id))
            limit = self.cfg.exit_max_attempts if rem_close > 0 else self.cfg.reject_retry_attempts
            if rec.rejects > limit:
                self.kernel.after(reply.latency, lambda: self._give_up(rec))
            else:
                self.kernel.after(reply.latency + self.cfg.reject_retry_cooldown_s, lambda: self._attempt(rec))
            return
        order = PlacedOrder(f'ORD{next(self._order_seq):05d}', self.now, rec.engine, rec.request_id, rec.request.contract,
                            self._side(rec, rem_close), order_lots, rec.attempts, rec.pclass)
        self.orders.append(order)
        if reply.kind == 'unconfirmed':
            rec.hidden = (order, reply)
            self.kernel.after(self.cfg.unconfirmed_after_s, lambda: self._go_unconfirmed(rec))
            if reply.resolve_after is not None:
                self.kernel.after(reply.resolve_after, lambda: self._resolve_unconfirmed(rec, 'order-update feed'))
            return
        delay = reply.latency + (self.cfg.ghost_recovery_s if reply.kind == 'ghost' else 0.0)
        self.kernel.after(delay, lambda: self._on_reply(rec, order, reply))

    def _side(self, rec: _Rec, rem_close: int) -> str:
        r = rec.request
        if r.kind == RequestKind.OPEN:
            return 'BUY' if r.direction == Direction.BULLISH else 'SELL'
        if r.kind == RequestKind.FLIP:
            return 'BUY' if r.to_direction == Direction.BULLISH else 'SELL'
        held = self._held(rec.engine, r.contract.token)
        return 'SELL' if held > 0 else 'BUY'

    def _fill_lots(self, rec: _Rec, order: PlacedOrder, lots: int) -> None:
        r = rec.request
        price = self._price_at(r.contract.token, self.now)
        if price is None:
            raise RuntimeError(f'no price for {r.contract.symbol} at {self.now}')
        sign = 1 if order.side == 'BUY' else -1
        px = price + sign * self.cfg.slippage_points
        to_close = min(lots, rec.close_target - rec.closed_lots)
        to_open = lots - to_close
        if to_close:
            fill = Fill(to_close, px, self.now, order.order_id)
            rec.closed_fills.append(fill)
            rec.closed_lots += to_close
            self._apply_fill(rec.engine, r.contract, sign * to_close, px)
        if to_open:
            fill = Fill(to_open, px, self.now, order.order_id)
            rec.opened_fills.append(fill)
            rec.opened_lots += to_open
            self._apply_fill(rec.engine, r.contract, sign * to_open, px)

    def _on_reply(self, rec: _Rec, order: PlacedOrder, reply: BrokerReply) -> None:
        if rec.state != 'running':
            return
        lots = order.lots if reply.kind in ('fill', 'ghost') else max(0, min(reply.lots or 0, order.lots))
        if lots:
            self._fill_lots(rec, order, lots)
        self._after_fill(rec)

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

    def _go_unconfirmed(self, rec: _Rec) -> None:
        if rec.state != 'running':
            return
        rec.state = 'unconfirmed'
        self._run_dec(rec)
        self._alert('critical', rec.engine, f'request {rec.request_id}: fill not confirmed; position status unknown')
        self._emit(rec, self._outcome(rec, OutcomeStatus.UNCONFIRMED, 'order placed, fill not confirmed'))
        if self.cfg.reconcile_interval_s is not None:
            self.kernel.after(self.cfg.reconcile_interval_s, lambda: self._begin_reconcile(rec))
        self._schedule_pump(0)

    def _begin_reconcile(self, rec: _Rec) -> None:
        """Read the broker's position book to settle an UNCONFIRMED request. Requests behind it on the same engine and
        contract wait for the answer, then act on the reconciled ledger."""
        if rec.state != 'unconfirmed':
            return
        rec.state = 'reconciling'
        self.kernel.after(self.cfg.reconcile_latency_s, lambda: self._resolve_unconfirmed(rec, 'broker book'))

    def _resolve_unconfirmed(self, rec: _Rec, source: str) -> None:
        if rec.state not in ('running', 'unconfirmed', 'reconciling') or rec.hidden is None:
            return
        order, reply = rec.hidden
        rec.hidden = None
        was_waiting = rec.state in ('unconfirmed', 'reconciling')
        lots = order.lots if reply.lots is None else max(0, min(reply.lots, order.lots))
        if was_waiting:
            rec.state = 'running'
            self._run_inc(rec)
        if lots == 0 and rec.closed_lots + rec.opened_lots == 0:
            self._alert('warning', rec.engine, f'request {rec.request_id}: {source} shows no fill; the order did not execute')
            self._finish(rec, OutcomeStatus.REJECTED, f'{source} shows no fill')
            return
        if lots:
            self._fill_lots(rec, order, lots)
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
        self._deliver_to(rec.engine, outcome)

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

    # ---- monitoring, crashes ----------------------------------------------------------------------------------------

    def _alert(self, level: str, engine: Optional[str], text: str, channel: Optional[str] = None) -> None:
        self.alerts.append(Alert(self.now, level, engine, text, channel))

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

    def _on_task_done(self, task: EngineTask) -> None:
        name = task.name
        if self._tasks.get(name) is not task:
            return
        if task.error is not None:
            self._crashed(task)
        elif self.engine_state.get(name) == 'running':
            self.engine_state[name] = 'ended'
            if not task.stop_delivered:
                self._alert('warning', name, 'engine returned without a Stop')

    def _crashed(self, task: EngineTask) -> None:
        name = task.name
        now = self.now
        window = self.cfg.restart_window_s
        self._crashes[name] = [t for t in self._crashes[name] if (now - t).total_seconds() < window] + [now]
        n = len(self._crashes[name])
        self._alert('error', name, f'engine crashed: {task.error!r}')
        if self.engine_state.get(name) == 'killed':
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
        if self.engine_state.get(name) not in ('running',):
            return
        self._launch(name)

    def _repeat_failed_alert(self, name: str) -> None:
        if self.engine_state.get(name) != 'failed':
            return
        holding = any(net for (eng, _), (net, _, _) in self._ledger.items() if eng == name)
        if holding:
            self._alert('critical', name, 'engine still failed and holds an open position')
        self.kernel.after(self.cfg.critical_repeat_s, lambda: self._repeat_failed_alert(name))

    # ---- misc -------------------------------------------------------------------------------------------------------

    def alerts_for(self, level: Optional[str] = None, engine: Optional[str] = None) -> List[Alert]:
        return [a for a in self.alerts if (level is None or a.level == level) and (engine is None or a.engine == engine)]

    def held(self, engine: str, contract: ContractRef) -> int:
        return self._held(engine, contract.token)

    def request_record(self, engine: str, request_id: str) -> Optional[_Rec]:
        return self._registry.get((engine, request_id))


# ---------------------------------------------------------------------------------------------------------------------
# The per-engine EngineContext
# ---------------------------------------------------------------------------------------------------------------------

class _Ctx:
    def __init__(self, hestia: FakeHestia, task: EngineTask):
        self._h, self._t = hestia, task

    def now(self):
        return self._h.now

    def wait(self, seconds):
        self._t.wait(seconds)

    def next_event(self, timeout):
        return self._t.next_event(timeout)

    def contracts(self, instrument):
        return self._h._infos(instrument)

    def set_trading_contract(self, contract):
        self._t.trading_token = contract.token

    def track(self, contract):
        h, t = self._h, self._t
        if contract.token in h._contracts and h._contracts[contract.token].minutes is not None:
            t.tracked.add(contract.token)
            h.kernel.after(1.0, lambda: h._deliver_to(t.name, TrackReady(contract)))
        else:
            h.kernel.after(1.0, lambda: h._deliver_to(t.name, TrackFailed(contract, 'no data for contract')))

    def untrack(self, contract):
        self._t.tracked.discard(contract.token)

    def ltp(self, contract):
        h = self._h
        price = h._price_at(contract.token, h.now)
        if price is None:
            return None
        since = h._stale.get(contract.token)
        return LtpQuote(price, since or h.now, (h.now - since).total_seconds() if since else 0.0)

    def _visible_rows(self, contract, last_n=None):
        h = self._h
        spec = h._contracts.get(contract.token)
        if spec is None or spec.minutes is None:
            return []
        st = h._st_for(contract.token, self._t.engine.spec)
        bars = h._bars_for(contract.token)
        v = h._visible.get(contract.token, 0)
        lo = 0 if last_n is None else max(0, v - last_n)
        return [(h._bar(bars.iloc[i]), h._point(st.iloc[i])) for i in range(lo, v)]

    def latest_bar(self, contract):
        rows = self._visible_rows(contract, 1)
        return rows[-1] if rows else None

    def st_series(self, contract, last_n):
        return tuple(self._visible_rows(contract, last_n))

    def price_near(self, contract, ts, tolerance_min):
        spec = self._h._contracts.get(contract.token)
        if spec is None or spec.minutes is None:
            return None
        m = spec.minutes
        window = m[(m['time_stamp'] >= pd.Timestamp(ts) - timedelta(minutes=tolerance_min))
                   & (m['time_stamp'] <= pd.Timestamp(ts) + timedelta(minutes=tolerance_min))]
        if window.empty:
            return None
        nearest = (window['time_stamp'] - pd.Timestamp(ts)).abs().idxmin()
        return float(window.loc[nearest, 'close'])

    def session_open(self):
        return self._h._session_open

    def position(self, contract):
        h = self._h
        net, avg, ts = h._ledger.get((self._t.name, contract.token), [0, None, h.now])
        return LedgerPosition(contract, net, avg, ts)

    def margin(self):
        return MarginSnapshot(self._h.available_cash(), self._h.now)

    def sizing(self):
        return self._h._sizing[self._t.name]

    def submit(self, request):
        return self._h._submit(self._t, request)

    def request_status(self, request_id):
        return self._h._status(self._t.name, request_id)

    def save_state(self, blob):
        self._h._saved_state[self._t.name] = blob

    def load_state(self):
        return self._h._saved_state.get(self._t.name)

    def alert(self, level, text, channel=None):
        self._h._alert(level, self._t.name, text, channel)

    def report_trade(self, record):
        extra = sorted(set(record) - set(TRADE_RECORD_COLUMNS))
        if extra:
            self._h.warnings.append(f'{self._t.name}: trade record keys dropped: {extra}')
        self._h.trades.append((self._t.name, {c: record.get(c) for c in TRADE_RECORD_COLUMNS}))
