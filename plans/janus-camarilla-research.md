# Plan: Janus, Camarilla pivot intraday research (MCX futures)

**Status (2026-10-05): Phase 0 built and run (`janus_backtest/`, see its README for the findings); Phase 1 not started.** Pure research track, independent of Prometheus, Selene, Helios, Typhon, Hestia and Delos. Nothing here touches live code, live data or the engines' configs. Scope agreed with the user on 2026-10-05: MCX futures first; mean reversion and breakout studied side by side on the same data.

**Name:** Janus (user, 2026-10-05): the two-faced god of thresholds, for levels read two ways (fade or break). Roman where the other engines are Greek, a deliberate nod to this being a different track. A working name, not necessarily the name of any engine that comes out of it.

## 1. The question

Do the Camarilla levels computed from the previous session's high, low and close carry any tradable intraday information on MCX futures, after realistic costs, and if so in which instruments and which regimes? Two classic readings of the same levels:

- **Mean reversion:** price reaching R3/S3 tends to fall back toward the pivot zone, so fade the touch with a stop beyond R4/S4.
- **Breakout:** price closing beyond R4/S4 tends to continue to R5/S5, so trade the break.

The levels are the same, the trades are opposite. Which one (if either) is right is a property of the regime and instrument, so the first study is descriptive, before any strategy is tuned.

## 2. Levels (all parameters live in `janus_backtest/configs.py`, nothing hardcoded)

For previous session high H, low L, close C, range R = H - L:

- R1..R4 = C + R x (1.1/12, 1.1/6, 1.1/4, 1.1/2); S1..S4 = C - R x the same factors.
- R5 = (H / L) x C and S5 = C - (R5 - C) (the common extension); R6 = R5 + 1.168 x (R5 - R4) and S6 mirrored about C (the usual second breakout target; computed from 2026-10-05, user's call: it costs nothing).
- Pivot (the usual central level) = (H + L + C) / 3, kept as an optional reference, not part of the core levels.

Choices to settle in configs and test one at a time: the session close C is the last 1-minute close of the previous session (MCX settlement prices differ slightly and are not in our data, so this is a stated approximation); session = one calendar trading day, 09:00 to 23:30 (23:55 in US DST periods), which already ends before midnight so there is no overnight-session ambiguity; the factor 1.1 is the standard value and the grid is fixed unless a later experiment varies it.

## 3. Data

- Fyers per-contract history back to 2023 under `data_pipeline/data/mcx_fyers/<INSTRUMENT>/<expiry>_futures.csv` (about 20 instruments), plus Angel One history from 2026-01 under `data_pipeline/data/mcx/`. Same one-file-per-contract format; 1-minute OHLCV with zero-volume placeholder minutes in Fyers files.
- Reuse the existing front-month stitching loader rather than writing a new one; levels for a day are computed from the contract that is the front month on that day, and on a roll day the previous session's high/low/close come from that same contract (not the expiring one) so a level never mixes two contracts' prices.
- Zerodha cross-check showed finalized Fyers history is the closer match to the chart, so Fyers history is the primary source and Angel One only fills the recent weeks.
- Instruments (user decision 2026-10-05): the four the engines already trade, liquidity already confirmed: CRUDEOILM, SILVERMIC, GOLDPETAL, NATGASMINI. If any is ever taken live it would be hosted by Hestia like the existing engines. Fyers history covers CRUDEOILM and NATGASMINI from 2023-04, SILVERMIC from 2021-04, GOLDPETAL from 2021-10, with a shared Fyers void 2026-04-01 to 2026-06-29 that is skipped, not filled (790 to 1,319 usable sessions per instrument).

## 4. Phases

**Phase 0, descriptive, no strategy.** For each instrument and each session compute the levels, then measure: how often price touches each of S3/R3/S4/R4/S5/R5; what happens after a touch (reverts to the central zone, ends the session beyond the level, how far it travels in each direction before the session close, forward excursions in R multiples); and how all of that depends on where the session opens (inside the H3/L3 band, between H3 and H4, beyond H4, gap size in R). Output: tables and a short report. This decides whether a strategy phase is warranted and which reading (reversion or breakout) the data supports. No parameters are optimised here.

**Phase 1, rule-based strategies on what Phase 0 supports.** One variable changed per experiment (repo convention). Candidate rules, to be pinned in configs before running: reversion entry at the first touch of R3/S3 (limit at the level, or on the first bar closing back inside), stop at R4/S4 or a fraction of R, target at the central zone or a fixed multiple of R; breakout entry on a 15-minute close beyond R4/S4, stop back inside H3/L3, target R5/S5 or a trailing exit; one trade per direction per session, flat at the session close, no entries in the first N minutes and none near the close (values in configs). Costs and slippage per instrument in configs (tick-based), applied from the first run, never added after.

**Phase 2, robustness.** Walk-forward by year (2023, 2024, 2025, 2026 year-to-date), per-instrument and pooled, parameter sensitivity around the chosen config, regime splits (volatility terciles, trend vs range days by the prior day's behaviour, month-end and expiry weeks), a shuffled-levels null (the same rules on levels computed from a random other day) to show the specific Camarilla geometry adds something beyond "any support and resistance". The Prometheus work showed an edge can be regime-dependent and vanish in earlier data, so out-of-sample years are the main test, not the pooled P&L.

**Phase 3, decision.** Only if Phase 2 holds: a go/no-go note with the instrument list, rules and expected frequency; building an engine would be a separate plan (the Hestia engine interface), not part of this research.

## 5. Evaluation metrics

Trades, win rate, average win and loss in R and in Rs per lot, total P&L, maximum drawdown, Calmar (unitless and annualised on the same basis used in `prometheus_backtest/README.md` so numbers are comparable), longest losing streak, trades per month, share of P&L from the top 5 trades (concentration), and results by year. Slippage and costs always on.

## 6. Layout

New folder `janus_backtest/` at the repo root: `janus_configs.py` (all parameters; module names are prefixed so they can never collide with the `configs.py` files in the other strategy directories), `janus_levels.py` (pure function: previous-session OHLC to levels), `janus_data.py` (loader on top of the existing front-month logic), `janus_events.py` (first touch and first-passage logic), `phase0_descriptive.py`, then `backtest.py` and per-experiment scripts, outputs under `janus_backtest/outputs/` (gitignored if large), a `README.md` kept current. Function-based, not class-based, matching the other backtest folders. Tests in `tests/test_camarilla_*.py` for the level arithmetic (hand-computed example), session assignment, the roll-day rule, and the entry and exit logic on constructed price paths.

## 7. Decisions (Phase 0)

1. Instrument set: decided 2026-10-05, the four engine instruments (section 3).
2. Whether the session close C should be the last 1-minute close (default) or another proxy.
3. Whether to include R5/S5 and the central pivot in Phase 0 (default: yes, they are cheap to compute and Phase 0 is descriptive).
4. Date range (default: everything the Fyers history has, from 2023, with 2026 as the clearest out-of-sample year).

## 8. Phase 0 result (2026-10-05)

Built and run on the four instruments with the defaults above (session close = last 1-minute close; R5/S5 and the central reference included; all Fyers history from each instrument's start, void skipped). On every instrument, after a touch of R3/S3 the inward level (R2/S2) came first less often than the driftless benchmark (75% for the R4-versus-R2 pair): excess between -5 and -12 percentage points, same sign in nearly every year and largest in 2026; after R4/S4 the move on to R5/S5 beat the benchmark by a similar margin. So the levels behave more like continuation than reversion zones in this data. It is not an edge yet: costs are not applied, and touching an extreme level selects trending days, so any "extreme day keeps going" effect would show the same sign. Phase 2's shuffled-levels null is the test that separates the two. Next step if the user agrees: Phase 1 breakout rules (the reading Phase 0 leans toward), with the reversion rules run alongside as the control.
