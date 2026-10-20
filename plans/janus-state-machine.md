# Plan: Janus state machine on range-based levels (user design 2026-10-20)

**Status: built and verified 2026-10-20 for CRUDEOILM (touch entries, plain thirds); first run recorded in section 6.** Research only, like the rest of Janus: nothing here touches live code, Hestia or Delos. Companion to `plans/janus-range-levels.md` (where the High and Low come from) and `plans/janus-camarilla-research.md` section 9 (the Phase 1 rules).

## 1. Why a state machine, and why not a sweep

The thesis (user): a range forms because bulls and bears are in equilibrium, and price mean-reverts inside it; when one side is overwhelmed the market trends until it settles and forms its next range. Camarilla levels are one way to trade that, and regular or Fibonacci pivots can be tried with the same concept afterwards. Timing of each level matters, so the work is a walk through each session minute by minute that keeps the state of every level and every setup, and writes down what happened in order, not a pooled sweep. Current work is CRUDEOILM only.

## 2. Rules (the existing Phase 1 rules, confirmed by the user 2026-10-20)

- **Levels** fixed before the open: the range from the setter-candle logic on the traded contract's own daily bars (back-month sessions included) gives High and Low, the previous session's close gives C, formulas as in `janus_levels.py`. `previous_session` stays available as the control (`--source previous_session`).
- **Fade** (mean reversion, entry on the reclaim, not on the first touch): long as price rises back through S3 after having traded below it (stop S4, targets R1 / R2 / R3); short as price falls back through R3 after having traded above it (stop R4, targets S1 / S2 / S3). The user confirmed the existing rule over a first-touch fade.
- **Breakout** (a new range is being established, targets 5, 6, 7): long as price rises through R4 (stop R3, targets R5 / R6 / R7); short as price falls through S4 (stop S3, targets S5 / S6 / S7).
- **Position:** three equal lots, lot k exits at target k, one stop for all open lots, no trail by default. A stop is a touch of the stop level. The third target closes anything still open.
- **Stop-and-reverse** is not coded as a rule: a breakout's stop (R3 or S3) is the fade's entry level and a fade's stop (R4 or S4) is the breakout's entry level, so the opposite entry fires at the same price from the same event loop. No cap on entries per day; a setup that has finished enters again the next time price crosses its entry level the right way.
- **Triggers:** touch for entries, stops and targets (close triggers later, user: "start with touch"). A touch entry crossed in the gap fills at the open; limit-type fills (targets, re-buys) fill at the level.
- **Time rules (provisional):** no entry in the first minute, none in the last 60 minutes, everything open exits 15 minutes before the last bar.
- **Bar order:** the order of high and low inside a minute is unknown, so every bar is walked along an assumed path (neutral; adverse = against the open net position). The logs are neutral; the adverse run is made alongside and `order_sensitive` in `trades.csv` marks the trades that change.

## 3. Toggles (user 2026-10-20), all off by default, all parameters in `janus_configs.py`

- **Re-buy** (`MACHINE_REBUY`, `--rebuy`): booked lots are bought back at the original entry level when price returns to it, for their own targets. Earlier results (plan 9b): adds about nothing.
- **Counter trade** (`MACHINE_COUNTER`, `--counter`): a fade's T3 opens its sister setup at that level. Earlier results: loses. Kept as a switch so the A2 and A3 arms stay reproducible; the user did not ask for it to be on.
- **Trailing stop** (`MACHINE_TSL`, `--tsl`): the stop is a property of the trade. `TSL_RULES` says where it goes after target k: default `{1: 'entry', 2: 'T1'}` (after T1 to the trade's first fill, after T2 to the T1 level); `'entry'`, `'T1'`, `'T2'` or `None`; it never moves backwards. Consequences written down so they can be corrected: a trailed stop is a plain stop and closing at it does NOT open the sister setup (it is no longer the sister's entry level); with re-buy also on, a stop at the entry level is checked before the re-buy at the same price, so the booked lots are not bought back (this is the collision plan 9a noted); the lot exit reason is `stop_trailed` once the stop has moved. Plan 9a found a trail to T1 after T2 destructive for fades, so the default rules are a starting point, not a recommendation.

## 4. What is logged (CSV, `janus_backtest/outputs/machine/<symbol>_<source>_rebuy-*_counter-*_tsl-*/`, gitignored, emptied at the start of each run)

- **`trades.csv`**, one row per trade: key (`date_setup_HHMM`, from the trade itself so it does not shift when a rule changes), setup, family, side, kind (entry or counter), entry time and price, initial and final stop, stop moves, risk in points and % of price, T1/T2/T3, legs, re-buy cycles, exit time, minutes held, lot exit reasons, P&L in points, R and % of price (summed over lots), MFE and MAE in points and R with the minute each was reached and minutes from entry, the range record (setter date, age, extensions, high, low), R of the levels against R of the control (previous-session) levels, level source, the toggles, bar order, `order_sensitive` and the P&L under the other bar order.
- **`legs.csv`**, one row per lot leg: its own entry and exit (time, price, reason), P&L, and its own MFE and MAE with times.
- **`events.csv`**, the ordered event log of every session: first touch of each of S7..R7 and C, entries, counter entries, lot exits, stops (with `gap` when filled at the open), stop moves, re-buys, hard exits.
- **`sessions.csv`**, one row per session: the range record, the control R and the full level set.
- **`minute/<trade_key>.csv`**, one row per minute from the entry bar to the exit bar: OHLC and volume, lots open / booked / closed, the live stop, the next target and the distances to it and to the stop, realised and unrealised points (marked at the bar close) and in R, running MFE and MAE with their minutes, the events of that minute.
- **Excursions** are measured from the fill. A lot's window is its fill to its exit and a trade's window is its first fill to its last exit. On the entry bar only the part of the assumed path after the fill counts, on the exit bar only the part up to the exit; a gap counts for a position that is open across it, not for a trade opened in it. The minute of an extreme is the bar in which it was first reached.

## 5. Verification (2026-10-20)

- `tests/test_janus_machine.py` (25 tests, each behaviour broken on purpose and caught: gap handling, whole-bar excursions, stop moves backwards, stop-before-re-buy priority, key uniqueness, toggle wiring, short-side signs, first-touch-only level events). Random-walk parity against `janus_engine.simulate_session` (touch) for all four arms and both bar orders, 30 sessions each.
- `machine_run.py --parity`: the machine with TSL off equals the engine leg for leg on real CRUDEOILM sessions (run 2026-10-20: all 884 Fyers sessions x 2 bar orders x 4 arms = 7,072 runs, 34,046 reference legs, 0 differ; the 40 latest sessions including the Angel One tail, 320 runs, 0 differ).
- Independent bound check of every MFE and MAE in the first run against the raw 1-minute bars: each lies between the value from the bars strictly between entry and exit and the value from the inclusive window, and each extreme's minute lies inside the trade.

## 6. First run and results

Data: the first run used Fyers only and stopped at 2026-09-21 (the October contract had no Fyers file because it had not been downloaded after its 19 Oct expiry); the tail is now on by default (`MACHINE_LIVE_TAIL`, `--no-tail` for Fyers only), so the run reaches the last full session, Mon 2026-10-19. After the 2026-10-19 contract was downloaded from Fyers (36,060 rows, 2026-10-20) the run has 897 Fyers sessions and 6 Angel One tail sessions (12 to 19 Oct, traded on the November contract, which has no Fyers file until it expires on 19 Nov; `data_source` column).

Base case (range-setter levels, plain thirds, touch, re-buy / counter / TSL off): 903 sessions, 1,425 trades, 4,275 legs, +264.3% of price per lot summed over legs (+239.6 R, +0.056 R per leg), no costs. By setup (trades, R): F_L 468 / +121.3, B_S 235 / +85.4, F_S 495 / +19.0, B_L 227 / +13.9. Mean MFE about 0.9 to 1.2 R and mean MAE about 0.67 R in every setup. One trade changes under the adverse bar order and four trades exist only under it. Gross, before costs and before any look at the control (previous-session levels), so no reading of these numbers is attempted here.

## 7. Open items

- Close triggers (1 / 5 / 15 minute) for entries and stops, and the marketable entry at the next bar's open; costs; the provisional time rules; the rest of the parked list in `plans/janus-camarilla-research.md` section 9c.
- Regular and Fibonacci pivots with the same concept (user, after Camarilla).
- Other instruments once CRUDEOILM has been read through.
