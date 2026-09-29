"""
The Prometheus engine: the decision layer of prometheus_production/prometheus.py against hestia_core.interface.

Engine and host split (plan sections 1 and 3): this file decides (which contract, when to enter, flip, stop, take a target, roll, what
size), Hestia executes (orders, retries, fill confirmation, the ledger, data, alerts). The engine sends ONE request per decision and
changes its position state only on a CONFIRMED outcome (FILLED or PARTIAL); it persists the request id BEFORE sending, so a restarted
engine can ask `request_status` what became of it instead of sending it again.

What changed from the standalone process, all forced by the split and each named where it happens:
  * the implicit retry ("lot status is still open, so the next tick re-fires the exit") is now an explicit pending-request state: while a
    request is outstanding the engine makes no new decision that could conflict with it, and an unconfirmed request is waited on, not
    re-sent;
  * a failed exit or flip is re-sent after a short cooldown with a fresh request id (Hestia has already retried inside the request);
  * an entry that fills only partly is accepted as it is (Hestia never tops an entry up); the standalone process kept retrying the
    remainder of a Rule 7 re-entry;
  * the exit half of a Rule 7 flip is completed by Hestia, so a FlipRequest is netted exactly as before (close old lots + open new
    lots as one order) and its outcome carries both halves;
  * the roll decisions are hestia_core.roll_policy's (a missing next contract means flatten); the engine only orchestrates them;
  * per-minute running rows and periodic Slack P&L updates are not reproduced here (Hestia's session report and trade log carry the
    closed trades); recorded in the plan as left for the host.
The position the engine believes it holds is reconciled against Hestia's ledger at every start, and the ledger wins.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from hestia_core import roll_policy as rp
from hestia_core.interface import (
    BarComplete, BarQuality, CloseRequest, CommandEvent, CommandKind, ContractInfo, ContractRef, DataSpec, Direction, DplFrozen,
    ExitReason, FeedRecovered, FeedStale, FlattenRequest, FlipRequest, OpenRequest, OutcomeStatus, ProvisionalBar, ProvisionalSpec,
    RequestKind, RequestOutcome, SessionStart, Stop, TrackFailed, TrackReady,
)
from prometheus_engine.engine_configs import DEFAULT, EngineConfig
from prometheus_engine.levels import build_levels, lot_pnl_points, margin_per_unit
from prometheus_engine.state import EngineState

log = logging.getLogger('prometheus_engine')

BULL, BEAR = 'bullish', 'bearish'


def _dir(name: str) -> Direction:
    return Direction.BULLISH if name == BULL else Direction.BEARISH


def _name(d: Optional[Direction]) -> Optional[str]:
    return None if d is None else (BULL if d == Direction.BULLISH else BEAR)


class PrometheusEngine:

    def __init__(self, cfg: EngineConfig = DEFAULT, name: str = 'prometheus'):
        self.cfg, self.name = cfg, name
        self.spec = DataSpec(cfg.instrument, 15, cfg.st_period, cfg.st_multiplier, provisional=ProvisionalSpec(cfg.provisional_enabled))
        self.state = EngineState()
        self.ctx = None
        # session-scoped, memory only (a restart re-arms them, as in the standalone process)
        self.started = False
        self.ended = False
        self.infos: Dict[str, ContractInfo] = {}
        self.contract: Optional[ContractRef] = None                  # the contract this engine trades and has told Hestia about
        self.session_date: Optional[date] = None
        self.session_open: Optional[datetime] = None
        self.rollover_at: Optional[datetime] = None
        self.session_close: Optional[datetime] = None                # derived: SessionStart carries the rollover time, not the close
        self.provisional_disabled = False
        self.provisional_pending: Optional[dict] = None
        self.exit_requested = False
        self.market_closed = False                                   # Hestia refused a request as after-the-close: stop deciding for the night
        self.flatten_done = False                                    # a no-next-contract roll flattened: no re-entry for the rest of tonight
        self.next_retry_at: Optional[datetime] = None
        self._alerted: Dict[str, datetime] = {}
        self._last_trade_update: Optional[datetime] = None                # periodic #trade-updates cadence, this life only
        self._last_running_row: Optional[datetime] = None                 # per-trade running-log cadence, this life only
        self.last_ltp: Optional[float] = None

    # ------------------------------------------------------------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------------------------------------------------------------

    def run(self, ctx) -> None:
        self.ctx = ctx
        self.state = EngineState.from_json(ctx.load_state())
        while not self.ended:
            timeout = self.cfg.tick_s_in_trade if self.state.status == 'in_trade' else self.cfg.tick_s_flat
            ev = ctx.next_event(timeout)
            if ev is None:
                if self.started:
                    self._tick()
                continue
            self._on_event(ev)
            if self.started and not self.ended:
                self._tick()

    def _on_event(self, ev) -> None:
        if isinstance(ev, SessionStart):
            self._on_session_start(ev)
        elif isinstance(ev, Stop):
            self._save()
            self.ended = True
        elif not self.started:
            return
        elif isinstance(ev, BarComplete):
            self._on_bar(ev)
        elif isinstance(ev, ProvisionalBar):
            self._on_provisional(ev)
        elif isinstance(ev, RequestOutcome):
            self._on_outcome(ev)
        elif isinstance(ev, CommandEvent):
            if ev.kind == CommandKind.EXIT:
                self._on_exit_command()
        elif isinstance(ev, TrackFailed):
            self._say('warning', f'could not track {ev.contract.symbol}: {ev.reason}', key='track-failed')
        elif isinstance(ev, FeedStale):
            self._say('warning', f'price feed for {ev.contract.symbol} stale for {ev.age_sec:.0f}s', key='feed-stale')
        elif isinstance(ev, DplFrozen):
            self._say('critical' if ev.frozen else 'info',
                      f'{ev.contract.symbol} price {"FROZEN at the circuit limit" if ev.frozen else "unfroze"} at {ev.price:.2f}; '
                      f'ST_15/ATR may be distorted, no action taken automatically', key='dpl')

    # ------------------------------------------------------------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------------------------------------------------------------

    def _now(self) -> datetime:
        return self.ctx.now()

    def _save(self) -> None:
        self.ctx.save_state(self.state.to_json())

    def _say(self, level: str, text: str, key: Optional[str] = None, channel: Optional[str] = None,
            emoji: Optional[str] = None) -> None:
        """An alert; `key` debounces a repeating one (stuck exits and flips) to once per `realert_debounce_s`."""
        now = self._now()
        if key is not None:
            last = self._alerted.get(key)
            if last is not None and (now - last).total_seconds() < self.cfg.realert_debounce_s:
                log.warning('%s (debounced)', text)
                return
            self._alerted[key] = now
        log.log({'info': logging.INFO, 'warning': logging.WARNING}.get(level, logging.ERROR), text)
        self.ctx.alert(level, text, channel, emoji=emoji)

    def _rid(self, purpose: str) -> str:
        """A request id unique across days and attempts: session date, trade number, purpose, attempt."""
        trade = self.state.trade_counter + (1 if self.state.status == 'watching' else 0)
        key = f'{trade}:{purpose}'
        n = self.state.attempts.get(key, 0) + 1
        self.state.attempts[key] = n
        return f'{self.name}-{self.session_date:%Y%m%d}-t{trade}-{purpose}-{n}'

    def _send(self, request, context: dict) -> None:
        """Persist the request id and what it is for BEFORE sending, then send."""
        self.state.pending[request.request_id] = context
        self._save()
        ack = self.ctx.submit(request)
        if ack.status.value == 'invalid':
            self.state.pending.pop(request.request_id, None)
            self._save()
            self._say('critical', f'request {request.request_id} refused as invalid: {ack.detail}')
        elif ack.status.value == 'duplicate':
            log.info('request %s already known to Hestia: %s', request.request_id, ack.detail)

    def _busy(self) -> bool:
        return bool(self.state.pending)

    def _ref(self, token: Optional[str]) -> Optional[ContractRef]:
        info = self.infos.get(token) if token else None
        return None if info is None else info.ref

    def _lot_size(self, token: Optional[str] = None) -> int:
        info = self.infos.get(token or (self.contract.token if self.contract else ''))
        return info.lot_size if info else 1

    def _minutes_since_open(self, now: datetime) -> float:
        return (now - self.session_open).total_seconds() / 60.0

    def _past_first_minute_guard(self, now: datetime) -> bool:
        return self._minutes_since_open(now) >= self.cfg.no_exit_before_buffer_min

    def _past_min_entry_guard(self, now: datetime) -> bool:
        return self._minutes_since_open(now) >= self.cfg.min_entry_buffer_min

    def _roll_armed(self) -> bool:
        return self.state.roll_target is not None and self.state.roll_executed_date != f'{self.session_date}'

    def _entry_suppressed(self, now: datetime) -> bool:
        return rp.entry_suppressed(self._roll_armed(), now, self.rollover_at)

    def _reentry_allowed(self, now: datetime) -> bool:
        return self._past_min_entry_guard(now) and not self._entry_suppressed(now) and not self.flatten_done

    def _current_units(self) -> int:
        """Sizing, read live on every new entry: static units, or the engine's own dynamic rule over the account's free cash (capped
        by the allocation when one is set) and the live margin per unit. Falls back to the static figure when it cannot compute."""
        sz = self.ctx.sizing()
        if not sz.dynamic:
            return sz.static_units
        try:
            cash = self.ctx.margin().available_cash
            if sz.allocation_rs is not None:
                cash = min(cash, sz.allocation_rs)
            mpu = margin_per_unit(self._ltp_value(), self._lot_size(), self.cfg)
            return max(1, min(int(cash // mpu), sz.unit_cap))
        except Exception as exc:                                       # noqa: BLE001
            self._say('warning', f'dynamic sizing failed ({exc!r}); using the static units')
            return sz.static_units

    def _margin_sufficient(self, units: int) -> bool:
        mpu = margin_per_unit(self._ltp_value(), self._lot_size(), self.cfg)
        cash = self.ctx.margin().available_cash
        if cash < units * mpu:
            self._say('warning', f'insufficient margin (available Rs {cash:,.0f}, need Rs {units * mpu:,.0f}): entry skipped')
            return False
        return True

    def _ltp_value(self, ref: Optional[ContractRef] = None) -> Optional[float]:
        q = self.ctx.ltp(ref or self.contract)
        if q is None:
            return None
        return q.price if q.age_sec <= self.cfg.ltp_max_age_s else None

    # ------------------------------------------------------------------------------------------------------------------------
    # Start: contract, ledger reconciliation, pending requests, roll arming, missed flip
    # ------------------------------------------------------------------------------------------------------------------------

    def _on_session_start(self, ev: SessionStart) -> None:
        ctx = self.ctx
        self.session_date, self.session_open, self.rollover_at = ev.session_date, ev.session_open, ev.rollover_time
        self.session_close = ev.rollover_time + timedelta(minutes=rp.ROLLOVER_BEFORE_CLOSE_MIN)
        self.infos = {i.ref.token: i for i in ev.contracts}
        pairs = [(i.ref, i.trading_days_left) for i in ev.contracts]
        eff_today = rp.effective_from_days_left(pairs, self.cfg.roll_window_days)
        s = self.state
        # which contract do we trade? the position's, unless it is on the wrong one
        position_ref = self._ref(s.contract_token) if s.status == 'in_trade' else None
        target = eff_today.contract
        action = rp.RestartAction.NONE
        if s.status == 'in_trade' and s.contract_token:
            if position_ref is None:
                self._freeze(f'the open position is on token {s.contract_token}, which Hestia does not list')
                return
            expiry = date.fromisoformat(s.contract_expiry) if s.contract_expiry else None
            action = rp.restart_action(s.contract_token, expiry, eff_today.contract.token, eff_today.contract.expiry)
            if action in (rp.RestartAction.NONE, rp.RestartAction.CATCH_UP):
                target = position_ref
            elif action == rp.RestartAction.MISSED_ROLL:
                target = eff_today.contract
            else:
                self._freeze('the position looks like a roll-forward that could not be resolved; refusing to roll backwards')
                return
        elif s.roll_executed_date is not None and s.contract_token and s.roll_executed_date == f'{ev.session_date}':
            target = self._ref(s.contract_token) or target                # already switched earlier today
        self.contract = target
        ctx.set_trading_contract(target)
        self.started = True
        self._announce_session_start()
        self._reconcile_with_ledger()
        if self.state.frozen:
            return
        self._resume_pending()
        if action == rp.RestartAction.MISSED_ROLL and self.state.status == 'in_trade':
            self._say('critical', f'MISSED ROLLOVER: position is on {position_ref.symbol}, effective contract is {target.symbol}; '
                                  f'rolling now')
            self.state.roll_target = {'token': target.token, 'symbol': target.symbol, 'expiry': target.expiry.isoformat(),
                                      'flatten_only': False, 'immediate': True}
            self._execute_roll(self._now(), old_ref=position_ref, new_ref=target, missed=True)
        self._arm_roll(eff_today)
        self._reconcile_missed_flip()
        self._save()

    def _announce_session_start(self) -> None:
        """The standalone Prometheus process's own two-message session-open announcement (`_slack(f'... starting ...')` /
        `'... ST_15 seeded ...'`), ported back 2026-09-29 (found missing entirely on Hestia -- no engine said anything at
        all on a clean start, only on the critical paths). Same per-event emoji vocabulary the standalone always used
        (`_tag`/`_slack`), not Hestia's own severity-only default -- an explicit `emoji=` override on `_say`/`ctx.alert`
        (added the same day for this purpose)."""
        self._say('info', f'starting — trading {self.contract.symbol} (session {self.session_open:%H:%M}–'
                          f'{self.session_close:%H:%M})', channel='tradebot-updates', emoji='⚡')
        series = self.ctx.st_series(self.contract, 300)
        latest = series[-1][1] if series else None
        if latest is not None and latest.trend is not None:
            trend_str, st_str = _name(latest.trend), f'{latest.value:.2f}'
        else:
            trend_str, st_str = 'warmup', 'n/a (warmup)'
        self._say('info', f'ST_15 seeded ({len(series)} bars). Trend: {trend_str}. ST={st_str}.',
                  channel='tradebot-updates', emoji='✅')

    def _freeze(self, why: str) -> None:
        self.state.frozen, self.started = True, True
        self._say('critical', f'engine frozen, needs the operator: {why}')
        self._save()

    def _reconcile_with_ledger(self) -> None:
        """Hestia's ledger is the truth about what is held. Where the engine's own belief differs, the ledger wins and it is told."""
        s = self.state
        ref = self._ref(s.contract_token) or self.contract
        pos = self.ctx.position(ref)
        held = pos.net_lots
        expected = ((1 if s.direction == BULL else -1) * s.open_lots()) if s.status == 'in_trade' else 0
        if held == expected:
            return
        if s.status == 'in_trade' and held == 0:
            self._say('critical', f'the engine believed it held {expected:+d} lots of {ref.symbol} but the ledger is flat: '
                                  f'returning to watching (no trade record is written for it)')
            self._reset_position()
        elif s.status == 'in_trade':
            self._say('critical', f'the engine believed {expected:+d} lots of {ref.symbol}, the ledger shows {held:+d}: '
                                  f'taking the ledger')
            self._rebuild_lots(abs(held), BULL if held > 0 else BEAR)
        else:
            self._say('critical', f'the ledger holds {held:+d} lots of {ref.symbol} but the engine believed it flat: adopting the '
                                  f'position with levels computed from its average price')
            direction = BULL if held > 0 else BEAR
            lots = abs(held)
            price = pos.avg_price or self._ltp_value(ref) or 0.0
            units = max(1, lots // (2 * self.cfg.lots_per_leg))
            s.contract_token, s.contract_symbol, s.contract_expiry = ref.token, ref.symbol, ref.expiry.isoformat()
            self._install_position(direction, units, price, lots, signal_ts=None, signal_close=None, adopted=True)
        self._save()

    def _rebuild_lots(self, lots: int, direction: str) -> None:
        s = self.state
        s.direction = direction
        units = s.units or max(1, lots // (2 * self.cfg.lots_per_leg))
        lot1 = min(lots, units * self.cfg.lots_per_leg)
        s.lot1_lots, s.lot2_lots = lot1, lots - lot1
        s.lot1_status = 'open' if lot1 else 'never_opened'
        s.lot2_status = 'open' if lots - lot1 else 'never_opened'

    def _resume_pending(self) -> None:
        """A request sent before the previous stop: ask Hestia what became of it. Never re-send under a new id blindly."""
        for rid, meta in list(self.state.pending.items()):
            st = self.ctx.request_status(rid)
            if st is None:                                           # never reached Hestia: forget it, the tick will decide again
                self.state.pending.pop(rid)
                self._say('warning', f'request {rid} ({meta.get("purpose")}) was never sent before the restart; it will be re-decided')
            elif st.status == OutcomeStatus.IN_FLIGHT:
                log.info('request %s still in flight; waiting for its outcome', rid)
            elif st.status == OutcomeStatus.UNCONFIRMED:
                log.info('request %s unconfirmed; Hestia is settling it', rid)
            else:
                self._on_outcome(st)

    def _arm_roll(self, eff_today: rp.Effective) -> None:
        """Checked once at start, well before the rollover time: does tomorrow's trading day resolve to a different contract?"""
        if self._roll_armed() or self.state.roll_executed_date == f'{self.session_date}':
            return
        pairs = [(i.ref, i.trading_days_left) for i in self.infos.values()]
        tomorrow = rp.effective_from_days_left(pairs, self.cfg.roll_window_days, days_offset=1)
        if tomorrow.no_next:
            if self.state.status == 'in_trade':
                self.state.roll_target = {'token': None, 'symbol': None, 'expiry': None, 'flatten_only': True}
                self._say('critical', 'inside the roll window with no next contract listed: the position will be FLATTENED at the '
                                      'rollover time, not carried into the tender window')
            return
        if tomorrow.contract.expiry <= self.contract.expiry:               # already on it (or on a later one after a catch-up)
            return
        new = tomorrow.contract
        if self.state.status == 'in_trade':
            self.state.roll_target = {'token': new.token, 'symbol': new.symbol, 'expiry': new.expiry.isoformat(), 'flatten_only': False}
            self._say('info', f'rolling to {new.symbol} tonight at {self.rollover_at:%H:%M}; tracking it all day', channel='tradebot-updates')
            self.ctx.track(new)
        else:
            self._switch_contract(new, 'flat on a roll eve: switched at once, nothing to protect')

    def _switch_contract(self, new: ContractRef, why: str) -> None:
        self.contract = new
        self.ctx.set_trading_contract(new)
        self.state.roll_executed_date = f'{self.session_date}'
        self.state.roll_target = None
        self.state.contract_token = new.token if self.state.status == 'in_trade' else self.state.contract_token
        self._say('info', f'now trading {new.symbol}: {why}', channel='tradebot-updates')
        self._save()

    # ------------------------------------------------------------------------------------------------------------------------
    # The 15-minute bar
    # ------------------------------------------------------------------------------------------------------------------------

    def _on_bar(self, ev: BarComplete) -> None:
        if self.state.frozen:
            return
        if self._at_or_after_close(ev.boundary_ts):
            return
        if ev.quality == BarQuality.GAP or ev.bar is None or ev.st is None:
            self._say('critical', f'no data for the 15-minute window ending {ev.boundary_ts:%H:%M}: the series has a gap', key='gap')
            return
        if ev.st.trend is None:                                      # supertrend still warming up
            return
        window_start = ev.bar.ts
        direction_now = _name(ev.st.trend)
        # a boundary already acted on provisionally: this is the reconciliation, never a second action
        if self.provisional_pending is not None and self.provisional_pending['boundary'] == ev.boundary_ts.isoformat():
            self._reconcile_provisional(direction_now, ev)
            self.state.last_processed_boundary = window_start.isoformat()
            self._save()
            return
        if ev.st.flip:
            # the raw signal itself, independent of what the engine goes on to decide -- production's own per-bar Slack
            # line (prometheus.py: "ST_15 flip -> *direction*"), ported here after being missed in the original build.
            self._say('info', f'ST_15 flip -> {direction_now} at {window_start:%H:%M} (close={ev.bar.close:.2f}, '
                              f'ST={ev.st.value:.2f})', channel='tradebot-updates')
        self.state.last_processed_boundary = window_start.isoformat()
        self._save()
        self._act_on_signal(direction_now, ev.st.flip, window_start, ev.bar.close, provisional=False)

    def _at_or_after_close(self, boundary: datetime) -> bool:
        """The last boundary of a session is the close itself: that bar completes after the market is shut, so it can never be traded
        (production refused orders after the closing time). It is left unprocessed and the watermark untouched, so the next session's
        missed-flip reconcile handles it, as it did live on 2026-09-23."""
        if self.session_close is not None and boundary >= self.session_close:
            log.info('bar at boundary %s is at or after the close %s: not acted on, watermark unchanged', boundary, self.session_close)
            return True
        return False

    def _act_on_signal(self, direction_now: str, flip: bool, window_start: datetime, close: float, provisional: bool) -> bool:
        """The shared branching of a real bar and a provisional one. Returns True if it acted."""
        s = self.state
        now = self._now()
        if s.status == 'in_trade':
            if flip and direction_now != s.direction:
                if self._busy() or s.pending_flip is not None:
                    log.warning('flip to %s at %s ignored: a request or flip is still resolving', direction_now, window_start)
                    return False
                if self._roll_armed() and self.state.roll_target.get('immediate'):
                    log.warning('flip to %s at %s ignored: an immediate (missed) roll owns the transition', direction_now, window_start)
                    return False
                if self._roll_armed() and rp.before_rollover(now, self.rollover_at) and not self.state.roll_target.get('flatten_only'):
                    self._coincident_flip_transition(direction_now, window_start, close)
                else:
                    self._start_flip(direction_now, window_start, close)
                return True
            return False
        if flip and not self._busy() and self._reentry_allowed(now):
            self._send_entry(direction_now, window_start, close)
            return True
        return False

    # ------------------------------------------------------------------------------------------------------------------------
    # Provisional-boundary trading
    # ------------------------------------------------------------------------------------------------------------------------

    def _on_provisional(self, ev: ProvisionalBar) -> None:
        cfg = self.cfg
        if self.state.frozen or ev.st.trend is None or ev.st.value is None or self._at_or_after_close(ev.boundary_ts):
            return
        if ev.prev_st is None:
            log.warning('provisional boundary %s: the previous bar has no supertrend (warm-up); skipped', ev.boundary_ts)
            return
        if self._roll_armed() or self.state.pending_flip is not None:
            log.info('provisional boundary %s skipped: a roll is pending or a flip is mid-transition', ev.boundary_ts)
            return
        close = ev.bar.close
        clear_pct = abs(close - ev.prev_st) / close * 100 if close else 0.0
        clears = clear_pct > cfg.provisional_margin_pct
        direction = _name(ev.st.trend)
        log.info('provisional boundary %s: close=%.2f ST=%.2f prev_ST=%.2f direction=%s flip=%s clear_prev_st_pct=%.3f (margin=%s) '
                 'clears=%s enabled=%s', ev.bar.ts, close, ev.st.value, ev.prev_st, direction, ev.st.flip, clear_pct,
                 cfg.provisional_margin_pct, clears, cfg.provisional_enabled)
        if not cfg.provisional_enabled or not clears or not ev.st.flip:
            return
        if self.provisional_disabled:
            log.warning('provisional action skipped: disabled for the rest of this session after a disagreement')
            return
        pre = (self.state.status, self.state.direction)
        self._say('warning', f'PROVISIONAL flip -> {direction} at {ev.bar.ts:%H:%M} (candle data incomplete, acting on a tick-built bar; '
                             f'cleared the previous ST by {clear_pct:.3f}%). Will reconcile against the real bar.',
                  channel='tradebot-updates')
        if self._act_on_signal(direction, True, ev.bar.ts, close, provisional=True):
            self.provisional_pending = {'boundary': ev.boundary_ts.isoformat(), 'direction': direction, 'window_start': ev.bar.ts,
                                        'pre': pre}

    def _reconcile_provisional(self, real_direction: str, ev: BarComplete) -> None:
        pending = self.provisional_pending
        self.provisional_pending = None
        if real_direction == pending['direction']:
            self._say('info', f'provisional {real_direction} flip at {pending["window_start"]:%H:%M} CONFIRMED by the real bar',
                      channel='tradebot-updates')
            return
        self.provisional_disabled = True
        self._say('critical', f'provisional {pending["direction"]} flip at {pending["window_start"]:%H:%M} DISAGREES with the real bar '
                              f'({real_direction}). The position may be WRONG: review manually. No automated reversal; provisional '
                              f'trading is off for the rest of this session.')
        if self.state.pending_flip is not None:
            self.state.pending_flip = None
            self._say('critical', 'abandoned an in-progress flip because of the provisional disagreement: check the broker terminal now')

    # ------------------------------------------------------------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------------------------------------------------------------

    def _send_entry(self, direction: str, signal_ts: datetime, signal_close: float) -> None:
        if not self._ltp_value():
            self._say('critical', 'no price available: cannot enter')
            return
        units = self._current_units()
        if not self._margin_sufficient(units):
            return
        lots = units * self.cfg.lots_per_leg * 2
        trade_ref = self.state.trade_counter + 1
        req = OpenRequest(self._rid('entry'), self.contract, _dir(direction), lots, trade_ref=trade_ref)
        self._send(req, {'purpose': 'entry', 'direction': direction, 'signal_ts': signal_ts.isoformat(), 'signal_close': signal_close,
                         'units': units, 'lots': lots, 'token': self.contract.token})

    # ------------------------------------------------------------------------------------------------------------------------
    # Rule 7: a flip is one netted order
    # ------------------------------------------------------------------------------------------------------------------------

    def _start_flip(self, direction_now: str, signal_ts: datetime, signal_close: float) -> None:
        old_open = self.state.open_lots()
        allowed = self._reentry_allowed(self._now())
        units = self._current_units()
        new_lots = units * self.cfg.lots_per_leg * 2 if allowed else 0
        if new_lots > 0 and not self._margin_sufficient(units):
            new_lots = 0                                             # cannot afford the new leg: still close the old one
        self.state.pending_flip = {'direction': direction_now, 'signal_ts': signal_ts.isoformat(), 'signal_close': signal_close,
                                   'units': units, 'new_lots': new_lots}
        self._save()
        if old_open == 0:                                            # nothing left to close: it is a plain entry (or nothing)
            self.state.pending_flip = None
            if new_lots:
                self._send_entry(direction_now, signal_ts, signal_close)
            return
        self._send_flip()

    def _send_flip(self) -> None:
        pf, s = self.state.pending_flip, self.state
        ref = self._ref(s.contract_token) or self.contract
        old_open = s.open_lots()
        trade_ref = s.trade_counter + 1
        if pf['new_lots'] <= 0:
            req = CloseRequest(self._rid('flip_exit'), ref, _dir(s.direction), ExitReason.TREND_FLIP, trade_ref=s.trade_counter)
            purpose = 'flip_exit'
        else:
            req = FlipRequest(self._rid('flip'), ref, _dir(s.direction), old_open, pf['new_lots'], trade_ref=trade_ref)
            purpose = 'flip'
        self._send(req, {'purpose': purpose, 'direction': pf['direction'], 'signal_ts': pf['signal_ts'],
                         'signal_close': pf['signal_close'], 'units': pf['units'], 'lots': pf['new_lots'], 'token': ref.token})

    # ------------------------------------------------------------------------------------------------------------------------
    # LTP-driven exits: stop, target 1, target 2 (the stop wins any same-tick tie)
    # ------------------------------------------------------------------------------------------------------------------------

    def _tick(self) -> None:
        if self.state.frozen or self.ended:
            return
        now = self._now()
        if self.market_closed or (self.session_close is not None and now >= self.session_close):
            return                                                    # nothing can fill after the close; Stop arrives momentarily
        s = self.state
        if s.status == 'in_trade':
            self._maybe_send_trade_update(now)
            self._maybe_send_running_row(now)
        if self.next_retry_at is not None and now < self.next_retry_at:
            return
        if self._busy():
            return
        if self.exit_requested and s.status == 'in_trade':
            self._send_manual_exit()
            return
        if s.pending_flip is not None:
            if s.status == 'in_trade':
                self._send_flip()
            else:
                s.pending_flip = None
                self._save()
            return
        self._retry_missed_flip(now)
        self._check_roll_timing(now)
        if not self._busy() and s.status == 'in_trade':
            self._check_exit_conditions(now)

    def _running_row(self, ltp: float, exit_reason: Optional[str] = None) -> dict:
        """One row of the per-trade running log (production's own `_append_running_row`): each lot's P&L at `ltp` -- its real
        exit price if already booked, `ltp` otherwise (so a lot still open gets marked to the SAME price the exiting lot just
        filled at, on an exit-time row, exactly as production's own call does by passing the fill price as `ltp`)."""
        s = self.state
        lot_size = self._lot_size(s.contract_token)

        def lot_pnl(lot: int) -> Tuple[float, float]:
            status = s.lot1_status if lot == 1 else s.lot2_status
            exit_price = s.lot1_exit_price if lot == 1 else s.lot2_exit_price
            lots = s.lot1_lots if lot == 1 else s.lot2_lots
            if not lots:
                return 0.0, 0.0
            price = exit_price if (status == 'booked' and exit_price is not None) else ltp
            pts = lot_pnl_points(s.direction, s.entry_price, price)
            return round(pts, 2), round(pts * lots * lot_size, 2)

        lot1_pts, lot1_rs = lot_pnl(1)
        lot2_pts, lot2_rs = lot_pnl(2)
        entry_ts = datetime.fromisoformat(s.entry_ts) if s.entry_ts else self._now()
        now = self._now()
        return {'trade_id': s.trade_counter, 'entry_ts': s.entry_ts, 'ts': now.isoformat(),
               'minutes_since_entry': int((now - entry_ts).total_seconds() // 60), 'ltp': ltp, 'sl_price': s.sl_price,
               'lot1_target': s.lot1_target, 'lot2_target': s.lot2_target, 'lot1_pnl_points': lot1_pts, 'lot1_pnl_rs': lot1_rs,
               'lot2_pnl_points': lot2_pts, 'lot2_pnl_rs': lot2_rs, 'total_pnl_points': round(lot1_pts + lot2_pts, 2),
               'total_pnl_rs': round(lot1_rs + lot2_rs, 2), 'exit_reason': exit_reason}

    def _maybe_send_running_row(self, now: datetime) -> None:
        if (self._last_running_row is not None
                and (now - self._last_running_row).total_seconds() < self.cfg.running_row_sec):
            return
        self._last_running_row = now
        ltp = self._ltp_value(self._ref(self.state.contract_token) or self.contract)
        if not ltp:
            return
        self.ctx.report_running_row(self._running_row(ltp))

    def _maybe_send_trade_update(self, now: datetime) -> None:
        """The periodic in-trade P&L ticker to #trade-updates (production's `_send_trade_update`, every `TRADE_UPDATE_SEC`,
        Slack-only, never logged). Ported 2026-09-29 after being missed in the original build -- the user flagged its
        absence live. Fires regardless of a pending request or the retry cooldown: it is read-only and never itself
        touches a request, so nothing about the request lifecycle should gate it."""
        if self._last_trade_update is not None and (now - self._last_trade_update).total_seconds() < self.cfg.trade_update_sec:
            return
        self._last_trade_update = now
        s = self.state
        ref = self._ref(s.contract_token) or self.contract
        ltp = self._ltp_value(ref) or 0.0
        pnl = self._compute_trade_pnl(ltp)
        units = s.units or 1
        msg = (f'{s.direction.upper()}  {ref.symbol if ref else s.contract_symbol}  Units: {units}\n'
              f'Entry: {s.entry_price or 0:.2f}  LTP: {ltp:.2f}\n'
              f'Realised: {pnl["realised_pts"]:+.2f} pts (Rs.{pnl["realised_rs"] / units:+,.0f}/unit)  '
              f'Unrealised: {pnl["unrealised_pts"]:+.2f} pts (Rs.{pnl["unrealised_rs"] / units:+,.0f}/unit)  '
              f'Total: Rs.{(pnl["realised_rs"] + pnl["unrealised_rs"]) / units:+,.0f}/unit')
        self.ctx.alert('info', msg, channel='trade-updates')

    def _compute_trade_pnl(self, ltp: Optional[float]) -> dict:
        """Realised (booked lots) plus unrealised (still-open lots at `ltp`) P&L in points and rupees, per production's own
        `_compute_trade_pnl` shape (lot1/lot2, not per-unit -- the caller divides by units for display)."""
        s = self.state
        lot_size = self._lot_size(s.contract_token)

        def realised(lot: int) -> Tuple[float, float]:
            status = s.lot1_status if lot == 1 else s.lot2_status
            price = s.lot1_exit_price if lot == 1 else s.lot2_exit_price
            lots = s.lot1_lots if lot == 1 else s.lot2_lots
            if status != 'booked' or price is None or not lots:
                return 0.0, 0.0
            pts = lot_pnl_points(s.direction, s.entry_price, price)
            return pts, pts * lots * lot_size

        def unrealised(lot: int) -> Tuple[float, float]:
            status = s.lot1_status if lot == 1 else s.lot2_status
            lots = s.lot1_lots if lot == 1 else s.lot2_lots
            if status != 'open' or not lots or not ltp:
                return 0.0, 0.0
            pts = lot_pnl_points(s.direction, s.entry_price, ltp)
            return pts, pts * lots * lot_size

        r1_pts, r1_rs = realised(1)
        r2_pts, r2_rs = realised(2)
        u1_pts, u1_rs = unrealised(1)
        u2_pts, u2_rs = unrealised(2)
        return {'realised_pts': round(r1_pts + r2_pts, 2), 'realised_rs': round(r1_rs + r2_rs, 2),
               'unrealised_pts': round(u1_pts + u2_pts, 2), 'unrealised_rs': round(u1_rs + u2_rs, 2)}

    def _check_exit_conditions(self, now: datetime) -> None:
        if not self._past_first_minute_guard(now):
            return
        s = self.state
        ref = self._ref(s.contract_token) or self.contract
        ltp = self._ltp_value(ref)
        if not ltp:
            return
        self.last_ltp = ltp
        bull = s.direction == BULL
        if s.sl_price is not None and ((ltp <= s.sl_price) if bull else (ltp >= s.sl_price)):
            self._send_exit_all('stop_loss', ExitReason.STOP_LOSS)
            return
        if s.lot1_status == 'open' and s.lot1_target is not None and ((ltp >= s.lot1_target) if bull else (ltp <= s.lot1_target)):
            self._send_exit_lot(1, 'target1')
            return
        if s.lot2_status == 'open' and ((ltp >= s.lot2_target) if bull else (ltp <= s.lot2_target)):
            self._send_exit_lot(2, f'target2_{s.lot2_source}')

    def _send_exit_all(self, reason: str, why: ExitReason, purpose: Optional[str] = None) -> None:
        s = self.state
        ref = self._ref(s.contract_token) or self.contract
        req = FlattenRequest(self._rid(purpose or 'exit_all'), ref, why, trade_ref=s.trade_counter)
        self._send(req, {'purpose': purpose or 'exit_all', 'reason': reason, 'token': ref.token})

    def _send_exit_lot(self, lot: int, reason: str) -> None:
        s = self.state
        ref = self._ref(s.contract_token) or self.contract
        lots = s.lot1_lots if lot == 1 else s.lot2_lots
        req = CloseRequest(self._rid(f'lot{lot}'), ref, _dir(s.direction), ExitReason.OTHER, lots=lots, trade_ref=s.trade_counter)
        self._send(req, {'purpose': f'lot{lot}', 'reason': reason, 'lot': lot, 'token': ref.token})

    # ------------------------------------------------------------------------------------------------------------------------
    # The operator's EXIT: liquidate and re-arm, do not end the session
    # ------------------------------------------------------------------------------------------------------------------------

    def _on_exit_command(self) -> None:
        s = self.state
        if s.status != 'in_trade':
            self._say('info', '`Exit Trade` received but there is no open position; still watching', channel='trade-alerts')
            return
        self._say('critical', '`Exit Trade` received: liquidating', channel='trade-alerts')
        if s.pending_flip is not None:
            s.pending_flip = None
            self._say('critical', 'a pending flip was abandoned because of the exit command (re-entry not completed, by design)')
        self.exit_requested = True
        self._save()

    def _send_manual_exit(self) -> None:
        self._send_exit_all('slack_exit', ExitReason.MANUAL_EXIT, purpose='manual_exit')

    # ------------------------------------------------------------------------------------------------------------------------
    # Outcomes: the only place position state changes
    # ------------------------------------------------------------------------------------------------------------------------

    def _on_outcome(self, o: RequestOutcome) -> None:
        meta = self.state.pending.get(o.request_id)
        if meta is None:
            log.info('outcome for %s which is not pending here (already applied or from another life): %s', o.request_id, o.status)
            return
        if o.status == OutcomeStatus.UNCONFIRMED:
            self._say('critical', f'{meta["purpose"]} request {o.request_id}: fill not confirmed, position status unknown; waiting for '
                                  f'Hestia to settle it', key=f'unconfirmed-{o.request_id}')
            return
        self.state.pending.pop(o.request_id, None)
        purpose = meta['purpose']
        handler = {'entry': self._done_entry, 'flip': self._done_flip, 'flip_exit': self._done_flip_exit, 'lot1': self._done_lot,
                   'lot2': self._done_lot, 'exit_all': self._done_exit_all, 'stop_loss': self._done_exit_all,
                   'manual_exit': self._done_manual_exit, 'roll_close': self._done_roll_close, 'roll_open': self._done_roll_open,
                   'coin_close': self._done_coin_close, 'coin_open': self._done_coin_open}.get(purpose)
        if handler is None:
            self._say('critical', f'outcome for an unknown purpose {purpose!r}')
        else:
            handler(o, meta)
        self._save()

    def _failed(self, o: RequestOutcome, meta: dict, what: str) -> None:
        if o.status == OutcomeStatus.LIMIT_REFUSED and 'market closed' in o.detail:
            # not transient: the market is shut. No retry; the position is carried to the next session and the start-up
            # reconcile deals with it (a stop still stands at its level, checked again once trading resumes)
            self.market_closed = True
            self._say('critical', f'{what} refused, the market is closed ({o.detail}); position carried to the next session',
                      key=f'closed-{meta["purpose"]}')
            return
        self._say('critical', f'{what} failed ({o.status.value}: {o.detail}); will retry', key=f'fail-{meta["purpose"]}')
        if o.status == OutcomeStatus.REJECTED:                        # the ledger may disagree with the engine: it wins
            self._reconcile_with_ledger()
        self.next_retry_at = self._now() + timedelta(seconds=self.cfg.retry_cooldown_s)

    def _already_flat(self, o: RequestOutcome) -> bool:
        return o.status == OutcomeStatus.FILLED and o.closed is not None and o.closed.lots == 0 and 'already flat' in o.detail

    def _done_entry(self, o: RequestOutcome, meta: dict) -> None:
        if not o.confirmed or o.opened is None or o.opened.lots <= 0:
            self._say('critical', f'entry order did not fill ({o.status.value}: {o.detail}): no position opened')
            return
        if o.status == OutcomeStatus.PARTIAL:
            self._say('warning', f'partial fill: requested {meta["lots"]} lots, got {o.opened.lots}', channel='trade-alerts')
        self._open_position(meta['direction'], meta['signal_ts'], meta['signal_close'], o.opened.avg_price, o.opened.lots, meta['lots'],
                            meta['units'], self._ref(meta['token']))

    def _done_flip(self, o: RequestOutcome, meta: dict) -> None:
        s = self.state
        if not o.confirmed or o.closed is None or o.closed.lots <= 0:
            if self._already_flat(o):
                self._reconcile_with_ledger()
                s.pending_flip = None
                return
            if (o.status in (OutcomeStatus.LIMIT_REFUSED, OutcomeStatus.MARGIN_REFUSED) and s.pending_flip
                    and s.pending_flip['new_lots'] > 0 and 'market closed' not in o.detail):
                # the new side is refused (unit cap, roll window, margin): the old side must still close, so retry as an exit only
                s.pending_flip['new_lots'] = 0
                self._say('critical', f'the flip re-entry was refused ({o.detail}); closing the old side only')
                self.next_retry_at = self._now() + timedelta(seconds=self.cfg.retry_cooldown_s)
                return
            self._failed(o, meta, 'the Rule 7 flip')
            return
        old_open_before = s.open_lots()
        self._apply_closed(o.closed.lots, o.closed.avg_price, 'trend_flip', order=(2, 1))
        if s.open_lots() > 0:                                        # part of the old side still open: keep the flip, re-send the rest
            self._say('critical', f'{s.open_lots()} old lot(s) still open after the flip order; retrying', key='flip-remainder')
            self.next_retry_at = self._now() + timedelta(seconds=self.cfg.retry_cooldown_s)
            return
        pf, s.pending_flip = s.pending_flip, None
        if o.opened is not None and o.opened.lots > 0:
            if o.opened.lots < meta['lots']:
                self._say('warning', f'flip re-entry partial: requested {meta["lots"]} lots, got {o.opened.lots}', channel='trade-alerts')
            self._open_position(meta['direction'], meta['signal_ts'], meta['signal_close'], o.opened.avg_price, o.opened.lots,
                                meta['lots'], meta['units'], self._ref(meta['token']))
        else:
            self._say('critical', 'the old side closed but the re-entry did not fill: now flat, watching for the next signal')
        log.info('Rule 7 pending flip fully resolved.')

    def _done_flip_exit(self, o: RequestOutcome, meta: dict) -> None:
        s = self.state
        if not o.confirmed or o.closed is None or o.closed.lots <= 0:
            if self._already_flat(o):
                self._reconcile_with_ledger()
                s.pending_flip = None
                return
            self._failed(o, meta, 'the flip exit')
            return
        self._apply_closed(o.closed.lots, o.closed.avg_price, 'trend_flip', order=(1, 2))
        if s.open_lots() == 0:
            s.pending_flip = None

    def _done_lot(self, o: RequestOutcome, meta: dict) -> None:
        if not o.confirmed or o.closed is None or o.closed.lots <= 0:
            if self._already_flat(o):
                self._reconcile_with_ledger()
                return
            self._failed(o, meta, f'lot{meta["lot"]} exit ({meta["reason"]})')
            return
        self._apply_lot_exit(meta['lot'], o.closed.avg_price, o.closed.lots, meta['reason'])

    def _done_exit_all(self, o: RequestOutcome, meta: dict) -> None:
        if not o.confirmed or o.closed is None or o.closed.lots <= 0:
            if self._already_flat(o):
                self._reconcile_with_ledger()
                return
            self._failed(o, meta, f'exit ({meta["reason"]})')
            return
        self._apply_closed(o.closed.lots, o.closed.avg_price, meta['reason'], order=(1, 2))

    def _done_manual_exit(self, o: RequestOutcome, meta: dict) -> None:
        if not o.confirmed or o.closed is None or o.closed.lots <= 0:
            if self._already_flat(o):
                self._reconcile_with_ledger()
                self.exit_requested = False
                return
            self._failed(o, meta, 'the manual exit')
            return
        self._apply_closed(o.closed.lots, o.closed.avg_price, 'slack_exit', order=(1, 2))
        if self.state.status != 'in_trade':
            self.exit_requested = False
            self._say('info', 'position liquidated; still watching for the next entry', channel='trade-alerts')

    # ------------------------------------------------------------------------------------------------------------------------
    # Position bookkeeping (a port of _finalize_new_position, _apply_confirmed_lot_exit and _finalize_trade)
    # ------------------------------------------------------------------------------------------------------------------------

    def _open_position(self, direction: str, signal_ts: Optional[str], signal_close: Optional[float], entry_price: float, filled_lots: int,
                       requested_lots: int, units: int, ref: ContractRef, basis: Optional[float] = None,
                       parent_trade_id: Optional[int] = None, lot2_only: bool = False) -> None:
        s = self.state
        s.trade_counter += 1
        s.contract_token, s.contract_symbol, s.contract_expiry = ref.token, ref.symbol, ref.expiry.isoformat()
        self._install_position(direction, units, entry_price, filled_lots, signal_ts, signal_close, basis=basis, lot2_only=lot2_only,
                               parent_trade_id=parent_trade_id)
        sl = f'{s.sl_price:.2f}' if s.sl_price is not None else 'n/a'
        lot1 = f'{s.lot1_target:.2f}' if s.lot1_target is not None else 'n/a (lot2-only)'
        self._say('info', f'Entered {direction.upper()}{" (rollover)" if parent_trade_id is not None else ""}  {ref.symbol} | Units: {units} '
                          f'({filled_lots} lots)  Entry: {entry_price:.2f}'
                          f'{f" (recalibration basis {basis:.2f})" if basis is not None else ""} | SL: {sl}  Lot1 target: {lot1} | '
                          f'Lot2 target: {s.lot2_target:.2f} ({s.lot2_source})', channel='trade-alerts')
        if filled_lots < requested_lots:
            log.warning('Partial fill: requested %d lots, filled %d', requested_lots, filled_lots)

    def _install_position(self, direction: str, units: int, entry_price: float, lots: int, signal_ts: Optional[str],
                          signal_close: Optional[float], basis: Optional[float] = None, lot2_only: bool = False,
                          parent_trade_id: Optional[int] = None, adopted: bool = False) -> None:
        s = self.state
        threshold = basis if basis is not None else entry_price
        lv = build_levels(direction, threshold, lots, units, self.cfg, lot2_only)
        now = self._now().isoformat()
        if adopted:
            s.trade_counter += 1
        s.status, s.direction, s.units = 'in_trade', direction, units
        s.entry_price, s.basis_price, s.entry_ts = entry_price, basis, now
        s.signal_ts, s.signal_close = signal_ts, signal_close
        s.sl_price, s.lot1_target, s.lot2_target, s.lot2_source = lv.sl_price, lv.lot1_target, lv.lot2_target, lv.lot2_source
        s.lot1_lots, s.lot2_lots = lv.lot1_lots, lv.lot2_lots
        s.lot1_status = 'open' if lv.lot1_lots > 0 else 'never_opened'
        s.lot2_status = 'open' if lv.lot2_lots > 0 else 'never_opened'
        s.lot1_exit_price = s.lot2_exit_price = None
        slippage = None if signal_close is None else round((entry_price - signal_close) if direction == BULL else (signal_close - entry_price), 2)
        s.trade_row = {'trade_id': s.trade_counter, 'contract_expiry': s.contract_expiry,
                       'direction': f'{direction}-rollover' if parent_trade_id is not None else direction, 'units': units,
                       'entry_ts': now, 'entry_price': entry_price, 'signal_ts': signal_ts, 'signal_close': signal_close,
                       'entry_slippage_points': slippage, 'sl_price': None if lv.sl_price is None else round(lv.sl_price, 2),
                       'lot1_target': None if lv.lot1_target is None else round(lv.lot1_target, 2),
                       'lot2_target': round(lv.lot2_target, 2), 'lot2_target_source': lv.lot2_source, 'parent_trade_id': parent_trade_id}

    def _apply_closed(self, lots: int, price: float, reason: str, order: Tuple[int, int]) -> None:
        """Book `lots` closed lots at `price` against the open lots, in the given lot order (the flip closes lot2 first, keeping lot1's
        nearer target alive on a favourable reversal; an exit-all closes lot1 first)."""
        s = self.state
        remaining = lots
        for lot in order:
            if remaining <= 0:
                break
            status = s.lot1_status if lot == 1 else s.lot2_status
            have = s.lot1_lots if lot == 1 else s.lot2_lots
            if status != 'open' or not have:
                continue
            take = min(remaining, have)
            self._apply_lot_exit(lot, price, take, reason, finalize=False)
            remaining -= take
        if s.status == 'in_trade' and s.lot1_status != 'open' and s.lot2_status != 'open':
            self._finalize_trade()

    def _apply_lot_exit(self, lot: int, price: float, lots: int, reason: str, finalize: bool = True) -> None:
        s = self.state
        pts = lot_pnl_points(s.direction, s.entry_price, price)
        rs = round(pts * lots * self._lot_size(s.contract_token), 2)
        now = self._now().isoformat()
        if lot == 1:
            s.lot1_status, s.lot1_exit_price = 'booked', round(price, 2)
        else:
            s.lot2_status, s.lot2_exit_price = 'booked', round(price, 2)
        s.trade_row.update({f'lot{lot}_exit_ts': now, f'lot{lot}_exit_price': round(price, 2), f'lot{lot}_exit_reason': reason,
                            f'lot{lot}_pnl_points': round(pts, 2), f'lot{lot}_pnl_rs': rs})
        self.ctx.report_running_row(self._running_row(price, exit_reason=f'lot{lot}_{reason}'))
        per_unit = rs / (s.units or 1)
        self._say('info', f'Lot{lot} exit: {reason}  (Units: {s.units})  Entry {s.entry_price:.2f} -> Exit {price:.2f} | P&L: {pts:+.2f} pts '
                          f' Rs.{per_unit:+,.0f}/unit', channel='trade-alerts')
        if finalize and s.lot1_status != 'open' and s.lot2_status != 'open':
            self._finalize_trade()

    def _finalize_trade(self) -> None:
        s = self.state
        row = dict(s.trade_row)
        lot1_pts, lot2_pts = row.get('lot1_pnl_points') or 0, row.get('lot2_pnl_points') or 0
        lot1_rs, lot2_rs = row.get('lot1_pnl_rs') or 0, row.get('lot2_pnl_rs') or 0
        row['total_pnl_points'], row['total_pnl_rs'] = round(lot1_pts + lot2_pts, 2), round(lot1_rs + lot2_rs, 2)
        self.ctx.report_trade(row)
        units = row.get('units') or s.units or 1
        self._say('info', f'Trade #{row.get("trade_id")} closed.  (Units: {units})  Total P&L: {row["total_pnl_points"]:+.2f} pts  '
                          f'Rs.{row["total_pnl_rs"] / units:+,.0f}/unit', channel='trade-alerts')
        self._reset_position()

    def _reset_position(self) -> None:
        s = self.state
        keep = dict(trade_counter=s.trade_counter, last_processed_boundary=s.last_processed_boundary, pending=s.pending,
                    pending_flip=s.pending_flip, pending_missed_flip=s.pending_missed_flip, roll_target=s.roll_target,
                    roll_executed_date=s.roll_executed_date, attempts=s.attempts, contract_token=s.contract_token,
                    contract_symbol=s.contract_symbol, contract_expiry=s.contract_expiry)
        # in place: callers hold `s = self.state` across a finalize (a flip books the old trade, then opens the new one)
        s.__dict__.update(EngineState(status='watching', **keep).__dict__)

    # ------------------------------------------------------------------------------------------------------------------------
    # Missed flip: a flip in a bar the previous session ended before reaching
    # ------------------------------------------------------------------------------------------------------------------------

    def _reconcile_missed_flip(self) -> None:
        s = self.state
        series = self.ctx.st_series(self.contract, 300)
        if not series:
            return
        if s.last_processed_boundary is None:                        # no watermark yet: baseline off the last bar, do not act on history
            last_bar, last_st = series[-1]
            if last_st.trend is not None:
                s.last_processed_boundary = last_bar.ts.isoformat()
            return
        mark = datetime.fromisoformat(s.last_processed_boundary)
        flips = [(b, st) for b, st in series if b.ts > mark and st.flip and st.trend is not None]
        if not flips:
            return
        bar, st = flips[-1]                                          # coalesce to the latest unprocessed flip
        direction_now = _name(st.trend)
        self._say('critical', f'Missed flip detected: ST_15 flipped -> {direction_now} at {bar.ts}: the session ended before this bar went '
                              f'live. Reconciling at startup.', channel='tradebot-updates')
        now = self._now()
        if s.status == 'in_trade':
            if direction_now != s.direction:
                self._start_flip(direction_now, bar.ts, bar.close)
            s.last_processed_boundary = bar.ts.isoformat()
        elif self._reentry_allowed(now):
            self._send_entry(direction_now, bar.ts, bar.close)
            s.last_processed_boundary = bar.ts.isoformat()
        else:
            s.pending_missed_flip = {'direction': direction_now, 'window_start': bar.ts.isoformat(), 'close': float(bar.close),
                                     'boundary_ts': bar.ts.isoformat()}
            self._say('info', f'missed-flip entry ({direction_now}) deferred: entry guards not yet clear; retrying every tick')

    def _retry_missed_flip(self, now: datetime) -> None:
        pf = self.state.pending_missed_flip
        if pf is None:
            return
        if self.state.status != 'watching':
            self.state.pending_missed_flip = None                    # superseded: state moved on without it
            return
        if not self._reentry_allowed(now):
            return
        self._send_entry(pf['direction'], datetime.fromisoformat(pf['window_start']), pf['close'])
        self.state.pending_missed_flip = None
        self.state.last_processed_boundary = pf['boundary_ts']

    # ------------------------------------------------------------------------------------------------------------------------
    # Rolling (the rules are hestia_core.roll_policy; this orchestrates them)
    # ------------------------------------------------------------------------------------------------------------------------

    def _check_roll_timing(self, now: datetime) -> None:
        s = self.state
        if not self._roll_armed():
            return
        target = s.roll_target
        if s.status == 'watching' and s.pending_flip is None and not target.get('flatten_only'):
            new = self._ref(target['token'])
            if new is not None:
                self._switch_contract(new, 'flat on a roll eve: switched at once')
            return
        if s.status == 'in_trade' and (now >= self.rollover_at or target.get('immediate')) and not self._busy():
            new = None if target.get('flatten_only') else self._ref(target['token'])
            self._execute_roll(now, old_ref=self._ref(s.contract_token), new_ref=new, missed=False)

    def _execute_roll(self, now: datetime, old_ref: ContractRef, new_ref: Optional[ContractRef], missed: bool) -> None:
        """The fallback roll (and the missed-roll recovery): flatten the old position unconditionally and reopen on the new contract
        only if the policy says go."""
        s = self.state
        new_dir = basis = None
        if new_ref is not None:
            latest = self.ctx.latest_bar(new_ref)
            new_dir = latest[1].trend if latest is not None else None
            entry_ts = datetime.fromisoformat(s.entry_ts)
            basis = self.ctx.price_near(new_ref, entry_ts, int(self.cfg.basis_tolerance_min))
        decision = rp.decide_rollover(_dir(s.direction), new_dir, basis)
        plan = rp.reopen_plan(s.lot1_status == 'open', s.lot1_lots or 0, s.lot2_status == 'open', s.lot2_lots or 0)
        self._say('info', f'rollover decision: {decision.reason}', channel='tradebot-updates')
        close = CloseRequest(self._rid('roll_close'), old_ref, _dir(s.direction), ExitReason.ROLL, trade_ref=s.trade_counter)
        self._send(close, {'purpose': 'roll_close', 'token': old_ref.token, 'new_token': None if new_ref is None else new_ref.token,
                           'reopen': decision.reopen, 'basis': basis, 'lots': plan.lots, 'lot2_only': plan.lot2_only,
                           'direction': s.direction, 'units': s.units, 'signal_ts': s.signal_ts, 'signal_close': s.signal_close,
                           'parent': s.trade_counter, 'missed': missed})
        if decision.reopen and new_ref is not None:
            reopen = OpenRequest(self._rid('roll_open'), new_ref, _dir(s.direction), plan.lots, trade_ref=s.trade_counter + 1,
                                 roll_reopen=True, parent_trade_ref=s.trade_counter, depends_on=close.request_id)
            self._send(reopen, {'purpose': 'roll_open', 'token': new_ref.token, 'basis': basis, 'lots': plan.lots,
                                'lot2_only': plan.lot2_only, 'direction': s.direction, 'units': s.units, 'signal_ts': s.signal_ts,
                                'signal_close': s.signal_close, 'parent': s.trade_counter})

    def _done_roll_close(self, o: RequestOutcome, meta: dict) -> None:
        s = self.state
        if not o.confirmed or o.closed is None or o.closed.lots <= 0:
            if self._already_flat(o):
                self._reconcile_with_ledger()
                return
            self._failed(o, meta, 'the rollover exit')
            return
        self._apply_closed(o.closed.lots, o.closed.avg_price, 'rollover', order=(1, 2))
        new = self._ref(meta['new_token']) if meta.get('new_token') else None
        if not meta.get('reopen') and new is not None:
            self._switch_contract(new, 'rolled, no reopen (veto)')
        elif new is None:
            s.roll_target = None
            s.roll_executed_date = f'{self.session_date}'
            self.flatten_done = True

    def _done_roll_open(self, o: RequestOutcome, meta: dict) -> None:
        new = self._ref(meta['token'])
        if self.state.status == 'in_trade':
            # DEPENDENCY_FAILED (or any outcome) while the old position is still open: its close did not land. Nothing structural
            # here: the contract stays, the roll target stays armed, and the tick re-sends the whole roll under fresh ids.
            log.warning('roll reopen on %s ended %s while the old position is still open: the roll will be retried', new.symbol,
                        o.status.value)
            return
        if not o.confirmed or o.opened is None or o.opened.lots <= 0:
            self._say('critical', f'rollover reopen on {new.symbol} did not fill ({o.status.value}: {o.detail}): flat on the new contract, '
                                  f'check manually')
            self._switch_contract(new, 'rolled, reopen failed')
            return
        self._switch_contract(new, 'rolled with the position carried')
        self._open_position(meta['direction'], meta['signal_ts'], meta['signal_close'], o.opened.avg_price, o.opened.lots, meta['lots'],
                            meta['units'], new, basis=meta['basis'], parent_trade_id=meta['parent'], lot2_only=meta['lot2_only'])

    def _coincident_flip_transition(self, direction_now: str, window_start: datetime, close: float) -> None:
        """On a roll eve, before the rollover time: the old contract flipped, so the position exits at a real fill; if the new contract
        ALSO flipped to the same direction on the same bar, a fresh entry follows on it."""
        s = self.state
        new = self._ref(s.roll_target['token'])
        latest = self.ctx.latest_bar(new) if new is not None else None
        coincident = False
        if latest is not None and latest[0].ts == window_start:
            # latest_bar carries no minute count (an interface gap, plan P5.3 notes): the window is assumed complete
            coincident = rp.coincident_flip(latest[1].flip, latest[1].trend, _dir(direction_now), rows_in_window=rp.COINCIDENT_MIN_ROWS)
        ref = self._ref(s.contract_token)
        self._say('info', f'{ref.symbol} flipped {direction_now} at {window_start:%H:%M} on a roll eve; {new.symbol} '
                          f'{"ALSO" if coincident else "did NOT"} flip: {"taking over with a fresh entry" if coincident else "exit only"}',
                  channel='tradebot-updates')
        close_req = CloseRequest(self._rid('coin_close'), ref, _dir(s.direction), ExitReason.TREND_FLIP, trade_ref=s.trade_counter)
        self._send(close_req, {'purpose': 'coin_close', 'token': ref.token, 'new_token': new.token, 'coincident': coincident,
                               'direction': direction_now, 'signal_ts': window_start.isoformat(), 'signal_close': close})
        if coincident and self._past_min_entry_guard(self._now()):
            units = self._current_units()
            if self._margin_sufficient(units):
                lots = units * self.cfg.lots_per_leg * 2
                self._send(OpenRequest(self._rid('coin_open'), new, _dir(direction_now), lots, trade_ref=s.trade_counter + 1,
                                       depends_on=close_req.request_id),
                           {'purpose': 'coin_open', 'token': new.token, 'direction': direction_now, 'units': units, 'lots': lots,
                            'signal_ts': window_start.isoformat(), 'signal_close': close})

    def _done_coin_close(self, o: RequestOutcome, meta: dict) -> None:
        if not o.confirmed or o.closed is None or o.closed.lots <= 0:
            if self._already_flat(o):
                self._reconcile_with_ledger()
                return
            self._failed(o, meta, 'the roll-eve flip exit')
            if self.state.status == 'in_trade' and self.state.pending_flip is None:
                # the flip exit must not wait for the fallback roll: retry it as a plain exit-only flip on the old contract
                self.state.pending_flip = {'direction': meta['direction'], 'signal_ts': meta['signal_ts'],
                                           'signal_close': meta['signal_close'], 'units': self.state.units, 'new_lots': 0}
            return
        self._apply_closed(o.closed.lots, o.closed.avg_price, 'trend_flip', order=(1, 2))
        new = self._ref(meta['new_token'])
        self._switch_contract(new, 'rolled early: flipped and exited on the old contract')

    def _done_coin_open(self, o: RequestOutcome, meta: dict) -> None:
        if self.state.status == 'in_trade':                           # its close did not land: the exit-only retry owns the transition
            log.warning('coincident-flip entry ended %s while the old position is still open', o.status.value)
            return
        if not o.confirmed or o.opened is None or o.opened.lots <= 0:
            self._say('critical', f'coincident-flip entry did not fill ({o.status.value}): flat on the new contract')
            return
        self._open_position(meta['direction'], meta['signal_ts'], meta['signal_close'], o.opened.avg_price, o.opened.lots, meta['lots'],
                            meta['units'], self._ref(meta['token']))


def build() -> PrometheusEngine:
    """The factory named in hestia_config.ENGINES."""
    return PrometheusEngine()
