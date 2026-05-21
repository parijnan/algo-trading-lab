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
    feed.get_last_tick_age(token)        -> float | None  (seconds since last tick)
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
import logging
import threading
import time

logger = logging.getLogger(__name__)

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

# Tick error debouncing — alert after N errors, then silence for cooldown period
_TICK_ERROR_ALERT_THRESHOLD = 10
_TICK_ERROR_COOLDOWN_S      = 300   # 5 minutes

# Stale tick watchdog — alert if a subscribed token has no tick for this long
# Covers illiquid contracts (e.g. deep OTM PE wing) and zombie connection scenarios.
# Broker sends ping/pong every 10s so a live connection with no ticks means illiquidity.
_STALE_TICK_THRESHOLD_S = 120   # 2 minutes
_STALE_ALERT_COOLDOWN_S = 300   # re-alert cooldown per token


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
        self._subscribed_options = {}    # {token: exchange_type_int}

        # Tokens to subscribe immediately on connect
        self._startup_tokens = []   # list of (exchange_type_int, token_str)

        # Reconnect state
        self._stop_requested         = False
        self._reconnect_thread       = None
        self._max_reconnect_attempts = 5
        self._alert_callback         = None

        # Saved connection params — used to rebuild SmartWebSocketV2 on reconnect
        self._auth_token  = None
        self._api_key     = None
        self._client_code = None
        self._feed_token  = None

        # Tick error tracking — written by WS thread only, no lock needed
        self._tick_errors          = 0
        self._tick_err_since_alert = 0
        self._last_err_alert_ts    = 0.0

        # Stale tick watchdog state
        self._last_tick_ts  = {}   # {token: float} — time.time() at last tick
        self._stale_alerted = {}   # {token: float} — time.time() of last stale alert
        self._watchdog_thread = None

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def start(self, auth_token, api_key, client_code, feed_token, startup_tokens=None, alert_callback=None):
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
        self._startup_tokens  = startup_tokens or []
        self._alert_callback  = alert_callback
        self._stop_requested  = False       # Reset on each start

        # Save for reconnect
        self._auth_token  = auth_token
        self._api_key     = api_key
        self._client_code = client_code
        self._feed_token  = feed_token

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

        self._watchdog_thread = threading.Thread(
            target=self._watchdog_worker, name="ws-feed-watchdog", daemon=True)
        self._watchdog_thread.start()

    def subscribe_options(self, tokens, exchange_type=None):
        """
        Subscribe to LTP feed for option leg tokens after entry.
        Initialises a fresh OHLC window for each token.

        Parameters
        ----------
        tokens        : list of str
        exchange_type : int, optional
            Exchange type constant (default: EXCHANGE_NSE_FO).
            Pass EXCHANGE_BSE_FO for Artemis (Sensex BFO options).
        """
        if exchange_type is None:
            exchange_type = EXCHANGE_NSE_FO
        new_tokens = [t for t in tokens if t not in self._subscribed_options]
        if not new_tokens:
            return

        token_list = [{"exchangeType": exchange_type, "tokens": new_tokens}]
        self._sws.subscribe(_CORRELATION_ID, MODE_LTP, token_list)

        with self._lock:
            for t in new_tokens:
                self._subscribed_options[t] = exchange_type
                self._ohlc[t] = self._empty_ohlc()

    def unsubscribe_all_options(self):
        """Unsubscribe all currently-subscribed option tokens."""
        with self._lock:
            by_exchange = {}
            for tok, exch in self._subscribed_options.items():
                by_exchange.setdefault(exch, []).append(tok)
        for exch, tokens in by_exchange.items():
            self.unsubscribe_options(tokens, exch)

    def unsubscribe_options(self, tokens, exchange_type=None):
        """
        Unsubscribe option leg tokens after exit.
        Clears LTP and OHLC entries from shared state.

        Parameters
        ----------
        tokens        : list of str
        exchange_type : int, optional
            Exchange type constant (default: EXCHANGE_NSE_FO).
            Pass EXCHANGE_BSE_FO for Artemis (Sensex BFO options).
        """
        if exchange_type is None:
            exchange_type = EXCHANGE_NSE_FO
        current = [t for t in tokens if t in self._subscribed_options]
        if not current:
            return

        token_list = [{"exchangeType": exchange_type, "tokens": current}]
        self._sws.unsubscribe(_CORRELATION_ID, MODE_LTP, token_list)

        with self._lock:
            for t in current:
                self._subscribed_options.pop(t, None)
                self._ltp.pop(t, None)
                self._tick_count.pop(t, None)
                self._ohlc.pop(t, None)
                self._last_tick_ts.pop(t, None)
                self._stale_alerted.pop(t, None)

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

    def get_tick_errors(self):
        """Return total tick decode error count since feed started."""
        return self._tick_errors

    def get_last_tick_age(self, token):
        """
        Seconds elapsed since the last tick for this token.
        Returns None if no tick has ever been received (normal before market open).
        Useful for detecting illiquid contracts — if the broker's ping/pong is
        healthy but this returns > 120s for an option leg, the contract has no trades.
        """
        with self._lock:
            ts = self._last_tick_ts.get(token)
        return None if ts is None else time.time() - ts

    def is_connected(self):
        """True if the WebSocket connection is open and on_open has fired."""
        with self._lock:
            return self._connected

    def resubscribe_all(self):
        """
        Resubscribe all currently tracked tokens using our own registry.
        Call after a reconnect. Fixes SDK RESUBSCRIBE_FLAG bug:
        only currently-wanted tokens are resubscribed, not stale ones.
        Groups option tokens by exchange type so BSE (Artemis) and NSE options
        are resubscribed to the correct exchange after reconnect.
        """
        with self._lock:
            index_tokens    = list(self._subscribed_index)
            options_by_exch = {}
            for tok, exch in self._subscribed_options.items():
                options_by_exch.setdefault(exch, []).append(tok)

        if index_tokens:
            self._sws.subscribe(
                _CORRELATION_ID, MODE_LTP,
                [{"exchangeType": EXCHANGE_NSE_CM, "tokens": index_tokens}]
            )
        for exch, tokens in options_by_exch.items():
            self._sws.subscribe(
                _CORRELATION_ID, MODE_LTP,
                [{"exchangeType": exch, "tokens": tokens}]
            )

    def stop(self):
        """
        Shut down the WebSocket feed cleanly.

        Sequence:
          1. Set _stop_requested to suppress reconnect attempts
          2. Close underlying socket to unblock C-level recv()
          3. Call close_connection() for SDK clean path
          4. Join thread with 5s timeout
          5. ctypes hard kill as fallback if thread survives
        """
        self._stop_requested = True
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
                self._last_tick_ts[token] = time.time()

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
            # Never let a tick handler exception crash the WS thread.
            # Count errors and fire a debounced alert if they pile up.
            self._tick_errors += 1
            self._tick_err_since_alert += 1
            now = time.time()
            if (self._tick_err_since_alert >= _TICK_ERROR_ALERT_THRESHOLD and
                    now - self._last_err_alert_ts >= _TICK_ERROR_COOLDOWN_S):
                self._tick_err_since_alert = 0
                self._last_err_alert_ts    = now
                if self._alert_callback:
                    try:
                        self._alert_callback(
                            f"⚠️ SharedFeed: {_TICK_ERROR_ALERT_THRESHOLD}+ tick decode "
                            f"errors detected — possible malformed data from broker.")
                    except Exception:
                        pass

    def _on_error(self, wsapp, error):
        with self._lock:
            self._connected = False
        if not self._stop_requested:
            self._trigger_reconnect()

    def _on_close(self, wsapp):
        with self._lock:
            self._connected = False
        if not self._stop_requested:
            self._trigger_reconnect()

    # -----------------------------------------------------------------------
    # Reconnect logic
    # -----------------------------------------------------------------------

    def _trigger_reconnect(self):
        """Spawn a reconnect worker thread if one is not already running."""
        with self._lock:
            if self._reconnect_thread and self._reconnect_thread.is_alive():
                return
        t = threading.Thread(target=self._reconnect_worker, name="ws-reconnect", daemon=True)
        with self._lock:
            self._reconnect_thread = t
        t.start()

    def _reconnect_worker(self):
        """
        Attempt to reconnect with exponential backoff (max _max_reconnect_attempts).
        On success: resubscribes all previously registered tokens.
        On exhaustion: fires alert_callback and gives up — REST fallback remains active.
        """
        _BACKOFF = [5, 10, 20, 40, 60]

        for attempt in range(self._max_reconnect_attempts):
            delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            logger.warning(
                f"SharedFeed: WS disconnected. "
                f"Reconnect attempt {attempt + 1}/{self._max_reconnect_attempts} in {delay}s.")

            # Interruptible sleep — exits promptly if stop() is called
            end = time.time() + delay
            while time.time() < end:
                if self._stop_requested:
                    return
                time.sleep(0.5)

            if self._stop_requested:
                return

            try:
                new_sws = SmartWebSocketV2(
                    self._auth_token, self._api_key,
                    self._client_code, self._feed_token,
                    max_retry_attempt=0
                )
                new_sws.on_open  = self._on_open
                new_sws.on_data  = self._on_data
                new_sws.on_error = self._on_error
                new_sws.on_close = self._on_close

                new_thread = threading.Thread(
                    target=new_sws.connect, name="shared-ws-feed", daemon=True)

                # Swap sws BEFORE starting thread so _on_open uses the new instance
                with self._lock:
                    self._sws       = new_sws
                    self._ws_thread = new_thread
                new_thread.start()

                # Wait up to 10s for _on_open to fire
                deadline = time.time() + 10
                while not self.is_connected() and time.time() < deadline:
                    if self._stop_requested:
                        return
                    time.sleep(0.1)

                if self.is_connected():
                    msg = f"WS reconnected after {attempt + 1} attempt(s). Resubscribing."
                    logger.info(f"SharedFeed: {msg}")
                    if self._alert_callback:
                        try:
                            self._alert_callback(f"✅ SharedFeed: {msg}")
                        except Exception:
                            pass
                    self.resubscribe_all()
                    return

                logger.warning(
                    f"SharedFeed: Reconnect attempt {attempt + 1} — no connect within 10s.")

            except Exception as exc:
                logger.warning(
                    f"SharedFeed: Reconnect attempt {attempt + 1} failed: {exc}")

        # All attempts exhausted
        msg = (f"WS reconnect failed after {self._max_reconnect_attempts} attempts "
               f"— REST fallback active.")
        logger.error(f"SharedFeed: {msg}")
        if self._alert_callback:
            try:
                self._alert_callback(f"⚠️ SharedFeed: {msg}")
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Stale tick watchdog
    # -----------------------------------------------------------------------

    def _watchdog_worker(self):
        """
        Background thread: checks all subscribed tokens every ~30s for stale ticks.
        Only fires when is_connected() is True — a live connection with no ticks
        on a subscribed token indicates an illiquid contract, not a feed failure.
        (Feed failures trigger on_close/on_error and are handled by _reconnect_worker.)

        Alerts via alert_callback, debounced per token by _STALE_ALERT_COOLDOWN_S.
        """
        while not self._stop_requested:
            for _ in range(60):   # 60 × 0.5s = 30s between checks
                if self._stop_requested:
                    return
                time.sleep(0.5)

            if not self.is_connected():
                continue

            now = time.time()
            with self._lock:
                check_tokens = list(self._subscribed_index) + list(self._subscribed_options)

            for token in check_tokens:
                with self._lock:
                    ts         = self._last_tick_ts.get(token)
                    last_alert = self._stale_alerted.get(token, 0.0)
                if ts is None:
                    continue   # never ticked — normal before market open
                age = now - ts
                if (age >= _STALE_TICK_THRESHOLD_S and
                        now - last_alert >= _STALE_ALERT_COOLDOWN_S):
                    with self._lock:
                        self._stale_alerted[token] = now
                    logger.warning(
                        f"SharedFeed: No tick for token {token} in {age:.0f}s "
                        f"— possible illiquid contract.")
                    if self._alert_callback:
                        try:
                            self._alert_callback(
                                f"No tick for token {token} in {age:.0f}s "
                                f"— possible illiquid contract or stale feed.")
                        except Exception:
                            pass

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
