# Apollo Production — Nifty High-VIX Trend Following (Live Execution)

Live execution module for the Apollo debit spread strategy.
Part of the **Algo Trading Lab** project.

Production config: **D-R-P2c** (ITM debit spread, PT 50%, time gate 1d/33%,
hard stop 67.5 pts, entry filters: no Tuesday trades, four excluded signal
candle times). See `APOLLO_PROJECT_CONTEXT.md` in the project root for full
backtest history and design decisions.

## Module structure

| File | Purpose |
|---|---|
| `apollo.py` | Main entry point — Apollo class and run loop |
| `configs_live.py` | All parameters — strategy + live execution |
| `websocket_feed.py` | WebSocket wrapper — connect, subscribe, OHLC aggregation, shutdown |
| `supertrend.py` | Supertrend seeding (cache-first), incremental updates, logout cache write |
| `state.py` | Atomic trade state persistence |
| `functions.py` | Slack/Telegram messaging and exception handling |
| `logger_setup.py` | Dual console + file logging (logs/debug.log), level from configs_live |
| `technical_indicators.py` | SupertrendIndicator — copied from apollo_backtest/ |
| `tests/ws_test.py` | WebSocket layer validation harness |

## Setup on delos

```bash
cd /home/parijnan/scripts/algo-trading-lab
git pull
cd apollo_production
# Copy data/user_credentials.csv and data/holidays.csv into data/
# Verify configs_live.py — DRY_RUN = True for paper trading, False for live
/home/parijnan/anaconda3/bin/python apollo.py
```

## Cron (delos)

```
# Apollo — start at 09:14, Mon-Fri
14 9 * * 1-5 cd /home/parijnan/scripts/algo-trading-lab/apollo_production && /home/parijnan/anaconda3/bin/python apollo.py >> logs/apollo_$(date +\\%Y\\%m\\%d).log 2>&1
```

## Key config flags

| Parameter | Testing value | Production value |
|---|---|---|
| `DRY_RUN` | `True` | `False` |
| `LOG_LEVEL` | `"DEBUG"` | `"INFO"` |
| `ST_75MIN_MULTIPLIER` | any | `3.0` |
| `ST_15MIN_MULTIPLIER` | any | `3.0` |
| `EXCLUDE_TRADE_DAYS` | `[]` | `[1]` |
| `EXCLUDE_SIGNAL_CANDLES` | `[]` | `['09:45', '10:00', '13:45', '14:00']` |

## Seeding strategy

On first run (or after a long absence), `supertrend.py` fetches 600 15-min
candles (60-day window) from the API and saves them to `data/nifty_15min_cache.csv`.
On subsequent runs it loads from cache and reconciles with the last few API
candles, accounting for weekends and holidays. At logout, today's candles are
appended to the cache and it is trimmed back to 600 rows.

This avoids the Wilder's smoothing warmup problem — 600 candles of history
ensures the ST values match the backtest and charting platform exactly.

## Status

- [x] WebSocket layer (`websocket_feed.py`) — validated in live market
- [x] Supertrend seeding and update (`supertrend.py`) — validated
- [x] State management (`state.py`) — validated
- [x] Entry/exit execution (`apollo.py`) — dry run validated
- [x] Signal generation — dry run validated (D-R-P2c filters confirmed)
- [x] Dry run fill prices — validated (correct option LTPs used)
- [x] Clean shutdown (Ctrl+C / kill) — validated
- [x] Missed flip recovery on restart — implemented
- [x] 15-min candle cache — validated (cache-first seeding confirmed)
- [ ] Full session dry run with entry + exit + trade log — pending
- [ ] Live (1 lot on delos) — pending