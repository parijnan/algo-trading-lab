"""
functions.py — Apollo Production Utility Functions
Slack messaging, Telegram messaging, and exception handling.

Mirrors the structure of Artemis functions.py.
Imports credentials from configs_live — loaded once at module level.
"""

import json
import threading

from os.path import exists
from requests import get, post
from re import sub
from time import sleep, time
from traceback import format_exc
from datetime import datetime

from SmartApi.smartWebSocketOrderUpdate import SmartWebSocketOrderUpdate
from configs_live import (
    slack_token, bot_token, bot_id, channel_id,
    SLACK_ERRORS_CHANNEL,
    DATA_DIR, ORDER_LIMIT,
    RMS_POLL_LIMIT, ORDER_BOOK_POLL_LIMIT, LTP_POLL_LIMIT, CANDLE_POLL_LIMIT
)
from logger_setup import get_logger

logger = get_logger('apollo.functions')

# ---------------------------------------------------------------------------
# Hardened Rate Limit Counters
# ---------------------------------------------------------------------------
_counters = {
    'rms': {'count': 0, 'limit': RMS_POLL_LIMIT, 'last_reset': 0},
    'order_book': {'count': 0, 'limit': ORDER_BOOK_POLL_LIMIT, 'last_reset': 0},
    'ltp': {'count': 0, 'limit': LTP_POLL_LIMIT, 'last_reset': 0},
    'candle': {'count': 0, 'limit': CANDLE_POLL_LIMIT, 'last_reset': 0},
    'order': {'count': 0, 'limit': ORDER_LIMIT, 'last_reset': 0}
}

def _check_limit(key):
    global _counters
    now = time()
    c = _counters[key]
    
    # Self-healing: If 1s has passed, this specific bucket is fresh
    if now - c['last_reset'] > 1.0:
        c['count'] = 0
        c['last_reset'] = now
    
    # If we are about to EXCEED the limit, sleep and reset EVERY bucket
    if c['count'] >= c['limit']:
        # print(f"Rate limit hit for {key.upper()}. Enforcing 1.1s cooldown...")
        sleep(1.1)
        _reset_counters()
        return

    c['count'] += 1

def _increment_rms_poll(): _check_limit('rms')
def _increment_order_book_poll(): _check_limit('order_book')
def _increment_ltp_poll(): _check_limit('ltp')
def _increment_candle_poll(): _check_limit('candle')
def _increment_order(): _check_limit('order')

def _reset_counters():
    """Manual reset of all counters — call after any significant sleep."""
    global _counters
    now = time()
    for k in _counters:
        _counters[k]['count'] = 0
        _counters[k]['last_reset'] = now


class OrderFillWatcher(SmartWebSocketOrderUpdate):
    """
    Background daemon for Angel One order update WebSocket.
    Captures AB05 (complete), AB02 (cancelled), AB03 (rejected) events
    into live_orders keyed by orderid. _ws_ready is set on AB00 ack.
    Strategy polls live_orders instead of orderBook() for fill verification.
    """

    _TERMINAL_STATUSES = ('AB05', 'AB02', 'AB03')

    def __init__(self):
        # Skip parent __init__ — it runs logzero with a relative path
        self.wsapp                  = None
        self.last_pong_timestamp    = None
        self.current_retry_attempt  = 0
        self.auth_token             = None
        self.api_key                = None
        self.client_code            = None
        self.feed_token             = None
        self._ws_ready              = threading.Event()
        self.live_orders            = {}
        self._lock                  = threading.Lock()

    def start(self, auth_token, api_key, client_code, feed_token):
        self.auth_token  = auth_token
        self.api_key     = api_key
        self.client_code = client_code
        self.feed_token  = feed_token
        t = threading.Thread(target=self._run, daemon=True, name='OrderFillWatcher')
        t.start()
        hb = threading.Thread(target=self._heartbeat, daemon=True, name='OrderFillWatcherHB')
        hb.start()

    def _heartbeat(self):
        while True:
            sleep(900)
            status = 'READY' if self._ws_ready.is_set() else 'NOT READY'
            with self._lock:
                n = len(self.live_orders)
            logger.info(f"OrderFillWatcher heartbeat: WS {status}, {n} orders tracked")

    def _run(self):
        try:
            self.connect()
        except Exception:
            pass  # Non-fatal — strategy falls back to REST orderBook

    def on_open(self, wsapp):
        pass

    def on_message(self, wsapp, message):
        self._handle(message)

    def on_data(self, wsapp, message, data_type, continue_flag):
        # AB00 ack arrives here, not on_message
        self._handle(message)

    def on_pong(self, wsapp, data):
        heartbeat = getattr(self, 'HEARTBEAT_MESSAGE', None)
        if heartbeat and data == heartbeat:
            self.last_pong_timestamp = time()
        else:
            self._handle(data)

    def on_error(self, wsapp, error):
        pass

    def on_close(self, wsapp, close_status_code, close_msg):
        self._ws_ready.clear()
        self.retry_connect()

    def _handle(self, raw):
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
        except Exception:
            return
        status = parsed.get('order-status')
        if status == 'AB00':
            self._ws_ready.set()
            return
        if status in self._TERMINAL_STATUSES:
            od = parsed.get('orderData')
            if od:
                oid = od.get('orderid')
                if oid:
                    with self._lock:
                        self.live_orders[oid] = od


def slack_bot_sendtext(msg, channel):
    """
    Send a Slack message via the bot. Fails silently — never crashes the caller.
    Logs failure to error_log.txt and attempts Telegram fallback.
    """
    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {slack_token}",
        "Content-Type":  "application/json",
    }
    payload = {"channel": channel, "text": msg}
    try:
        response = post(url, headers=headers, json=payload, timeout=5)
        return response.json() if 'response' in dir() else None
    except Exception as e:
        trace_msg = format_exc()
        msg_txt = (f"Time: {datetime.now():%Y-%m-%d %H:%M:%S}.\n"
                   f"Slack message failed.\nException:\n{format(e)}\n{trace_msg}")
        print(msg_txt)
        telegram_bot_sendtext("Apollo: Slack message failed. Check log.", 'bot')
        _write_error_log(msg_txt)
    return None


def telegram_bot_sendtext(bot_message, medium='channel'):
    """
    Send a Telegram message. Used as fallback when Slack fails.
    medium='bot'     — muted private bot message
    medium='channel' — channel notification
    """
    def _escape_markdown_v2(text):
        escape_chars = r'[_*[\]()~`>#+-=|{}.!]'
        return sub(escape_chars, r'\\\g<0>', text)

    bot_chat_id  = bot_id if medium == 'bot' else channel_id
    bot_message  = _escape_markdown_v2(bot_message)
    send_text    = (
        f"https://api.telegram.org/bot{bot_token}/sendMessage"
        f"?chat_id={bot_chat_id}&parse_mode=MarkdownV2&text={bot_message}"
    )
    try:
        response = get(send_text, timeout=5)
        return response.json()
    except Exception as e:
        trace_msg = format_exc()
        msg_txt = (f"Time: {datetime.now():%Y-%m-%d %H:%M:%S}.\n"
                   f"Telegram message failed.\nException:\n{format(e)}\n{trace_msg}")
        print(msg_txt)
        _write_error_log(msg_txt)
        sleep(1)
        try:
            response = get(send_text, timeout=5)
            return response.json()
        except Exception:
            pass
    return None


def handle_exception(e):
    """
    Log exception with full traceback to console and error_log.txt.
    Send Slack error alert.
    """
    trace_msg = format_exc()
    msg_txt_detailed = (
        f"Time: {datetime.now():%Y-%m-%d %H:%M:%S}.\n"
        f"Exception:\n{format(e)}\n{trace_msg}"
    )
    print(msg_txt_detailed)
    slack_bot_sendtext(
        f"APOLLO ERROR at {datetime.now():%Y-%m-%d %H:%M:%S} — "
        f"{format(e)} — check logs.",
        SLACK_ERRORS_CHANNEL
    )
    _write_error_log(msg_txt_detailed)


def _write_error_log(msg):
    """Append message to data/error_log.txt."""
    import os
    log_path = os.path.join(DATA_DIR, 'error_log.txt')
    mode = 'a' if exists(log_path) else 'w'
    try:
        with open(log_path, mode) as f:
            f.writelines(msg)
    except Exception:
        pass   # never let log writing crash the process