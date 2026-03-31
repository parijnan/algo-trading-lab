# Apollo Production — Nifty High-VIX Trend Following (Live Execution)

Live execution module for the Apollo debit spread strategy.
Part of the **Algo Trading Lab** project.

Production config: **D-R03fg-hs-b** (ITM debit spread, PT 50%,
time gate 1d/33%, hard stop 67.5 pts). See `APOLLO_PROJECT_CONTEXT.md`
in the project root for full backtest history and design decisions.

## Module structure

| File | Purpose |
|---|---|
| `apollo.py` | Main entry point — Apollo class and run loop |
| `configs_live.py` | All parameters — strategy + live execution |
| `websocket_feed.py` | WebSocket wrapper — connect, subscribe, shutdown |
| `supertrend.py` | Supertrend seeding and incremental candle updates |
| `state.py` | State file read/write helpers |
| `tests/ws_test.py` | WebSocket layer validation harness |

## Setup on delos
```bash
cd /home/parijnan/scripts/algo-trading-lab
git pull
cd apollo_production
# Ensure data/ exists with required credential file
# Edit configs_live.py — verify all parameters match D-R03fg-hs-b
/home/parijnan/anaconda3/bin/python apollo.py
```

## Cron (delos)
```
# Apollo — start at 09:14, Mon-Fri
14 9 * * 1-5 cd /home/parijnan/scripts/algo-trading-lab/apollo_production && /home/parijnan/anaconda3/bin/python apollo.py >> logs/apollo_$(date +\%Y\%m\%d).log 2>&1
```

## Status

- [ ] WebSocket layer (`websocket_feed.py`) — validated
- [ ] Supertrend seeding and update (`supertrend.py`) — in development
- [ ] State management (`state.py`) — in development
- [ ] Entry/exit execution (`apollo.py`) — in development
- [ ] Paper trading validation — pending
- [ ] Live (1 lot) — pending
```

---

**Commit message:**
```
feat(apollo_production): scaffold live execution module

Add apollo_production/ directory structure for the Apollo debit spread
live execution module (production config D-R03fg-hs-b).

- apollo_production/ folder with README, placeholder files, data/ and
  logs/ directories with .gitkeep
- tests/ws_test.py: WebSocket layer validation harness — tests feed
  start/stop, Nifty+VIX subscribe on connect, mid-session option
  subscribe/unsubscribe, thread-safe shared state, and clean shutdown
  with ctypes fallback for SDK close_connection() bug
- .gitignore additions: exclude live state, trade logs, ST cache,
  and SDK log output from version control

WebSocket layer test pending market hours validation on delos.