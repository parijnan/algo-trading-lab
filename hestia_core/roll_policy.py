"""
The roll policy library (plans/hestia-p5-prometheus-engine.md, slice P5.1; plans/selene-production.md sections 1.7 and 3).

One rule for every commodity (user decision, 2026-09-28): roll before the tender-margin period starts, 5 trading days before expiry,
counting today, holiday-aware. On the eve of a roll, with a position open:
  * a trend flip on the old contract before the rollover time exits it; if the NEW contract's own supertrend flips to the same
    direction on the same bar (with a full window of data), the engine enters fresh on the new contract, at a real fill price;
  * a position still open at the rollover time (23:15, or 23:40 when the session closes at 23:55) is flattened unconditionally and
    reopened on the new contract only if the new contract's supertrend agrees (the veto), sized to the lots that survived, with its
    stop recalibrated off the historical basis (the new contract's price at the time of the old entry);
  * flat on a roll eve: switch to the new contract at once, there is nothing to protect.
A restart that finds the position on a different contract from today's effective one is either a roll-forward already done today
(catch up) or a genuinely missed roll (roll now).

This module is pure: functions of dates, contracts, prices and directions, with no I/O, no clock, no broker and no engine state, so
the same rules serve every engine and the tests need no fixtures. It is ported from prometheus_production/prometheus.py sections
4 to 9 and 18 and pinned to production's own contract resolution and basis lookup by tests/test_hestia_roll_policy.py. The two
deliberate differences from Prometheus, both from the plan: a missing next contract means FLATTEN (Prometheus carries the position
through the tender window with a warning), and the roll window is a parameter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import AbstractSet, Iterable, Optional, Sequence, Tuple

from hestia_core import trading_calendar as hcal
from hestia_core.interface import ContractRef, Direction

ROLL_WINDOW_DAYS = 5                    # trading days, counting today, at or below which the front contract is rolled out of
BASIS_TOLERANCE_MIN = 5                 # the historical basis price must come from a minute within this of the old entry
COINCIDENT_MIN_ROWS = 15                # the new contract needs a complete 15-minute window to claim a coincident flip
ROLLOVER_BEFORE_CLOSE_MIN = 15          # the fallback roll runs this long before the session's close


class NoContract(Exception):
    """No live contract is listed for the instrument on that date."""


# ---------------------------------------------------------------------------------------------------------------------
# Which contract is effective
# ---------------------------------------------------------------------------------------------------------------------

def days_left(on_date: date, expiry: date, fully_closed: AbstractSet[date]) -> int:
    """Trading days from `on_date` to `expiry`, both counted (a one-session holiday still counts, as in production)."""
    return hcal.count_trading_days_inclusive(on_date, expiry, fully_closed)


@dataclass(frozen=True)
class Effective:
    contract: ContractRef
    days_left: int                       # the FRONT contract's trading days left on the date asked about
    rolled_early: bool                   # the front is inside the roll window and the next contract is returned instead
    no_next: bool                        # inside the window with no later contract listed: the policy is to flatten


def effective_contract(contracts: Iterable[ContractRef], on_date: date, fully_closed: AbstractSet[date],
                       roll_window_days: int = ROLL_WINDOW_DAYS) -> Effective:
    """The contract an engine should be trading on `on_date`: the nearest unexpired one, or the next one out when the nearest has
    `roll_window_days` or fewer trading days left. With no next contract listed the front is returned with `no_next=True`."""
    live = sorted((c for c in contracts if c.expiry >= on_date), key=lambda c: c.expiry)
    if not live:
        raise NoContract(f'no live contract on {on_date}')
    front = live[0]
    left = days_left(on_date, front.expiry, fully_closed)
    if left <= roll_window_days:
        if len(live) > 1:
            return Effective(live[1], left, True, False)
        return Effective(front, left, False, True)
    return Effective(front, left, False, False)


def tracked_contracts(contracts: Iterable[ContractRef], on_date: date, fully_closed: AbstractSet[date], n: int = 2,
                      roll_window_days: int = ROLL_WINDOW_DAYS) -> List[ContractRef]:
    """The `n` contracts actually worth tracking price data for on `on_date`: the currently-EFFECTIVE contract
    (`effective_contract`) plus the next `n - 1` after it by expiry. A contract that has already rolled past drops out
    of tracking immediately, even if its own calendar expiry hasn't arrived yet -- unlike a raw "not yet expired" cut
    (`sorted(c for c in contracts if c.expiry >= on_date)[:n]`), which keeps an already-rolled-off, about-to-expire
    contract in the tracked set right up through its own expiry date.

    Found 2026-09-30: that raw cut is what both `LiveData.prepare()` (the live seeding step) and `--check` used to
    apply directly. A genuine data gap on an already-rolled-off contract's near-expiry, thin trading blocked
    `prepare()` -- a hard blocking call every engine's session-start waits on -- for 8 minutes, for every engine, not
    just the one that traded the stale contract."""
    live = sorted((c for c in contracts if c.expiry >= on_date), key=lambda c: c.expiry)
    if not live:
        return live
    try:
        eff = effective_contract(live, on_date, fully_closed, roll_window_days)
    except NoContract:
        return live[:n]
    idx = next((i for i, c in enumerate(live) if c == eff.contract), 0)
    return live[idx:idx + n]


def effective_from_days_left(contracts_with_days: Iterable[Tuple[ContractRef, int]],
                             roll_window_days: int = ROLL_WINDOW_DAYS, days_offset: int = 0) -> Effective:
    """`effective_contract` for an engine that knows each contract's trading days left today but has no holiday calendar: the front
    contract's days left is `days_offset` fewer than today's (an offset of 1 answers "what is tomorrow's effective contract", because
    tomorrow's trading day is the next trading day, exactly one fewer trading day from expiry)."""
    live = sorted(contracts_with_days, key=lambda cd: cd[0].expiry)
    if not live:
        raise NoContract('no live contract')
    front, left = live[0][0], live[0][1] - days_offset
    if left <= roll_window_days:
        if len(live) > 1:
            return Effective(live[1][0], left, True, False)
        return Effective(front, left, False, True)
    return Effective(front, left, False, False)


class NoNextAction(str, Enum):
    FLATTEN = 'flatten'                  # the plan's policy: never carry a position into the tender window
    CARRY = 'carry'                      # Prometheus's current behaviour, kept only for parity checks


@dataclass(frozen=True)
class RollNeed:
    new_contract: Optional[ContractRef]  # tomorrow's effective contract when it differs from today's, else None
    no_next: bool = False                # tomorrow is inside the window and no later contract exists


def roll_needed_tonight(current: ContractRef, today: date, contracts: Iterable[ContractRef], fully_closed: AbstractSet[date],
                        roll_window_days: int = ROLL_WINDOW_DAYS) -> RollNeed:
    """Checked once at setup: does tomorrow's TRADING day (not a naive today + 1) resolve to a different contract?"""
    tomorrow = hcal.next_trading_day(today, fully_closed)
    eff = effective_contract(contracts, tomorrow, fully_closed, roll_window_days)
    if eff.no_next:
        return RollNeed(None, True)
    return RollNeed(eff.contract if eff.contract.token != current.token else None, False)


class ArmAction(str, Enum):
    NONE = 'none'
    SWITCH_NOW = 'switch_now'            # flat: move to the new contract immediately, no churn on the old one
    ARM_EVENING = 'arm_evening'          # in a trade: track the new contract all day and decide at the rollover time


def arm_action(need: RollNeed, in_trade: bool) -> ArmAction:
    if need.new_contract is None:
        return ArmAction.NONE
    return ArmAction.ARM_EVENING if in_trade else ArmAction.SWITCH_NOW


def flat_switch_due(armed: bool, status_watching: bool, pending_flip: bool) -> bool:
    """On a roll-armed evening the flat switch is re-checked every cycle so it self-heals when a position closes mid-day. Only a
    plain `watching` state counts, and never while a Rule 7 flip is mid-flight (it is still on the old contract)."""
    return armed and status_watching and not pending_flip


# ---------------------------------------------------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------------------------------------------------

def rollover_time(session_date: date, session_close: time, before_close_min: int = ROLLOVER_BEFORE_CLOSE_MIN) -> datetime:
    """23:15 on a 23:30 close, 23:40 on a 23:55 close."""
    close = datetime.combine(session_date, session_close)
    return close - timedelta(minutes=before_close_min)


def entry_suppressed(armed: bool, now: datetime, rollover_at: datetime) -> bool:
    """From the rollover time on, once a roll is armed, no fresh or Rule 7 entry: it would open seconds before being flattened."""
    return armed and now >= rollover_at


def before_rollover(now: datetime, rollover_at: datetime) -> bool:
    return now < rollover_at


# ---------------------------------------------------------------------------------------------------------------------
# The historical basis and the reopen size
# ---------------------------------------------------------------------------------------------------------------------

def basis_price(closes: Iterable[Tuple[datetime, float]], entry_ts: datetime,
                tolerance_min: float = BASIS_TOLERANCE_MIN) -> Optional[float]:
    """What the NEW contract was trading at, at the timestamp the old position was entered: the close of its nearest minute
    (the earliest wins a tie), or None if nothing lies within `tolerance_min`. Never a guessed or interpolated price."""
    best: Optional[Tuple[timedelta, float]] = None
    for ts, close in closes:
        delta = abs(ts - entry_ts)
        if best is None or delta < best[0]:
            best = (delta, float(close))
    if best is None or best[0] > timedelta(minutes=tolerance_min):
        return None
    return best[1]


@dataclass(frozen=True)
class ReopenPlan:
    lots: int
    lot2_only: bool                      # only the far-target lot survived to the roll: reopen it alone, not a fresh split


def reopen_plan(lot1_open: bool, lot1_lots: int, lot2_open: bool, lot2_lots: int) -> ReopenPlan:
    """Size the reopen to the lots that actually survived to the roll, not blindly both."""
    lots = (lot1_lots or 0 if lot1_open else 0) + (lot2_lots or 0 if lot2_open else 0)
    return ReopenPlan(lots, lot2_open and not lot1_open)


# ---------------------------------------------------------------------------------------------------------------------
# The decisions
# ---------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RollDecision:
    flatten: bool                        # close the old position (always, whatever else)
    reopen: bool                         # and open the carried direction on the new contract
    reason: str


def decide_rollover(position: Optional[Direction], new_direction: Optional[Direction], basis: Optional[float],
                    alignment_ok: bool = True) -> RollDecision:
    """The fallback roll at the rollover time. The old position is flattened unconditionally; it is reopened only if the new
    contract's supertrend agrees with it (the veto), the optional alignment filter agrees, and a basis price exists for the stop.
    Incomplete data is a no-go, never a guess."""
    if position is None:
        return RollDecision(False, False, 'flat: housekeeping only, nothing to veto')
    if new_direction is None:
        return RollDecision(True, False, 'no-go: the new contract supertrend is unavailable')
    if new_direction != position:
        return RollDecision(True, False, f'no-go: new contract is {new_direction.value}, position is {position.value}')
    if not alignment_ok:
        return RollDecision(True, False, 'no-go: the alignment filter disagrees')
    if basis is None:
        return RollDecision(True, False, 'go, but no basis price is available: flatten only')
    return RollDecision(True, True, 'go')


def coincident_flip(new_bar_flipped: bool, new_direction: Optional[Direction], flip_direction: Direction, rows_in_window: int,
                    min_rows: int = COINCIDENT_MIN_ROWS) -> bool:
    """On a roll eve, before the rollover time, an old-contract flip exits the position; this says whether the new contract ALSO
    flipped to the same direction on the same bar, in which case a fresh entry follows. An incomplete new-contract window never
    claims a coincidence (the exit is unaffected either way)."""
    if new_direction is None or rows_in_window < min_rows:
        return False
    return new_bar_flipped and new_direction == flip_direction


class RestartAction(str, Enum):
    NONE = 'none'                        # the position is on today's effective contract
    CATCH_UP = 'catch_up'                # it is on a LATER contract: an early switch already happened today; adopt that contract
    MISSED_ROLL = 'missed_roll'          # it is on an EARLIER contract: a roll was missed overnight; roll immediately
    REFUSE = 'refuse'                    # looks like a roll-forward but the lookup failed: never auto-roll backwards


def restart_action(position_token: Optional[str], position_expiry: Optional[date], effective_token: str, effective_expiry: date,
                   catch_up_lookup_ok: bool = True) -> RestartAction:
    """What a restarted engine does when its open position is not on today's effective contract."""
    if position_token is None or position_token == effective_token:
        return RestartAction.NONE
    if position_expiry is not None and position_expiry > effective_expiry:
        return RestartAction.CATCH_UP if catch_up_lookup_ok else RestartAction.REFUSE
    return RestartAction.MISSED_ROLL
