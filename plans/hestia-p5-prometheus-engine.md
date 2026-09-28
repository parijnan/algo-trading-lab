# Hestia P5: the Prometheus engine, verified by replay against recorded live days

**Status: started 2026-09-28. Recorded data pulled (read-only, approved) and reconnoitred; nothing built yet.** Phase P5 of `plans/selene-production.md` §10. Gate: the Prometheus engine, driven through the fake Hestia over recorded live days, agrees decision for decision with what live Prometheus did (`prometheus_trades.csv`, the dated logs, the recorded state), including at least one trend flip with Rule 7, a stop, a target scale-out, the machinery around a roll, and the injected-fault cases; a full test suite. `prometheus_production/` stays untouched as the rollback and as the reference.

## 1. What was pulled

Read-only, each command approved by the user, from Delos into `hestia_data/replay_pull/` (gitignored), 3.8 MB in all:

- `data/`: `prometheus_trades.csv` (40 trades, ids 9 to 49, 2026-09-04 to 2026-09-28), `prometheus_state.csv`, `trade_counter.txt`, `trade_logs/` (one running-row file per trade).
- `logs/`: the dated session logs `prometheus_20260915.log` to `prometheus_20260928.log` (10 live sessions, `DRY_RUN=False` since 2026-09-15; the cron duplicates and manual logs were not pulled). The 09-28 log is a snapshot: trade 50 was open on Delos when it was taken, so **live Prometheus was trading during this work and nothing here touched it**.
- The 1-minute price history already on this machine (the pipeline sync): CRUDEOILM `2026-10-19_futures.csv` runs to 2026-09-25 23:30, enough for 09-15 to 09-25.

## 2. Reconnaissance: what the recorded days can and cannot prove

The logs record every 15-minute bar as `15m bar HH:MM — ST=… close=…  no flip | FLIP -> direction`, plus every order, fill, entry, exit and Rule 7 line, so the logs are a complete decision oracle. Comparing Hestia's data path (`history.resample_1m` and `indicators.compute_st` over the pipeline's 1-minute file, seeded 18 days back) with the logged bars, per day:

| Date | bars logged | all fields match | close differs | flip differs | ST differs (worst) |
|---|---|---|---|---|---|
| 09-15 | 54 | 30 | 3 | 0 | 24 (0.19) |
| 09-16 | 57 | 21 | 5 | 0 | 35 (3.25) |
| 09-17 | 57 | 41 | 4 | 0 | 12 (0.13) |
| 09-18 | 57 | 36 | 5 | 0 | 19 (0.40) |
| 09-21 | 57 | 24 | 5 | 0 | 31 (1.93) |
| 09-22 | 57 | 15 | 7 | 0 | 42 (0.70) |
| 09-23 | 57 | 48 | 1 | 2 | 8 (153.6 at one bar) |
| 09-24 | 57 | 52 | 5 | 0 | **0 (0.005)** |
| 09-25 | 57 | 41 | 10 | 0 | 8 (0.30) |
| total | 510 | 308 | 45 | **2** | 179 |

**Findings.**
1. **The implementation is right.** 09-24 matches to the logged precision (ST within 0.005, every flip), and 508 of 510 flip verdicts agree over all ten days. The seed, the resampler, the 09:00 anchor and Supertrend are the same numbers as live.
2. **The differences are the live system's own imperfections, not Hestia's.** A live bar is built once at its boundary from whatever minutes the poller has delivered (waiting up to the cutoff), and is never rebuilt when a late minute arrives; the pipeline's nightly file is the after-the-fact complete one. Example, 09-16 13:00: live close 9526, pipeline 9531, because the live window lacked its last minute. One missing minute shifts a close by a point or two, and because Supertrend's ATR carries history, the ST then differs by tenths for hours. Hestia's live data service builds bars the same way, so it will have the same behaviour live; nothing is to be "fixed" here.
3. **The two flip mismatches are one razor-thin cross.** 09-23 22:15: live close 8791 against a live ST of 8791.89 (bearish, entry at 22:30, trade 38); the pipeline's slightly different ST is 8790.63, so its close of 8791 does not cross and the flip appears one bar later. That is exactly the thin-cross class the provisional margin guard is about, caused by an earlier partial bar the same day (21:15 live ST 8778.44 vs 8780.54).
4. **The recorded window contains no contract roll.** The roll to the 2026-10-19 contract happened around 09-15/16, on the day of a Kill Switch (`Teardown after KILL — leaving open position untouched`), and the log window has no rollover or coincident-flip decisions. It does contain: Rule 7 flips (many), target1 and target2 scale-outs, one stop-loss (trade 43, 09-24 23:15), and the Kill Switch. The first live roll of this contract is around 2026-10-12 to 10-13 (the 5-trading-day window before the 2026-10-19 expiry, holiday-aware; roll eve is the evening before). **So the roll logic cannot be verified by replay; it needs the unit tests of Prometheus's own rollover suite ported, the historical parity tests, and a DRY_RUN Selene roll (P8) as the first real exercise.** The user should know this before P6.

**Consequence for the method.** Two tiers, both needed:
- **Tier 1, decision replay.** Drive the engine with the *logged* bar events (bar time, close, ST, flip; the previous bar's ST is the previous log line's) and the 1-minute prices for the LTP path, and compare its requests and state changes with the logged orders and `prometheus_trades.csv`. This isolates the engine's decisions from data imperfections and can match exactly on flips and entries; exits by stop or target are compared to the minute (live watched ticks every half second, replay has 1-minute highs and lows).
- **Tier 2, data-path agreement** (the table above): kept as a permanent regression check against the pulled files, with the accepted tolerance stated (flip verdicts within 2 of 510, ST exact on a clean day).

## 3. Slices

| Slice | Work | Gate |
|---|---|---|
| **P5.1** | The **roll-policy library**, standalone and tested first (plan §3, §4): 5-working-day roll day, coincident-flip re-entry when the new contract's ST agrees, else fallback at rollover time (23:15, 23:40 on 23:55 sessions) with the ST-disagreement veto and the basis recalibration of the stop; "no next contract means flatten". Pure functions over bars and prices, no I/O. Ported from `prometheus_production` §4 to §9 and §18, checked against its tests | Its own tests, ported Prometheus roll tests, no dependency on Hestia |
| **P5.2** | **Recorded-day tooling** (now a scratch script) becomes a repo tool: parse the logs into bar events, orders, fills and state, and the trades file into the oracle; `LoggedData` (a `DataPort` over the logged bars plus 1-minute prices) | Tool reproduces the table above from the pulled files |
| **P5.3** | The **Prometheus engine**: the decision layer of `prometheus.py` against `EngineContext` (2-lot scale-out, targets 1 and 2, stop, Rule 7 as a `FlipRequest`, sizing from the live sizing config and margin per unit, provisional-boundary logic with the previous-bar margin guard, missed-flip and market-close recovery, the request-pending state so a stop never re-fires while a request is in flight, request ids that include the session date and trade id) | Engine unit tests; ports of `tests/test_prometheus_*.py` (about 1,750 lines) |
| **P5.4** | **Tier 1 replay** of the ten recorded sessions and the comparison report; the injected-fault cases (a rejected exit, a partial fill, an UNCONFIRMED exit, an engine crash and resume mid-trade, a restart with an open position) | Decision-for-decision agreement, differences explained |

## 4. Decisions and open points

- **No further Delos pull is needed now.** The state file shows `last_processed_boundary` empty on Delos at the time of the pull; noted for the port's missed-flip logic, which keys on it.
- **Trade 50 is open on Delos** (bearish, 5 units, entry 9149.95 at 17:45 on 2026-09-28 at the time of the pull). Any restart of the standalone process, and any later cutover, follows the restart skill; the Hestia port never runs beside it (one account, one session).
- **Prometheus's recorded live P&L is not the engine's job to reproduce to the rupee**: fills come from the real market, replay fills at the 1-minute price. The comparison is on decisions, times and reasons, with fill prices reported alongside.
