"""
websocket_feed.py — Apollo Production WebSocket Feed
Wraps SmartWebSocketV2 with thread management, shared state, and clean shutdown.

Public interface (all apollo.py ever calls):
    feed.start(smart_connect_obj, auth_token, user_name)
    feed.subscribe_options(buy_token, sell_token)
    feed.unsubscribe_options(buy_token, sell_token)
    feed.get_ltp(token)       -> float | None
    feed.is_connected()       -> bool
    feed.stop()

Design principles:
    - Single daemon thread runs sws.connect() — main thread never blocks
    - threading.Lock protects all shared state reads and writes
    - Own subscription registry fixes SDK RESUBSCRIBE_FLAG bug:
      we track what is currently subscribed so any future reconnect
      logic in apollo.py can resubscribe exactly the right tokens
    - Shutdown: sock.close() -> close_connection() -> join(5s) -> ctypes fallback
    - No Slack messaging — caller's responsibility
    - No strategy logic — pure feed layer
"""

import ctypes
import threading
import time
from datetime import datetime

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
        feed.start(obj, auth_token, user_name)   # called once at session start
        feed.subscribe_options(buy_tok, sell_tok) # called after entry
        feed.unsubscribe_options(buy_tok, sell_tok) # called after exit
        feed.stop()                               # called at session end
    """

    def __init__(self):
        self._sws          = None         # SmartWebSocketV2 instance
        self._ws_thread    = None         # daemon thread running sws.connect()
        self._lock         = threading.Lock()

        # Shared state — written by WS thread, read by main thread
        self._ltp          = {}           # {token: float}
        self._tick_count   = {}           # {token: int}
        self._connected    = False        # True once on_open fires

        # Subscription registry — our source of truth for what is subscribed.
        # Fixes SDK RESUBSCRIBE_FLAG bug: SDK tracks subscriptions in
        # input_request_dict but sets RESUBSCRIBE_FLAG=True on unsubscribe,
        # so a reconnect would resubscribe unsubscribed tokens. We maintain
        # our own registry so apollo.py can call feed.resubscribe_all() on
        # reconnect with exactly the right tokens.
        self._subscribed_index   = set()  # index tokens currently subscribed
        self._subscribed_options = set()  # option tokens currently subscribed

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def start(self, smart_connect_obj, auth_token, user_name):
        """
        Initialise and start the WebSocket feed.

        Parameters
        ----------
        smart_connect_obj : SmartConnect
            Authenticated SmartConnect instance. api_key read from
            smart_connect_obj.api_key. Feed token fetched via getfeedToken().
        auth_token : str
            JWT token from the login response (data['data']['jwtToken']).
        user_name : str
            Angel One client code — required by SmartWebSocketV2.__init__().
        """
        api_key    = smart_connect_obj.api_key
        feed_token = smart_connect_obj.getfeedToken()

        self._sws = SmartWebSocketV2(
            auth_token, api_key, user_name, feed_token,
            max_retry_attempt=0   # no auto-reconnect — apollo.py decides
        )

        # Wire callbacks
        self._sws.on_open  = self._on_open
        self._sws.on_data  = self._on_data
        self._sws.on_error = self._on_error
        self._sws.on_close = self._on_close

        # Start feed in a daemon thread — dies automatically if main exits
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

        Parameters
        ----------
        buy_token : str
            Instrument token for the ITM (buy) leg.
        sell_token : str
            Instrument token for the OTM (sell) leg.
        """
        tokens = [t for t in [buy_token, sell_token]
                  if t not in self._subscribed_options]
        if not tokens:
            return   # already subscribed — nothing to do

        token_list = [{"exchangeType": EXCHANGE_NSE_FO, "tokens": tokens}]
        self._sws.subscribe(_CORRELATION_ID, MODE_LTP, token_list)

        with self._lock:
            self._subscribed_options.update(tokens)

    def unsubscribe_options(self, buy_token, sell_token):
        """
        Unsubscribe option leg tokens after exit.
        Also clears their LTP entries from shared state.
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

    def get_ltp(self, token):
        """
        Get the last traded price for a token.

        Returns
        -------
        float or None
            None if no tick has been received yet for this token.
        """
        with self._lock:
            return self._ltp.get(token)

    def get_tick_count(self, token):
        """Return the total number of ticks received for a token."""
        with self._lock:
            return self._tick_count.get(token, 0)

    def is_connected(self):
        """True if the WebSocket connection is open and on_open has fired."""
        with self._lock:
            return self._connected

    def resubscribe_all(self):
        """
        Resubscribe all currently tracked tokens.
        Call this from apollo.py if a reconnect is needed after a network drop.
        Uses our own registry — not the SDK's input_request_dict — so only
        currently-wanted tokens are resubscribed.
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
          1. Close the underlying socket to unblock C-level recv()
          2. Call close_connection() for the SDK clean path
          3. Join thread with 5s timeout
          4. ctypes hard kill as fallback if thread survives
        """
        if self._ws_thread is None:
            return

        # Step 1: Unblock the C recv() call
        try:
            if self._sws.wsapp and self._sws.wsapp.sock:
                self._sws.wsapp.sock.close()
        except Exception:
            pass

        # Step 2: SDK close
        try:
            self._sws.close_connection()
        except Exception:
            pass

        # Step 3: Wait for clean exit
        self._ws_thread.join(timeout=5)
        if not self._ws_thread.is_alive():
            return

        # Step 4: ctypes hard kill
        self._ctypes_kill(self._ws_thread)
        self._ws_thread.join(timeout=3)

    # -----------------------------------------------------------------------
    # WebSocket callbacks — run on the WS thread
    # -----------------------------------------------------------------------

    def _on_open(self, wsapp):
        with self._lock:
            self._connected = True

        # Subscribe Nifty index and VIX immediately on connect
        token_list = [{"exchangeType": EXCHANGE_NSE_CM,
                       "tokens": [NIFTY_TOKEN, VIX_TOKEN]}]
        self._sws.subscribe(_CORRELATION_ID, MODE_LTP, token_list)

        with self._lock:
            self._subscribed_index.update([NIFTY_TOKEN, VIX_TOKEN])

    def _on_data(self, wsapp, message):
        """
        Tick handler — must be fast, no blocking operations.
        Decodes last_traded_price (raw int / 100 = actual price).
        """
        try:
            token = message.get("token")
            raw   = message.get("last_traded_price")
            if token is not None and raw is not None:
                ltp = float(raw) / 100.0
                with self._lock:
                    self._ltp[token]        = ltp
                    self._tick_count[token] = self._tick_count.get(token, 0) + 1
        except Exception:
            pass   # never let a tick handler exception crash the WS thread

    def _on_error(self, wsapp, error):
        with self._lock:
            self._connected = False
        # Caller (apollo.py) detects disconnection via is_connected() == False
        # and decides whether to attempt a restart. We do not auto-reconnect here.

    def _on_close(self, wsapp):
        with self._lock:
            self._connected = False

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _ctypes_kill(thread):
        """
        Inject SystemExit into a thread via CPython internals.
        Only reaches the thread if it is executing Python bytecode —
        sock.close() above ensures it is no longer blocked in C recv().
        """
        if not thread.is_alive():
            return
        exc = ctypes.py_object(SystemExit)
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_long(thread.ident), exc)
        if res > 1:
            # Too many threads affected — undo immediately
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_long(thread.ident), None)