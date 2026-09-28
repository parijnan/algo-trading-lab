"""
AngelBrokerPort: the BrokerPort over Angel One's SmartConnect, through the gateway (hestia_core/gateway.py).

Ported from prometheus_functions.place_order / get_fill_price_and_qty (not imported: those modules read import-time
configuration and log into the real dated file). What it keeps: the per-contract lot size (not a global), freeze-quantity
chunking, ghost-order recovery guarded by the ids this process placed itself, the market-hours refusal, WebSocket fills with
REST fallback. What moved to the core: retrying a rejection (the core owns retry policy and priority, this adapter makes ONE
attempt and reports what happened), and interpreting an unconfirmed order (the core asks `read_order`).

Blocking work runs on the executor; every result is handed back with `scheduler.post`, so the core stays single-threaded.
Paper trading is not here: paper engines are routed to PaperBroker by the BrokerRouter.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from hestia_core.gateway import BrokerGateway, TRANSPORT_ERRORS, is_rate_limit, is_session_failure
from hestia_core.interface import ContractInfo, ContractRef
from hestia_core.ports import OrderRead, OrderSpec, PlaceResult, PositionRow

log = logging.getLogger('hestia_angel')

TERMINAL = ('complete', 'rejected', 'cancelled')
LIVE_HIGH_PRIORITY = 2                                      # PriorityClass.CLOSE and below use the reserved lane


@dataclass
class AngelConfig:
    exchange: str = 'MCX'
    product: str = 'CARRYFORWARD'
    order_timeout_s: float = 30.0          # how long one attempt waits for a fill before reporting it unconfirmed
    poll_interval_s: float = 1.0
    ws_first_s: float = 5.0                # give the order-update socket this long before also polling REST
    rate_limit_cooldown_s: float = 2.0
    ghost_cooldown_s: float = 2.0
    ghost_lookback_s: float = 60.0
    max_transport_retries: int = 6         # placement retries after a lost response (each preceded by a ghost search)
    cash_max_age_s: float = 120.0          # older than this, free_cash reports 0 so entries fail closed
    cash_refresh_s: float = 30.0


def _row_status(o: dict) -> str:
    return str(o.get('status') or o.get('orderstatus') or '').strip().lower()


def _row_fill(o: dict) -> Tuple[int, float]:
    status = _row_status(o)
    qty = o.get('filledshares')
    if qty in (None, ''):
        qty = o.get('quantity', 0) if status == 'complete' else 0
    return int(float(qty or 0)), float(o.get('averageprice') or 0.0)


class AngelBrokerPort:

    def __init__(self, gateway: BrokerGateway, scheduler, executor, lot_size_by_token: Callable[[str], Optional[int]],
                 info_by_ref: Callable[[ContractRef], Optional[ContractInfo]], config: Optional[AngelConfig] = None,
                 feed=None, wall_now: Callable[[], datetime] = datetime.now,
                 closing_at: Callable[[], Optional[datetime]] = lambda: None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 alert: Optional[Callable[[str, str], None]] = None):
        self.gateway, self.scheduler, self.executor = gateway, scheduler, executor
        self.cfg = config or AngelConfig()
        self._lot_size_by_token, self._info_by_ref = lot_size_by_token, info_by_ref
        self._feed, self._wall_now, self._closing_at = feed, wall_now, closing_at
        self._sleep, self._clock = sleep, clock
        self._alert = alert or (lambda level, text: log.log(logging.CRITICAL if level == 'critical' else logging.WARNING, text))
        self._placed_ids: set = set()                       # ghost-recovery collision guard, shared by every engine
        self._watched: Dict[str, str] = {}                  # order id -> composite id of an unconfirmed attempt
        self._lot_by_order: Dict[str, int] = {}             # order id -> the contract's lot size, for shares -> lots
        self._listener = None
        self._cash: Optional[float] = None
        self._cash_ts: Optional[float] = None
        self._lock = threading.Lock()
        self.session_failed = False
        if feed is not None:
            feed.subscribe(self._on_feed_update)

    # ---- BrokerPort bookkeeping ----------------------------------------------------------------------------------------

    def register_engine(self, name: str, paper: bool) -> None:
        if paper:
            raise ValueError('AngelBrokerPort serves live engines only; route paper engines with a BrokerRouter')

    def pool_of(self, engine: str) -> str:
        return 'live'

    def set_order_listener(self, fn) -> None:
        self._listener = fn

    # ---- cash ------------------------------------------------------------------------------------------------------------

    def free_cash(self, engine: str = '') -> float:
        with self._lock:
            cash, ts = self._cash, self._cash_ts
        if cash is None or ts is None or self._clock() - ts > self.cfg.cash_max_age_s:
            return 0.0                                      # fail closed: an entry cannot be sized against a stale balance
        return cash

    def refresh_cash_blocking(self) -> None:
        try:
            try:
                value = float(self.gateway.rms()['data']['availablecash'])
            except Exception as exc:                        # one retry: an isolated AB1007 blip is not a session failure
                log.warning('rmsLimit failed (%s); retrying once', exc)
                self._sleep(1.0)
                value = float(self.gateway.rms()['data']['availablecash'])
        except Exception as exc:
            self._alert('warning', f'could not refresh available cash: {exc}')
            return
        with self._lock:
            self._cash, self._cash_ts = value, self._clock()

    def start_cash_refresh(self) -> None:
        """Refresh now and every `cash_refresh_s`, on the executor."""
        def tick():
            self.executor.submit(self.refresh_cash_blocking)
            self.scheduler.after(self.cfg.cash_refresh_s, tick)
        tick()

    # ---- placement -------------------------------------------------------------------------------------------------------

    def place(self, spec: OrderSpec, on_result, on_placed=None) -> None:
        fut = self.executor.submit(self._place_blocking, spec, on_placed)

        def done(f):
            try:
                res = f.result()
            except Exception as exc:                        # noqa: BLE001 - never let an unexpected error cause a blind re-send
                self._alert('critical', f'unexpected error placing {spec.request_id}: {exc!r}; treating the order as unconfirmed')
                res = PlaceResult('unconfirmed', detail=repr(exc))
            self.scheduler.post(lambda: on_result(res))
        fut.add_done_callback(done)

    def _place_blocking(self, spec: OrderSpec, on_placed=None) -> PlaceResult:
        closing = self._closing_at()
        if closing is not None and self._wall_now() >= closing:
            return PlaceResult('rejected', detail=f'refused: at or after the session close ({closing:%H:%M})')
        info = self._info_by_ref(spec.contract)
        if info is None:
            return PlaceResult('rejected', detail='refused: unknown contract')
        high = spec.priority <= LIVE_HIGH_PRIORITY
        freeze = max(1, info.freeze_qty_lots)
        chunks, remaining = [], spec.lots
        while remaining > 0:
            chunks.append(min(remaining, freeze))
            remaining -= chunks[-1]

        order_ids: List[str] = []
        for lots in chunks:
            outcome, value = self._place_chunk(spec, info, lots, high)
            if outcome == 'ok':
                order_ids.append(value)
            elif outcome == 'rejected':
                break                                       # later chunks are not sent; what was placed is settled below
            else:                                           # 'lost': a placement that may or may not exist and cannot be found
                composite = ','.join(order_ids) or None
                self._alert('critical', f'{spec.request_id}: an order may exist but could not be found; '
                                        f'not re-sending. Check the order book.')
                return PlaceResult('unconfirmed', order_id=composite, detail=value)
        if not order_ids:
            return PlaceResult('rejected', detail=value if chunks else 'nothing to place')
        composite = ','.join(order_ids)
        if on_placed is not None:                           # the order exists: let the core journal its id before the fill wait
            self.scheduler.post(lambda: on_placed(composite))
        settled = self._await(order_ids, info.lot_size, high)
        if settled is None:
            for oid in order_ids:
                self._watched[oid] = composite
            return PlaceResult('unconfirmed', order_id=composite, detail='fill not confirmed in time')
        lots_filled, price = settled
        if lots_filled == 0:
            return PlaceResult('rejected', detail='the exchange rejected or cancelled the order')
        return PlaceResult('filled' if lots_filled == spec.lots else 'partial', lots_filled, price, composite)

    def _place_chunk(self, spec: OrderSpec, info: ContractInfo, lots: int, high: bool) -> Tuple[str, Optional[str]]:
        side, symbol, token = spec.side, spec.contract.symbol, spec.contract.token
        qty = int(lots * info.lot_size)
        params = {'variety': 'NORMAL', 'tradingsymbol': symbol, 'symboltoken': token, 'transactiontype': side,
                  'exchange': self.cfg.exchange, 'ordertype': 'MARKET', 'producttype': self.cfg.product,
                  'duration': 'DAY', 'quantity': str(qty), 'price': '0', 'triggerprice': '0'}
        transport_failures, rate_limited = 0, 0
        while True:
            try:
                resp = self.gateway.place_order(params, high=high)
            except TRANSPORT_ERRORS as exc:
                if is_rate_limit(exc):                      # the request never reached the broker: safe to re-send
                    rate_limited += 1
                    if rate_limited > 20:
                        return 'lost', f'rate limited {rate_limited} times'
                    self._sleep(self.cfg.rate_limit_cooldown_s)
                    continue
                transport_failures += 1
                self._sleep(self.cfg.ghost_cooldown_s)
                found = self._find_ghost(symbol, side, qty)
                if found:
                    log.info('ghost order recovered for %s: %s', symbol, found)
                    self._lot_by_order[found] = info.lot_size
                    return 'ok', found
                if transport_failures > self.cfg.max_transport_retries:
                    return 'lost', f'{transport_failures} lost responses and no matching order'
                continue
            except Exception as exc:                        # noqa: BLE001
                if is_session_failure(exc):
                    self.session_failed = True
                    self._alert('critical', f'session failure placing {symbol}: {exc}')
                    return 'rejected', f'session failure: {exc}'
                return 'rejected', f'placement failed: {exc}'
            if (resp or {}).get('message') == 'SUCCESS':
                oid = resp['data']['orderid']
                self._placed_ids.add(oid)
                self._lot_by_order[oid] = info.lot_size
                return 'ok', oid
            return 'rejected', (resp or {}).get('message', 'unknown broker error')

    def _find_ghost(self, symbol: str, side: str, qty: int) -> Optional[str]:
        try:
            book = (self.gateway.order_book(high=True) or {}).get('data') or []
        except Exception as exc:                            # noqa: BLE001
            log.warning('order book check failed during ghost recovery: %s', exc)
            return None
        now = self._wall_now()
        for o in book:
            if not (o.get('tradingsymbol') == symbol and o.get('transactiontype') == side
                    and int(float(o.get('quantity', 0))) == qty
                    and _row_status(o) in ('complete', 'open', 'validation pending')):
                continue
            oid = o.get('orderid')
            if not oid or oid in self._placed_ids:
                continue
            try:
                fresh = (now - datetime.strptime(o['updatetime'], '%d-%b-%Y %H:%M:%S')).total_seconds() < self.cfg.ghost_lookback_s
            except Exception:                               # noqa: BLE001
                fresh = False
            if fresh:
                self._placed_ids.add(oid)
                return oid
        return None

    # ---- fills and settling ------------------------------------------------------------------------------------------------

    def _rows(self, order_ids: List[str], use_rest: bool, high: bool) -> Dict[str, dict]:
        rows: Dict[str, dict] = {}
        if self._feed is not None and self._feed.ready():
            for oid in order_ids:
                od = self._feed.get(oid)
                if od:
                    rows[oid] = od
        if use_rest and len(rows) < len(order_ids):
            try:
                book = (self.gateway.order_book(high=high) or {}).get('data') or []
                by_id = {str(o.get('orderid')): o for o in book}
                for oid in order_ids:
                    if oid not in rows and oid in by_id:
                        rows[oid] = by_id[oid]
            except Exception as exc:                        # noqa: BLE001
                log.warning('orderBook poll failed: %s', exc)
        return rows

    @staticmethod
    def _aggregate(order_ids: List[str], rows: Dict[str, dict], lot_size: int) -> Optional[Tuple[int, Optional[float]]]:
        """(lots filled, average price) once every order is terminal; None while any is still working or missing."""
        total_qty, total_val = 0, 0.0
        for oid in order_ids:
            o = rows.get(oid)
            if o is None:
                return None
            if _row_status(o) and _row_status(o) not in TERMINAL:
                return None
            qty, avg = _row_fill(o)
            total_qty += qty
            total_val += avg * qty
        lots = total_qty // lot_size
        return lots, (round(total_val / total_qty, 2) if total_qty else None)

    def _await(self, order_ids: List[str], lot_size: int, high: bool) -> Optional[Tuple[int, Optional[float]]]:
        start = self._clock()
        deadline = start + self.cfg.order_timeout_s
        while True:
            use_rest = self._feed is None or not self._feed.ready() or self._clock() - start >= self.cfg.ws_first_s
            agg = self._aggregate(order_ids, self._rows(order_ids, use_rest, high), lot_size)
            if agg is not None:
                return agg
            if self._clock() >= deadline:
                return None
            self._sleep(self.cfg.poll_interval_s)

    def read_order(self, order_id: Optional[str], on_result) -> None:
        fut = self.executor.submit(self._read_blocking, order_id)

        def done(f):
            try:
                res = f.result()
            except Exception as exc:                        # noqa: BLE001
                log.warning('read_order failed: %s', exc)
                res = OrderRead('pending')                  # an unreadable order is never reported as 'no fill'
            self.scheduler.post(lambda: on_result(res))
        fut.add_done_callback(done)

    def _read_blocking(self, order_id: Optional[str]) -> OrderRead:
        if not order_id:
            return OrderRead('pending')
        ids = order_id.split(',')
        rows = self._rows(ids, True, True)
        lot_size = self._lot_by_order.get(ids[0])
        for o in rows.values():                             # after a restart the order is not ours in memory: use its token
            lot_size = lot_size or self._lot_size_by_token(str(o.get('symboltoken', '')))
        agg = self._aggregate(ids, rows, lot_size or 1)
        if agg is None:
            return OrderRead('pending')
        lots, price = agg
        return OrderRead('complete', lots, price)

    def _on_feed_update(self, od: dict) -> None:
        composite = self._watched.get(str(od.get('orderid')))
        if composite is None or self._listener is None:
            return

        def deliver():
            ids = composite.split(',')
            rows = self._rows(ids, False, True)
            lot_size = (self._lot_by_order.get(ids[0]) or self._lot_size_by_token(str(od.get('symboltoken', ''))) or 1)
            agg = self._aggregate(ids, rows, lot_size)
            if agg is not None:
                for oid in ids:
                    self._watched.pop(oid, None)
                self._listener(composite, OrderRead('complete', agg[0], agg[1]))
        self.scheduler.post(deliver)

    # ---- the position book -------------------------------------------------------------------------------------------------

    def read_positions(self, on_result) -> None:
        fut = self.executor.submit(self._positions_blocking)

        def done(f):
            try:
                res = f.result()
            except Exception as exc:                        # noqa: BLE001
                log.warning('position read failed: %s', exc)
                res = None
            self.scheduler.post(lambda: on_result(res))
        fut.add_done_callback(done)

    def _positions_blocking(self) -> Dict[str, PositionRow]:
        data = (self.gateway.positions(high=True) or {}).get('data') or []
        out: Dict[str, PositionRow] = {}
        for p in data:
            token = str(p.get('symboltoken') or '')
            if not token:
                continue
            exchange = str(p.get('exchange') or '')
            if exchange and exchange.upper() != self.cfg.exchange.upper():
                continue                                     # the account's other segments (an ETF sold intraday) are not Hestia's
            lot = self._lot_size_by_token(token) or 1
            net = int(float(p.get('netqty', 0) or 0)) // lot
            avg = float(p.get('netprice') or 0.0) or None
            out[token] = PositionRow(net, avg)
        return out
