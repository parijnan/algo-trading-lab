"""
The Oleus engine: Janus "plain thirds" on CRUDEOILM against `hestia_core.interface` (plans/oleus-janus-engine.md).

No Supertrend and no 15-minute boundary: the engine is driven by price crossings of fixed levels and by the clock. The day's Camarilla levels
come from the previous session's high, low and close (the traded contract's own one-minute bars through `ctx.minute_bars`); four setups, each
a touch entry with three equal lots exiting at three targets and one stop for all open lots; flat before the close (hard exit), no entry in the
first minute or the last hour. The rules live in `oleus_engine/machine.py`, a pure copy of the research machine pinned to it by a test.

Request discipline is the other engines': an engine changes its position state ONLY on a confirmed outcome. The machine proposes the next
event of the price's move (`find_next`), the engine asks Hestia, and on a confirmed fill applies it and continues the same move from the event's
trigger price (so a stop that is also another setup's entry level reverses). One request at a time.

Prices come from ticks (`ctx.ltp`, polled every 0.5 to 1 s). The completed one-minute bars are a safety net for a stop or a target that fell
between two polls: it fires the event late and counts it (`late_catches`). A missed ENTRY touch is never chased, it is only counted
(`missed_entries`). A trade carried past the hard exit is not possible: everything is closed 15 minutes before the last bar, and the product
type is INTRADAY (MIS) when the engine is live.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional

from hestia_core import roll_policy as rp
from hestia_core.interface import (
    CloseRequest, CommandEvent, CommandKind, ContractInfo, ContractRef, DataSpec, Direction, DplFrozen, ExitReason, FeedRecovered, FeedStale,
    FlattenRequest, OpenRequest, OutcomeStatus, ProvisionalSpec, RequestOutcome, SessionStart, Stop, TrackFailed, TrackReady,
)
from hestia_core.sizing import dynamic_units
from oleus_engine import levels as lv
from oleus_engine.engine_configs import DEFAULT, TRADE_EXTRA_COLUMNS, EngineConfig
from oleus_engine.machine import BOOKED, CLOSED, OPEN, Event, Machine
from oleus_engine.state import EngineState

log = logging.getLogger('oleus_engine')

REASONS = ('T1', 'T2', 'T3')


def _dir(side: int) -> Direction:
    return Direction.BULLISH if side > 0 else Direction.BEARISH


class OleusEngine:

    trade_extra_columns = TRADE_EXTRA_COLUMNS

    def __init__(self, cfg: EngineConfig = DEFAULT, name: str = 'oleus'):
        self.cfg, self.name = cfg, name
        # Oleus does not use the Supertrend; the spec carries Prometheus's settings so the two engines share one cached bar series.
        self.spec = DataSpec(cfg.instrument, 15, cfg.st_period, cfg.st_multiplier, provisional=ProvisionalSpec(False))
        self.state = EngineState()
        self.machine: Optional[Machine] = None
        self.ctx = None
        self.started = False
        self.ended = False
        self.infos: Dict[str, ContractInfo] = {}
        self.contract: Optional[ContractRef] = None
        self.session_date: Optional[date] = None
        self.session_open: Optional[datetime] = None
        self.session_close: Optional[datetime] = None
        self.market_closed = False
        self.exit_requested = False
        self.next_retry_at: Optional[datetime] = None
        self.feed_stale: set = set()
        self._alerted: Dict[str, datetime] = {}
        self._levels_try_at: Optional[datetime] = None
        self._levels_failed_since: Optional[datetime] = None
        self._last_trade_update: Optional[datetime] = None
        self._last_running_row: Optional[datetime] = None

    # ------------------------------------------------------------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------------------------------------------------------------

    def run(self, ctx) -> None:
        self.ctx = ctx
        self.state = EngineState.from_json(ctx.load_state())
        while not self.ended:
            timeout = self.cfg.tick_s_in_trade if self.state.trades else self.cfg.tick_s_flat
            ev = ctx.next_event(timeout)
            if ev is not None:
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
        elif isinstance(ev, RequestOutcome):
            self._on_outcome(ev)
        elif isinstance(ev, CommandEvent):
            if ev.kind == CommandKind.EXIT:
                self._on_exit_command()
        elif isinstance(ev, TrackFailed):
            self._say('warning', f'could not track {ev.contract.symbol}: {ev.reason}', key='track-failed')
        elif isinstance(ev, FeedStale):
            self.feed_stale.add(ev.contract.token)
            self._say('warning', f'price feed for {ev.contract.symbol} stale for {ev.age_sec:.0f}s', key='feed-stale')
        elif isinstance(ev, FeedRecovered):
            self.feed_stale.discard(ev.contract.token)
        elif isinstance(ev, DplFrozen):
            self._say('critical' if ev.frozen else 'info',
                      f'{ev.contract.symbol} price {"FROZEN at the circuit limit" if ev.frozen else "unfroze"} at {ev.price:.2f}', key='dpl')
        # BarComplete / ProvisionalBar / TrackReady are ignored: nothing here reads the 15-minute series

    # ------------------------------------------------------------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------------------------------------------------------------

    def _now(self) -> datetime:
        return self.ctx.now()

    def _save(self) -> None:
        if self.machine is not None:
            self.state.machine = self.machine.to_dict()
        self.ctx.save_state(self.state.to_json())

    def _say(self, level: str, text: str, key: Optional[str] = None, channel: Optional[str] = None, emoji: Optional[str] = None) -> None:
        now = self._now()
        if key is not None:
            last = self._alerted.get(key)
            if last is not None and (now - last).total_seconds() < self.cfg.realert_debounce_s:
                log.warning('%s (debounced)', text)
                return
            self._alerted[key] = now
        log.log({'info': logging.INFO, 'warning': logging.WARNING}.get(level, logging.ERROR), text)
        self.ctx.alert(level, text, channel, emoji=emoji)

    def _rid(self, trade: int, purpose: str) -> str:
        key = f'{trade}:{purpose}'
        n = self.state.attempts.get(key, 0) + 1
        self.state.attempts[key] = n
        return f'{self.name}-{self.session_date:%Y%m%d}-t{trade}-{purpose}-{n}'

    def _send(self, request, context: dict) -> None:
        log.info('REQUEST %s %s %s lots=%s purpose=%s', request.request_id, type(request).__name__, getattr(request, 'direction', getattr(request, 'expected_direction', '')),
                 getattr(request, 'lots', ''), context.get('purpose'))
        self.state.pending[request.request_id] = context
        self._save()
        ack = self.ctx.submit(request)
        if ack.status.value == 'invalid':
            self.state.pending.pop(request.request_id, None)
            self._save()
            self._say('critical', f'request {request.request_id} refused as invalid: {ack.detail}')

    def _busy(self) -> bool:
        return bool(self.state.pending)

    def _lot_size(self) -> int:
        info = self.infos.get(self.contract.token) if self.contract else None
        return info.lot_size if info else 1

    def _price(self):
        q = self.ctx.ltp(self.contract)
        return q if q is not None else None

    def _floor(self, t: datetime) -> datetime:
        return t.replace(second=0, microsecond=0)

    @property
    def first_bar(self) -> datetime:
        return self._floor(self.session_open)

    @property
    def last_bar(self) -> datetime:
        return self._floor(self.session_close) - timedelta(minutes=1)

    def _entries_open(self, now: datetime) -> bool:
        m = self._floor(now)
        return (m >= self.first_bar + timedelta(minutes=self.cfg.at_open_minutes)
                and m < self.last_bar - timedelta(minutes=self.cfg.entry_cutoff_min))

    def _hard_exit_due(self, now: datetime) -> bool:
        return self._floor(now) >= self.last_bar - timedelta(minutes=self.cfg.hard_exit_min)

    def _current_units(self) -> int:
        sz = self.ctx.sizing()
        if not sz.dynamic:
            return sz.static_units
        try:
            m = self.ctx.margin()
            q = self._price()
            mpu = lv.margin_per_unit(q.price if q else None, self._lot_size(), self.cfg)
            d = dynamic_units(m.total_margin, m.sharing_engines, sz.allocation_rs, mpu, sz.unit_cap)
        except Exception as exc:                                       # noqa: BLE001
            self._say('warning', f'dynamic sizing failed ({exc!r}); using the static units')
            return sz.static_units
        if d.units < 1:
            self._say('warning', f'dynamic sizing: {d.reason}; entry skipped')
        return d.units

    def _margin_sufficient(self, units: int, price: float) -> bool:
        if units < 1:
            return False
        mpu = lv.margin_per_unit(price, self._lot_size(), self.cfg)
        cash = self.ctx.margin().available_cash
        if cash < units * mpu:
            self._say('warning', f'insufficient margin (available Rs {cash:,.0f}, need Rs {units * mpu:,.0f}): entry skipped', key='margin')
            return False
        return True

    # ------------------------------------------------------------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------------------------------------------------------------

    def _on_session_start(self, ev: SessionStart) -> None:
        self.session_date, self.session_open = ev.session_date, ev.session_open
        self.session_close = ev.rollover_time + timedelta(minutes=rp.ROLLOVER_BEFORE_CLOSE_MIN)
        self.infos = {i.ref.token: i for i in ev.contracts}
        s = self.state
        if s.session_date != f'{ev.session_date}':
            left_over = bool(s.trades)
            old = (s.contract_token, s.trades)
            s.new_day(f'{ev.session_date}')
            if left_over:
                self._say('critical', f'a trade from {old[1]} was still open in the state of a previous session; it was dropped, the ledger check '
                                      f'below decides whether anything is still held')
        pairs = [(i.ref, i.trading_days_left) for i in ev.contracts]
        eff = rp.effective_from_days_left(pairs, self.cfg.roll_window_days)
        target = eff.contract
        if s.contract_token and s.contract_token in self.infos:
            target = self.infos[s.contract_token].ref                  # restart in the same session: keep the day's contract
        self.contract = target
        s.contract_token, s.contract_symbol, s.contract_expiry = target.token, target.symbol, target.expiry.isoformat()
        self.ctx.set_trading_contract(target)
        if target not in ev.seeded:
            self.ctx.track(target)
        self.started = True
        self._say('info', f'starting — trading {target.symbol} (session {self.session_open:%H:%M}–{self.session_close:%H:%M})',
                  channel='tradebot-updates', emoji='⚡')
        self._build_machine()
        self._reconcile_with_ledger()
        if s.frozen:
            return
        self._resume_pending()
        self._save()

    def _build_machine(self) -> None:
        s = self.state
        if s.levels:
            self.machine = Machine(lv.build_setups(s.levels))
            self.machine.load(s.machine)
            self._announce_levels()

    def _prepare_levels(self, now: datetime) -> None:
        """Compute today's levels from the previous session of the traded contract (retried until the minute bars are there)."""
        s = self.state
        if s.levels or s.no_trade_reason or (self._levels_try_at is not None and now < self._levels_try_at):
            return
        self._levels_try_at = now + timedelta(seconds=30)

        def day_bars(d: date):
            return list(self.ctx.minute_bars(self.contract, datetime.combine(d, time.min), datetime.combine(d, time(23, 59, 59))))
        prev = lv.previous_session(day_bars, self.session_date, self.cfg)
        if prev is None:
            if self.ctx.minute_bars(self.contract, datetime.combine(self.session_date - timedelta(days=self.cfg.max_prev_session_gap_days), time.min),
                                    datetime.combine(self.session_date, time.min)):
                s.no_trade_reason = 'the previous session of this contract is missing or too short'
                self._say('critical', f'no trading today: {s.no_trade_reason}', channel='tradebot-updates')
                self._save()
                return
            if self._levels_failed_since is None:
                self._levels_failed_since = now
            if (now - self._levels_failed_since).total_seconds() > 300:
                self._say('critical', f'no minute bars of {self.contract.symbol} for the previous session yet; still waiting, no entries until the '
                                      f'levels exist', key='levels')
            return
        d, high, low, close = prev
        s.levels = lv.camarilla_levels(high, low, close, self.cfg)
        s.prev_session = f'{d}'
        log.info('LEVELS %s from previous session %s H=%.2f L=%.2f C=%.2f: %s', self.session_date, d, high, low, close,
                 {k: round(v, 2) for k, v in s.levels.items()})
        self.machine = Machine(lv.build_setups(s.levels))
        self.machine.load(s.machine)
        self._announce_levels(high, low, close)
        self._save()

    def _announce_levels(self, high=None, low=None, close=None) -> None:
        L = self.state.levels
        basis = f'previous session {self.state.prev_session}' + (f' H {high:.2f} L {low:.2f} C {close:.2f}' if high is not None else '')
        self._say('info', f'levels ({basis}): S4 {L["S4"]:.2f}  S3 {L["S3"]:.2f}  S1 {L["S1"]:.2f}  C {L["C"]:.2f}  R1 {L["R1"]:.2f}  '
                          f'R3 {L["R3"]:.2f}  R4 {L["R4"]:.2f}', channel='tradebot-updates', emoji='✅')

    # ------------------------------------------------------------------------------------------------------------------------
    # Ledger and pending requests
    # ------------------------------------------------------------------------------------------------------------------------

    def _expected_net(self) -> int:
        return sum(t['side'] * sum(sz for sz, st in zip(t['sizes'], self.machine.trades[k]['lots']) if st == OPEN)
                   for k, t in self.state.trades.items() if self.machine and self.machine.trades.get(k))

    def _reconcile_with_ledger(self) -> None:
        s = self.state
        held = self.ctx.position(self.contract).net_lots
        expected = self._expected_net()
        if held == expected:
            return
        self._say('critical', f'the engine believed {expected:+d} lots of {self.contract.symbol}, the ledger shows {held:+d}: dropping the engine\'s '
                              f'trades' + ('' if held == 0 else ' and flattening the ledger position'))
        self._drop_trades()
        if held != 0 and not self._busy():
            self._send_flatten('ledger_mismatch')
        self._save()

    def _drop_trades(self) -> None:
        s = self.state
        s.trades.clear()
        if self.machine is not None:
            self.machine.close_all()
        s.just = None

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

    # ------------------------------------------------------------------------------------------------------------------------
    # The tick
    # ------------------------------------------------------------------------------------------------------------------------

    def _tick(self) -> None:
        s = self.state
        if s.frozen or self.ended or self.contract is None:
            return
        now = self._now()
        if self.market_closed or now >= self.session_close:
            return
        if self.machine is None:
            self._prepare_levels(now)
            if self.machine is None:
                return
        self._log_status(now)
        if s.trades:
            self._maybe_send_trade_update(now)
            self._maybe_send_running_row(now)
        if self.next_retry_at is not None and now < self.next_retry_at:
            return
        if self._busy():
            return
        if self.exit_requested and s.trades:
            self._send_flatten('manual_exit')
            return
        if s.trades and self._hard_exit_due(now):
            self._send_flatten('hard_exit')
            return
        if s.trades and self._late_catch(now):
            return
        self._scan(now)

    def _log_status(self, now: datetime) -> None:
        """One line a minute: the price, how old it is, where the scan stands, what is open and the nearest levels either side."""
        minute = self._floor(now)
        if getattr(self, '_status_minute', None) == minute:
            return
        self._status_minute = minute
        q = self._price()
        near = ''
        if q is not None and self.state.levels:
            lv_ = sorted((v, k) for k, v in self.state.levels.items() if k not in ('R',))
            below = [x for x in lv_ if x[0] <= q.price]
            above = [x for x in lv_ if x[0] > q.price]
            near = f"below {below[-1][1]} {below[-1][0]:.2f}" if below else 'below none'
            near += f" above {above[0][1]} {above[0][0]:.2f}" if above else ' above none'
        log.info('STATUS %s ltp=%s age=%s prev_scan=%s entries_open=%s hard_exit=%s open=%s late_catches=%d missed_entries=%d %s',
                 self.session_date, None if q is None else q.price, None if q is None else round(q.age_sec, 1), self.state.prev_price,
                 self._entries_open(now), self._hard_exit_due(now),
                 {k: self.machine.trades[k]['lots'] for k in self.state.trades if self.machine and self.machine.trades.get(k)},
                 self.state.late_catches, self.state.missed_entries, near)

    def _scan(self, now: datetime) -> None:
        s = self.state
        q = self._price()
        if q is None or q.age_sec > self.cfg.ltp_max_age_s or self.contract.token in self.feed_stale:
            return
        b = q.price
        if s.prev_price is None:
            s.prev_price = b
            return
        a = s.prev_price
        entries_open = self._entries_open(now)
        cur, just, skipped = None, s.just, set()
        while True:
            ev = self.machine.find_next(a, b, entries_open, cur, just, skipped)
            if ev is None:
                break
            if ev.kind == 'entry':
                stale = q.age_sec > self.cfg.ltp_max_age_entry_s
                units = 0 if stale else self._current_units()
                if stale or not self.machine.entry_ok(ev.key, b) or not self._margin_sufficient(units, b):
                    log.info('ENTRY NOT TAKEN %s trigger %.2f price %.2f age %.1fs stale=%s past_T1=%s units=%s', ev.key, ev.trigger, b, q.age_sec, stale,
                             not self.machine.entry_ok(ev.key, b), units)
                    if stale or units < 1:
                        s.missed_entries += 1
                        self._say('warning', f'{ev.key} entry touch at {ev.trigger:.2f} not taken ({"stale price" if stale else "no units / margin"})',
                                  key=f'missed-{ev.key}')
                    cur = just = ev.trigger
                    skipped.add(ev.fill_key)
                    continue
            self._send_event(ev, b, units if ev.kind == 'entry' else None)
            return
        s.prev_price = b
        if b != a:
            s.just = None                                               # the continued move is over; an unmoved price keeps it (stop-and-reverse)

    def _late_catch(self, now: datetime) -> bool:
        """Minute-bar safety net for stops and targets. A trade is only checked on minutes after the one it entered in (inside that minute the
        ticks decide). Returns True when it sent a request."""
        s = self.state
        done = datetime.fromisoformat(s.last_minute_checked) if s.last_minute_checked else self._floor(now) - timedelta(minutes=2)
        bars = self.ctx.minute_bars(self.contract, done + timedelta(minutes=1), now)
        for bar in bars:
            for key, t in s.trades.items():
                mt = self.machine.trades.get(key)
                if mt is None or bar.ts <= self._floor(datetime.fromisoformat(t['entry_ts'])):
                    continue
                side = t['side']
                stop = mt['stop']
                hit_stop = bar.low <= stop if side > 0 else bar.high >= stop
                if hit_stop:
                    ev = Event('stop', key, None, stop, stop, 0)
                else:
                    ev = None
                    for j, st in enumerate(mt['lots']):
                        if st == OPEN and ((bar.high >= t['targets'][j]) if side > 0 else (bar.low <= t['targets'][j])):
                            ev = Event('target', key, j, t['targets'][j], t['targets'][j], 1)
                            break
                if ev is not None:
                    s.late_catches += 1
                    self._say('warning', f'late catch: {key} {ev.kind}{"" if ev.idx is None else f" T{ev.idx + 1}"} at {ev.trigger:.2f} seen only in the '
                                         f'{bar.ts:%H:%M} minute bar (the ticks missed it)', channel='trade-alerts')
                    q = self._price()
                    self._send_event(ev, q.price if q else ev.trigger, None)
                    return True
            s.last_minute_checked = bar.ts.isoformat()
        return False

    # ------------------------------------------------------------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------------------------------------------------------------

    def _send_event(self, ev: Event, price: float, units: Optional[int]) -> None:
        s, ref = self.state, self.contract
        setup = self.machine.setups[ev.key]
        side = setup['side']
        base = {'key': ev.key, 'kind': ev.kind, 'idx': ev.idx, 'trigger': ev.trigger, 'level': ev.level, 'seen_price': price}
        log.info('DECISION %s %s%s at trigger %.2f (level %.2f), price seen %.2f, prev scan price %s, session %s', ev.key, ev.kind,
                 '' if ev.idx is None else f' T{ev.idx + 1}', ev.trigger, ev.level, price, s.prev_price, self.session_date)
        if ev.kind == 'entry':
            lots = 3 * self.cfg.lots_per_target * units
            trade = s.trade_counter + 1
            req = OpenRequest(self._rid(trade, f'entry-{ev.key}'), ref, _dir(side), lots, trade_ref=trade)
            self._send(req, {**base, 'purpose': 'entry', 'lots': lots, 'units': units})
            return
        t = s.trades[ev.key]
        mt = self.machine.trades[ev.key]
        if ev.kind == 'stop':
            idxs = [j for j, st in enumerate(mt['lots']) if st == OPEN]
            lots = sum(t['sizes'][j] for j in idxs)
            req = CloseRequest(self._rid(t['trade_id'], 'stop'), ref, _dir(side), ExitReason.STOP_LOSS, lots=lots, trade_ref=t['trade_id'])
            self._send(req, {**base, 'purpose': 'stop', 'lots': lots})
        else:
            idxs = [ev.idx]
            if ev.idx == 2:                                              # the last target also closes any lot still open
                idxs += [j for j, st in enumerate(mt['lots']) if st == OPEN and j != 2]
            lots = sum(t['sizes'][j] for j in idxs)
            req = CloseRequest(self._rid(t['trade_id'], f'T{ev.idx + 1}'), ref, _dir(side), ExitReason.OTHER, lots=lots, trade_ref=t['trade_id'])
            self._send(req, {**base, 'purpose': 'target', 'lots': lots})

    def _send_flatten(self, reason: str) -> None:
        ref = self.contract
        trade = self.state.trade_counter
        req = FlattenRequest(self._rid(trade, f'flatten-{reason}'), ref, ExitReason.MANUAL_EXIT if reason == 'manual_exit' else ExitReason.OTHER,
                             trade_ref=trade)
        self._send(req, {'purpose': 'flatten', 'reason': reason})

    def _on_exit_command(self) -> None:
        if not self.state.trades:
            self._say('info', '`Exit Trade` received but there is no open position; still watching', channel='trade-alerts')
            return
        self._say('critical', '`Exit Trade` received: liquidating', channel='trade-alerts')
        self.exit_requested = True

    # ------------------------------------------------------------------------------------------------------------------------
    # Outcomes: the only place position state changes
    # ------------------------------------------------------------------------------------------------------------------------

    def _on_outcome(self, o: RequestOutcome) -> None:
        meta = self.state.pending.get(o.request_id)
        if meta is None:
            log.info('outcome for %s which is not pending here (already applied or from another life): %s', o.request_id, o.status)
            return
        if o.status == OutcomeStatus.UNCONFIRMED:
            self._say('critical', f'{meta["purpose"]} request {o.request_id}: fill not confirmed, position status unknown; waiting for Hestia to '
                                  f'settle it', key=f'unconfirmed-{o.request_id}')
            return
        self.state.pending.pop(o.request_id, None)
        fill = o.opened or o.closed
        log.info('OUTCOME %s %s %s lots=%s avg=%s trigger=%.2f (fill minus trigger %s) detail=%r', o.request_id, meta['purpose'], o.status.value,
                 getattr(fill, 'lots', None), getattr(fill, 'avg_price', None), meta.get('trigger', float('nan')),
                 None if fill is None or fill.avg_price is None or 'trigger' not in meta else round(fill.avg_price - meta['trigger'], 2), o.detail)
        handler = {'entry': self._done_entry, 'target': self._done_close, 'stop': self._done_close, 'flatten': self._done_flatten}.get(meta['purpose'])
        if handler is None:
            self._say('critical', f'outcome for an unknown purpose {meta["purpose"]!r}')
        else:
            handler(o, meta)
        self._save()

    def _failed(self, o: RequestOutcome, meta: dict, what: str) -> None:
        if o.status == OutcomeStatus.LIMIT_REFUSED and 'market closed' in o.detail:
            self.market_closed = True
            self._say('critical', f'{what} refused, the market is closed ({o.detail})', key=f'closed-{meta["purpose"]}')
            return
        self._say('critical', f'{what} failed ({o.status.value}: {o.detail})' + ('' if meta['purpose'] == 'entry' else '; will retry'),
                  key=f'fail-{meta["purpose"]}-{meta.get("key", "")}')
        if o.status == OutcomeStatus.REJECTED:
            self._reconcile_with_ledger()
        self.next_retry_at = self._now() + timedelta(seconds=self.cfg.retry_cooldown_s)

    def _advance(self, meta: dict) -> None:
        """An event was applied: the same move continues from its trigger price."""
        self.state.prev_price, self.state.just = meta['trigger'], meta['trigger']

    def _done_entry(self, o: RequestOutcome, meta: dict) -> None:
        s = self.state
        if not o.confirmed or o.opened is None or o.opened.lots <= 0:
            self._failed(o, meta, f'{meta["key"]} entry')
            s.missed_entries += 1
            s.prev_price = meta['seen_price']                           # the touch is given up; no repeated attempts at the same crossing
            s.just = None
            return
        key, units = meta['key'], meta['units']
        per = self.cfg.lots_per_target * units
        filled = o.opened.lots
        sizes = [max(0, min(per, filled - j * per)) for j in range(3)]
        if o.status == OutcomeStatus.PARTIAL:
            self._say('warning', f'partial fill: requested {meta["lots"]} lots, got {filled}', channel='trade-alerts')
        setup = self.machine.setups[key]
        self.machine.apply_entry(key, o.opened.avg_price, tuple(OPEN if sz else CLOSED for sz in sizes))
        s.trade_counter += 1
        now = self._now().isoformat()
        s.trades[key] = {'trade_id': s.trade_counter, 'setup': key, 'side': setup['side'], 'units': units, 'sizes': sizes, 'entry_ts': now,
                         'entry_price': o.opened.avg_price, 'level': meta['level'], 'stop': setup['stop'], 'targets': list(setup['targets']),
                         'exits': [None, None, None]}
        self._advance(meta)
        t = s.trades[key]
        self._say('info', f'Entered {_dir(t["side"]).value.upper()} {self.contract.symbol} [{key}] | Units: {units} ({filled} lot(s))  '
                          f'Entry: {t["entry_price"]:.2f} (level {t["level"]:.2f}) | SL: {t["stop"]:.2f}  T1/T2/T3: '
                          f'{t["targets"][0]:.2f} / {t["targets"][1]:.2f} / {t["targets"][2]:.2f}', channel='trade-alerts')
        self._last_trade_update = None

    def _done_close(self, o: RequestOutcome, meta: dict) -> None:
        key = meta['key']
        t = self.state.trades.get(key)
        if t is None:
            log.info('close outcome for %s, which is no longer open here', key)
            return
        if not o.confirmed or o.closed is None or o.closed.lots <= 0:
            if o.status == OutcomeStatus.FILLED and o.closed is not None and 'already flat' in o.detail:
                self._reconcile_with_ledger()
                return
            self._failed(o, meta, f'{key} {meta["kind"]}' + ('' if meta['idx'] is None else f' T{meta["idx"] + 1}'))
            return
        price = o.closed.avg_price
        if meta['kind'] == 'stop':
            for j in self.machine.apply_stop(key):
                self._record_exit(t, j, price, 'stop')
        else:
            for j in self.machine.apply_target(key, meta['idx']):
                self._record_exit(t, j, price, 'T3_flat')
            self._record_exit(t, meta['idx'], price, REASONS[meta['idx']])
        self._advance(meta)
        if self.machine.trades[key] is None:
            self._finalize_trade(key)

    def _done_flatten(self, o: RequestOutcome, meta: dict) -> None:
        s = self.state
        if not o.confirmed or o.closed is None or o.closed.lots <= 0:
            if o.status == OutcomeStatus.FILLED or (o.closed is not None and 'already flat' in o.detail):
                self._reconcile_with_ledger()
                if meta['reason'] == 'manual_exit':
                    self.exit_requested = False
                return
            self._failed(o, meta, f'the {meta["reason"]} flatten')
            return
        price = o.closed.avg_price
        reason = meta['reason']
        for key, lots in self.machine.close_all().items():
            t = s.trades.get(key)
            if t is not None:
                for j in lots:
                    self._record_exit(t, j, price, reason)
                self._finalize_trade(key)
        s.trades.clear()
        s.prev_price, s.just = price, None
        if reason == 'manual_exit':
            self.exit_requested = False
            self._say('info', 'position liquidated; still watching for the next entry', channel='trade-alerts')

    def _record_exit(self, t: dict, j: int, price: float, reason: str) -> None:
        t['exits'][j] = {'ts': self._now().isoformat(), 'price': round(price, 2), 'reason': reason}
        pts = t['side'] * (price - t['entry_price'])                  # banked so far, read by the session report while the trade is open
        t['booked_pts'] = round(t.get('booked_pts', 0.0) + pts, 2)
        t['booked_rs'] = round(t.get('booked_rs', 0.0) + pts * t['sizes'][j] * self._lot_size(), 2)

    def _finalize_trade(self, key: str) -> None:
        s = self.state
        t = s.trades.pop(key)
        lot_size = self._lot_size()
        side, entry = t['side'], t['entry_price']
        rec = {'trade_id': t['trade_id'], 'contract_expiry': s.contract_expiry, 'direction': _dir(side).value, 'units': t['units'],
               'entry_ts': t['entry_ts'], 'entry_price': entry, 'signal_ts': None, 'signal_close': t['level'],
               'entry_slippage_points': round(side * (entry - t['level']), 2), 'sl_price': round(t['stop'], 2),
               'lot1_target': round(t['targets'][0], 2), 'lot2_target': round(t['targets'][1], 2), 'lot2_target_source': 'camarilla',
               'parent_trade_id': None, 'setup': key, 'lot3_target': round(t['targets'][2], 2)}
        total_pts = total_rs = 0.0
        for j in range(3):
            ex = t['exits'][j]
            if ex is None or not t['sizes'][j]:
                continue
            pts = round(side * (ex['price'] - entry), 2)
            rs = round(pts * t['sizes'][j] * lot_size, 2)
            n = j + 1
            rec.update({f'lot{n}_exit_ts': ex['ts'], f'lot{n}_exit_price': ex['price'], f'lot{n}_exit_reason': ex['reason'],
                        f'lot{n}_pnl_points': pts, f'lot{n}_pnl_rs': rs})
            total_pts += pts
            total_rs += rs
        rec['total_pnl_points'], rec['total_pnl_rs'] = round(total_pts, 2), round(total_rs, 2)
        self.ctx.report_running_row(self._running_row(t, self._price().price if self._price() else entry, exit_reason=t['exits'][2]['reason'] if t['exits'][2] else 'closed'))
        self.ctx.report_trade(rec)
        self._say('info', f'Trade #{t["trade_id"]} [{key}] closed.  (Units: {t["units"]})  P&L: {total_pts:+.2f} pts  Rs.{total_rs / t["units"]:+,.0f}/unit  '
                          f'exits: ' + ', '.join(f'lot{j + 1} {t["exits"][j]["reason"]} @{t["exits"][j]["price"]:.2f}' for j in range(3) if t['exits'][j]),
                  channel='trade-alerts')

    # ------------------------------------------------------------------------------------------------------------------------
    # Running rows and the trade-update ticker (one open trade at a time)
    # ------------------------------------------------------------------------------------------------------------------------

    def _marks(self, t: dict, ltp: float):
        side, entry = t['side'], t['entry_price']
        pts = []
        for j in range(3):
            ex = t['exits'][j]
            px = ex['price'] if ex else ltp
            pts.append(round(side * (px - entry), 2) if t['sizes'][j] else 0.0)
        rs = [round(p * t['sizes'][j] * self._lot_size(), 2) for j, p in enumerate(pts)]
        return pts, rs

    def _running_row(self, t: dict, ltp: float, exit_reason: Optional[str] = None) -> dict:
        pts, rs = self._marks(t, ltp)
        now = self._now()
        entry_ts = datetime.fromisoformat(t['entry_ts'])
        return {'trade_id': t['trade_id'], 'entry_ts': t['entry_ts'], 'ts': now.isoformat(), 'minutes_since_entry': int((now - entry_ts).total_seconds() // 60),
                'ltp': ltp, 'sl_price': t['stop'], 'lot1_target': t['targets'][0], 'lot2_target': t['targets'][1], 'lot1_pnl_points': pts[0],
                'lot1_pnl_rs': rs[0], 'lot2_pnl_points': pts[1], 'lot2_pnl_rs': rs[1], 'total_pnl_points': round(sum(pts), 2),
                'total_pnl_rs': round(sum(rs), 2), 'exit_reason': exit_reason}

    def _maybe_send_running_row(self, now: datetime) -> None:
        if self._last_running_row is not None and (now - self._last_running_row).total_seconds() < self.cfg.running_row_sec:
            return
        self._last_running_row = now
        q = self._price()
        if q is None:
            return
        for t in self.state.trades.values():
            self.ctx.report_running_row(self._running_row(t, q.price))

    def _maybe_send_trade_update(self, now: datetime) -> None:
        if self._last_trade_update is not None and (now - self._last_trade_update).total_seconds() < self.cfg.trade_update_sec:
            return
        self._last_trade_update = now
        q = self._price()
        if q is None:
            return
        for t in self.state.trades.values():
            pts, rs = self._marks(t, q.price)
            u = t['units'] or 1
            booked = sum(rs[j] for j in range(3) if t['exits'][j])
            msg = (f'{_dir(t["side"]).value.upper()}  {self.contract.symbol} [{t["setup"]}]  Units: {u}\n'
                   f'Entry: {t["entry_price"]:.2f}  LTP: {q.price:.2f}\n'
                   f'Realised: Rs.{booked / u:+,.0f}/unit  Total: {sum(pts):+.2f} pts  Rs.{sum(rs) / u:+,.0f}/unit')
            self.ctx.alert('info', msg, channel='trade-updates', log_locally=False)


def build() -> OleusEngine:
    return OleusEngine()
