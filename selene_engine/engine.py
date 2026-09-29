"""
The Selene engine: the decision layer of `selene_backtest/parity_backtest_selene.py` against `hestia_core.interface`.

Selene is materially simpler than Prometheus (`prometheus_engine/engine.py`, which this file's structure mirrors): a single
lot, no scale-out, no targets. The only exits are the stop and the opposite trend flip, plus the roll machinery. See
`plans/hestia-p7-selene-engine.md` for what is and is not ported, and why.

Engine and host split, request lifecycle, ledger-wins reconciliation, the request-pending discipline, retry policy and the
post-close guard are identical in spirit to Prometheus's — see that file's own docstring for the reasoning, not repeated here.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from hestia_core import roll_policy as rp
from hestia_core.interface import (
    BarComplete, BarQuality, CloseRequest, CommandEvent, CommandKind, ContractInfo, ContractRef, DataSpec, Direction, DplFrozen,
    ExitReason, FeedRecovered, FeedStale, FlattenRequest, FlipRequest, OpenRequest, OutcomeStatus, ProvisionalSpec, RequestOutcome,
    SessionStart, Stop, TrackFailed, TrackReady,
)
from selene_engine.engine_configs import DEFAULT, EngineConfig
from selene_engine.levels import build_levels, lot_pnl_points, margin_per_unit
from selene_engine.state import EngineState

log = logging.getLogger('selene_engine')

BULL, BEAR = 'bullish', 'bearish'


def _dir(name: str) -> Direction:
    return Direction.BULLISH if name == BULL else Direction.BEARISH


def _name(d: Optional[Direction]) -> Optional[str]:
    return None if d is None else (BULL if d == Direction.BULLISH else BEAR)


class SeleneEngine:

    def __init__(self, cfg: EngineConfig = DEFAULT, name: str = 'selene'):
        self.cfg, self.name = cfg, name
        self.spec = DataSpec(cfg.instrument, 15, cfg.st_period, cfg.st_multiplier, provisional=ProvisionalSpec(False))
        self.state = EngineState()
        self.ctx = None
        self.started = False
        self.ended = False
        self.infos: Dict[str, ContractInfo] = {}
        self.contract: Optional[ContractRef] = None
        self.session_date: Optional[date] = None
        self.session_open: Optional[datetime] = None
        self.rollover_at: Optional[datetime] = None
        self.session_close: Optional[datetime] = None
        self.market_closed = False
        self.exit_requested = False
        self.flatten_done = False
        self.next_retry_at: Optional[datetime] = None
        self._alerted: Dict[str, datetime] = {}

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
                      f'{ev.contract.symbol} price {"FROZEN at the circuit limit" if ev.frozen else "unfroze"} at {ev.price:.2f}',
                      key='dpl')

    # ------------------------------------------------------------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------------------------------------------------------------

    def _now(self) -> datetime:
        return self.ctx.now()

    def _save(self) -> None:
        self.ctx.save_state(self.state.to_json())

    def _say(self, level: str, text: str, key: Optional[str] = None, channel: Optional[str] = None) -> None:
        now = self._now()
        if key is not None:
            last = self._alerted.get(key)
            if last is not None and (now - last).total_seconds() < self.cfg.realert_debounce_s:
                log.warning('%s (debounced)', text)
                return
            self._alerted[key] = now
        log.log({'info': logging.INFO, 'warning': logging.WARNING}.get(level, logging.ERROR), text)
        self.ctx.alert(level, text, channel)

    def _rid(self, purpose: str) -> str:
        trade = self.state.trade_counter + (1 if self.state.status == 'watching' else 0)
        key = f'{trade}:{purpose}'
        n = self.state.attempts.get(key, 0) + 1
        self.state.attempts[key] = n
        return f'{self.name}-{self.session_date:%Y%m%d}-t{trade}-{purpose}-{n}'

    def _send(self, request, context: dict) -> None:
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

    def _at_or_after_close(self, boundary: datetime) -> bool:
        """The last boundary of a session is the close itself: that bar completes after the market is shut, so it is never
        traded (production refused orders after the closing time; the same gap was found live for Prometheus, P5.4)."""
        if self.session_close is not None and boundary >= self.session_close:
            log.info('bar at boundary %s is at or after the close %s: not acted on, watermark unchanged', boundary, self.session_close)
            return True
        return False

    # ------------------------------------------------------------------------------------------------------------------------
    # Start: contract, ledger reconciliation, pending requests, roll arming, missed flip
    # ------------------------------------------------------------------------------------------------------------------------

    def _on_session_start(self, ev: SessionStart) -> None:
        ctx = self.ctx
        self.session_date, self.session_open, self.rollover_at = ev.session_date, ev.session_open, ev.rollover_time
        self.rollover_at = self._own_rollover_time(ev)
        self.session_close = self.rollover_at + timedelta(minutes=self.cfg.rollover_buffer_min)
        self.infos = {i.ref.token: i for i in ev.contracts}
        pairs = [(i.ref, i.trading_days_left) for i in ev.contracts]
        eff_today = rp.effective_from_days_left(pairs, self.cfg.roll_window_days)
        s = self.state
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
            target = self._ref(s.contract_token) or target
        self.contract = target
        ctx.set_trading_contract(target)
        self.started = True
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

    def _own_rollover_time(self, ev: SessionStart) -> datetime:
        """Selene's own rollover buffer (14 min, not Prometheus's 15, `selene_configs.ROLLOVER_BUFFER_MIN`): `ev.rollover_time` is
        always exactly 15 minutes before the session close, both for MCX's two real closing times (23:15 before a 23:30 close,
        23:40 before a 23:55 close -- `core._session_start_event`, `CoreConfig.rollover_time`). Recover the close from it and
        reapply our own buffer."""
        session_close = ev.rollover_time + timedelta(minutes=15)
        return session_close - timedelta(minutes=self.cfg.rollover_buffer_min)

    def _freeze(self, why: str) -> None:
        self.state.frozen, self.started = True, True
        self._say('critical', f'engine frozen, needs the operator: {why}')
        self._save()

    def _reconcile_with_ledger(self) -> None:
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
            s.direction, s.lots = (BULL if held > 0 else BEAR), abs(held)
        else:
            self._say('critical', f'the ledger holds {held:+d} lots of {ref.symbol} but the engine believed it flat: adopting the '
                                  f'position with a level computed from its average price')
            direction = BULL if held > 0 else BEAR
            lots = abs(held)
            price = pos.avg_price or self._ltp_value(ref) or 0.0
            units = max(1, lots)
            s.contract_token, s.contract_symbol, s.contract_expiry = ref.token, ref.symbol, ref.expiry.isoformat()
            self._install_position(direction, units, price, lots, signal_ts=None, signal_close=None, adopted=True)
        self._save()

    def _resume_pending(self) -> None:
        for rid, meta in list(self.state.pending.items()):
            st = self.ctx.request_status(rid)
            if st is None:
                self.state.pending.pop(rid)
                self._say('warning', f'request {rid} ({meta.get("purpose")}) was never sent before the restart; it will be re-decided')
            elif st.status == OutcomeStatus.IN_FLIGHT:
                log.info('request %s still in flight; waiting for its outcome', rid)
            elif st.status == OutcomeStatus.UNCONFIRMED:
                log.info('request %s unconfirmed; Hestia is settling it', rid)
            else:
                self._on_outcome(st)

    def _arm_roll(self, eff_today: rp.Effective) -> None:
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
        if tomorrow.contract.expiry <= self.contract.expiry:
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
        if ev.st.trend is None:
            return
        window_start = ev.bar.ts
        direction_now = _name(ev.st.trend)
        if ev.st.flip:
            # the raw signal itself, independent of what the engine goes on to decide -- same per-bar Slack line Prometheus's
            # engine sends (ported from prometheus.py's own "ST_15 flip -> *direction*"), so Selene's alerts match Prometheus's.
            self._say('info', f'ST_15 flip -> {direction_now} at {window_start:%H:%M} (close={ev.bar.close:.2f}, '
                              f'ST={ev.st.value:.2f})', channel='tradebot-updates')
        self.state.last_processed_boundary = window_start.isoformat()
        self._save()
        self._act_on_signal(direction_now, ev.st.flip, window_start, ev.bar.close)

    def _act_on_signal(self, direction_now: str, flip: bool, window_start: datetime, close: float) -> bool:
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
    # Entry
    # ------------------------------------------------------------------------------------------------------------------------

    def _send_entry(self, direction: str, signal_ts: datetime, signal_close: float) -> None:
        if not self._ltp_value():
            self._say('critical', 'no price available: cannot enter')
            return
        units = self._current_units()
        if not self._margin_sufficient(units):
            return
        lots = units
        trade_ref = self.state.trade_counter + 1
        req = OpenRequest(self._rid('entry'), self.contract, _dir(direction), lots, trade_ref=trade_ref)
        self._send(req, {'purpose': 'entry', 'direction': direction, 'signal_ts': signal_ts.isoformat(), 'signal_close': signal_close,
                         'units': units, 'lots': lots, 'token': self.contract.token})

    # ------------------------------------------------------------------------------------------------------------------------
    # Rule 7: a flip is one netted request
    # ------------------------------------------------------------------------------------------------------------------------

    def _start_flip(self, direction_now: str, signal_ts: datetime, signal_close: float) -> None:
        old_open = self.state.open_lots()
        allowed = self._reentry_allowed(self._now())
        units = self._current_units()
        new_lots = units if allowed else 0
        if new_lots > 0 and not self._margin_sufficient(units):
            new_lots = 0
        self.state.pending_flip = {'direction': direction_now, 'signal_ts': signal_ts.isoformat(), 'signal_close': signal_close,
                                   'units': units, 'new_lots': new_lots}
        self._save()
        if old_open == 0:
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
    # LTP-driven stop
    # ------------------------------------------------------------------------------------------------------------------------

    def _tick(self) -> None:
        if self.state.frozen or self.ended:
            return
        now = self._now()
        if self.market_closed or (self.session_close is not None and now >= self.session_close):
            return
        if self.next_retry_at is not None and now < self.next_retry_at:
            return
        s = self.state
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
            self._check_stop(now)

    def _check_stop(self, now: datetime) -> None:
        if not self._past_first_minute_guard(now):
            return
        s = self.state
        ref = self._ref(s.contract_token) or self.contract
        ltp = self._ltp_value(ref)
        if not ltp:
            return
        bull = s.direction == BULL
        if s.sl_price is not None and ((ltp <= s.sl_price) if bull else (ltp >= s.sl_price)):
            self._send_exit_all('stop_loss', ExitReason.STOP_LOSS)

    def _send_exit_all(self, reason: str, why: ExitReason, purpose: Optional[str] = None) -> None:
        s = self.state
        ref = self._ref(s.contract_token) or self.contract
        req = FlattenRequest(self._rid(purpose or 'exit_all'), ref, why, trade_ref=s.trade_counter)
        self._send(req, {'purpose': purpose or 'exit_all', 'reason': reason, 'token': ref.token})

    # ------------------------------------------------------------------------------------------------------------------------
    # The operator's EXIT
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
        handler = {'entry': self._done_entry, 'flip': self._done_flip, 'flip_exit': self._done_flip_exit,
                   'exit_all': self._done_exit_all, 'stop_loss': self._done_exit_all, 'manual_exit': self._done_manual_exit,
                   'roll_close': self._done_roll_close, 'roll_open': self._done_roll_open, 'coin_close': self._done_coin_close,
                   'coin_open': self._done_coin_open}.get(purpose)
        if handler is None:
            self._say('critical', f'outcome for an unknown purpose {purpose!r}')
        else:
            handler(o, meta)
        self._save()

    def _failed(self, o: RequestOutcome, meta: dict, what: str) -> None:
        if o.status == OutcomeStatus.LIMIT_REFUSED and 'market closed' in o.detail:
            self.market_closed = True
            self._say('critical', f'{what} refused, the market is closed ({o.detail}); position carried to the next session',
                      key=f'closed-{meta["purpose"]}')
            return
        self._say('critical', f'{what} failed ({o.status.value}: {o.detail}); will retry', key=f'fail-{meta["purpose"]}')
        if o.status == OutcomeStatus.REJECTED:
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
                s.pending_flip['new_lots'] = 0
                self._say('critical', f'the flip re-entry was refused ({o.detail}); closing the old side only')
                self.next_retry_at = self._now() + timedelta(seconds=self.cfg.retry_cooldown_s)
                return
            self._failed(o, meta, 'the Rule 7 flip')
            return
        self._apply_closed(o.closed.lots, o.closed.avg_price, 'trend_flip')
        if s.open_lots() > 0:
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

    def _done_flip_exit(self, o: RequestOutcome, meta: dict) -> None:
        s = self.state
        if not o.confirmed or o.closed is None or o.closed.lots <= 0:
            if self._already_flat(o):
                self._reconcile_with_ledger()
                s.pending_flip = None
                return
            self._failed(o, meta, 'the flip exit')
            return
        self._apply_closed(o.closed.lots, o.closed.avg_price, 'trend_flip')
        if s.open_lots() == 0:
            s.pending_flip = None

    def _done_exit_all(self, o: RequestOutcome, meta: dict) -> None:
        if not o.confirmed or o.closed is None or o.closed.lots <= 0:
            if self._already_flat(o):
                self._reconcile_with_ledger()
                return
            self._failed(o, meta, f'exit ({meta["reason"]})')
            return
        self._apply_closed(o.closed.lots, o.closed.avg_price, meta['reason'])

    def _done_manual_exit(self, o: RequestOutcome, meta: dict) -> None:
        if not o.confirmed or o.closed is None or o.closed.lots <= 0:
            if self._already_flat(o):
                self._reconcile_with_ledger()
                self.exit_requested = False
                return
            self._failed(o, meta, 'the manual exit')
            return
        self._apply_closed(o.closed.lots, o.closed.avg_price, 'slack_exit')
        if self.state.status != 'in_trade':
            self.exit_requested = False
            self._say('info', 'position liquidated; still watching for the next entry', channel='trade-alerts')

    # ------------------------------------------------------------------------------------------------------------------------
    # Position bookkeeping
    # ------------------------------------------------------------------------------------------------------------------------

    def _open_position(self, direction: str, signal_ts: Optional[str], signal_close: Optional[float], entry_price: float, filled_lots: int,
                       requested_lots: int, units: int, ref: ContractRef, basis: Optional[float] = None,
                       parent_trade_id: Optional[int] = None) -> None:
        s = self.state
        s.trade_counter += 1
        s.contract_token, s.contract_symbol, s.contract_expiry = ref.token, ref.symbol, ref.expiry.isoformat()
        self._install_position(direction, units, entry_price, filled_lots, signal_ts, signal_close, basis=basis,
                               parent_trade_id=parent_trade_id)
        sl = f'{s.sl_price:.2f}' if s.sl_price is not None else 'n/a'
        self._say('info', f'Entered {direction.upper()}{" (rollover)" if parent_trade_id is not None else ""}  {ref.symbol} | '
                          f'Units: {units} ({filled_lots} lot(s))  Entry: {entry_price:.2f}'
                          f'{f" (recalibration basis {basis:.2f})" if basis is not None else ""} | SL: {sl}', channel='trade-alerts')
        if filled_lots < requested_lots:
            log.warning('Partial fill: requested %d lots, filled %d', requested_lots, filled_lots)

    def _install_position(self, direction: str, units: int, entry_price: float, lots: int, signal_ts: Optional[str],
                          signal_close: Optional[float], basis: Optional[float] = None, parent_trade_id: Optional[int] = None,
                          adopted: bool = False) -> None:
        s = self.state
        threshold = basis if basis is not None else entry_price
        lv = build_levels(direction, threshold, self.cfg)
        now = self._now().isoformat()
        if adopted:
            s.trade_counter += 1
        s.status, s.direction, s.units = 'in_trade', direction, units
        s.entry_price, s.basis_price, s.entry_ts = entry_price, basis, now
        s.signal_ts, s.signal_close = signal_ts, signal_close
        s.sl_price, s.lots = lv.sl_price, lots
        slippage = None if signal_close is None else round((entry_price - signal_close) if direction == BULL else (signal_close - entry_price), 2)
        # the shared TRADE_RECORD_COLUMNS format (core.report_trade coerces every engine's row to it): a single lot with no
        # target maps onto Prometheus's lot1_*/total_pnl_* fields, lot2_* left blank -- the same shape Prometheus itself uses
        # when its own lot 2 never opens.
        s.trade_row = {'trade_id': s.trade_counter, 'contract_expiry': s.contract_expiry,
                       'direction': f'{direction}-rollover' if parent_trade_id is not None else direction, 'units': units,
                       'entry_ts': now, 'entry_price': entry_price, 'signal_ts': signal_ts, 'signal_close': signal_close,
                       'entry_slippage_points': slippage, 'sl_price': None if lv.sl_price is None else round(lv.sl_price, 2),
                       'lot1_target': None, 'lot2_target': None, 'lot2_target_source': None, 'parent_trade_id': parent_trade_id}

    def _apply_closed(self, lots: int, price: float, reason: str) -> None:
        s = self.state
        pts = lot_pnl_points(s.direction, s.entry_price, price)
        rs = round(pts * lots * self._lot_size(s.contract_token), 2)
        now = self._now().isoformat()
        s.trade_row.update({'lot1_exit_ts': now, 'lot1_exit_price': round(price, 2), 'lot1_exit_reason': reason,
                            'lot1_pnl_points': round(pts, 2), 'lot1_pnl_rs': rs, 'total_pnl_points': round(pts, 2),
                            'total_pnl_rs': rs})
        per_unit = rs / (s.units or 1)
        self._say('info', f'Exit: {reason}  (Units: {s.units})  Entry {s.entry_price:.2f} -> Exit {price:.2f} | P&L: {pts:+.2f} pts '
                          f' Rs.{per_unit:+,.0f}/unit', channel='trade-alerts')
        self._finalize_trade()

    def _finalize_trade(self) -> None:
        s = self.state
        row = dict(s.trade_row)
        self.ctx.report_trade(row)
        self._say('info', f'Trade #{row.get("trade_id")} closed.  (Units: {s.units})  P&L: {row.get("total_pnl_points", 0):+.2f} pts',
                  channel='trade-alerts')
        self._reset_position()

    def _reset_position(self) -> None:
        s = self.state
        keep = dict(trade_counter=s.trade_counter, last_processed_boundary=s.last_processed_boundary, pending=s.pending,
                    pending_flip=s.pending_flip, pending_missed_flip=s.pending_missed_flip, roll_target=s.roll_target,
                    roll_executed_date=s.roll_executed_date, attempts=s.attempts, contract_token=s.contract_token,
                    contract_symbol=s.contract_symbol, contract_expiry=s.contract_expiry)
        s.__dict__.update(EngineState(status='watching', **keep).__dict__)

    # ------------------------------------------------------------------------------------------------------------------------
    # Missed flip
    # ------------------------------------------------------------------------------------------------------------------------

    def _reconcile_missed_flip(self) -> None:
        s = self.state
        series = self.ctx.st_series(self.contract, 300)
        if not series:
            return
        if s.last_processed_boundary is None:
            last_bar, last_st = series[-1]
            if last_st.trend is not None:
                s.last_processed_boundary = last_bar.ts.isoformat()
            return
        mark = datetime.fromisoformat(s.last_processed_boundary)
        flips = [(b, st) for b, st in series if b.ts > mark and st.flip and st.trend is not None]
        if not flips:
            return
        bar, st = flips[-1]
        direction_now = _name(st.trend)
        self._say('critical', f'Missed flip detected: ST_15 flipped -> {direction_now} at {bar.ts}: the session ended before this bar '
                              f'went live. Reconciling at startup.', channel='tradebot-updates')
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
            self.state.pending_missed_flip = None
            return
        if not self._reentry_allowed(now):
            return
        self._send_entry(pf['direction'], datetime.fromisoformat(pf['window_start']), pf['close'])
        self.state.pending_missed_flip = None
        self.state.last_processed_boundary = pf['boundary_ts']

    # ------------------------------------------------------------------------------------------------------------------------
    # Rolling
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
        s = self.state
        new_dir = basis = None
        if new_ref is not None:
            latest = self.ctx.latest_bar(new_ref)
            new_dir = latest[1].trend if latest is not None else None
            entry_ts = datetime.fromisoformat(s.entry_ts)
            basis = self.ctx.price_near(new_ref, entry_ts, int(self.cfg.basis_tolerance_min))
        decision = rp.decide_rollover(_dir(s.direction), new_dir, basis)
        lots = s.open_lots()
        self._say('info', f'rollover decision: {decision.reason}', channel='tradebot-updates')
        close = CloseRequest(self._rid('roll_close'), old_ref, _dir(s.direction), ExitReason.ROLL, trade_ref=s.trade_counter)
        self._send(close, {'purpose': 'roll_close', 'token': old_ref.token, 'new_token': None if new_ref is None else new_ref.token,
                           'reopen': decision.reopen, 'basis': basis, 'lots': lots, 'direction': s.direction, 'units': s.units,
                           'signal_ts': s.signal_ts, 'signal_close': s.signal_close, 'parent': s.trade_counter, 'missed': missed})
        if decision.reopen and new_ref is not None:
            reopen = OpenRequest(self._rid('roll_open'), new_ref, _dir(s.direction), lots, trade_ref=s.trade_counter + 1,
                                 roll_reopen=True, parent_trade_ref=s.trade_counter, depends_on=close.request_id)
            self._send(reopen, {'purpose': 'roll_open', 'token': new_ref.token, 'basis': basis, 'lots': lots, 'direction': s.direction,
                                'units': s.units, 'signal_ts': s.signal_ts, 'signal_close': s.signal_close, 'parent': s.trade_counter})

    def _done_roll_close(self, o: RequestOutcome, meta: dict) -> None:
        s = self.state
        if not o.confirmed or o.closed is None or o.closed.lots <= 0:
            if self._already_flat(o):
                self._reconcile_with_ledger()
                return
            self._failed(o, meta, 'the rollover exit')
            return
        self._apply_closed(o.closed.lots, o.closed.avg_price, 'rollover')
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
            log.warning('roll reopen on %s ended %s while the old position is still open: the roll will be retried', new.symbol,
                        o.status.value)
            return
        if not o.confirmed or o.opened is None or o.opened.lots <= 0:
            self._say('critical', f'rollover reopen on {new.symbol} did not fill ({o.status.value}: {o.detail}): flat on the new '
                                  f'contract, check manually')
            self._switch_contract(new, 'rolled, reopen failed')
            return
        self._switch_contract(new, 'rolled with the position carried')
        self._open_position(meta['direction'], meta['signal_ts'], meta['signal_close'], o.opened.avg_price, o.opened.lots, meta['lots'],
                            meta['units'], new, basis=meta['basis'], parent_trade_id=meta['parent'])

    def _coincident_flip_transition(self, direction_now: str, window_start: datetime, close: float) -> None:
        s = self.state
        new = self._ref(s.roll_target['token'])
        latest = self.ctx.latest_bar(new) if new is not None else None
        coincident = False
        if latest is not None and latest[0].ts == window_start:
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
                lots = units
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
                self.state.pending_flip = {'direction': meta['direction'], 'signal_ts': meta['signal_ts'],
                                           'signal_close': meta['signal_close'], 'units': self.state.units, 'new_lots': 0}
            return
        self._apply_closed(o.closed.lots, o.closed.avg_price, 'trend_flip')
        new = self._ref(meta['new_token'])
        self._switch_contract(new, 'rolled early: flipped and exited on the old contract')

    def _done_coin_open(self, o: RequestOutcome, meta: dict) -> None:
        if self.state.status == 'in_trade':
            log.warning('coincident-flip entry ended %s while the old position is still open', o.status.value)
            return
        if not o.confirmed or o.opened is None or o.opened.lots <= 0:
            self._say('critical', f'coincident-flip entry did not fill ({o.status.value}): flat on the new contract')
            return
        self._open_position(meta['direction'], meta['signal_ts'], meta['signal_close'], o.opened.avg_price, o.opened.lots, meta['lots'],
                            meta['units'], self._ref(meta['token']))


def build() -> SeleneEngine:
    """The factory named in hestia_config.ENGINES."""
    return SeleneEngine()
