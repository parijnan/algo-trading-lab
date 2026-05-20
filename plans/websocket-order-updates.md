# Plan: Order Update WebSocket Migration

## 1. Background & Motivation
Currently, all active strategies (Athena, Apollo, Artemis) rely on **Synchronous REST API Polling** to verify order execution. After an order is placed, the bots sleep and repeatedly call the `OrderBook` endpoint until the order ID appears as 'complete'. 
This approach has several drawbacks:
- **Rate Limiting:** Frequent polling can exhaust the 1 request/second limit for the OrderBook endpoint.
- **Legging Risk:** The 1-2 second sleep delays the execution of subsequent legs in multi-leg strategies (Double Calendars, Iron Condors), exposing the position to market slippage.
- **Blocking Threads:** The main strategy loop is blocked during polling, preventing it from monitoring emergency exits, Slack flags, or real-time PnL.

Migrating to the **Order Update WebSocket** will eliminate polling by pushing real-time order status updates from Angel One directly into a shared local memory state, allowing microsecond reactions and completely bypassing OrderBook rate limits.

## 2. Scope & Impact
- **Impacted Strategies:** Athena (`athena_engine.py`), Apollo (`apollo.py`), Artemis (`iron_condor.py`).
- **Impacted Logic:** The `_fetch_order_details` method and the quantity-splitting logic in all strategies.
- **Architectural Shift:** Introduces a multi-threaded architecture (Main Strategy Loop + Background WebSocket Daemon) to all bots.

## 3. Proposed Solution
1. **Shared Memory Dictionary:** Each strategy class will instantiate a `self.live_orders = {}` dictionary.
2. **Background Daemon Thread:** A new method `_start_order_websocket()` will authenticate with the Angel One SmartWebSocket and run continuously in a background daemon thread.
3. **Event Callback:** When an order update is received over the socket, the `on_message` callback will parse the JSON and update `self.live_orders[order_id]`.
4. **Non-Blocking Fetch:** The `_fetch_order_details()` method will be rewritten to run a fast `while` loop (with `sleep(0.05)`) that only checks the local `self.live_orders` dictionary, rather than making external HTTP requests.

## 4. Implementation Steps

### Phase 1: Research & Prototyping — COMPLETE (2026-05-20)

Live testing via `tests/ws_order_test.py` on both NFO (Nifty) and BFO (Sensex) confirmed:

**Delivery path:**
- AB00 (connection-ack) arrives on `on_data`, not `on_message`
- All order events arrive on `on_data`

**Event sequence (consistent across both exchanges):**
- AB09 (after-market-delete) fires first, then AB01 (open), then AB05 (complete) — all
  within ~1ms of each other. AB09 must **not** be treated as a terminal event; always wait
  for AB05 before treating an order as done.

**Field types (from live `orderData` payload):**
| Field | Type | Notes |
|---|---|---|
| `filledshares` | `str` | Cast required: `int(data['filledshares'])`. Empty string `''` on non-filled states. |
| `averageprice` | `float` | No cast needed. |
| `updatetime` | `str` | Format: `'%d-%b-%Y %H:%M:%S'` (e.g. `20-May-2026 11:13:29`) |
| `orderid` | `str` | Echo of the placed order ID |
| `quantity` | `str` | Total requested quantity |

**Latency:** 150–290ms from `placeOrder()` call to WS event arrival (VPS → broker → VPS round trip).

**`individual_order_details()` ruled out:** Returns `AB1007: Order not found` for all
confirmed-filled orders on this account (both NFO and BFO). REST polling via `orderBook()`
remains the fallback. See `plans/individual-order-details.md`.

### Phase 2: Athena Integration — COMPLETE (2026-05-20)

`OrderFillWatcher` daemon added to `athena_production/functions.py`. `_order_watcher` instantiated in `Athena.__init__`, started at the top of `Athena.run()` via `obj.getfeedToken()`. `_fetch_order_details` rewritten with WS fast-path prepended to REST fallback. All 5 call sites unpack 3-tuple `(avg_price, filled_lots, fill_time)`.

### Phase 3: Artemis & Apollo Integration — COMPLETE (2026-05-20)

**Apollo:** `OrderFillWatcher` added to `apollo_production/functions.py`. Instantiated in `Apollo.__init__`, started in `_setup()`. WS fast-path added to `_fetch_order_details`. All 4 call sites updated.

**Artemis:** `OrderFillWatcher` added to `artemis_production/functions.py`. `_order_watcher = None` in `CreditSpread.__init__`; one shared watcher created in `IronCondor.set_session()` and assigned to both `pe_spread` and `ce_spread`. `auth_token` threaded through `leto._run_artemis()` → `artemis.run()` → `iron_condor.set_session()` → `watcher.start()`. All 17 call sites in `credit_spread.py` updated.

### Phase 4: Cleanup — Partial (2026-05-20)

Rate limit counters and REST polling are **retained** as the active fallback. The WS fast-path is prepended and transparent — if the socket is not ready or times out after `ORDER_TIMEOUT_SEC`, execution falls back to the original REST path with no disruption. The `USE_WS_ORDERS` flag from the original plan was not implemented; the WS path is always attempted first and silently skipped if not ready.

## 5. Final Architecture

```
OrderFillWatcher (daemon thread)
  ├── connects via SmartWebSocketOrderUpdate
  ├── sets _ws_ready event on AB00 ack
  ├── populates live_orders{orderid: orderData} on AB05/AB02/AB03
  └── reconnects automatically on close

_fetch_order_details()
  ├── if _ws_ready: poll live_orders every 50ms for up to ORDER_TIMEOUT_SEC
  │     └── on timeout or not ready: fall through to REST
  └── REST fallback: original orderBook() polling loop (unchanged)
```

**Status: IMPLEMENTED** — Committed 2026-05-20 as `2016ff6 Feature: WebSocket order fill verification for Apollo, Athena, Artemis`.
