# Plan: Universal LTP WebSocket Migration (Athena + Artemis) — [IMPLEMENTED]

## Context

Apollo already uses `websocket_feed.py` for real-time LTP via a background WS daemon — all LTP reads are instant dict lookups, no API calls. Athena and Artemis still use REST polling every 20-60s. This creates two problems:
1. **Slippage risk**: Parachute or SL triggers can be badly delayed — by the time the bot wakes from `sleep(20)`, the market may have moved dramatically.
2. **Rate limit budget**: Every monitoring iteration burns quota from the strict Angel One API limits.

The plan migrates Athena and Artemis to the same WS architecture, decoupling SL/parachute monitoring (sub-second) from Slack reporting (configurable interval, e.g. 20s).

Reference: `plans/universal-ltp-websocket.md` — existing high-level plan doc.

---

## Design Decisions

| Decision | Choice | Reason |
|---|---|---|
| Strike selection | **Stays REST** | Unknown symbols before selection; can't pre-subscribe |
| Index tokens (Nifty, VIX) | **WS from startup** | Known tokens, pre-subscribed before any trading logic |
| Option leg tokens | **WS after entry** | Subscribe on entry, unsubscribe on exit (Apollo's pattern) |
| Monitoring loop interval | **500ms** | Sub-second SL/parachute reaction; free from WS dict |
| Reporting frequency | **Counter-gated at `TRADE_UPDATE_INTERVAL`** | Apollo's `_update_elapsed` pattern — same user-visible cadence |
| WS failure handling | **Log + Slack alert + REST fallback** | WS is enhancement, not replacement |
| REST fallback frequency | **`TRADE_UPDATE_INTERVAL` / `monitor_frequency`** | Revert to old behaviour — rate limits naturally respected |
| Shared feed module | **Root-level `websocket_feed.py`** | Move from Apollo; Apollo updated to import from root in same step; old file deleted |
| Feed instance ownership | **Strategy class** (`AthenaEngine`, `IronCondor`) | Passed into spread/engine instances, not a singleton |

---

## Files to Modify

- `apollo_production/websocket_feed.py` → **move + refactor** to `websocket_feed.py` (repo root)
- `apollo_production/apollo.py` → update import path
- `athena_production/athena_engine.py`
- `artemis_production/credit_spread.py`
- `artemis_production/iron_condor.py`
- All READMEs that reference `apollo_production/websocket_feed.py`

---

## Phase 1: Shared Feed Module

1. Create `websocket_feed.py` at repo root (move + refactor from `apollo_production/websocket_feed.py`).
2. Delete `apollo_production/websocket_feed.py` after move.
3. Refactor `websocket_feed.py`:
   - Remove Apollo-specific imports (`configs_live`, `NIFTY_TOKEN`, `VIX_TOKEN`, etc.).
   - Constructor accepts `auth_token, api_key, client_code, feed_token` (same as `OrderFillWatcher.start()`).
   - `startup_tokens` parameter (list of `(exchange, token)`) for index/VIX pre-subscription.
   - Dynamic subscription still via `subscribe_options(tokens)` / `unsubscribe_options(tokens)`.
   - Public interface unchanged: `get_ltp(token)`, `is_connected()`.
4. Update `apollo_production/apollo.py` to import from root-level `websocket_feed`.
5. Update all READMEs that reference `apollo_production/websocket_feed.py` to point to root-level `websocket_feed.py`.
6. Verify Apollo smoke-run still passes before proceeding to Phase 2.

---

## Phase 2: Athena Integration

### LTP points that move to WS (after entry, legs subscribed)

| File | Line | Old call | New call |
|---|---|---|---|
| `athena_engine.py` | 615 | `_get_ltp(NSE, 'NIFTY 50', NIFTY_INDEX_TOKEN)` | `self.feed.get_ltp(NIFTY_INDEX_TOKEN)` |
| `athena_engine.py` | 620 | `_get_ltp(NFO, sym, tok)` per leg | `self.feed.get_ltp(tok)` per leg |
| `athena_engine.py` | 686 | `_get_ltp(NSE, 'INDIA VIX', VIX_TOKEN)` | `self.feed.get_ltp(VIX_TOKEN)` |
| `athena_engine.py` | 757 | `_get_ltp(...)` spot + VIX pre-entry | `self.feed.get_ltp(...)` |

### LTP points that stay REST (unknown symbols at call time)

- `_find_delta_strike()` line 199 — strike scan
- `_calculate_lots()` lines 439-443 — pre-entry premium pricing

### Loop restructure (`run()` method)

**Current:** `sleep(TRADE_UPDATE_INTERVAL)` at line 773 — monitoring and reporting coupled.

**After:**
```python
self._update_elapsed = 0.0  # add to __init__

# in run() in_trade branch — replace sleep(TRADE_UPDATE_INTERVAL) with:
prices = self._poll_prices()       # now uses feed.get_ltp() — instant
if prices.get('spot'):
    self._manage_emergency_hedge(prices['spot'])
    if self._update_elapsed >= TRADE_UPDATE_INTERVAL:
        self._append_trade_log_row(prices=prices)
        self._send_trade_update(prices=prices)
        self._update_elapsed = 0.0
sleep(0.5)
self._update_elapsed += 0.5

# fallback: if not self.feed.is_connected(), sleep(TRADE_UPDATE_INTERVAL) and use REST
```

### WS feed lifecycle in Athena

- Instantiate feed in `AthenaEngine.__init__()`.
- Call `feed.start(auth_token, api_key, client_code, feed_token)` at session start, pre-subscribing `[(NSE, NIFTY_INDEX_TOKEN), (NSE, VIX_TOKEN)]`.
- After `_execute_entry()` completes: `feed.subscribe_options(leg_tokens)`.
- After `_execute_exit()` completes: `feed.unsubscribe_options(leg_tokens)`.

### `_poll_prices()` — REST fallback path

Keep the method. Add at top:
```python
if not self.feed.is_connected():
    # log warning + Slack once (debounced), return REST result
    return self._poll_prices_rest()
```
Rename current body to `_poll_prices_rest()`.

---

## Phase 3: Artemis Integration

### LTP points that move to WS

| File | Line | After |
|---|---|---|
| `credit_spread.py` | 881 | `self.index_ltp = self.feed.get_ltp(index_token)` |
| `credit_spread.py` | 882 | `self.buy_ltp = self.feed.get_ltp(self.buy_token)` |
| `credit_spread.py` | 883 | `self.sell_ltp = self.feed.get_ltp(self.sell_token)` |
| `credit_spread.py` | 887 | `self.additional_buy_ltp = self.feed.get_ltp(self.additional_buy_token)` |
| `credit_spread.py` | 904, 920 | `self.index_ltp = self.feed.get_ltp(index_token)` |
| `credit_spread.py` | 936 | `self.sell_ltp = self.feed.get_ltp(self.sell_token)` |

### LTP points that stay REST

- `initialize_spread()` lines 446, 458, 478 — strike scan / premium ladder
- Lines 489, 539 — ELM re-initialization

### Loop restructure (`iron_condor.py`)

`_sleep_for_set_time()` (line 709) currently does `sleep(monitor_frequency)`.

**After:**
```python
# IronCondor.__init__: add self._update_elapsed = 0.0

def _sleep_for_set_time(self):
    if not self.feed.is_connected():
        sleep(monitor_frequency)          # fallback: old behaviour
        reset_counters()
        return
    sleep(0.5)
    self._update_elapsed += 0.5

# continue_monitoring(): gate _send_status_update() and _update_trade_log():
def continue_monitoring(self):
    if ...:
        # SL checks (monitor_trade) already ran — always execute
        if self._update_elapsed >= monitor_frequency or not self.feed.is_connected():
            self._update_trade_book()
            self._update_trade_log()
            self._send_status_update()
            self._update_elapsed = 0.0
        self._sleep_for_set_time()
        self._set_current_datetime()
```

### WS feed lifecycle in Artemis

- Feed instantiated in `IronCondor.__init__()`, passed to `CreditSpread.__init__()` as `feed` parameter.
- Started at `set_session()` with index pre-subscription (Nifty + VIX).
- `feed.subscribe_options(leg_tokens)` called after each `execute_spread()` completes for PE and CE legs.
- `feed.unsubscribe_options(leg_tokens)` after each `exit_spread()`.

### WS failure handling (both strategies)

```python
# Add to feed's on_close() / heartbeat:
logger.warning("WS LTP feed disconnected.")
slack_bot_sendtext("⚠️ *[Strategy]*: WS LTP feed disconnected — REST fallback active.", "#error-alerts")
```
Debounce flag prevents repeated Slack messages if the connection flaps.

---

## Phase 4: Rate Limit Cleanup

After Phases 2 and 3 are stable:
- Audit `functions.py` `_counters` — `ltp` counter limit was set defensively for 20s polling bursts. With WS, REST LTP calls only happen in fallback or during strike selection. Limits can be revised.
- Review any `reset_counters()` calls that were placed to recover from `sleep()` coupling.

---

## Implementation Order

1. Move + refactor `apollo_production/websocket_feed.py` → root `websocket_feed.py`
2. Update Apollo import — smoke-test Apollo
3. Athena: `_poll_prices()` → WS + `_poll_prices_rest()` fallback
4. Athena: loop restructure (500ms + `_update_elapsed`)
5. Athena: WS lifecycle (start, subscribe, unsubscribe)
6. Artemis: `_fetch_ltp()` in `monitor_spread()` → WS
7. Artemis: loop restructure (`_sleep_for_set_time()` + `_update_elapsed`)
8. Artemis: WS lifecycle (feed passed to `CreditSpread`)
9. Rate limit cleanup

---

## Verification

1. **Apollo regression**: After Phase 1, run Apollo in DRY_RUN through a full candle cycle. Confirm WS ticks flow and `feed.get_ltp()` returns valid prices.
2. **Athena WS smoke test**: Set `DRY_RUN = True`. Confirm `_poll_prices()` reads from feed dict (log tick timestamps). Confirm `_manage_emergency_hedge()` reacts within 500ms of a threshold breach.
3. **Artemis WS smoke test**: Confirm `monitor_spread()` reads from feed dict. Confirm SL evaluation fires every 500ms. Confirm `_send_status_update()` fires every `monitor_frequency` seconds (not every 500ms).
4. **Fallback test**: Kill the WS connection mid-session. Confirm REST fallback activates, Slack alert fires, monitoring continues at 20s.
5. **Rate limit audit**: After full session, confirm `_counters['ltp']` count is low (only REST fallback and strike selection calls, not monitoring loop calls).
