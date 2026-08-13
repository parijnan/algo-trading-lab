"""
check_cas_stock_referenceprice.py — follow-up to check_cas_indicative_price.py
(2026-08-12): that script found SmartAPI's new `referenceLimitPrice` REST field
read 0.0 throughout on NIFTY/SENSEX index tokens, hypothesized to be because
it's a stock-level field (tied to is_cas_enabled, itself stock-level) not
populated for derived indices. This script tests that hypothesis directly
against 6 actual CAS-eligible stocks on both NSE and BSE (the same 6
heavyweights used throughout this investigation: HDFCBANK, RELIANCE,
ICICIBANK, INFY, TCS, ITC).

Run manually, once, during a live CAS window. Logs in once, runs both
WebSocket (SnapQuote, every tick logged) and REST (getMarketData FULL,
every 20s) against all 12 tokens, then logs out cleanly.

Standing constraint (feedback_no_angelone_during_live): only run once Iris
(or any live strategy) is confirmed stood down for the day.
"""

import csv
import json
import os
import threading
import time
from datetime import datetime, time as dtime

import pandas as pd
from pyotp import TOTP
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

REPO_ROOT  = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CREDS_FILE = os.path.join(REPO_ROOT, "data", "user_credentials.csv")
OUT_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")
os.makedirs(OUT_DIR, exist_ok=True)

EXCHANGE_NSE_CM = 1
EXCHANGE_BSE_CM = 3
SNAP_QUOTE_MODE = 3
CORRELATION_ID  = "cas_stock_check"

# token, exchange_type_int, exchange_str (for REST), label
STOCKS = [
    ("1333",   EXCHANGE_NSE_CM, "NSE", "HDFCBANK"),
    ("2885",   EXCHANGE_NSE_CM, "NSE", "RELIANCE"),
    ("4963",   EXCHANGE_NSE_CM, "NSE", "ICICIBANK"),
    ("1594",   EXCHANGE_NSE_CM, "NSE", "INFY"),
    ("11536",  EXCHANGE_NSE_CM, "NSE", "TCS"),
    ("1660",   EXCHANGE_NSE_CM, "NSE", "ITC"),
    ("500180", EXCHANGE_BSE_CM, "BSE", "HDFCBANK"),
    ("500325", EXCHANGE_BSE_CM, "BSE", "RELIANCE"),
    ("532174", EXCHANGE_BSE_CM, "BSE", "ICICIBANK"),
    ("500209", EXCHANGE_BSE_CM, "BSE", "INFY"),
    ("532540", EXCHANGE_BSE_CM, "BSE", "TCS"),
    ("500875", EXCHANGE_BSE_CM, "BSE", "ITC"),
]

RUN_END = dtime(15, 40, 0)
POLL_INTERVAL_S = 20

today = datetime.now().strftime("%Y-%m-%d")
WS_OUT   = os.path.join(OUT_DIR, f"ws_stock_ticks_{today}.csv")
REST_OUT = os.path.join(OUT_DIR, f"rest_stock_polls_{today}.jsonl")


def login():
    creds = pd.read_csv(CREDS_FILE).iloc[0]
    obj = SmartConnect(api_key=creds["api_key"])
    totp = TOTP(creds["qr_code"]).now()
    data = obj.generateSession(creds["user_name"], str(creds["password"]), totp)
    auth_token = data["data"]["jwtToken"]
    feed_token = obj.getfeedToken()
    print(f"[{datetime.now():%H:%M:%S}] Logged in as {creds['user_name']}.")
    return obj, auth_token, feed_token, creds["api_key"], creds["user_name"]


_ws_rows = []
_ws_lock = threading.Lock()
_token_label = {t: (exch, label) for t, _, exch, label in STOCKS}


def _on_data(wsapp, message):
    row = dict(message)
    row["_wall_clock"] = datetime.now().isoformat()
    with _ws_lock:
        _ws_rows.append(row)
    tok = str(row.get("token"))
    exch, label = _token_label.get(tok, ("?", tok))
    print(f"[WS {datetime.now():%H:%M:%S}] {exch}:{label} ltp={row.get('last_traded_price')}")


def _on_open(wsapp):
    print(f"[{datetime.now():%H:%M:%S}] WS open — subscribing SnapQuote for 12 stock tokens.")
    nse_tokens = [t for t, e, _, _ in STOCKS if e == EXCHANGE_NSE_CM]
    bse_tokens = [t for t, e, _, _ in STOCKS if e == EXCHANGE_BSE_CM]
    sws.subscribe(CORRELATION_ID, SNAP_QUOTE_MODE, [
        {"exchangeType": EXCHANGE_NSE_CM, "tokens": nse_tokens},
        {"exchangeType": EXCHANGE_BSE_CM, "tokens": bse_tokens},
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


def poll_rest_once(obj):
    exchange_tokens = {}
    for t, _, exch, _ in STOCKS:
        exchange_tokens.setdefault(exch, []).append(t)
    try:
        resp = obj.getMarketData(mode="FULL", exchangeTokens=exchange_tokens)
    except Exception as e:
        resp = {"error": str(e)}
    record = {"_wall_clock": datetime.now().isoformat(), "response": resp}
    with open(REST_OUT, "a") as f:
        f.write(json.dumps(record) + "\n")
    fetched = resp.get("data", {}).get("fetched", []) if isinstance(resp, dict) else []
    for item in fetched:
        rlp = item.get("referenceLimitPrice")
        flag = " <-- NONZERO" if rlp not in (0, 0.0, None) else ""
        print(f"[REST {datetime.now():%H:%M:%S}] {item.get('tradingSymbol', item.get('symbolToken'))} "
              f"ltp={item.get('ltp')} referenceLimitPrice={rlp}{flag}")
    return resp


def main():
    obj, auth_token, feed_token, api_key, client_code = login()
    ws_thread = start_ws(auth_token, api_key, client_code, feed_token)
    time.sleep(2)

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
