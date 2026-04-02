"""
websocket_feed.py — Apollo Production WebSocket Feed
Wraps SmartWebSocketV2 with thread management, shared state, and clean shutdown.

Public interface (all apollo.py ever calls):
    feed.start(smart_connect_obj, auth_token, user_name)
    feed.subscribe_options(buy_token, sell_token)
    feed.unsubscribe_options(buy_token, sell_token)
    feed.get_ltp(token)            -> float | None
    feed.get_ohlc(token)           -> dict {open, high, low, close} | None
    feed.is_connected()            -> bool
    feed.resubscribe_all()
    feed.stop()

Design principles:
    - Single daemon thread runs sws.connect() — main thread never blocks
    - threading.Lock protects all shared state reads and writes
    - OHLC aggregated from LTP ticks per token — resets on every get_ohlc() call
    - Own subscription registry fixes SDK RESUBSCRIBE_FLAG bug
    - Shutdown: sock.close() -> close_connection() -> join(5s) -> ctypes fallback
    - No Slack messaging — caller's responsibility
    - No strategy logic — pure feed layer
"""

import ctypes
import threading
import time

from SmartApi.smartWebSocketV2 import SmartWebSocketV2

# ---------------------------------------------------------------------------
# Exchange type constants
# ---------------------------------------------------------------------------
EXCHANGE_NSE_CM = 1   # NSE Cash / Index  — Nifty index, VIX
EXCHANGE_NSE_FO = 2   # NSE F&O           — Nifty options

# Subscription mode
MODE_LTP = 1

# Correlation ID used for all subscribe/unsubscribe calls
_CORRELATION_ID = "apollo_feed"

# Index tokens — fixed, never change
NIFTY_TOKEN = "99926000"
VIX_TOKEN   = "99926017"


class ApolloFeed:
    """
    WebSocket feed manager for Apollo live execution.

    Lifecycle:
        feed = ApolloFeed()
        feed.start(obj, auth_token, user_name)    # called once at session start
        feed.subscribe_options(buy_tok, sell_tok) # called after entry
        feed.unsubscribe_options(buy_tok, sell_tok) # called after exit
        feed.stop()                               # called at session end
    """

    def __init__(self):
        self._sws          = None
        self._ws_thread    = None
        self._lock         = threading.Lock()

        # LTP shared state — written by WS thread, read by main thread
        self._ltp          = {}   # {token: float}
        self._tick_count   = {}   # {token: int}
        self._connected    = False

        # OHLC aggregation state — one window per token, resets on get_ohlc()
        # Structure: {token: {open, high, low, close, has_tick}}
        self._ohlc         = {}

        # Subscription registry — our source of truth, fixes SDK RESUBSCRIBE_FLAG bug
        self._subscribed_index   = set()
        self._subscribed_options = set()

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def start(self, smart_connect_obj, auth_token, user_name):
        """
        Initialise and start the WebSocket feed.

        Parameters
        ----------
        smart_connect_obj : SmartConnect
            Authenticated SmartConnect instance.
            api_key read from smart_connect_obj.api_key.
            Feed token fetched via getfeedToken().
        auth_token : str
            JWT token from login response (data['data']['jwtToken']).
        user_name : str
            Angel One client code — required by SmartWebSocketV2.__init__().
        """
        api_key    = smart_connect_obj.api_key
        feed_token = smart_connect_obj.getfeedToken()

        self._sws = SmartWebSocketV2(
            auth_token, api_key, user_name, feed_token,
            max_retry_attempt=0   # no auto-reconnect — apollo.py decides
        )

        self._sws.on_open  = self._on_open
        self._sws.on_data  = self._on_data
        self._sws.on_error = self._on_error
        self._sws.on_close = self._on_close

        self._ws_thread = threading.Thread(
            target=self._sws.connect,
            name="apollo-ws-feed",
            daemon=True
        )
        self._ws_thread.start()

        # Wait up to 10s for connection to establish
        deadline = time.time() + 10
        while not self.is_connected() and time.time() < deadline:
            time.sleep(0.1)

        if not self.is_connected():
            raise RuntimeError(
                "WebSocket feed did not connect within 10 seconds. "
                "Check auth_token, feed_token, and network connectivity."
            )

    def subscribe_options(self, buy_token, sell_token):
        """
        Subscribe to LTP feed for both option legs after entry.
        Initialises a fresh OHLC window for each token.
        """
        tokens = [t for t in [buy_token, sell_token]
                  if t not in self._subscribed_options]
        if not tokens:
            return

        token_list = [{"exchangeType": EXCHANGE_NSE_FO, "tokens": tokens}]
        self._sws.subscribe(_CORRELATION_ID, MODE_LTP, token_list)

        with self._lock:
            for t in tokens:
                self._subscribed_options.add(t)
                self._ohlc[t] = self._empty_ohlc()

    def unsubscribe_options(self, buy_token, sell_token):
        """
        Unsubscribe option leg tokens after exit.
        Clears LTP and OHLC entries from shared state.
        """
        tokens = [t for t in [buy_token, sell_token]
                  if t in self._subscribed_options]
        if not tokens:
            return

        token_list = [{"exchangeType": EXCHANGE_NSE_FO, "tokens": tokens}]
        self._sws.unsubscribe(_CORRELATION_ID, MODE_LTP, token_list)

        with self._lock:
            for t in tokens:
                self._subscribed_options.discard(t)
                self._ltp.pop(t, None)
                self._tick_count.pop(t, None)
                self._ohlc.pop(t, None)

    def get_ltp(self, token):
        """
        Get the last traded price for a token.
        Returns float or None if no tick received yet.
        """
        with self._lock:
            return self._ltp.get(token)

    def get_ohlc(self, token):
        """
        Get the OHLC aggregated from ticks since the last call for this token,
        and reset the window for the next interval.

        Returns dict with keys {open, high, low, close} or None if no ticks
        have been received since the last reset.

        Called every TRADE_UPDATE_INTERVAL seconds from apollo.py.
        """
        with self._lock:
            window = self._ohlc.get(token)
            if window is None or not window['has_tick']:
                return None
            result = {
                'open':  window['open'],
                'high':  window['high'],
                'low':   window['low'],
                'close': window['close'],
            }
            # Reset window for next interval
            self._ohlc[token] = self._empty_ohlc()
            return result

    def get_tick_count(self, token):
        """Return total ticks received for a token."""
        with self._lock:
            return self._tick_count.get(token, 0)

    def is_connected(self):
        """True if the WebSocket connection is open and on_open has fired."""
        with self._lock:
            return self._connected

    def resubscribe_all(self):
        """
        Resubscribe all currently tracked tokens using our own registry.
        Call from apollo.py after a reconnect. Fixes SDK RESUBSCRIBE_FLAG bug:
        only currently-wanted tokens are resubscribed, not stale ones.
        """
        with self._lock:
            index_tokens  = list(self._subscribed_index)
            option_tokens = list(self._subscribed_options)

        if index_tokens:
            self._sws.subscribe(
                _CORRELATION_ID, MODE_LTP,
                [{"exchangeType": EXCHANGE_NSE_CM, "tokens": index_tokens}]
            )
        if option_tokens:
            self._sws.subscribe(
                _CORRELATION_ID, MODE_LTP,
                [{"exchangeType": EXCHANGE_NSE_FO, "tokens": option_tokens}]
            )

    def stop(self):
        """
        Shut down the WebSocket feed cleanly.

        Sequence:
          1. Close underlying socket to unblock C-level recv()
          2. Call close_connection() for SDK clean path
          3. Join thread with 5s timeout
          4. ctypes hard kill as fallback if thread survives
        """
        if self._ws_thread is None:
            return

        try:
            if self._sws.wsapp and self._sws.wsapp.sock:
                self._sws.wsapp.sock.close()
        except Exception:
            pass

        try:
            self._sws.close_connection()
        except Exception:
            pass

        self._ws_thread.join(timeout=5)
        if not self._ws_thread.is_alive():
            return

        self._ctypes_kill(self._ws_thread)
        self._ws_thread.join(timeout=3)

    # -----------------------------------------------------------------------
    # WebSocket callbacks — run on the WS thread
    # -----------------------------------------------------------------------

    def _on_open(self, wsapp):
        with self._lock:
            self._connected = True

        # Subscribe Nifty index and VIX immediately on connect
        # Initialise OHLC windows for index tokens
        token_list = [{"exchangeType": EXCHANGE_NSE_CM,
                       "tokens": [NIFTY_TOKEN, VIX_TOKEN]}]
        self._sws.subscribe(_CORRELATION_ID, MODE_LTP, token_list)

        with self._lock:
            for t in [NIFTY_TOKEN, VIX_TOKEN]:
                self._subscribed_index.add(t)
                self._ohlc[t] = self._empty_ohlc()

    def _on_data(self, wsapp, message):
        """
        Tick handler — fast, no blocking.
        Updates LTP and OHLC window for every tick received.
        Decodes last_traded_price (raw int / 100 = actual price).
        """
        try:
            token = message.get("token")
            raw   = message.get("last_traded_price")
            if token is None or raw is None:
                return

            ltp = float(raw) / 100.0

            with self._lock:
                # Update LTP
                self._ltp[token]        = ltp
                self._tick_count[token] = self._tick_count.get(token, 0) + 1

                # Update OHLC window
                if token not in self._ohlc:
                    self._ohlc[token] = self._empty_ohlc()

                window = self._ohlc[token]
                if not window['has_tick']:
                    # First tick in this interval — set open
                    window['open']     = ltp
                    window['high']     = ltp
                    window['low']      = ltp
                    window['close']    = ltp
                    window['has_tick'] = True
                else:
                    if ltp > window['high']:
                        window['high'] = ltp
                    if ltp < window['low']:
                        window['low']  = ltp
                    window['close']    = ltp

        except Exception:
            pass   # never let a tick handler exception crash the WS thread

    def _on_error(self, wsapp, error):
        with self._lock:
            self._connected = False

    def _on_close(self, wsapp):
        with self._lock:
            self._connected = False

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _empty_ohlc():
        """Return a fresh empty OHLC window."""
        return {'open': None, 'high': None, 'low': None, 'close': None, 'has_tick': False}

    @staticmethod
    def _ctypes_kill(thread):
        """
        Inject SystemExit into a thread via CPython internals.
        sock.close() above ensures thread is not blocked in C recv().
        """
        if not thread.is_alive():
            return
        exc = ctypes.py_object(SystemExit)
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_long(thread.ident), exc)
        if res > 1:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_long(thread.ident), None)