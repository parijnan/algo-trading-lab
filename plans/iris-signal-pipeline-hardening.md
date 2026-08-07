# Plan: Iris Signal-Pipeline Hardening (vs. Apollo Parity)

**Status: Scoping complete, nothing implemented yet.** Findings gathered 2026-08-07 from reading today's live log (`iris_production/logs/iris_20260807.log`) plus a direct code comparison against `apollo_production/`, which the user has tested live more extensively and considers more robust. One item (§5) is explicitly blocked on post-market-close data collection, not yet done. Everything else is ready for implementation approval.

---

## 0. Why this exists — today's incident

Analysis of `iris_production/logs/iris_20260807.log` (09:15–13:10, the window available at analysis time):

- No trades entered. Not a bug — the 5-min trend stayed `bearish` all session with zero fresh 5m flips; the only regime event was the 15-min regime catching up to `bearish` at 11:15:02. Correct, expected behavior.
- **54 `getCandleData` rate-limit warnings** (`Access denied because of exceeding access rate`) in under 4 hours — 30 first-attempt, 14 second-attempt, 10 that exhausted all 3 retries.
- **8 of 47 expected 5-minute bars (17%) never appear in the log at all**: 09:55, 10:15, 10:25, 10:30, 10:55, 11:00, 12:15, 12:55.
- Traced to the code, not just correlation: `_fetch_candle()` (`iris.py:358-376`) fetches a narrow 3-bar window and extracts exactly one candle. `_update_5m_st()` (`iris.py:378-390`) appends that single row to the running `self._df_5m` series. When `fetch_candles` exhausts its 3 retries and returns nothing, the cycle is skipped and **that candle is gone permanently** — the next successful cycle only fetches its own new candle, it never backfills. If a genuine flip had occurred inside one of those 8 gaps, it would have been structurally undetectable, not just delayed — today's monotonic trend means this probably didn't cost anything, but that's luck, not a property of the system.
- This is a recurrence of an already-documented AngelOne behavior, not a new mystery — `plans/closing-auction-session.md` §7.4 recorded the same symptom during the CAS investigation: "AngelOne's historical-candle endpoint hit a sustained, undocumented throttle... well under the documented 3/sec-180/min-5000/hour limits."

This prompted a systematic comparison against `apollo_production/`, which handles an analogous problem (Nifty-index candle fetching + Supertrend signal generation) and has significantly more live-trading history. Every item below is a concrete gap found by reading both codebases side by side, not a general impression.

---

## 1. Candle-fetch retry/backoff resilience

**Apollo** (`apollo.py:334-407`, `apollo_configs.py:159-160`): inner 3-attempt burst (same as Iris) via `_fetch_latest_candle`, but if that fails entirely, escalates to an outer loop — `CANDLE_FETCH_RETRIES = 5` more attempts, `CANDLE_FETCH_RETRY_INTERVAL = 10` seconds apart (up to 18 total attempts over 50+ seconds). Calls `_reset_counters()` before each outer retry — actively resets client-side rate-limit counters rather than retrying into the same throttle window. If still unrecovered, the timestamp goes into a persistent `self._missed_candle_ts_list`, which is actively retried on *every subsequent loop iteration* (not abandoned), and when recovered, the candle is backfilled into the ST series in correct chronological order before the current bar is processed.

**Iris** (`iris_functions.py` → `fetch_candles`): 3 attempts, 1 second apart, then permanently gives up. No counter reset. No backfill queue — `_fetch_candle` returning `None` just skips that cycle forever.

**Recommended:** port Apollo's exact pattern — outer retry loop with counter reset, persistent missed-timestamp queue retried each loop iteration, chronological backfill into `self._df_5m` (and `self._df_15m` where relevant) before processing the current bar.

## 2. 75-min/15-min resampling consistency

**Apollo**: 75-min timeframe is *always* derived by resampling the already-fetched 15-min dataframe (`_compute_75min_st`, `supertrend.py:304`) — zero extra API calls, ever. Necessary in Apollo's case since AngelOne's `getCandleData` has no native 75-min interval.

**Iris**: resamples 5m→15m correctly at seed time (`_resample_to_15m`, `iris_functions.py:142`, used in `seed_st`) — but the live loop doesn't reuse that pattern. `_update_15m_regime()` (`iris.py:407-408`) calls `_fetch_candle(close_ts, REGIME_TF_MIN)`, which sets `interval = 'FIFTEEN_MINUTE'` and makes a **separate, independent API call** every 15 minutes, on top of the `FIVE_MINUTE` call already happening every 5 minutes. Inconsistent with Iris's own seed-time logic, and roughly a third more `getCandleData` traffic than necessary — a direct contributor to today's rate-limit exhaustion.

**Recommended:** make the live loop resample the 15-min regime from `self._df_5m` the same way `seed_st` does, and drop the separate `FIFTEEN_MINUTE` fetch entirely. Needs to be reconciled with §5 below — if 5-min candles are themselves corrupted during the CAS window, resampling just inherits that corruption rather than fixing anything, so this fix and that investigation are linked, not independent.

## 3. On-disk ST cache persistence

**Apollo**: `data/supertrend_cache.csv` (`apollo_configs.py:24`, `_save_st_cache`, `supertrend.py:367-378`), overwritten after every candle close — 15-min OHLC + Supertrend + trend + trend_flip, trimmed to `ST_HISTORY_CANDLES`. Serves both intra-session restart recovery and as a durable, inspectable record.

**Iris**: no cache file at all. No restart recovery without re-seeding from the API, and no way to examine the running signal history except parsing the log file after the fact — exactly what today's whole investigation required.

**Recommended (concretized per item 7 below):** add an equivalent CSV, written after every 5-min *and* 15-min update, holding OHLC + ST_5 + trend_5 + flip_5 + ST_15 + trend_15 + flip_15 per row.

## 4. Slack reporting granularity — general

Neither strategy pings Slack for a "pure" regime flip independent of a resulting trade action currently — flips feed into entry/exit logic, which has its own alerts. Two concrete gaps found on Iris's side specifically:

- Apollo's seed-completion Slack message reports the initial regime state ("*Apollo*: Supertrend seeded (N candles). 75-min trend: bullish/bearish.", `apollo.py:159-161`). Iris's startup message (`iris.py:206-208`) is generic — strategy name + lot count — and doesn't report the initial 5m/15m regime at all.
- Apollo has a **missed-flip-recovery mechanism** at startup (`get_last_completed_flip()`, `supertrend.py:195-213`; used in `apollo.py` around line 550-576): if a flip happened earlier today before Apollo started/restarted and the entry window is still open, it detects this, alerts Slack ("*Apollo*: Missed flip detected at HH:MM..."), and can still act on it. **Iris has no equivalent at all** (confirmed — zero matches for `missed.flip`/`get_last_completed_flip`/`recover` in `iris.py`/`iris_functions.py`). A restart mid-session silently loses any flip that occurred while Iris was down — no detection, no alert, no recovery.

**Recommended:** add regime state to Iris's startup Slack message; port Apollo's missed-flip-recovery mechanism (5m and/or 15m, to be decided).

## 5. Live 5-min candle integrity during the CAS window (15:15-15:25) — OPEN, BLOCKED ON POST-CLOSE DATA

Not yet investigated — needs today's post-market-close data, and per the standing rule (no ad-hoc AngelOne logins while Iris is live-trading), can't be pulled before then anyway.

**The concern:** under CAS, continuous underlying trading halts at 15:15; a ~15-min call auction follows with no ticks; the terminal print lands somewhere in 15:16-15:35. User's expectation, based on the already-solved version of this problem in `data_downloader_angelone.py` (`fill_missing_candles`/`extend_to_day_close`, historical-batch-download path only): the 15:15 and 15:20 *live* 5-min candles likely show up flat (same OHLC, = the 15:10 close), and 15:25 likely opens at that flat price and closes at the day's actual settlement print. Unconfirmed whether AngelOne's *live* `getCandleData` (the path Iris's `fetch_candles` actually calls) behaves the same way as the historical/batch path, or differently (e.g., genuinely empty rows instead of synthetic flat OHLC).

**To check once market's closed today:**
1. Pull today's actual `getCandleData` response for the 5-min bars spanning 15:10-15:30 and see what's really there.
2. Cross-check against today's full Iris log for that window (today's log was only available through 13:10 at analysis time).
3. If candles are genuinely flat/duplicated: `compute_st`'s trend/ATR computation ingests two zero-range bars then one wide-range bar — check whether that alone is enough to spuriously trigger or suppress a `trend_flip` right at the point where a reliable reading matters most.
4. Feeds directly into §2 — a resampling fix doesn't help if the underlying 5-min bars are themselves corrupted in this window; may need the same flat-fill-but-preserve-terminal-print treatment already proven on the data-pipeline side, or a decision to simply not trust/act on signals generated in this specific window (Iris's own `EXIT_BY_TIME=15:15` already avoids acting on in-trade decisions past this point, for related reasons).

## 6. Dedicated Slack reporting for regime changes

Distinct from §4 (which is about missing context in *existing* messages) — this is a new capability. Add explicit Slack notifications on every regime change — both the 5m flip (`_update_5m_st`) and the 15m regime flip (`_update_15m_regime`) in `iris.py` — independent of whether a trade results. Today's 11:15 regime flip only exists in the log file; nothing about it reached Slack, which is part of why this whole investigation needed a log read after the fact rather than being visible live.

## 7. Log/persist ST_5 and ST_15 on every 5-min fetch

Concretizes §3. Two parts:
- At minimum, log both ST_5 and ST_15 on every 5-min cycle — today's log only prints the 5-min bar's own ST value in the "Bar HH:MM" line; the 15-min ST/regime value is logged only when the 15-min boundary itself updates, not every cycle.
- Preferably, persist to CSV the way Apollo does (§3) — this would have made today's entire investigation a CSV read instead of a log-text reconstruction, and doubles as restart-recovery data as a side effect.

---

## Open questions for review

- Confirm scope/ordering before implementation starts — all of §1/§2/§3/§4/§6/§7 together, or staged (e.g., §1 candle-fetch resilience first since it's the most directly tied to today's incident)?
- §5 needs to happen after today's market close before anything else in this plan can be considered complete, since §2's fix depends on its answer.
- Confirm CSV schema/filename for §3/§7 (suggest mirroring Apollo: `iris_production/data/supertrend_cache.csv`, one row per 5-min bar with both timeframes' ST columns) before implementing.
- Confirm whether §6's regime-change alerts should go to `SLACK_TRADEBOT_CHANNEL` (session lifecycle, matching Apollo's seed message) or a dedicated channel — current Slack channel conventions (`CLAUDE.md`) route entries/exits to `#trade-alerts` and periodic in-trade updates to `#trade-updates`; a pure regime-flip ping without a trade attached doesn't map cleanly to either.
