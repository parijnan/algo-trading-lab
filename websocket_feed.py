"""
websocket_feed.py — Shared WebSocket LTP Feed
Wraps SmartWebSocketV2 with thread management, shared state, and clean shutdown.
Used by Apollo, Athena, and Artemis.

Public interface:
    feed.start(auth_token, api_key, client_code, feed_token, startup_tokens=None)
    feed.subscribe_options(tokens)       # list of token strings
    feed.unsubscribe_options(tokens)     # list of token strings
    feed.get_ltp(token)                  -> float | None
    feed.get_ohlc(token)                 -> dict {open, high, low, close} | None
    feed.is_connected()                  -> bool
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
EXCHANGE_BSE_CM = 3   # BSE Cash / Index  — Sensex
EXCHANGE_BSE_FO = 4   # BSE F&O           — Sensex options

# Subscription mode
MODE_LTP = 1

# Correlation ID used for all subscribe/unsubscribe calls
_CORRELATION_ID = "shared_feed"

# Well-known index tokens — re-exported for caller convenience
NIFTY_TOKEN = "99926000"
VIX_TOKEN   = "99926017"


class SharedFeed:
    """
    WebSocket feed manager. Strategy-agnostic — works for Apollo, Athena, Artemis.

    Lifecycle:
        feed = SharedFeed()
        feed.start(auth_token, api_key, client_code, feed_token,
                   startup_tokens=[(EXCHANGE_NSE_CM, NIFTY_TOKEN), ...])
        feed.subscribe_options([buy_tok, sell_tok])   # after entry
        feed.unsubscribe_options([buy_tok, sell_tok]) # after exit
        feed.stop()                                   # session end
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

        # Tokens to subscribe immediately on connect
        self._startup_tokens = []   # list of (exchange_type_int, token_str)

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def start(self, auth_token, api_key, client_code, feed_token, startup_tokens=None):
        """
        Initialise and start the WebSocket feed.

        Parameters
        ----------
        auth_token     : str
            JWT token from login response (data['data']['jwtToken']).
        api_key        : str
            Angel One API key.
        client_code    : str
            Angel One client code (user name).
        feed_token     : str
            Feed token from SmartConnect.getfeedToken().
        startup_tokens : list of (int, str), optional
            Exchange/token pairs to subscribe immediately on connect.
            e.g. [(EXCHANGE_NSE_CM, NIFTY_TOKEN), (EXCHANGE_NSE_CM, VIX_TOKEN)]
        """
        self._startup_tokens = startup_tokens or []

        self._sws = SmartWebSocketV2(
            auth_token, api_key, client_code, feed_token,
            max_retry_attempt=0   # no auto-reconnect — caller decides
        )

        self._sws.on_open  = self._on_open
        self._sws.on_data  = self._on_data
        self._sws.on_error = self._on_error
        self._sws.on_close = self._on_close

        self._ws_thread = threading.Thread(
            target=self._sws.connect,
            name="shared-ws-feed",
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

    def subscribe_options(self, tokens):
        """
        Subscribe to LTP feed for option leg tokens after entry.
        Initialises a fresh OHLC window for each token.

        Parameters
        ----------
        tokens : list of str
        """
        new_tokens = [t for t in tokens if t not in self._subscribed_options]
        if not new_tokens:
            return

        token_list = [{"exchangeType": EXCHANGE_NSE_FO, "tokens": new_tokens}]
        self._sws.subscribe(_CORRELATION_ID, MODE_LTP, token_list)

        with self._lock:
            for t in new_tokens:
                self._subscribed_options.add(t)
                self._ohlc[t] = self._empty_ohlc()

    def unsubscribe_all_options(self):
        """Unsubscribe all currently-subscribed option tokens."""
        with self._lock:
            tokens = list(self._subscribed_options)
        if tokens:
            self.unsubscribe_options(tokens)

    def unsubscribe_options(self, tokens):
        """
        Unsubscribe option leg tokens after exit.
        Clears LTP and OHLC entries from shared state.

        Parameters
        ----------
        tokens : list of str
        """
        current = [t for t in tokens if t in self._subscribed_options]
        if not current:
            return

        token_list = [{"exchangeType": EXCHANGE_NSE_FO, "tokens": current}]
        self._sws.unsubscribe(_CORRELATION_ID, MODE_LTP, token_list)

        with self._lock:
            for t in current:
                self._subscribed_options.discard(t)
                self._ltp.pop(t, None)
                self._tick_count.pop(t, None)
                self._ohlc.pop(t, None)

    def get_ltp(self, token):
        """Return last traded price for a token, or None if no tick yet."""
        with self._lock:
            return self._ltp.get(token)

    def get_ohlc(self, token):
        """
        Get OHLC aggregated from ticks since the last call for this token,
        and reset the window for the next interval.

        Returns dict {open, high, low, close} or None if no ticks received.
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
        Call after a reconnect. Fixes SDK RESUBSCRIBE_FLAG bug:
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

        if not self._startup_tokens:
            return

        # Group startup tokens by exchange type for a single subscribe call each
        by_exchange = {}
        for exchange_type, token in self._startup_tokens:
            by_exchange.setdefault(exchange_type, []).append(token)

        for exchange_type, tokens in by_exchange.items():
            self._sws.subscribe(
                _CORRELATION_ID, MODE_LTP,
                [{"exchangeType": exchange_type, "tokens": tokens}]
            )
            with self._lock:
                for t in tokens:
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
                self._ltp[token]        = ltp
                self._tick_count[token] = self._tick_count.get(token, 0) + 1

                if token not in self._ohlc:
                    self._ohlc[token] = self._empty_ohlc()

                window = self._ohlc[token]
                if not window['has_tick']:
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
