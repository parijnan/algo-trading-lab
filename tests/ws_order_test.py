"""
ws_order_test.py — WebSocket Order Update Prototype

Purpose: Validate SmartWebSocketOrderUpdate before integrating into production strategies.

What this tests:
  1. Do auth headers need "Bearer " prefix or raw JWT?
  2. Does AB00 (connection ack) arrive on on_message or on_data (pong quirk)?
  3. What is the type of filledshares in live payloads? (docs say string)
  4. Do intermediate states (AB01 open, AB04 modify) actually push?
  5. What is the latency from order placement to WS update arrival?
  6. Does a pre-connection order's completion arrive after reconnect?

Usage:
  cd /home/parijnan/scripts/algo-trading-lab
  python tests/ws_order_test.py

  # To test pre-connection recovery:
  python tests/ws_order_test.py --reconnect-test
"""

import argparse
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from SmartApi import SmartConnect
from SmartApi.smartWebSocketOrderUpdate import SmartWebSocketOrderUpdate
from pyotp import TOTP


# ---------------------------------------------------------------------------
# Credential loading
# ---------------------------------------------------------------------------

CREDS_PATH = Path(__file__).parent.parent / "data" / "user_credentials.csv"


def load_credentials():
    df = pd.read_csv(CREDS_PATH)
    row = df.iloc[0]
    return row["api_key"], row["user_name"], str(row["password"]), row["qr_code"]


def login(api_key, user_name, password, qr_code):
    obj = SmartConnect(api_key=api_key)
    totp = TOTP(qr_code).now()
    data = obj.generateSession(user_name, password, totp)
    if not data.get("status"):
        raise RuntimeError(f"Login failed: {data}")
    auth_token = data["data"]["jwtToken"]
    feed_token = obj.getfeedToken()
    print(f"[LOGIN] OK — client: {user_name}")
    return obj, auth_token, feed_token


# ---------------------------------------------------------------------------
# Probe subclass
# ---------------------------------------------------------------------------

STATUS_CODES = {
    "AB00": "connection-ack",
    "AB01": "open",
    "AB02": "cancelled",
    "AB03": "rejected",
    "AB04": "modified",
    "AB05": "complete",
    "AB06": "after-market-pending",
    "AB07": "after-market-reject",
    "AB08": "after-market-modified",
    "AB09": "after-market-delete",
    "AB10": "after-market-cancel",
    "AB11": "pending",
}


def _ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


class OrderUpdateProbe(SmartWebSocketOrderUpdate):
    """Thin subclass that logs every event with millisecond timestamps."""

    def __init__(self, auth_token, api_key, client_code, feed_token):
        # Skip SDK's logzero setup (it cds into logs/ relative to cwd)
        self.auth_token = auth_token
        self.api_key = api_key
        self.client_code = client_code
        self.feed_token = feed_token
        self.wsapp = None
        self.last_pong_timestamp = None
        self.current_retry_attempt = 0

        # Probe state
        self.ready = threading.Event()       # set on AB00 ack
        self.messages = []                   # (ts, source, parsed_or_raw)
        self._lock = threading.Lock()
        self.order_place_ts = {}             # orderid -> placement timestamp

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _record(self, source: str, raw):
        ts = datetime.now()
        entry = {"ts": ts, "source": source, "raw": raw}
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
            entry["parsed"] = parsed
        except Exception:
            entry["parsed"] = None

        with self._lock:
            self.messages.append(entry)

        self._print_event(ts, source, entry.get("parsed"), raw)

        if entry["parsed"] and entry["parsed"].get("order-status") == "AB00":
            self.ready.set()

    def _print_event(self, ts, source, parsed, raw):
        ts_str = ts.strftime("%H:%M:%S.%f")[:-3]
        print(f"\n{'='*60}")
        print(f"[{ts_str}] source={source}")
        if parsed:
            status_code = parsed.get("order-status", "?")
            status_label = STATUS_CODES.get(status_code, "unknown")
            print(f"  order-status : {status_code} ({status_label})")
            print(f"  error-message: {parsed.get('error-message', '')!r}")
            od = parsed.get("orderData")
            if od:
                orderid = od.get("orderid", "?")
                filled = od.get("filledshares")
                avg    = od.get("averageprice")
                qty    = od.get("quantity")
                sym    = od.get("tradingsymbol", "?")
                txn    = od.get("transactiontype", "?")
                upd    = od.get("updatetime", "?")
                print(f"  orderid      : {orderid}")
                print(f"  symbol       : {sym}  txn={txn}")
                print(f"  qty/filled   : {qty}/{filled}  (filledshares type: {type(filled).__name__})")
                print(f"  avg_price    : {avg}  (type: {type(avg).__name__})")
                print(f"  updatetime   : {upd}")

                # Latency from order placement
                if orderid in self.order_place_ts:
                    latency_ms = (ts - self.order_place_ts[orderid]).total_seconds() * 1000
                    print(f"  latency      : {latency_ms:.1f} ms from order placement")
        else:
            print(f"  raw: {raw!r}")
        print(f"{'='*60}")

    # ------------------------------------------------------------------
    # SDK overrides
    # ------------------------------------------------------------------

    def on_open(self, wsapp):
        print(f"[{_ts()}] WebSocket connection opened")

    def on_message(self, wsapp, message):
        self._record("on_message", message)

    def on_data(self, wsapp, message, data_type, continue_flag):
        # SDK routes non-"ping" pong frames here — must intercept
        self._record("on_data", message)

    def on_pong(self, wsapp, data):
        if data == self.HEARTBEAT_MESSAGE:
            ts = time.time()
            self.last_pong_timestamp = ts
            print(f"[{_ts()}] PONG (heartbeat) received")
        else:
            # Non-ping pong — route to on_data just like SDK, but also record
            self._record("on_pong->on_data", data)

    def on_error(self, wsapp, error):
        print(f"[{_ts()}] ERROR: {error}", file=sys.stderr)

    def on_close(self, wsapp, close_status_code, close_msg):
        print(f"[{_ts()}] Connection closed: {close_status_code} {close_msg}")
        self.ready.clear()
        # Don't auto-retry during reconnect test — let the test control it
        if not getattr(self, "_suppress_retry", False):
            self.retry_connect()

    def mark_order_placed(self, orderid: str):
        with self._lock:
            self.order_place_ts[orderid] = datetime.now()


# ---------------------------------------------------------------------------
# SmartConnect order helpers
# ---------------------------------------------------------------------------

def fetch_individual_order(smart_obj: SmartConnect, orderid: str, label: str = "") -> dict | None:
    tag = f"  [{label}]" if label else ""
    ts = datetime.now()
    resp = smart_obj.individual_order_details(orderid)
    latency_ms = (datetime.now() - ts).total_seconds() * 1000
    print(f"\n{'~'*60}")
    print(f"[{_ts()}] individual_order_details{tag}  ({latency_ms:.0f} ms)")
    if not resp:
        print("  response: None / error")
        return None
    data = resp.get("data") if isinstance(resp, dict) else None
    if not data:
        print(f"  raw response: {resp}")
        return None
    fields = [
        ("status",          data.get("status")),
        ("orderstatus",     data.get("orderstatus")),
        ("orderid",         data.get("orderid")),
        ("tradingsymbol",   data.get("tradingsymbol")),
        ("transactiontype", data.get("transactiontype")),
        ("quantity",        data.get("quantity")),
        ("filledshares",    f"{data.get('filledshares')!r}  (type: {type(data.get('filledshares')).__name__})"),
        ("averageprice",    f"{data.get('averageprice')!r}  (type: {type(data.get('averageprice')).__name__})"),
        ("unfilledshares",  data.get("unfilledshares")),
        ("updatetime",      data.get("updatetime")),
    ]
    for k, v in fields:
        print(f"  {k:18}: {v}")
    print(f"{'~'*60}")
    return data


def place_test_order(smart_obj: SmartConnect, symbol: str, token: str,
                     txn_type: str, qty: int, exchange: str = "NFO") -> str:
    params = {
        "variety":          "NORMAL",
        "tradingsymbol":    symbol,
        "symboltoken":      token,
        "transactiontype":  txn_type,
        "exchange":         exchange,
        "ordertype":        "MARKET",
        "producttype":      "CARRYFORWARD",
        "duration":         "DAY",
        "price":            "0",
        "squareoff":        "0",
        "stoploss":         "0",
        "quantity":         str(qty),
    }
    orderid = smart_obj.placeOrder(params)  # returns orderid string on success, None on failure
    if not orderid:
        print(f"[{_ts()}] Order FAILED: {txn_type} {qty}x {symbol} — check logs above", file=sys.stderr)
        return ""
    print(f"[{_ts()}] Order placed: {txn_type} {qty}x {symbol} → orderid={orderid!r}")
    return orderid


# ---------------------------------------------------------------------------
# Test sequences
# ---------------------------------------------------------------------------

def run_basic_test(smart_obj, probe, symbol, token, lot_size, exchange="NFO"):
    """
    Step 1-6: Connect, wait for AB00, place 1-lot BUY, wait for AB05,
    place closing SELL, summarise.
    """
    print("\n" + "#"*60)
    print("PHASE 1: Basic order lifecycle test")
    print("#"*60)

    print(f"[{_ts()}] Waiting for AB00 connection-ack (up to 15s)…")
    if not probe.ready.wait(timeout=15):
        print("ERROR: AB00 not received within 15s. Check auth headers / token.", file=sys.stderr)
        return False

    print(f"\n[{_ts()}] Placing opening BUY order: {symbol}")
    buy_id = place_test_order(smart_obj, symbol, token, "BUY", lot_size, exchange)
    if not buy_id:
        print(f"[{_ts()}] BUY order failed — aborting test.", file=sys.stderr)
        return False
    probe.mark_order_placed(buy_id)
    fetch_individual_order(smart_obj, buy_id, "BUY immediate")

    print(f"[{_ts()}] Waiting 30s for order lifecycle messages…")
    time.sleep(30)
    fetch_individual_order(smart_obj, buy_id, "BUY after 30s")

    print(f"\n[{_ts()}] Placing closing SELL order: {symbol}")
    sell_id = place_test_order(smart_obj, symbol, token, "SELL", lot_size, exchange)
    if sell_id:
        probe.mark_order_placed(sell_id)
        fetch_individual_order(smart_obj, sell_id, "SELL immediate")

    print(f"[{_ts()}] Waiting 30s for closing lifecycle messages…")
    time.sleep(30)
    if sell_id:
        fetch_individual_order(smart_obj, sell_id, "SELL after 30s")

    return True


def run_reconnect_test(smart_obj, probe, symbol, token, lot_size, exchange="NFO"):
    """
    Step 7-9: Force-close the WS, place an order via REST, reconnect,
    check if the completion event arrives.
    """
    print("\n" + "#"*60)
    print("PHASE 2: Pre-connection order recovery test")
    print("#"*60)

    print(f"[{_ts()}] Force-closing WebSocket connection…")
    probe._suppress_retry = True
    probe.close_connection()
    time.sleep(2)

    print(f"[{_ts()}] Placing BUY order while disconnected…")
    pre_id = place_test_order(smart_obj, symbol, token, "BUY", lot_size, exchange)
    pre_ts = datetime.now()
    probe.mark_order_placed(pre_id)

    print(f"[{_ts()}] Waiting 5s before reconnecting…")
    time.sleep(5)

    print(f"[{_ts()}] Reconnecting…")
    probe._suppress_retry = False
    probe.ready.clear()
    ws_thread = threading.Thread(target=probe.connect, daemon=True)
    ws_thread.start()

    if not probe.ready.wait(timeout=15):
        print("ERROR: AB00 not received after reconnect.", file=sys.stderr)
        return False

    print(f"[{_ts()}] Reconnected. Waiting 20s to see if pre-connection order arrives…")
    time.sleep(20)

    # Check if pre_id appeared in messages after reconnect
    with probe._lock:
        post_msgs = [m for m in probe.messages
                     if m["ts"] > pre_ts and m.get("parsed")]
        matched = [m for m in post_msgs
                   if m["parsed"].get("orderData", {}).get("orderid") == pre_id]

    if matched:
        print(f"[{_ts()}] RESULT: Pre-connection order DID arrive after reconnect ({len(matched)} events).")
    else:
        print(f"[{_ts()}] RESULT: Pre-connection order did NOT arrive after reconnect.")
        print("         Conclusion: WS only pushes real-time; pre-connection fills need REST fallback.")

    # Close the position
    print(f"\n[{_ts()}] Closing position via SELL…")
    close_id = place_test_order(smart_obj, symbol, token, "SELL", lot_size, exchange)
    probe.mark_order_placed(close_id)
    time.sleep(20)

    return True


def print_summary(probe):
    print("\n" + "#"*60)
    print("SUMMARY")
    print("#"*60)

    with probe._lock:
        msgs = list(probe.messages)

    sources = {}
    for m in msgs:
        sources[m["source"]] = sources.get(m["source"], 0) + 1

    print(f"Total messages received : {len(msgs)}")
    print("By source:")
    for src, cnt in sources.items():
        print(f"  {src:30s}: {cnt}")

    # Check where AB00 arrived
    for m in msgs:
        p = m.get("parsed")
        if p and p.get("order-status") == "AB00":
            print(f"\nAB00 delivery path : {m['source']}")
            break

    # filledshares type check
    for m in msgs:
        p = m.get("parsed")
        od = p.get("orderData") if p else None
        if od and "filledshares" in od:
            print(f"filledshares type   : {type(od['filledshares']).__name__}  "
                  f"(value: {od['filledshares']!r})")
            break

    # Latency distribution
    latencies = []
    for m in msgs:
        p = m.get("parsed")
        od = p.get("orderData") if p else None
        if od:
            oid = od.get("orderid")
            if oid and oid in probe.order_place_ts:
                lat_ms = (m["ts"] - probe.order_place_ts[oid]).total_seconds() * 1000
                latencies.append(lat_ms)

    if latencies:
        print(f"\nLatency (order-placed → WS event):")
        print(f"  min: {min(latencies):.0f} ms")
        print(f"  max: {max(latencies):.0f} ms")
        print(f"  avg: {sum(latencies)/len(latencies):.0f} ms")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="WebSocket order update prototype")
    parser.add_argument("--symbol",         default="",    help="Trading symbol (e.g. NIFTY25MAY24600CE)")
    parser.add_argument("--token",          default="",    help="Symbol token from SmartAPI scrip master")
    parser.add_argument("--lot-size",       default=75,    type=int, help="Lot size (default 75 for Nifty)")
    parser.add_argument("--exchange",       default="NFO", help="Exchange (default NFO)")
    parser.add_argument("--reconnect-test", action="store_true", help="Also run the pre-connection recovery test")
    parser.add_argument("--connect-only",   action="store_true",
                        help="Connect, wait for AB00, listen for 30s, then exit — no orders placed")
    parser.add_argument("--listen",         default=30,    type=int,
                        help="Seconds to listen in --connect-only mode (default 30)")
    parser.add_argument("--bearer",         action="store_true",
                        help="Prepend 'Bearer ' to auth_token in headers (test if required)")
    args = parser.parse_args()

    if not args.connect_only and (not args.symbol or not args.token):
        print("ERROR: --symbol and --token are required (or use --connect-only).", file=sys.stderr)
        print("  Connection-only test (no orders):")
        print("    python tests/ws_order_test.py --connect-only")
        print("  Full order lifecycle test:")
        print("    python tests/ws_order_test.py \\")
        print("      --symbol NIFTY25MAY24600CE \\")
        print("      --token 12345 \\")
        print("      --lot-size 75")
        sys.exit(1)

    # Login
    api_key, user_name, password, qr_code = load_credentials()
    smart_obj, auth_token, feed_token = login(api_key, user_name, password, qr_code)

    # Auth token format test
    ws_auth = f"Bearer {auth_token}" if args.bearer else auth_token
    if args.bearer:
        print("[INFO] Using 'Bearer <token>' format in Authorization header")
    else:
        print("[INFO] Using raw JWT in Authorization header (no 'Bearer ' prefix)")

    # Build probe
    probe = OrderUpdateProbe(
        auth_token=ws_auth,
        api_key=api_key,
        client_code=user_name,
        feed_token=feed_token,
    )

    # Start WS in daemon thread
    ws_thread = threading.Thread(target=probe.connect, daemon=True)
    ws_thread.start()
    print(f"[{_ts()}] WebSocket thread started")

    try:
        if args.connect_only:
            print("\n" + "#"*60)
            print("CONNECTION-ONLY TEST (no orders)")
            print("#"*60)
            print(f"[{_ts()}] Waiting for AB00 connection-ack (up to 15s)…")
            if probe.ready.wait(timeout=15):
                print(f"[{_ts()}] AB00 received. Listening for {args.listen}s…")
                time.sleep(args.listen)
            else:
                print("ERROR: AB00 not received within 15s.", file=sys.stderr)
        else:
            ok = run_basic_test(smart_obj, probe, args.symbol, args.token, args.lot_size, args.exchange)
            if ok and args.reconnect_test:
                run_reconnect_test(smart_obj, probe, args.symbol, args.token, args.lot_size, args.exchange)
    except KeyboardInterrupt:
        print(f"\n[{_ts()}] Interrupted by user")
    finally:
        print_summary(probe)
        probe.close_connection()


if __name__ == "__main__":
    main()
