"""
The order-update WebSocket (Angel One SmartWebSocketOrderUpdate), ported from prometheus_functions.OrderFillWatcher
(itself from Iris): the fast path for learning that an order filled. It only records terminal order updates; the order
adapter falls back to REST `orderBook` polling when the socket is not ready or silent, and Hestia has exactly one of these
per process. Connecting needs real session tokens, so nothing here connects on import or in tests: tests call `handle`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Dict, List, Optional

try:
    from SmartApi.smartWebSocketOrderUpdate import SmartWebSocketOrderUpdate
    _WS_AVAILABLE = True
except Exception:                                           # pragma: no cover - only without the package installed
    SmartWebSocketOrderUpdate = object
    _WS_AVAILABLE = False

log = logging.getLogger('hestia_order_feed')

TERMINAL_STATUSES = ('AB05', 'AB02', 'AB03')                # complete, cancelled, rejected update codes


class OrderUpdateFeed(SmartWebSocketOrderUpdate):

    def __init__(self):
        if _WS_AVAILABLE:
            self.wsapp = None
            self.last_pong_timestamp = None
            self.current_retry_attempt = 0
            self.auth_token = self.api_key = self.client_code = self.feed_token = None
        self._ws_ready = threading.Event()
        self.live_orders: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._subscribers: List[Callable[[dict], None]] = []
        self.last_message_ts: Optional[float] = None

    # -- what the order adapter uses -----------------------------------------------------------------------------------
    def ready(self) -> bool:
        return self._ws_ready.is_set()

    def get(self, order_id: str) -> Optional[dict]:
        with self._lock:
            return self.live_orders.get(str(order_id))

    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._subscribers.append(fn)

    # -- connection (live only) ----------------------------------------------------------------------------------------
    def start(self, auth_token, api_key, client_code, feed_token) -> None:
        if not _WS_AVAILABLE:
            log.warning('SmartWebSocketOrderUpdate not available: REST order polling only.')
            return
        self.auth_token, self.api_key = auth_token, api_key
        self.client_code, self.feed_token = client_code, feed_token
        threading.Thread(target=self._run, daemon=True, name='OrderUpdateFeed').start()

    def _run(self):
        try:
            self.connect()
        except Exception:
            pass

    def on_open(self, wsapp):
        pass

    def on_message(self, wsapp, message):
        self.handle(message)

    def on_data(self, wsapp, message, data_type, continue_flag):
        self.handle(message)

    def on_pong(self, wsapp, data):
        heartbeat = getattr(self, 'HEARTBEAT_MESSAGE', None)
        if heartbeat and data == heartbeat:
            self.last_pong_timestamp = time.time()
        else:
            self.handle(data)

    def on_error(self, wsapp, error):
        pass

    def on_close(self, wsapp, close_status_code, close_msg):
        self._ws_ready.clear()
        if _WS_AVAILABLE:
            self.retry_connect()

    # -- parsing (called by the socket callbacks, and directly by tests) ------------------------------------------------
    def handle(self, raw) -> None:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
        except Exception:
            return
        self.last_message_ts = time.time()
        status = parsed.get('order-status')
        if status == 'AB00':
            self._ws_ready.set()
            return
        if status in TERMINAL_STATUSES:
            od = parsed.get('orderData')
            oid = od.get('orderid') if od else None
            if oid:
                with self._lock:
                    self.live_orders[str(oid)] = od
                for fn in list(self._subscribers):
                    try:
                        fn(od)
                    except Exception:
                        log.exception('order-update subscriber failed')
