"""
functions.py — Athena Production Utility Functions
Hardened Rate Limiting & Messaging.
"""

import os
import logging
from requests import get, post
from re import sub
from time import sleep, time
from traceback import format_exc
from datetime import datetime

from configs_live import (
    slack_token, bot_token, bot_id, channel_id,
    SLACK_ERRORS_CHANNEL,
    DATA_DIR, ORDER_LIMIT,
    RMS_POLL_LIMIT, ORDER_BOOK_POLL_LIMIT, LTP_POLL_LIMIT, CANDLE_POLL_LIMIT
)

logger = logging.getLogger("athena.functions")

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
        logger.warning(f"Rate limit hit for {key.upper()}. Enforcing 1.1s cooldown and resetting all budgets...")
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

# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------

def slack_bot_sendtext(msg, channel):
    """Send a Slack message via the bot. Fails silently."""
    url = "https://slack.com/api/chat.postMessage"
    headers = {"Authorization": f"Bearer {slack_token}", "Content-Type": "application/json"}
    payload = {"channel": channel, "text": msg}
    try:
        post(url, headers=headers, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Slack message failed: {e}")
        telegram_bot_sendtext(f"Athena: Slack failed. {msg[:50]}...", 'bot')
    return None

def telegram_bot_sendtext(bot_message, medium='channel'):
    """Telegram fallback."""
    def _escape_markdown_v2(text):
        return sub(r'[_*[\]()~`>#+-=|{}.!]', r'\\\g<0>', text)
    bot_chat_id = bot_id if medium == 'bot' else channel_id
    bot_message = _escape_markdown_v2(bot_message)
    send_text = f"https://api.telegram.org/bot{bot_token}/sendMessage?chat_id={bot_chat_id}&parse_mode=MarkdownV2&text={bot_message}"
    try:
        get(send_text, timeout=5)
    except: pass
    return None

def handle_exception(e):
    """Global exception handler with Slack alerting."""
    trace_msg = format_exc()
    logger.error(f"EXCEPTION: {e}\n{trace_msg}")
    slack_bot_sendtext(f"🚨 *ATHENA ERROR*: {format(e)} — check logs.", SLACK_ERRORS_CHANNEL)
    _write_error_log(f"{datetime.now()}: {e}\n{trace_msg}")

def _write_error_log(msg):
    try:
        with open(os.path.join(DATA_DIR, 'error_log.txt'), 'a') as f:
            f.write(msg + '\n')
    except: pass
