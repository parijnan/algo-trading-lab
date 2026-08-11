"""
check_cas_indicative_price.py — one-off check: does SmartAPI expose a live-updating
price for Nifty/Sensex during the 15:15-15:30 CAS freeze window (WebSocket SnapQuote
and REST getMarketData FULL, both), or does it hold the last 15:14/15:15 tick like the
raw feed already does?

Context: the Angel One terminal started showing a live CAS indicative price on
2026-08-11. This checks whether that's backed by a SmartAPI field we can consume
programmatically, or a terminal-only UI feature with no API equivalent. See
plans/closing-auction-session.md §5.5 / §9 item 13.

Library-level check already done (no login needed): SmartWebSocketV2's binary parser
(_parse_binary_data) has no dedicated "indicative"/"auction" field in any mode. If
SmartAPI carries anything live during the freeze, the only place it could show up is
last_traded_price (WS) / ltp (REST) actually changing during 15:15-15:30, or via the
best-5 depth arrays — not a distinctly-named field. That's what this script watches for.

Run manually, once, during a live CAS window — not on a cron. Logs in once, runs both
WebSocket (continuous, every tick logged) and REST polling (every 20s, per instruction)
against NIFTY_TOKEN/SENSEX_TOKEN, from RUN_START to RUN_END, then logs out cleanly.

Standing constraint (feedback_no_angelone_during_live): only run this once Iris has
logged off for the day — do not run while Iris (or any strategy) is live. Iris's own
cutoff was moved to 15:17 specifically to make this kind of check possible; wait for
explicit go-ahead before running.

Usage:
    python research/cas_smartapi_check/check_cas_indicative_price.py
"""

import csv
import json
import os
import sys
import threading
import time
from datetime import datetime, time as dtime

import pandas as pd
from pyotp import TOTP
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

REPO_ROOT   = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CREDS_FILE  = os.path.join(REPO_ROOT, "data", "user_credentials.csv")
OUT_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")
os.makedirs(OUT_DIR, exist_ok=True)

NIFTY_TOKEN  = "99926000"
SENSEX_TOKEN = "99919000"
EXCHANGE_NSE_CM = 1
EXCHANGE_BSE_CM = 3
SNAP_QUOTE_MODE = 3
CORRELATION_ID  = "cas_check"

RUN_START = dtime(15, 10, 0)   # a few minutes before the freeze, for baseline
RUN_END   = dtime(15, 40, 0)   # past derivatives close, generous margin
POLL_INTERVAL_S = 20

today = datetime.now().strftime("%Y-%m-%d")
WS_OUT   = os.path.join(OUT_DIR, f"ws_ticks_{today}.csv")
REST_OUT = os.path.join(OUT_DIR, f"rest_polls_{today}.jsonl")


def login():
    creds = pd.read_csv(CREDS_FILE).iloc[0]
    obj = SmartConnect(api_key=creds["api_key"])
    totp = TOTP(creds["qr_code"]).now()
    data = obj.generateSession(creds["user_name"], str(creds["password"]), totp)
    auth_token = data["data"]["jwtToken"]
    feed_token = obj.getfeedToken()
    print(f"[{datetime.now():%H:%M:%S}] Logged in as {creds['user_name']}.")
    return obj, auth_token, feed_token, creds["api_key"], creds["user_name"]


# ---------------------------------------------------------------------------
# WebSocket side — log every tick received for either token, unmodified
# ---------------------------------------------------------------------------

_ws_rows = []
_ws_lock = threading.Lock()


def _on_data(wsapp, message):
    row = dict(message)
    row["_wall_clock"] = datetime.now().isoformat()
    with _ws_lock:
        _ws_rows.append(row)
    tok = row.get("token")
    ltp = row.get("last_traded_price")
    print(f"[WS {datetime.now():%H:%M:%S}] token={tok} last_traded_price={ltp}")


def _on_open(wsapp):
    print(f"[{datetime.now():%H:%M:%S}] WS open — subscribing SnapQuote for Nifty/Sensex.")
    sws.subscribe(CORRELATION_ID, SNAP_QUOTE_MODE, [
        {"exchangeType": EXCHANGE_NSE_CM, "tokens": [NIFTY_TOKEN]},
        {"exchangeType": EXCHANGE_BSE_CM, "tokens": [SENSEX_TOKEN]},
    ])


def _on_error(wsapp, error):
    print(f"[WS ERROR {datetime.now():%H:%M:%S}] {error}")


def _on_close(wsapp):
    print(f"[{datetime.now():%H:%M:%S}] WS closed.")


sws = None


def start_ws(auth_token, api_key, client_code, feed_token):
    global sws
    sws = SmartWebSocketV2(auth_token, api_key, client_code, feed_token, max_retry_attempt=0)
    sws.on_open = _on_open
    sws.on_data = _on_data
    sws.on_error = _on_error
    sws.on_close = _on_close
    t = threading.Thread(target=sws.connect, daemon=True)
    t.start()
    return t


def dump_ws_rows():
    with _ws_lock:
        rows = list(_ws_rows)
    if not rows:
        print("No WS rows captured.")
        return
    fieldnames = sorted({k for r in rows for k in r.keys()})
    with open(WS_OUT, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    print(f"Wrote {len(rows)} WS rows to {WS_OUT}")


# ---------------------------------------------------------------------------
# REST side — poll getMarketData(mode="FULL") every 20s, dump raw JSON
# ---------------------------------------------------------------------------

def poll_rest_once(obj):
    try:
        resp = obj.getMarketData(
            mode="FULL",
            exchangeTokens={"NSE": [NIFTY_TOKEN], "BSE": [SENSEX_TOKEN]},
        )
    except Exception as e:
        resp = {"error": str(e)}
    record = {"_wall_clock": datetime.now().isoformat(), "response": resp}
    with open(REST_OUT, "a") as f:
        f.write(json.dumps(record) + "\n")
    fetched = resp.get("data", {}).get("fetched", []) if isinstance(resp, dict) else []
    for item in fetched:
        print(f"[REST {datetime.now():%H:%M:%S}] {item.get('tradingSymbol', item.get('symbolToken'))} "
              f"ltp={item.get('ltp')} avgPrice={item.get('avgPrice')}")
    return resp


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    now = datetime.now().time()
    if now < RUN_START:
        wait_s = (datetime.combine(datetime.today(), RUN_START) - datetime.now()).total_seconds()
        print(f"Waiting {wait_s:.0f}s until {RUN_START} before starting.")
        time.sleep(max(0, wait_s))

    obj, auth_token, feed_token, api_key, client_code = login()

    ws_thread = start_ws(auth_token, api_key, client_code, feed_token)
    time.sleep(2)  # let WS connect/subscribe settle before the first REST poll

    print(f"Polling REST every {POLL_INTERVAL_S}s until {RUN_END}. WS logging continuously.")
    try:
        while datetime.now().time() < RUN_END:
            poll_rest_once(obj)
            time.sleep(POLL_INTERVAL_S)
    except KeyboardInterrupt:
        print("Interrupted — shutting down early.")
    finally:
        try:
            sws.close_connection()
        except Exception:
            pass
        dump_ws_rows()
        try:
            obj.terminateSession(client_code)
            print(f"[{datetime.now():%H:%M:%S}] Session terminated.")
        except Exception as e:
            print(f"terminateSession failed (non-fatal): {e}")

    print(f"Done. WS rows: {WS_OUT}  REST polls: {REST_OUT}")


if __name__ == "__main__":
    main()
