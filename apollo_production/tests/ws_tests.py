"""
ws_test.py — SmartWebSocketV2 Test Harness
Apollo Live Execution — WebSocket Layer Validation

Tests:
  1. Feed start: connect, subscribe Nifty + VIX on open, confirm ticks arrive
  2. Decode: verify last_traded_price / 100 produces sensible LTP values
  3. Mid-session subscribe: add a test option token after 30s
  4. Unsubscribe: drop the option token after another 30s
  5. Clean shutdown: close_connection() + ctypes fallback if thread survives
  6. Thread safety: shared state read from main thread every 5s throughout

Run from the artemis directory on delos:
  /home/parijnan/anaconda3/bin/python ws_test.py

Requires:
  - data/user_credentials.csv  (same file Artemis uses)
  - A test option token passed as TEST_OPTION_TOKEN below
"""

import ctypes
import threading
import time
from datetime import datetime
from pyotp import TOTP
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
import pandas as pd

# ---------------------------------------------------------------------------
# CONFIG — fill in before running
# ---------------------------------------------------------------------------

# Any live Nifty weekly option token from the scrip master.
# Look up in data/scrip_master.csv filtered on exch_seg=NFO, name=NIFTY.
# Example: a nearby ATM strike CE or PE.
TEST_OPTION_TOKEN = "40732"   # <-- fill this in

# Tokens that are subscribed at connect and stay for the whole session
NIFTY_TOKEN = "99926000"
VIX_TOKEN   = "99926017"

# Exchange type constants (from SDK source)
EXCHANGE_NSE_CM = 1   # NSE Cash / Index (Nifty, VIX)
EXCHANGE_NSE_FO = 2   # NSE F&O (options)

# LTP mode
MODE_LTP = 1

# ---------------------------------------------------------------------------
# SHARED STATE — written by WS thread, read by main thread
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_ltp = {}          # {token_str: float}  — last traded price, decoded
_tick_count = {}   # {token_str: int}    — total ticks received per token
_ws_thread = None  # set after thread starts


def _update_ltp(token, price):
    with _state_lock:
        _ltp[token] = price
        _tick_count[token] = _tick_count.get(token, 0) + 1


def get_ltp(token):
    with _state_lock:
        return _ltp.get(token)


def get_tick_count(token):
    with _state_lock:
        return _tick_count.get(token, 0)


# ---------------------------------------------------------------------------
# WEBSOCKET CALLBACKS
# ---------------------------------------------------------------------------

def on_data(wsapp, message):
    """Called on the WS thread for every tick. Must be fast — no blocking."""
    try:
        token = message.get("token")
        raw   = message.get("last_traded_price")
        if token is not None and raw is not None:
            ltp = float(raw) / 100.0
            _update_ltp(token, ltp)
    except Exception as e:
        print(f"[on_data ERROR] {e}")


def on_open(wsapp):
    print(f"\n[{_ts()}] WebSocket OPENED")
    # Subscribe Nifty index and VIX immediately on connect
    token_list = [{"exchangeType": EXCHANGE_NSE_CM, "tokens": [NIFTY_TOKEN, VIX_TOKEN]}]
    sws.subscribe("apollo_ws_test", MODE_LTP, token_list)
    print(f"[{_ts()}] Subscribed: Nifty ({NIFTY_TOKEN}), VIX ({VIX_TOKEN})")


def on_error(wsapp, error):
    print(f"[{_ts()}] WS ERROR: {error}")


def on_close(wsapp):
    print(f"[{_ts()}] WebSocket CLOSED")


# ---------------------------------------------------------------------------
# THREAD MANAGEMENT
# ---------------------------------------------------------------------------

def _start_ws():
    """Target function for the WS daemon thread."""
    sws.connect()


def start_feed():
    """Start the WebSocket in a daemon thread. Returns the thread object."""
    global _ws_thread
    _ws_thread = threading.Thread(target=_start_ws, name="ws-feed", daemon=True)
    _ws_thread.start()
    return _ws_thread


def stop_feed(thread, timeout_sec=5):
    print(f"\n[{_ts()}] Requesting WebSocket shutdown...")

    # Step 1: Close the underlying socket directly.
    # This unblocks the C-level recv() call immediately, returning
    # the thread to Python bytecode so ctypes can reach it.
    try:
        if sws.wsapp and sws.wsapp.sock:
            sws.wsapp.sock.close()
    except Exception as e:
        print(f"[{_ts()}] sock.close() raised: {e}")

    # Step 2: Now call close_connection() for the clean SDK path
    try:
        sws.close_connection()
    except Exception as e:
        print(f"[{_ts()}] close_connection() raised: {e}")

    # Step 3: Give the thread a moment to die naturally
    thread.join(timeout=timeout_sec)
    if not thread.is_alive():
        print(f"[{_ts()}] Thread died cleanly. ✓")
        return True

    # Step 4: ctypes hard kill — thread is now in Python bytecode
    # (unblocked by sock.close()), so injection will take effect
    print(f"[{_ts()}] Thread still alive after {timeout_sec}s — using ctypes hard kill...")
    _ctypes_kill_thread(thread)
    thread.join(timeout=3)
    if not thread.is_alive():
        print(f"[{_ts()}] Thread killed via ctypes. ✓")
        return True
    else:
        print(f"[{_ts()}] WARNING: Thread still alive after ctypes kill.")
        return False


def _ctypes_kill_thread(thread):
    if not thread.is_alive():
        return
    exc = ctypes.py_object(SystemExit)
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_long(thread.ident), exc)
    if res == 0:
        print(f"  ctypes: thread id {thread.ident} not found")
    elif res > 1:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_long(thread.ident), None)
        print(f"  ctypes: affected {res} threads — undone")


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _ts():
    return datetime.now().strftime("%H:%M:%S")


def _print_state(label=""):
    """Read shared state from main thread and print current LTPs."""
    nifty = get_ltp(NIFTY_TOKEN)
    vix   = get_ltp(VIX_TOKEN)
    opt   = get_ltp(TEST_OPTION_TOKEN)
    n_ticks_nifty = get_tick_count(NIFTY_TOKEN)
    n_ticks_vix   = get_tick_count(VIX_TOKEN)
    n_ticks_opt   = get_tick_count(TEST_OPTION_TOKEN)

    nifty_str = f"{nifty:.2f} ({n_ticks_nifty} ticks)" if nifty else "— no data yet"
    vix_str   = f"{vix:.2f} ({n_ticks_vix} ticks)"     if vix   else "— no data yet"
    opt_str   = f"{opt:.2f} ({n_ticks_opt} ticks)"      if opt   else "— no data yet"

    tag = f" [{label}]" if label else ""
    print(f"  [{_ts()}]{tag}  Nifty: {nifty_str}  |  VIX: {vix_str}  |  Option: {opt_str}")


def _wait_with_status(seconds, interval=5):
    """Wait for `seconds` total, printing shared state every `interval` seconds."""
    elapsed = 0
    while elapsed < seconds:
        time.sleep(interval)
        elapsed += interval
        _print_state()


# ---------------------------------------------------------------------------
# LOGIN
# ---------------------------------------------------------------------------

def login():
    print(f"[{_ts()}] Loading credentials...")
    creds = pd.read_csv("/home/parijnan/scripts/algo-trading-lab/apollo_production/data/user_credentials.csv")
    row = creds.iloc[0]
    api_key   = row["api_key"]
    user_name = row["user_name"]
    password  = str(row["password"])
    qr_code   = row["qr_code"]

    print(f"[{_ts()}] Logging in as {user_name}...")
    obj = SmartConnect(api_key=api_key)
    while True:
        try:
            totp = TOTP(qr_code).now()
            data = obj.generateSession(user_name, password, totp)
            break
        except Exception as e:
            print(f"  Login failed: {e} — retrying in 1s")
            time.sleep(1)

    auth_token = data["data"]["jwtToken"]
    feed_token = obj.getfeedToken()
    print(f"[{_ts()}] Login OK. Auth token obtained. Feed token obtained.")
    return obj, auth_token, feed_token, api_key, user_name


# ---------------------------------------------------------------------------
# MAIN TEST SEQUENCE
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    if TEST_OPTION_TOKEN == "FILL_IN_BEFORE_RUNNING":
        print("ERROR: Set TEST_OPTION_TOKEN before running.")
        print("Look up a live Nifty option token in data/scrip_master.csv")
        print("Filter: exch_seg=NFO, name=NIFTY, any nearby strike CE or PE")
        raise SystemExit(1)

    print("=" * 60)
    print("Apollo WebSocket Layer Test")
    print("=" * 60)

    # ------------------------------------------------------------------
    # 1. Login
    # ------------------------------------------------------------------
    obj, auth_token, feed_token, api_key, user_name = login()

    # ------------------------------------------------------------------
    # 2. Build SmartWebSocketV2 with max_retry_attempt=0
    #    This prevents the SDK from auto-reconnecting on close_connection().
    #    If you want auto-reconnect during normal operation (e.g. network
    #    blip), set max_retry_attempt=3 in production. For this test we
    #    keep it at 0 so shutdown behaviour is clean and predictable.
    # ------------------------------------------------------------------
    sws = SmartWebSocketV2(
        auth_token, api_key, user_name, feed_token,
        max_retry_attempt=0
    )
    sws.on_open  = on_open
    sws.on_data  = on_data
    sws.on_error = on_error
    sws.on_close = on_close

    # ------------------------------------------------------------------
    # 3. Start feed
    # ------------------------------------------------------------------
    print(f"\n[{_ts()}] === TEST 1: Start feed, subscribe Nifty + VIX ===")
    ws_thread = start_feed()
    print(f"[{_ts()}] WS thread started (daemon={ws_thread.daemon}, id={ws_thread.ident})")

    # Wait for connection to establish
    print(f"[{_ts()}] Waiting 5s for connection to establish...")
    time.sleep(5)
    print(f"[{_ts()}] Thread alive: {ws_thread.is_alive()}")

    # Watch for 30 seconds — should see Nifty and VIX ticks
    print(f"\n[{_ts()}] Monitoring Nifty + VIX for 30s (reading from main thread):")
    _wait_with_status(30)

    # Sanity check: confirm we got ticks
    nifty_ticks = get_tick_count(NIFTY_TOKEN)
    vix_ticks   = get_tick_count(VIX_TOKEN)
    if nifty_ticks > 0 and vix_ticks > 0:
        print(f"[{_ts()}] ✓ Ticks confirmed: Nifty={nifty_ticks}, VIX={vix_ticks}")
    else:
        print(f"[{_ts()}] ✗ WARNING: No ticks received. Nifty={nifty_ticks}, VIX={vix_ticks}")
        print("  Possible causes: market closed, token wrong, auth issue")
        print("  Continuing test anyway...")

    # ------------------------------------------------------------------
    # 4. Mid-session subscribe: add test option token
    # ------------------------------------------------------------------
    print(f"\n[{_ts()}] === TEST 2: Mid-session subscribe option token {TEST_OPTION_TOKEN} ===")
    option_token_list = [{"exchangeType": EXCHANGE_NSE_FO, "tokens": [TEST_OPTION_TOKEN]}]
    sws.subscribe("apollo_ws_test", MODE_LTP, option_token_list)
    print(f"[{_ts()}] subscribe() called for option token {TEST_OPTION_TOKEN}")

    print(f"[{_ts()}] Monitoring all 3 tokens for 30s:")
    _wait_with_status(30)

    opt_ticks = get_tick_count(TEST_OPTION_TOKEN)
    if opt_ticks > 0:
        print(f"[{_ts()}] ✓ Option ticks confirmed: {opt_ticks}")
    else:
        print(f"[{_ts()}] ✗ WARNING: No option ticks received after mid-session subscribe.")

    # ------------------------------------------------------------------
    # 5. Unsubscribe option token
    # ------------------------------------------------------------------
    print(f"\n[{_ts()}] === TEST 3: Unsubscribe option token {TEST_OPTION_TOKEN} ===")
    unsub_token_list = [{"exchangeType": EXCHANGE_NSE_FO, "tokens": [TEST_OPTION_TOKEN]}]
    sws.unsubscribe("apollo_ws_test", MODE_LTP, unsub_token_list)
    print(f"[{_ts()}] unsubscribe() called for option token {TEST_OPTION_TOKEN}")

    # Record tick count at unsubscribe time
    opt_ticks_before = get_tick_count(TEST_OPTION_TOKEN)
    print(f"[{_ts()}] Option tick count at unsubscribe: {opt_ticks_before}")
    print(f"[{_ts()}] Monitoring for 30s — option ticks should stop (or slow to 0):")
    _wait_with_status(30)

    opt_ticks_after = get_tick_count(TEST_OPTION_TOKEN)
    new_opt_ticks   = opt_ticks_after - opt_ticks_before
    if new_opt_ticks == 0:
        print(f"[{_ts()}] ✓ No new option ticks after unsubscribe")
    else:
        print(f"[{_ts()}] ~ {new_opt_ticks} new option ticks after unsubscribe.")
        print("  Note: SDK sets RESUBSCRIBE_FLAG=True on unsubscribe — if a reconnect")
        print("  occurred, the token may have been re-subscribed automatically.")
        print("  In Apollo production, we manage our own sub list to handle this.")

    # Confirm Nifty/VIX still flowing
    nifty_after = get_tick_count(NIFTY_TOKEN)
    print(f"[{_ts()}] Nifty ticks still flowing: {nifty_after} total (was {nifty_ticks})")

    # ------------------------------------------------------------------
    # 6. Clean shutdown
    # ------------------------------------------------------------------
    print(f"\n[{_ts()}] === TEST 4: Clean shutdown ===")
    print(f"[{_ts()}] Thread alive before shutdown: {ws_thread.is_alive()}")
    success = stop_feed(ws_thread, timeout_sec=5)
    print(f"[{_ts()}] Thread alive after shutdown: {ws_thread.is_alive()}")
    if success:
        print(f"[{_ts()}] ✓ Shutdown complete")
    else:
        print(f"[{_ts()}] ✗ Shutdown may not be clean — check for zombie threads")

    # ------------------------------------------------------------------
    # 7. Final summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    print(f"  Feed start + Nifty/VIX subscribe  : {'✓' if nifty_ticks > 0 and vix_ticks > 0 else '✗'}")
    print(f"  Nifty ticks received               : {get_tick_count(NIFTY_TOKEN)}")
    print(f"  VIX ticks received                 : {get_tick_count(VIX_TOKEN)}")
    print(f"  Mid-session option subscribe       : {'✓' if opt_ticks > 0 else '✗'}")
    print(f"  Option ticks received              : {get_tick_count(TEST_OPTION_TOKEN)}")
    print(f"  Unsubscribe (new ticks after)      : {new_opt_ticks}")
    print(f"  Clean shutdown                     : {'✓' if success else '✗'}")
    print(f"  Final Nifty LTP                    : {get_ltp(NIFTY_TOKEN)}")
    print(f"  Final VIX LTP                      : {get_ltp(VIX_TOKEN)}")
    print("=" * 60)

    # Logout
    try:
        obj.terminateSession(user_name)
        print(f"[{_ts()}] Logged out.")
    except Exception as e:
        print(f"[{_ts()}] Logout failed: {e}")