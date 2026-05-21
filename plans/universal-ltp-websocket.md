# Plan: Universal LTP WebSocket Migration

**Status: IMPLEMENTED** — All phases complete as of May 2026.

## What was built

`websocket_feed.py` at repo root provides `SharedFeed` — a background WebSocket daemon that
maintains a live `{token: ltp}` dict. All three strategies use it:

- **Apollo** — original implementation; seeded Nifty + VIX at startup, subscribes/unsubscribes option legs on entry/exit.
- **Athena** — `SharedFeed` instantiated in `AthenaEngine.__init__()`. Nifty + VIX pre-subscribed. `_poll_prices()` reads from `feed.get_ltp()`. Monitoring loop runs at 500ms with `_update_elapsed` counter gating Slack/log updates to `TRADE_UPDATE_INTERVAL`.
- **Artemis** — `SharedFeed` instantiated in `IronCondor.__init__()`, passed to `CreditSpread`. Sensex index pre-subscribed. `monitor_spread()` reads from `feed.get_ltp()`. Loop runs at 500ms with `_update_elapsed` gating status updates to `monitor_frequency`.

REST `_get_ltp()` / `_fetch_ltp()` calls are retained as fallback when `feed.is_connected()` returns False.

## Original motivation

Athena and Artemis previously polled REST every 20-60s. SL and parachute triggers could be delayed
by up to the full polling interval. Apollo already used WS — the migration brought parity.
Rate limit budget also freed up significantly (LTP calls now only happen in fallback or strike scan).
