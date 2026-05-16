# Plan: Universal LTP WebSocket Migration

## 1. Background & Motivation
Currently, Athena and Artemis rely on REST API polling every 20-60 seconds to retrieve the Latest Traded Price (LTP) for underlying indices and option legs. 
This approach has significant limitations:
- **Rate Limiting:** Frequent API requests consume the strict API rate limit budget.
- **Slippage Risk:** A 20-second polling interval can result in massive slippage during sudden market volatility (e.g., Sensex flash crashes or Nifty spikes). By the time the bot wakes up to check the price, the Stop Loss or Parachute trigger may have already been violently breached.
- **Inconsistency:** Apollo already utilizes a highly efficient WebSocket feed for its dual-timeframe Supertrend logic. The other strategies should leverage this proven architecture.

*Correction Note:* Athena relies on the **CE Parachute** as its emergency upward defense (the PE wing is executed right at entry). The CE Parachute deployment requires instantaneous reaction to the Nifty Spot price, making the WebSocket feed critical.

## 2. Scope & Impact
- **Impacted Strategies:** Athena (`athena_engine.py`), Artemis (`iron_condor.py`).
- **Architectural Shift:** Extraction of Apollo's `websocket_feed.py` into a universal shared module.
- **Impacted Logic:** Removal of `sleep()` based polling intervals and `_poll_prices()` REST API calls in favor of high-frequency local dictionary reads.

## 3. Proposed Solution
1. **Shared Data Feed Module:** Abstract the existing `websocket_feed.py` from Apollo into a root-level or shared utility that any strategy can instantiate.
2. **Basket Subscription:** At trade entry, the strategy will register a "basket" of required tokens (e.g., Nifty Spot, CE Parachute strike, PE and CE spread legs) with the WebSocket listener.
3. **Local Memory State:** The WebSocket will run in a background thread, constantly overwriting a local dictionary (`self.current_prices`) with the absolute latest tick data.
4. Sub-second Monitoring Loop: The main strategy loop will change from a `sleep(20)` REST call to a `sleep(0.5)` local dictionary check, allowing near-instantaneous triggering of SL and Parachute conditions.

## 4. Decoupling Monitoring from Reporting (The Apollo Pattern)
To prevent Slack spam while achieving sub-second safety, we will adopt the **Apollo Reporting Pattern**:
- **Monitoring Loop (High Frequency):** The main loop will run with a minimal sleep (e.g., 500ms). It will execute `_check_slack_commands()`, poll the local WebSocket memory for SL/Parachute triggers, and increment a local `self._update_elapsed` counter.
- **Gated Reporting (Low Frequency):** Based on the existing `TRADE_UPDATE_INTERVAL` (e.g., 20 or 60 seconds), the bot will only execute `_send_trade_update()` and `_append_trade_log_row()` when `self._update_elapsed >= TRADE_UPDATE_INTERVAL`.
- **Reset:** After a report is sent, `self._update_elapsed` is reset to zero. This ensures the user sees exactly the same frequency of updates as before, but the strategy's "nervous system" is reacting at tick-speed.

## 5. Implementation Steps


### Phase 1: Abstraction & Refactoring
- Move `apollo_production/websocket_feed.py` to a shared location (e.g., `shared/` or `utils/`).
- Refactor the class to allow dynamic token subscription updates (adding/removing tokens on the fly without dropping the connection).
- Update Apollo to use the new shared module and verify it still functions flawlessly.

### Phase 2: Athena Integration (CE Parachute Focus)
- Integrate the shared WebSocket feed into `athena_engine.py`.
- Subscribe to the Nifty Index token (for VIX filtering and Spot tracking) and all active option leg tokens.
- Replace the `_poll_prices()` logic with local dictionary lookups.
- Specifically test the **CE Parachute** logic to ensure it deploys instantly when the Nifty Spot breaches the threshold in the local memory state.

### Phase 3: Artemis Integration (Stop Loss Focus)
- Integrate the shared WebSocket feed into `iron_condor.py`.
- Subscribe to the Sensex Index token and all 4 Iron Condor leg tokens.
- Replace the `monitor_trade()` sleep cycle with a sub-second local memory check.
- Verify that individual leg Stop Losses trigger immediately upon the tick crossing the SL price.

### Phase 4: Rate Limit Cleanup
- Remove any remaining `sleep()` buffers that were previously required to protect the REST API.
- Audit `functions.py` to ensure the rate limit counters are accurately reflecting the massive reduction in API calls.

## 5. Verification & Testing
- **Latency Benchmarking:** Log the timestamp of the WebSocket tick vs the timestamp of the SL order placement to confirm sub-second reaction times.
- **Stress Testing:** Run multiple bots simultaneously subscribing to overlapping tokens to ensure the shared/individual socket connections don't conflict or exceed Angel One's active socket limits.

## 6. Migration & Rollback Strategy
- Keep the `_poll_prices()` REST logic in the codebase but bypassed (e.g., as `_poll_prices_rest_fallback()`).
- Implement a heartbeat check: If the WebSocket hasn't received a tick for a subscribed token in X seconds during market hours, automatically fall back to the REST API to ensure the bot doesn't fly blind if the socket silently dies.