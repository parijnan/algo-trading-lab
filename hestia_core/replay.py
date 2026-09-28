"""
The simulated ports for the fake Hestia: replayed market data (DataPort) and a scripted broker (BrokerPort).

ReplayData builds 15-minute bars from 1-minute frames anchored at each day's first minute (as production does) and computes
Supertrend once per (contract, engine spec) with hestia_core.indicators; bars are revealed at their boundary (Supertrend is
causal, so precomputing changes nothing an engine can see). SimBroker fills whatever a behaviour function scripts.

Modelling notes, deliberately simple and stated so nobody mistakes them for market truth:
- price at time t is the minute's open in the first half of the minute and its close in the second half; `set_price` and
  `schedule_price` override it for scripted tests;
- a PARTIAL bar reports fewer minutes but keeps the true OHLC and Supertrend; a RECOVERED bar is the true bar delivered late;
- a provisional bar takes its OHLC from the true bar unless `provisional_override` says otherwise;
- fills happen at the price source's price at the fill instant, plus `slippage_points` against the trader.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from hestia_core import trading_calendar as hcal
from hestia_core.indicators import compute_st
from hestia_core.interface import (
    Bar, BarComplete, BarQuality, ContractInfo, ContractRef, Direction, DplFrozen, FeedRecovered, FeedStale, LtpQuote,
    ProvisionalBar, SupertrendPoint, TrackFailed, TrackReady,
)
from hestia_core.ledger import apply_fill
from hestia_core.ports import OrderRead, OrderSpec, PlaceResult, PositionRow

BAR_MIN = 15


# ---------------------------------------------------------------------------------------------------------------------
# Scripted broker
# ---------------------------------------------------------------------------------------------------------------------

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
    pending_for: Optional[float] = None     # unconfirmed only: the order stays 'working' (open) for this many seconds after placement


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


class SimBroker:
    """BrokerPort whose behaviour is scripted. Keeps an account book (net lots and average per token) that its own fills
    move, which is what `free_cash` and reconciliation read."""

    def __init__(self, kernel, behavior: Callable[[BrokerCall], BrokerReply], price_fn: Callable[[str], Optional[float]],
                 margin_fn: Callable[[str, int, float], float], cash: float, slippage: float, unconfirmed_after_s: float,
                 ghost_recovery_s: float, reconcile_latency_s: float):
        self.kernel, self.behavior, self.price_fn, self.margin_fn = kernel, behavior, price_fn, margin_fn
        self.cash, self.slippage = cash, slippage
        self.unconfirmed_after_s, self.ghost_recovery_s, self.reconcile_latency_s = (
            unconfirmed_after_s, ghost_recovery_s, reconcile_latency_s)
        self.orders: List[PlacedOrder] = []
        self.rejections: List[Tuple[datetime, str, str]] = []
        self.book: Dict[str, list] = {}                 # token -> [net_lots, avg_price]
        self._order_seq = itertools.count(1)
        self._hidden: Dict[str, tuple] = {}             # order_id -> (truth_lots, spec, placed_ts, pending_for)
        self._listener = None

    def set_order_listener(self, fn) -> None:
        self._listener = fn

    def register_engine(self, name: str, paper: bool) -> None:
        if paper:
            raise ValueError('SimBroker is the live-side simulator; wrap it in a BrokerRouter for paper engines')

    def pool_of(self, engine: str) -> str:
        return 'live'

    def read_positions(self, on_result) -> None:
        rows = {tok: PositionRow(net, avg) for tok, (net, avg) in self.book.items() if net}
        self.kernel.after(self.reconcile_latency_s, lambda: on_result(rows))

    def seed(self, token: str, net: int, avg: float) -> None:
        self.book[token] = [net, avg]

    def free_cash(self, engine: str = '') -> float:
        used = sum(self.margin_fn(tok, net, avg) for tok, (net, avg) in self.book.items() if net and avg is not None)
        return self.cash - used

    def _px(self, spec: OrderSpec) -> Optional[float]:
        price = self.price_fn(spec.contract.token)
        if price is None:
            return None
        return price + (1 if spec.side == 'BUY' else -1) * self.slippage

    def _move_book(self, spec: OrderSpec, lots: int, price: Optional[float]) -> None:
        if not lots or price is None:
            return
        net, avg = self.book.get(spec.contract.token, [0, None])
        self.book[spec.contract.token] = list(apply_fill(net, avg, (1 if spec.side == 'BUY' else -1) * lots, price))

    def place(self, spec: OrderSpec, on_result, on_placed=None) -> None:
        reply = self.behavior(BrokerCall(spec.engine, spec.request, spec.attempt, spec.lots, spec.close_lots, spec.open_lots))
        if reply.kind == 'reject':
            self.rejections.append((self.kernel.now, spec.engine, spec.request_id))
            self.kernel.after(reply.latency, lambda: on_result(PlaceResult('rejected', detail='scripted rejection')))
            return
        order = PlacedOrder(f'ORD{next(self._order_seq):05d}', self.kernel.now, spec.engine, spec.request_id, spec.contract,
                            spec.side, spec.lots, spec.attempt, spec.priority)
        self.orders.append(order)
        if on_placed is not None:
            on_placed(order.order_id)
        if reply.kind == 'unconfirmed':
            truth = spec.lots if reply.lots is None else max(0, min(reply.lots, spec.lots))
            self._hidden[order.order_id] = (truth, spec, self.kernel.now, reply.pending_for)
            self._move_book(spec, truth, self._px(spec))
            fast = reply.resolve_after is not None and reply.resolve_after < self.unconfirmed_after_s and truth > 0
            if fast:                                      # the order-update feed beat Hestia's timeout: an ordinary fill
                def report():
                    kind = 'filled' if truth == spec.lots else 'partial'
                    on_result(PlaceResult(kind, truth, self._px(spec), order.order_id))
                self.kernel.after(reply.resolve_after, report)
                return
            self.kernel.after(self.unconfirmed_after_s,
                              lambda: on_result(PlaceResult('unconfirmed', order_id=order.order_id)))
            if reply.resolve_after is not None:
                self.kernel.after(reply.resolve_after, lambda: self._listener and self._listener(order.order_id,
                                                                                                 self._read(order.order_id)))
            return

        def finish():
            full = reply.kind in ('fill', 'ghost')
            lots = spec.lots if full else max(0, min(reply.lots or 0, spec.lots))
            price = self._px(spec)
            self._move_book(spec, lots, price)
            on_result(PlaceResult('filled' if full else 'partial', lots, price, order.order_id))
        self.kernel.after(reply.latency + (self.ghost_recovery_s if reply.kind == 'ghost' else 0.0), finish)

    def _read(self, order_id: str) -> OrderRead:
        truth, spec, placed, pending_for = self._hidden[order_id]
        if pending_for is not None and (self.kernel.now - placed).total_seconds() < pending_for:
            return OrderRead('pending')
        return OrderRead('complete', truth, self._px(spec) if truth else None)

    def read_order(self, order_id: str, on_result) -> None:
        self.kernel.after(self.reconcile_latency_s, lambda: on_result(self._read(order_id)))


# ---------------------------------------------------------------------------------------------------------------------
# Replayed data
# ---------------------------------------------------------------------------------------------------------------------

@dataclass
class ContractSpec:
    ref: ContractRef
    lot_size: int = 1
    tick_size: float = 1.0
    freeze_qty_lots: int = 10
    minutes: Optional[pd.DataFrame] = None      # columns: time_stamp, open, high, low, close, volume


class ReplayData:

    def __init__(self, kernel, holidays: Optional[set] = None):
        self.kernel = kernel
        self.holidays = set(holidays or ())
        self.core = None
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
        self.session_date: Optional[date] = None
        self.session_open: Optional[datetime] = None
        self.session_close: Optional[datetime] = None

    def attach(self, core) -> None:
        self.core = core

    # ---- setup and scenario ----------------------------------------------------------------------------------------

    def add_contract(self, spec: ContractSpec) -> None:
        self._contracts[spec.ref.token] = spec
        self._visible.setdefault(spec.ref.token, 0)

    def set_price(self, token: str, price: Optional[float]) -> None:
        if price is None:
            self._prices.pop(token, None)
        else:
            self._prices[token] = price

    def schedule_price(self, token: str, when: datetime, price: float) -> None:
        self.kernel.at(when, lambda: self.set_price(token, price))

    def inject_feed_stale(self, engine: str, contract: ContractRef, start: datetime, end: datetime) -> None:
        def go_stale():
            self._stale[contract.token] = start
            self.core.deliver_to(engine, FeedStale(contract, age_sec=(self.kernel.now - start).total_seconds()))

        def recover():
            self._stale.pop(contract.token, None)
            self.core.deliver_to(engine, FeedRecovered(contract))
        self.kernel.at(start, go_stale)
        self.kernel.at(end, recover)

    def inject_dpl_freeze(self, engine: str, contract: ContractRef, when: datetime, price: float, frozen: bool) -> None:
        self.kernel.at(when, lambda: self.core.deliver_to(engine, DplFrozen(contract, price, frozen)))

    # ---- session ---------------------------------------------------------------------------------------------------

    def begin_session(self, session_date: date, session_open: Optional[datetime] = None) -> None:
        """Set the session, reveal the seed bars and schedule the day's boundaries. The core launches the engines."""
        self.session_date = session_date
        tokens = self._tokens_with_data()
        opens = [self._first_minute(t, session_date) for t in tokens]
        opens = [o for o in opens if o is not None]
        self.session_open = session_open or (min(opens) if opens else datetime.combine(session_date, time(9, 0)))
        for token in tokens:
            bars = self._bars_for(token)
            self._visible[token] = int((bars['time_stamp'] + timedelta(minutes=BAR_MIN) <= self.session_open).sum())
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
        self.session_close = max(closes).to_pydatetime() if closes else None

    # ---- DataPort --------------------------------------------------------------------------------------------------

    def knows(self, token: str) -> bool:
        s = self._contracts.get(token)
        return s is not None

    def infos(self, instrument: str) -> Tuple[ContractInfo, ...]:
        today = (self.session_date or self.kernel.now.date())
        out = []
        for spec in sorted(self._contracts.values(), key=lambda s: s.ref.expiry):
            if spec.ref.instrument != instrument:
                continue
            left = hcal.count_trading_days_inclusive(today, spec.ref.expiry, self.holidays)
            out.append(ContractInfo(spec.ref, spec.lot_size, spec.tick_size, spec.freeze_qty_lots, left))
        return tuple(out)

    def info(self, ref: ContractRef) -> Optional[ContractInfo]:
        for i in self.infos(ref.instrument):
            if i.ref.token == ref.token:
                return i
        return None

    def ref_for(self, token: str) -> Optional[ContractRef]:
        s = self._contracts.get(token)
        return None if s is None else s.ref

    def seeded(self, instrument: str) -> Tuple[ContractRef, ...]:
        return tuple(i.ref for i in self.infos(instrument) if self._visible.get(i.ref.token, 0) > 0)

    def price(self, token: str) -> Optional[float]:
        return self._price_at(token, self.kernel.now)

    def ltp_quote(self, token: str) -> Optional[LtpQuote]:
        price = self.price(token)
        if price is None:
            return None
        since = self._stale.get(token)
        now = self.kernel.now
        return LtpQuote(price, since or now, (now - since).total_seconds() if since else 0.0)

    def _rows(self, token: str, spec, last_n: Optional[int]):
        s = self._contracts.get(token)
        if s is None or s.minutes is None:
            return []
        st = self._st_for(token, spec)
        bars = self._bars_for(token)
        v = self._visible.get(token, 0)
        lo = 0 if last_n is None else max(0, v - last_n)
        return [(self._bar(bars.iloc[i]), self._point(st.iloc[i])) for i in range(lo, v)]

    def latest_bar(self, token: str, spec):
        rows = self._rows(token, spec, 1)
        return rows[-1] if rows else None

    def st_series(self, token: str, spec, last_n: int):
        return tuple(self._rows(token, spec, last_n))

    def price_near(self, token: str, ts: datetime, tolerance_min: int) -> Optional[float]:
        s = self._contracts.get(token)
        if s is None or s.minutes is None:
            return None
        m = s.minutes
        window = m[(m['time_stamp'] >= pd.Timestamp(ts) - timedelta(minutes=tolerance_min))
                   & (m['time_stamp'] <= pd.Timestamp(ts) + timedelta(minutes=tolerance_min))]
        if window.empty:
            return None
        nearest = (window['time_stamp'] - pd.Timestamp(ts)).abs().idxmin()
        return float(window.loc[nearest, 'close'])

    def track(self, task, contract: ContractRef) -> None:
        core = self.core
        s = self._contracts.get(contract.token)
        if s is not None and s.minutes is not None:
            task.tracked.add(contract.token)
            self.kernel.after(1.0, lambda: core.deliver_to(task.name, TrackReady(contract)))
        else:
            self.kernel.after(1.0, lambda: core.deliver_to(task.name, TrackFailed(contract, 'no data for contract')))

    def untrack(self, task, contract: ContractRef) -> None:
        task.tracked.discard(contract.token)

    # ---- bars ------------------------------------------------------------------------------------------------------

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
        for task in self.core.traders_of(token):
            self.core.deliver(task, BarComplete(self._contracts[token].ref, boundary, None, None, None, BarQuality.GAP, 0))

    def _release_bar(self, token: str, boundary: datetime, idx: int, quality: BarQuality, reconciles: bool) -> None:
        self._visible[token] = max(self._visible.get(token, 0), idx)
        bars = self._bars_for(token)
        row_pos = idx - 1
        for task in self.core.traders_of(token):
            spec = task.engine.spec
            st = self._st_for(token, spec)
            row, prev = st.iloc[row_pos], (st.iloc[row_pos - 1] if row_pos >= 1 else None)
            prev_st = None if prev is None or pd.isna(prev['supertrend']) else float(prev['supertrend'])
            partial = self.bar_partial.get((token, boundary))
            minutes = int(partial if partial else bars.iloc[row_pos]['minutes'])
            self.core.deliver(task, BarComplete(self._contracts[token].ref, boundary, self._bar(bars.iloc[row_pos]),
                                                self._point(row), prev_st, quality, minutes, reconciles))

    def _send_provisional(self, token: str, boundary: datetime, idx: int) -> None:
        bars = self._bars_for(token)
        true = bars.iloc[idx - 1].copy()
        override = self.provisional_override.get((token, boundary), {})
        for k, v in override.items():
            true[k] = v
        true['high'] = max(true['high'], true['open'], true['close'])
        true['low'] = min(true['low'], true['open'], true['close'])
        for task in self.core.traders_of(token):
            spec = task.engine.spec
            if not spec.provisional.enabled:
                continue
            visible = bars.iloc[:idx - 1]
            combined = pd.concat([visible, pd.DataFrame([true])], ignore_index=True)
            st = compute_st(combined, spec.st_period, spec.st_multiplier)
            prev_st = None
            if len(st) >= 2 and not pd.isna(st.iloc[-2]['supertrend']):
                prev_st = float(st.iloc[-2]['supertrend'])
            self.core.deliver(task, ProvisionalBar(self._contracts[token].ref, boundary, self._bar(true),
                                                   self._point(st.iloc[-1]), prev_st))
