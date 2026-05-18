from os.path import exists
from requests import get, post
from re import sub
from time import sleep, time
from traceback import format_exc
from datetime import datetime
from configs import slack_token, bot_token, bot_id, channel_id, order_limit, poll_limit, poll_counter, order_counter

# Function for alerts from Slack. To be called when an order is executed or to send any other alert
def slack_bot_sendtext(msg, channel):
    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {slack_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "channel": channel,
        "text": msg
    }
    try:
        response = post(url, headers=headers, json=payload, timeout=5)
    except Exception as e:
        trace_msg = format_exc()
        msg_txt_detailed = (f"Time: {datetime.now():%Y-%m-%d %H:%M:%S}.\nException:\n {format(e)} \n{trace_msg}")
        print(msg_txt_detailed)
        telegram_bot_sendtext("Slack Message Failed. Check log for details.", 'bot')
        mode = 'a' if exists('data/error_log.txt') else 'w'
        with open('data/error_log.txt', mode) as error_log:
            error_log.writelines(msg_txt_detailed)
    return response.json() if 'response' in locals() else None

# Function for alerts from Telegram. To be called when an order is executed or to send any other alert
def telegram_bot_sendtext(bot_message, medium='channel'):
    # Private helper function to handle special characters in Telegram messages. Only needed by this function
    def _escape_markdown_v2(text):
        escape_chars = r'[_*[\]()~`>#+-=|{}.!]'
        return sub(escape_chars, r'\\\g<0>', text)

    # Set bot_chat_ID based on whether I want a muted or unmuted notification
    bot_chat_ID = bot_id if medium == 'bot' else channel_id
    # Escape special characters in the message
    bot_message = _escape_markdown_v2(bot_message)
    send_text = 'https://api.telegram.org/bot' + bot_token + '/sendMessage?chat_id=' + bot_chat_ID + '&parse_mode=MarkdownV2&text=' + bot_message
    try:
        response = get(send_text)
    except Exception as e:
        trace_msg = format_exc()
        msg_txt_detailed = (f"Time: {datetime.now():%Y-%m-%d %H:%M:%S}.\nException:\n {format(e)} \n{trace_msg}")
        print(msg_txt_detailed)
        slack_bot_sendtext("Telegram Message Failed. Check log for details.", "#error-alerts")
        mode = 'a' if exists('data/error_log.txt') else 'w'
        with open('data/error_log.txt', mode) as error_log:
            error_log.writelines(msg_txt_detailed)
        sleep(1)
        response = get(send_text)
    return response.json()
    
# Function to handle exceptions
def handle_exception(e):
    trace_msg = format_exc()
    msg_txt_detailed = (f"Time: {datetime.now():%Y-%m-%d %H:%M:%S}.\nException:\n {format(e)} \n{trace_msg}")
    print(msg_txt_detailed)
    slack_bot_sendtext(
        f"ARTEMIS ERROR at {datetime.now():%Y-%m-%d %H:%M:%S} — "
        f"{format(e)} — check logs.",
        "#error-alerts"
    )
    mode = 'a' if exists('data/error_log.txt') else 'w'
    with open('data/error_log.txt', mode) as error_log:
        error_log.writelines(msg_txt_detailed)

# ---------------------------------------------------------------------------
# Hardened Rate Limit Counters
# ---------------------------------------------------------------------------
_counters = {
    'rms': {'count': 0, 'limit': 2, 'last_reset': 0},
    'order_book': {'count': 0, 'limit': 1, 'last_reset': 0},
    'ltp': {'count': 0, 'limit': 10, 'last_reset': 0},
    'order': {'count': 0, 'limit': 10, 'last_reset': 0}
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
        reset_counters()
        return

    c['count'] += 1

def increment_rms_poll(): _check_limit('rms')
def increment_poll_counter(): _check_limit('ltp') # Poll in Artemis is primarily LTP
def increment_order_book_poll(): _check_limit('order_book')
def increment_order_counter(): _check_limit('order')

# Function to reset counters
def reset_counters():
    """Manual reset of all counters — call after any significant sleep."""
    global _counters
    now = time()
    for k in _counters:
        _counters[k]['count'] = 0
        _counters[k]['last_reset'] = now