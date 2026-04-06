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