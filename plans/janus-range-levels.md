# Plan: Janus range-based Camarilla levels (user design 2026-10-12)

**Status: steps 1 to 3 built and run 2026-10-12 (level source, tests, descriptive pass); the engine rerun (step 4) waits for the user.** Research only, like the rest of Janus: nothing here touches live code, Hestia or Delos. This plan changes only where the High and Low that feed the Camarilla formulas come from; the formulas, setups, engine and entry modes in `plans/janus-camarilla-research.md` are unchanged. The idea borrows the setter-candle logic from `plans/range-detection-research.md` (sections 1 and 3), used on daily bars of the MCX contracts.

## 1. The change

Today the levels for day D come from the previous session's High, Low and Close. The new source replaces High and Low with the bounds of a price range tracked on daily bars, and keeps Close as the previous session's close.

- **Close:** always the previous session's close (unchanged).
- **High and Low:** the range high and range low as they stand after the previous session's close, known before the open and fixed for the whole day.
- **Formulas:** unchanged (R1-R4 = C plus or minus R times 1.1 over 12, 6, 4, 2; R5 = C times H over L; R6 and R7 by the 1.168 extension; S-side mirrored), with R now the range width (range high minus range low).

## 2. Range rules (the setter-candle logic, stripped down)

Daily bars, one per session, built from the contract being traded that day.

1. The first session of a contract's own data is a setter: the range is that bar's High and Low.
2. After each session's close, compare the close with the current range. A close strictly above the range high or strictly below the range low starts a new range: that session's bar is the new setter and its High and Low become the bounds. Close outside is the only trigger.
3. Otherwise (close inside, bounds inclusive) the range extends to include the session's High and Low (wick extension), so the range can only widen while it lasts.
4. No confirmation bars (the Nifty plan's `--breakout-confirm` is set to zero), no minimum range length (a one-bar range already supplies levels), no gap-open rule.

Consequences that follow directly from the rules:
- On a setter session's following day the levels equal today's previous-session levels (the range is that one bar), so the new source only differs from the current rule on days when the previous session closed inside the existing range.
- A breakout beyond R4 or S4 that closes outside the range makes the next day's levels come from that one day's bar alone (new setter).
- While a range ages, R grows, so R3/S3 and R4/S4 sit further from C and the stop distance (0.275 R) widens; the reward multiples in stop-distance units stay the same. C is no longer near the middle of the High-Low span, so the levels can be asymmetric around the range.
- No look-ahead: the levels use only information up to the previous close, with no confirmation lag and no provisional bounds.

## 3. Contract handling (decided, user 2026-10-12)

Janus is intraday with no rollovers. The range for the contract traded on a given day is derived from THAT contract's own price history: its previous setter candle and the extensions (wicks) made since, i.e. the setter state machine of section 2 run over the contract's own daily bars from the first session of its data. The range is not restarted when the traded contract changes: a newly traded contract already carries the range its own history has built, including the sessions on which it was a back-month contract (before it became the front month). The Camarilla levels are then computed from that range's High and Low and the contract's own previous-session close.

Data check (2026-10-12, Fyers contract files): before its first traded session a contract's file already holds a median of 22 calendar days of history for CRUDEOILM, GOLDPETAL and NATGASMINI (minimum 2 to 3 days) and 73 days for SILVERMIC (minimum 50), against roughly 21 traded sessions per contract (44 for SILVERMIC). Because a setter resets the range, the range after the first close outside it does not depend on where the history starts; only the stretch before the first break does. Back-month sessions are thin: Fyers zero-volume placeholder minutes carry the previous close in all four prices, so they cannot extend a high or low, but thin real prints can; the descriptive pass reports how many traded sessions rest on back-month-only history.

## 4. Implementation outline (to do, in this order)

1. Add a level-source switch in `janus_backtest/janus_levels.py` (and the setting in `janus_configs.py`, nothing hardcoded): `previous_session` (default, the control) and `range_setter`. Pure function over the per-contract daily bars.
2. Tests (`tests/test_janus_*.py`), each behaviour mutation-checked as elsewhere: first session is a setter; strict close above and below starts a new range; a close exactly on a bound stays inside; extension by a wick; extension never narrows; breakout then next-day reset to the single bar; Close always the previous session's; a contract's range is built from its own bars only and never from another contract's (two contracts with different price levels give independent ranges); the output equals the current rule whenever the range is one bar.
3. Descriptive pass before any P&L: per instrument, the share of sessions whose levels differ from the current rule, the distribution of R(range) over R(previous session), the distribution of range age, how often a session closes outside the range (setter rate), and the sessions whose range rests only on back-month history, with the levels recomputed with and without those sessions.
4. Rerun the day-simulation engine (`janus_engine.py`) on the new levels with the touch entry as the control and plain thirds (A0) first; same costs grid, same two bar orders. Close-armed entries only if wanted afterwards. Compare against the previous-session control on the same sessions, one variable changed.
5. Record results in this plan and the README. Nothing committed without the user's request.

## 5. Open points and caveats

- Back-month history is thin; the descriptive pass must show how much it matters (sessions whose range rests only on back-month sessions, and the levels with and without them).
- All earlier Janus results (thirds, re-buy, counter trade, entries) were produced with previous-session levels and are not guaranteed to carry over; they are the control, not the answer.
- The provisional rules in the Janus plan (first-minute rule, 60-minute entry cut-off, 15-minute hard exit) and the unknown real costs still apply and still decide viability.
- The parked list in the Janus plan (real costs, marketable entry at the next bar's open, breakout-only book, cap test, stop-trigger grid, reclaim-waiting counter) is unchanged; this change is an additional line of work, not a replacement.
- A wider R means fewer touches and a larger stop as a share of price, so trade counts and the cost break-even per leg will shift in a direction not predictable in advance.

## 6. Descriptive pass result (2026-10-12, `janus_backtest/range_levels_descriptive.py`, `outputs/range_levels_descriptive.txt`)

Built: `janus_levels.range_states`, `janus_data.load_sessions(level_source=...)` (default `previous_session`, so earlier results are unchanged), config `LEVEL_SOURCE`, `LEVEL_SOURCES`, `RANGE_MIN_BARS`; 11 tests in `tests/test_janus_range_levels.py`, each behaviour broken on purpose and caught. A day takes part in a contract's range history only with at least 120 bars and some traded volume (Fyers delivers a bar for every minute, zero-volume placeholders carrying the previous close). No traded session fell back to the control levels.

| | CRUDEOILM | SILVERMIC | GOLDPETAL | NATGASMINI |
|---|---|---|---|---|
| Contracts / traded sessions | 42 / 884 | 27 / 1,383 | 60 / 1,274 | 42 / 888 |
| Sessions whose levels differ from the control | 63% | 67% | 66% | 63% |
| Previous session was itself a setter | 36% | 32% | 33% | 35% |
| R(range) / R(control) where they differ, median (q75, q90, max) | 2.0 (2.8, 3.7, 10.2) | 2.5 (3.8, 6.2, 33.8) | 2.4 (3.6, 5.3, 19.5) | 2.0 (2.7, 3.7, 9.2) |
| Range age in sessions, median (q75, q90, max) | 2 (5, 10, 34) | 3 (6, 15, 99) | 3 (5, 9.7, 25) | 2 (4, 8, 20) |
| Stop distance (0.275 R) as % of the previous close, median: control to range | 0.70 to 1.13 | 0.47 to 0.90 | 0.22 to 0.40 | 1.16 to 1.84 |
| Sessions resting on back-month history | 19% | 14% | 18% | 13% |
| Sessions whose levels change if the range is rebuilt from traded sessions only (median R shift) | 157 (46%) | 198 (63%) | 211 (59%) | 104 (47%) |

Reading: the levels differ from today's on about two sessions in three, R is typically two to two and a half times wider on those, so the stop distance as a share of price is roughly 1.6 to 1.9 times larger on the median session (for example CRUDEOILM 0.70% to 1.13%); a setter (a close outside the range) follows about one session in three, so most ranges are short (median age 2 to 3 sessions). The extreme cases are long-lived ranges (SILVERMIC up to 99 sessions, R up to 34 times the control) and should be looked at before the engine rerun. Including back-month sessions matters: 13% to 19% of traded sessions rest on it and dropping it moves R by around half on those sessions, so the back-month inclusion (decided) is a material choice and the engine rerun should be repeated without it as a sensitivity.

