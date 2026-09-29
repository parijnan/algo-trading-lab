"""
Hestia <-> engine interface, version 1.1 (v1 confirmed by the user 2026-09-28; v1.1, adopted by the user the same day, adds the six P3
clarifications made while building the fake Hestia, listed in plans/hestia-interface-spec.md, which is the prose spec).

The split (user, 2026-09-28): each strategy engine evaluates and decides (entries, exits, stops, the roll) on
processed data delivered by Hestia; Hestia performs the actual work (orders, fills, retries, reconciliation, data,
alerts). An engine sends ONE request per decision and changes its own state only on a confirmed outcome.

Everything crossing the boundary is one of the immutable types below. Nothing here does I/O, reads the clock, or
imports a broker library, so the same engine code can run against the live Hestia and against the fake/replay Hestia
used for verification. Engines must take time only from `EngineContext.now()` / `wait()`, never from `datetime.now()`
or `time.sleep()`.

Timestamps are timezone-naive IST datetimes, the convention used everywhere in this repo.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from enum import Enum, IntEnum
from typing import Optional, Protocol, Tuple, Union, runtime_checkable

INTERFACE_VERSION = '1.1'


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

class Direction(str, Enum):
    BULLISH = 'bullish'
    BEARISH = 'bearish'


class BarQuality(str, Enum):
    """How trustworthy a delivered 15-minute bar is. Hestia reports it; the engine decides what to do with it."""
    COMPLETE = 'complete'          # all minutes present from the candle endpoint
    RECOVERED = 'recovered'        # complete, but some minutes arrived late through the recovery queue
    PARTIAL = 'partial'            # built from what was on hand after the deferred-bar cutoff
    PROVISIONAL = 'provisional'    # built from tick-aggregated OHLC because the candle window was incomplete
    GAP = 'gap'                    # no minutes at all; no bar was built and the ST series has a hole


class ExitReason(str, Enum):
    STOP_LOSS = 'stop_loss'
    TREND_FLIP = 'trend_flip'
    ROLL = 'roll'                  # closing the old contract as part of a roll
    MANUAL_EXIT = 'manual_exit'    # the EXIT command
    OTHER = 'other'


class RequestKind(str, Enum):
    OPEN = 'open'
    CLOSE = 'close'
    FLIP = 'flip'                  # Rule 7: close and reopen the opposite side on ONE instrument as one netted order
    FLATTEN = 'flatten'            # close everything this engine holds in the contract, whatever its size


class OutcomeStatus(str, Enum):
    FILLED = 'filled'                      # every requested lot confirmed
    PARTIAL = 'partial'                    # Hestia stopped with fewer lots than requested (entries; exits are completed)
    REJECTED = 'rejected'                  # broker refused after Hestia's retries, or the ledger contradicted the request
    MARGIN_REFUSED = 'margin_refused'      # not enough margin at admission
    LIMIT_REFUSED = 'limit_refused'        # a Hestia hard limit: unit cap, roll window, freeze handling, circuit
    DEPENDENCY_FAILED = 'dependency_failed'  # a request it depended on did not fill
    UNCONFIRMED = 'unconfirmed'            # order placed but the fill could not be confirmed; position status unknown
    ABANDONED = 'abandoned'                # engine stopped (KILL) before Hestia finished; nothing was cancelled
    IN_FLIGHT = 'in_flight'                # only ever returned by request_status() for a request still being worked;
                                           # never delivered as an event, never confirmed


class AckStatus(str, Enum):
    ACCEPTED = 'accepted'          # queued; the outcome arrives later as an event
    DUPLICATE = 'duplicate'        # this request_id was seen before; `outcome` carries its status if final
    INVALID = 'invalid'


class PriorityClass(IntEnum):
    """Order of service when engines clash (plan section 1.8). Lower is served first."""
    STOP_OR_FLATTEN = 1
    CLOSE = 2
    ROLL_EXIT = 3
    OPEN = 4
    ROLL_REOPEN = 5


class CommandKind(str, Enum):
    EXIT = 'exit'                  # liquidate this engine's position, re-arm to watching, keep the session
    KILL = 'kill'                  # stop this engine, leave any position open and untouched


class StopReason(str, Enum):
    SESSION_END = 'session_end'
    KILL = 'kill'
    HOST_KILL = 'host_kill'
    SHUTDOWN = 'shutdown'


# ---------------------------------------------------------------------------
# Market facts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContractRef:
    instrument: str        # 'SILVERMIC'
    token: str             # broker symbol token
    symbol: str            # trading symbol, e.g. 'SILVERMIC30NOV26FUT'
    expiry: date


@dataclass(frozen=True)
class ContractInfo:
    ref: ContractRef
    lot_size: int
    tick_size: float
    freeze_qty_lots: int
    trading_days_left: int  # holiday-aware, counting today, to the expiry date


@dataclass(frozen=True)
class Bar:
    ts: datetime           # window start
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class SupertrendPoint:
    value: Optional[float]           # None during warm-up
    trend: Optional[Direction]       # None during warm-up
    flip: bool


@dataclass(frozen=True)
class LtpQuote:
    price: float
    ts: datetime
    age_sec: float                   # how old the tick is at read time; the engine decides what stale means


@dataclass(frozen=True)
class LedgerPosition:
    """What actually filled and is held, per engine and contract, from confirmed fills reconciled to the broker book."""
    contract: ContractRef
    net_lots: int                    # signed: > 0 long, < 0 short, 0 flat
    avg_price: Optional[float]
    updated_ts: datetime


@dataclass(frozen=True)
class MarginSnapshot:
    available_cash: float            # the account-wide pool at read time
    ts: datetime


@dataclass(frozen=True)
class SizingConfig:
    """Both sizing modes are kept (user, 2026-09-28). The engine chooses units: static -> `static_units`; dynamic ->
    its own rule over capital and margin per unit (Prometheus: max(1, capital // margin_per_unit)). Hestia only
    supplies the live-read settings and enforces the cap."""
    dynamic: bool                    # False -> static_units; True -> the engine's dynamic rule (Selene defaults to off)
    static_units: int                # live-read override, per engine
    unit_cap: int                    # Hestia refuses OPEN/FLIP requests that would exceed it
    allocation_rs: Optional[float] = None   # capital the engine may size against under dynamic mode; None means the
                                            # account's available cash (with several engines on one pool, set it)


# ---------------------------------------------------------------------------
# What an engine registers: its data spec
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProvisionalSpec:
    """Ask Hestia to build provisional bars from ticks when the candle window is incomplete at the boundary.
    The margin guard and the decision to act stay in the engine (plan section 1.9)."""
    enabled: bool = True


@dataclass(frozen=True)
class DataSpec:
    instrument: str
    timeframe_min: int
    st_period: int
    st_multiplier: float
    seed_days: int = 18
    provisional: ProvisionalSpec = ProvisionalSpec()
    watch_dpl: bool = True


# ---------------------------------------------------------------------------
# Events: Hestia -> engine, delivered on the engine's own queue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionStart:
    session_date: date
    session_open: datetime           # the actual first tradeable moment (17:00 on an evening-only day)
    rollover_time: datetime          # when a fallback roll runs today
    evening_only: bool
    contracts: Tuple[ContractInfo, ...]
    seeded: Tuple[ContractRef, ...]  # contracts whose bars and ST are ready to read


@dataclass(frozen=True)
class BarComplete:
    contract: ContractRef
    boundary_ts: datetime            # the boundary this bar closes at
    bar: Optional[Bar]               # None only when quality is GAP (no bar was built)
    st: Optional[SupertrendPoint]    # None only when quality is GAP
    prev_st: Optional[float]         # the previous bar's supertrend, the line this bar had to cross
    quality: BarQuality
    minutes_present: int
    reconciles_provisional: bool = False   # True when this is the real bar for a boundary that had a ProvisionalBar


@dataclass(frozen=True)
class ProvisionalBar:
    contract: ContractRef
    boundary_ts: datetime
    bar: Bar                          # tick-derived
    st: SupertrendPoint               # computed with the engine's own period and multiplier
    prev_st: Optional[float]


@dataclass(frozen=True)
class TrackReady:
    contract: ContractRef


@dataclass(frozen=True)
class TrackFailed:
    contract: ContractRef
    reason: str


@dataclass(frozen=True)
class FeedStale:
    contract: ContractRef
    age_sec: float


@dataclass(frozen=True)
class FeedRecovered:
    contract: ContractRef


@dataclass(frozen=True)
class DplFrozen:
    contract: ContractRef
    price: float
    frozen: bool                      # False when it unfreezes


@dataclass(frozen=True)
class CommandEvent:
    kind: CommandKind


@dataclass(frozen=True)
class Stop:
    """Hestia is ending this engine's thread. With KILL/HOST_KILL, no order is sent and any position stays open."""
    reason: StopReason
    leave_position: bool


# ---------------------------------------------------------------------------
# Requests: engine -> Hestia. One per decision. `request_id` is created by the engine and PERSISTED BEFORE SENDING,
# so a restarted engine can ask Hestia what became of it instead of sending it again (plan section 1.4).
# ---------------------------------------------------------------------------

def _check_request(request_id: str, lots: Optional[int] = None, name: str = 'lots') -> None:
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError('request_id must be a non-empty string')
    if lots is not None and (not isinstance(lots, int) or isinstance(lots, bool) or lots <= 0):
        raise ValueError(f'{name} must be a positive int, got {lots!r}')


@dataclass(frozen=True)
class OpenRequest:
    request_id: str
    contract: ContractRef
    direction: Direction
    lots: int
    trade_ref: int                     # the engine's trade id, for the ledger and trade log
    roll_reopen: bool = False          # the second half of a roll (lowest priority class)
    parent_trade_ref: Optional[int] = None
    depends_on: Optional[str] = None   # send only after this request_id is confirmed FILLED

    kind = RequestKind.OPEN

    def __post_init__(self):
        _check_request(self.request_id, self.lots)


@dataclass(frozen=True)
class CloseRequest:
    request_id: str
    contract: ContractRef
    expected_direction: Direction      # what the engine believes it holds; Hestia REJECTs if the ledger disagrees
    reason: ExitReason
    lots: Optional[int] = None         # None means every lot held; a partial close must say how many
    trade_ref: Optional[int] = None
    depends_on: Optional[str] = None

    kind = RequestKind.CLOSE

    def __post_init__(self):
        _check_request(self.request_id, self.lots)


@dataclass(frozen=True)
class FlipRequest:
    request_id: str
    contract: ContractRef
    from_direction: Direction
    close_lots: int
    open_lots: int
    trade_ref: int                     # the NEW trade's id
    reason: ExitReason = ExitReason.TREND_FLIP
    depends_on: Optional[str] = None

    kind = RequestKind.FLIP

    def __post_init__(self):
        _check_request(self.request_id, self.close_lots, 'close_lots')
        _check_request(self.request_id, self.open_lots, 'open_lots')

    @property
    def to_direction(self) -> Direction:
        return Direction.BEARISH if self.from_direction == Direction.BULLISH else Direction.BULLISH


@dataclass(frozen=True)
class FlattenRequest:
    request_id: str
    contract: ContractRef
    reason: ExitReason
    trade_ref: Optional[int] = None

    kind = RequestKind.FLATTEN

    def __post_init__(self):
        _check_request(self.request_id)


Request = Union[OpenRequest, CloseRequest, FlipRequest, FlattenRequest]


def priority_class(req: Request) -> PriorityClass:
    """Deterministic service class for a request (plan section 1.8). A netted flip is served as a close: delaying the
    exit half is the costly part."""
    if isinstance(req, FlattenRequest):
        return PriorityClass.STOP_OR_FLATTEN
    if isinstance(req, CloseRequest):
        if req.reason == ExitReason.STOP_LOSS:
            return PriorityClass.STOP_OR_FLATTEN
        return PriorityClass.ROLL_EXIT if req.reason == ExitReason.ROLL else PriorityClass.CLOSE
    if isinstance(req, FlipRequest):
        return PriorityClass.STOP_OR_FLATTEN if req.reason == ExitReason.STOP_LOSS else PriorityClass.CLOSE
    if isinstance(req, OpenRequest):
        return PriorityClass.ROLL_REOPEN if req.roll_reopen else PriorityClass.OPEN
    raise TypeError(f'not a request: {req!r}')


# ---------------------------------------------------------------------------
# Acknowledgement and outcome
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fill:
    lots: int
    price: float
    ts: datetime
    order_id: str


@dataclass(frozen=True)
class FillSummary:
    lots: int
    avg_price: Optional[float]
    fills: Tuple[Fill, ...] = ()


@dataclass(frozen=True)
class RequestAck:
    request_id: str
    status: AckStatus
    detail: str = ''


@dataclass(frozen=True)
class RequestOutcome:
    """The final word on a request, delivered as an event. An engine changes its state ONLY on a `confirmed` outcome
    (the fill-confirmation invariant, from the 2026-08-31 incident). UNCONFIRMED and ABANDONED are not confirmed:
    Hestia keeps reconciling an UNCONFIRMED request and sends a later RequestOutcome when it resolves."""
    request_id: str
    status: OutcomeStatus
    kind: RequestKind
    requested_lots: int
    ts: datetime
    closed: Optional[FillSummary] = None    # the exit half (CLOSE, FLATTEN, FLIP)
    opened: Optional[FillSummary] = None    # the entry half (OPEN, FLIP)
    detail: str = ''

    @property
    def confirmed(self) -> bool:
        return self.status in (OutcomeStatus.FILLED, OutcomeStatus.PARTIAL)


Event = Union[SessionStart, BarComplete, ProvisionalBar, TrackReady, TrackFailed, FeedStale, FeedRecovered,
              DplFrozen, CommandEvent, Stop, RequestOutcome]


# ---------------------------------------------------------------------------
# Engine-persisted request bookkeeping (the restart-safety net)
# ---------------------------------------------------------------------------

@dataclass
class PendingRequest:
    """What an engine writes to its own state BEFORE calling submit(). On resume it asks
    EngineContext.request_status(request_id): FILLED -> apply the outcome; in flight -> wait for the outcome event;
    unknown -> it was never sent, so send it again under the same id."""
    request_id: str
    kind: str
    reason: str
    created_ts: str                   # ISO
    trade_ref: Optional[int] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: str) -> 'PendingRequest':
        return PendingRequest(**json.loads(raw))


# ---------------------------------------------------------------------------
# Trade records: the same fixed columns as Prometheus's prometheus_trades.csv (user, 2026-09-28)
# ---------------------------------------------------------------------------

# Copied from prometheus_production/prometheus_functions.py TRADE_LOG_COLUMNS; a test keeps the two identical.
# `report_trade` reindexes every record against this tuple (extra keys dropped with a warning, missing keys null), so
# the file's shape never depends on which keys a particular trade happened to carry. Selene fills the lot1 columns
# and leaves the lot2 and target columns empty. The per-minute running rows use Prometheus's running-row format too.
TRADE_RECORD_COLUMNS = (
    'trade_id', 'contract_expiry', 'direction', 'units', 'entry_ts', 'entry_price',
    'signal_ts', 'signal_close', 'entry_slippage_points', 'sl_price',
    'lot1_target', 'lot2_target', 'lot2_target_source', 'parent_trade_id',
    'lot1_exit_ts', 'lot1_exit_price', 'lot1_exit_reason', 'lot1_pnl_points', 'lot1_pnl_rs',
    'lot2_exit_ts', 'lot2_exit_price', 'lot2_exit_reason', 'lot2_pnl_points', 'lot2_pnl_rs',
    'total_pnl_points', 'total_pnl_rs',
)


# The per-minute running-row log: one row roughly every 60s while a trade is open, appended (never rewritten, so a crash never
# loses earlier rows), one file per trade -- same columns as production's own TRADE_LOG_COLUMNS (prometheus_functions.py). A row
# with `exit_reason` set is the trade's last one, written at the moment a lot exits.
RUNNING_ROW_COLUMNS = (
    'trade_id', 'entry_ts', 'ts', 'minutes_since_entry', 'ltp', 'sl_price', 'lot1_target', 'lot2_target',
    'lot1_pnl_points', 'lot1_pnl_rs', 'lot2_pnl_points', 'lot2_pnl_rs', 'total_pnl_points', 'total_pnl_rs', 'exit_reason',
)


# ---------------------------------------------------------------------------
# The two protocols
# ---------------------------------------------------------------------------

@runtime_checkable
class EngineContext(Protocol):
    """Everything an engine may call. Implemented by the live Hestia and by the fake/replay Hestia."""

    # time: engines never touch the wall clock
    def now(self) -> datetime: ...
    def wait(self, seconds: float) -> None: ...

    # the engine's own loop: blocks up to `timeout` seconds for the next event, returns None on timeout. Each call
    # is also the engine's heartbeat; Hestia alerts when it has not been called for the silence threshold.
    def next_event(self, timeout: float) -> Optional[Event]: ...

    # facts and data
    def contracts(self, instrument: str) -> Tuple[ContractInfo, ...]: ...
    def set_trading_contract(self, contract: ContractRef) -> None: ...    # tells Hestia which contract this engine trades
    def track(self, contract: ContractRef) -> None: ...          # answered by TrackReady or TrackFailed
    def untrack(self, contract: ContractRef) -> None: ...
    def ltp(self, contract: ContractRef) -> Optional[LtpQuote]: ...
    def latest_bar(self, contract: ContractRef) -> Optional[Tuple[Bar, SupertrendPoint]]: ...
    def price_near(self, contract: ContractRef, ts: datetime, tolerance_min: int) -> Optional[float]: ...
    def st_series(self, contract: ContractRef, last_n: int) -> Tuple[Tuple[Bar, SupertrendPoint], ...]: ...
    def session_open(self) -> datetime: ...

    # account
    def position(self, contract: ContractRef) -> LedgerPosition: ...
    def margin(self) -> MarginSnapshot: ...
    def sizing(self) -> SizingConfig: ...

    # execution: one request per decision
    def submit(self, request: Request) -> RequestAck: ...
    def request_status(self, request_id: str) -> Optional[RequestOutcome]: ...   # None only if Hestia has never seen it;
                                                                                 # status IN_FLIGHT while still being worked

    # persistence of the engine's own decision state (an opaque string; Hestia stores it per engine)
    def save_state(self, blob: str) -> None: ...
    def load_state(self) -> Optional[str]: ...

    # reporting
    def alert(self, level: str, text: str, channel: Optional[str] = None, emoji: Optional[str] = None) -> None: ...
    # emoji: an explicit per-event override (e.g. an engine's own "starting"/"seeded" messages) -- None (the default) falls
    # back to AlertRouter's severity-based emoji, unchanged from before this parameter existed.
    def report_trade(self, record: dict) -> None: ...      # keys from TRADE_RECORD_COLUMNS
    def report_running_row(self, record: dict) -> None: ...  # keys from RUNNING_ROW_COLUMNS, in-trade, roughly once a minute


@runtime_checkable
class Engine(Protocol):
    name: str
    spec: DataSpec

    def run(self, ctx: EngineContext) -> None:
        """Blocks until Stop arrives (or the engine decides to end). Exceptions propagate to Hestia, which alerts and
        auto-resumes the engine from its saved state with the restart limit."""
        ...
