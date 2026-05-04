"""
functions.py — Athena Production Utility Functions
Slack messaging, Telegram messaging, and exception handling.

Reused from Apollo/Artemis with minimal changes for Athena.
"""

import os
from requests import get, post
from re import sub
from time import sleep
from traceback import format_exc
from datetime import datetime

from configs_live import (
    slack_token, bot_token, bot_id, channel_id,
    SLACK_ERRORS_CHANNEL,
    DATA_DIR, ORDER_LIMIT
)

# ---------------------------------------------------------------------------
# Rate limit counters
# ---------------------------------------------------------------------------
_rms_poll_counter = 0
_order_book_poll_counter = 0
_ltp_poll_counter = 0
_order_counter = 0

_RMS_POLL_LIMIT = 2
_ORDER_BOOK_POLL_LIMIT = 1
_LTP_POLL_LIMIT = 10
_ORDER_LIMIT = ORDER_LIMIT


def _increment_rms_poll():
    global _rms_poll_counter
    _rms_poll_counter += 1
    if _rms_poll_counter >= _RMS_POLL_LIMIT:
        sleep(1)
        _reset_counters()


def _increment_order_book_poll():
    global _order_book_poll_counter
    _order_book_poll_counter += 1
    if _order_book_poll_counter >= _ORDER_BOOK_POLL_LIMIT:
        sleep(1)
        _reset_counters()


def _increment_ltp_poll():
    global _ltp_poll_counter
    _ltp_poll_counter += 1
    if _ltp_poll_counter >= _LTP_POLL_LIMIT:
        sleep(1)
        _reset_counters()


def _increment_order():
    global _order_counter
    _order_counter += 1
    if _order_counter >= _ORDER_LIMIT:
        sleep(1)
        _reset_counters()


def _reset_counters():
    global _rms_poll_counter, _order_book_poll_counter, _ltp_poll_counter, _order_counter
    _rms_poll_counter = 0
    _order_book_poll_counter = 0
    _ltp_poll_counter = 0
    _order_counter = 0


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------

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
        telegram_bot_sendtext("Athena: Slack message failed. Check log.", 'bot')
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
        f"ATHENA ERROR at {datetime.now():%Y-%m-%d %H:%M:%S} — "
        f"{format(e)} — check logs.",
        SLACK_ERRORS_CHANNEL
    )
    _write_error_log(msg_txt_detailed)


def _write_error_log(msg):
    """Append message to data/error_log.txt."""
    log_path = os.path.join(DATA_DIR, 'error_log.txt')
    mode = 'a' if os.path.exists(log_path) else 'w'
    try:
        with open(log_path, mode) as f:
            f.write(msg + '\n')
    except Exception:
        pass
