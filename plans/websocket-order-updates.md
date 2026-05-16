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

### Phase 1: Research & Prototyping
- Review the official `SmartApi` Python SDK for the specific Order Update WebSocket implementation. (Ensure distinction between Market Data WS and Order Update WS).
- Build a standalone prototype script `ws_order_test.py` to authenticate, connect, place a dummy order, and log the exact JSON payload structure returned by Angel One.

### Phase 2: Athena Integration (High Priority)
- Introduce the threading and WebSocket initialization to `athena_engine.py`.
- Rewrite `_fetch_order_details` to use local memory.
- Add a fallback mechanism (if the WebSocket disconnects, fallback to REST polling).
- Test entry and exit sequence timing.

### Phase 3: Artemis & Apollo Integration
- Port the validated WebSocket architecture to `iron_condor.py` (Artemis).
- Port the architecture to `apollo.py` (Apollo).
- Note: Apollo already has a market data WebSocket. Ensure the Order WebSocket does not conflict or block the Market Data WebSocket.

### Phase 4: Cleanup
- Remove rate limit counters and sleep delays previously used for the `OrderBook` API calls.
- Optimize the multi-leg order placement flow to fire subsequent legs immediately upon WebSocket confirmation.

## 5. Verification & Testing
- **Paper Testing:** Run the bots with 1 lot deep OTM options to trigger real network flow and verify the WebSocket catches the fill.
- **Disconnection Test:** Artificially close the WebSocket connection to ensure the daemon automatically reconnects or falls back to REST API safely without dropping the trade state.

## 6. Migration & Rollback Strategy
- The original REST API `_fetch_order_details` logic will be preserved in a renamed method (e.g., `_fetch_order_details_rest_fallback`).
- If the WebSocket stream becomes unstable in live markets, a boolean flag `USE_WS_ORDERS = False` in `configs_live.py` can be flipped to instantly revert to the old synchronous polling method without needing a code rollback.
