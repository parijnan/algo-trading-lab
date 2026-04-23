# Apollo Production — Nifty High-VIX Trend Following (> 25 VIX)

Live execution module for the Apollo debit spread strategy.
Part of the **Algo Trading Lab** project.

Production config: **D-R-D06g** (ITM debit spread, direction-split PT/gate/hard stop,
entry filters: no Tuesday trades, direction-specific day and candle exclusions).
See `APOLLO_PROJECT_CONTEXT.md` in the project root for full backtest history
and design decisions.

## Module structure

| File | Purpose |
|---|---|
| `apollo.py` | Entry point — `Apollo(obj, auth_token, instrument_df)` called by Leto |
| `configs_live.py` | All parameters — strategy + live execution |
| `websocket_feed.py` | WebSocket wrapper — connect, subscribe, OHLC aggregation, shutdown |
| `supertrend.py` | Supertrend seeding and incremental updates |
| `state.py` | Atomic trade state persistence |
| `functions.py` | Slack/Telegram messaging and exception handling |
| `logger_setup.py` | Dual console + file logging (logs/debug.log), level from configs_live |
| `technical_indicators.py` | SupertrendIndicator — copied from apollo_backtest/ |
| `tests/ws_test.py` | WebSocket layer validation harness |

## Setup on delos

```bash
cd /home/parijnan/scripts/algo-trading-lab/apollo_production
# Ensure data/ symlinks are in place:
#   data/user_credentials.csv -> ../../data/user_credentials.csv
#   data/holidays.csv         -> ../../data/holidays.csv
# Verify configs_live.py — DRY_RUN = False for live
# Apollo is launched via Leto — not run directly
```

## Session model

Apollo does not manage its own Angel One session. Leto owns login, market/holiday
checks, scrip master download, and session teardown. Apollo receives:
- `obj` — authenticated `SmartConnect` instance
- `auth_token` — JWT token from `generateSession` response (required for WebSocket feed)
- `instrument_df` — Nifty NFO rows filtered from the scrip master

Apollo owns everything else: Supertrend seeding, WebSocket feed start/stop,
the run loop, and all entry/exit logic. `apollo.run()` returns to Leto with the
feed already stopped. Leto then calls `terminateSession`.

## Key config flags

| Parameter | Testing value | Production value |
|---|---|---|
| `DRY_RUN` | `True` | `False` |
| `LOG_LEVEL` | `"DEBUG"` | `"INFO"` |
| `LOT_CALC` | `False` | `False` |
| `LOT_COUNT` | `1` | as required |
| `ST_75MIN_MULTIPLIER` | any | `3.0` |
| `ST_15MIN_MULTIPLIER` | any | `3.0` |
| `EXCLUDE_TRADE_DAYS` | `[]` | `[1]` (no Tuesday) |
| `EXCLUDE_BEARISH_DAYS` | `[]` | `[0]` (no Monday bearish) |
| `EXCLUDE_SIGNAL_CANDLES` | `[]` | `['10:00', '10:15', '14:15', '14:30']` |

## Exit mechanism stack (D-R-D06g)

| Priority | Mechanism | Bullish | Bearish |
|---|---|---|---|
| 1 | Hard stop | 40.0 pts | 67.5 pts |
| 2 | Profit target | 35% of max profit | 60% of max profit |
| 3 | Time gate (Day 1 09:30) | 25% of max profit | 35% of max profit |
| 4 | Trend flip | 15-min ST flips against position | — |
| 5 | Pre-expiry exit | 15:15 day before expiry | — |

## Trade state

`data/apollo_state.csv` — one row, atomically overwritten on every state change.
`status` field values: `idle` / `in_trade` / `exiting`.

On restart with `status = in_trade`, Apollo re-subscribes the option tokens
to the WebSocket feed and resumes monitoring from where it left off.

Leto checks this file before routing — if `status = in_trade` or `exiting`,
Leto routes to Apollo regardless of current VIX.

## Seeding strategy

On session start, `supertrend.py` fetches 600 15-min candles (60-day window)
from the Angel One API and saves them to `data/supertrend_cache.csv`. The cache
is updated after every 15-min candle close. This ensures Wilder's smoothing
warmup is always complete and ST values match the backtest exactly.

## Status

- [x] WebSocket layer — validated in live market
- [x] Supertrend seeding and update — validated
- [x] State management — validated
- [x] Entry/exit execution — live
- [x] Signal generation (D-R-D06g filters) — live
- [x] Clean shutdown (Ctrl+C / kill) — validated
- [x] Missed flip recovery on restart — implemented
- [x] Leto integration — session management moved to Leto