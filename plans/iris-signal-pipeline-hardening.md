# Plan: Iris Signal-Pipeline Hardening (vs. Apollo Parity)

**Status: Scoping complete, nothing implemented yet.** Findings gathered 2026-08-07 from reading today's live log (`iris_production/logs/iris_20260807.log`), a direct code comparison against `apollo_production/` (which the user has tested live more extensively and considers more robust), and a live post-close AngelOne research session that resolved §5 (the CAS-window candle-integrity question) against both raw API pulls and the actual chart. Everything is now scoped, including the two implementation items (§8 for Iris, §9 for the separate `data_pipeline` codebase) that came out of §5's answer. Ready for implementation-order approval.

---

## 0. Why this exists — today's incident

Analysis of `iris_production/logs/iris_20260807.log`, full session: 09:15:09 start to 14:21:52 stop (Slack Kill Switch, triggered manually once the pattern below was found — clean shutdown, "Dropping control immediately", no stuck state, no open position at the time):

- No trades entered all session. Not a bug — the 5-min trend stayed `bearish` the entire day with zero fresh 5m flips; the only regime event was the 15-min regime catching up to `bearish` at 11:15:02. Since `ST_FAST` needs an actual crossing (`trend != trend.shift(1)` in `compute_st`) to fire, a trend that never left `bearish` in the first place could never produce one — confirmed there was no signal being missed by stopping early.
- **69 `getCandleData` rate-limit warnings** (`Access denied because of exceeding access rate`) across the session — 13 instances where all 3 retries were exhausted.
- **9 of 61 expected 5-minute bars (15%) never appear in the log at all**: 09:55, 10:15, 10:25, 10:30, 10:55, 11:00, 12:15, 12:55, 13:30.
- Traced to the code, not just correlation: `_fetch_candle()` (`iris.py:358-376`) fetches a narrow 3-bar window and extracts exactly one candle. `_update_5m_st()` (`iris.py:378-390`) appends that single row to the running `self._df_5m` series. When `fetch_candles` exhausts its 3 retries and returns nothing, the cycle is skipped and **that candle is gone permanently** — the next successful cycle only fetches its own new candle, it never backfills. If a genuine flip had occurred inside one of those 9 gaps, it would have been structurally undetectable, not just delayed — today's monotonic trend means this probably didn't cost anything, but that's luck, not a property of the system. The missing-bar rate held steady (~15-17%) across the whole session rather than worsening over time.
- This is a recurrence of an already-documented AngelOne behavior, not a new mystery — `plans/closing-auction-session.md` §7.4 recorded the same symptom during the CAS investigation: "AngelOne's historical-candle endpoint hit a sustained, undocumented throttle... well under the documented 3/sec-180/min-5000/hour limits."
- **Note for §5**: this log stops at 14:21, well before the 15:15-15:25 CAS auction window even opens. It cannot answer §5 on its own — that still needs a direct post-close `getCandleData` pull for that specific window, since Iris was never live-polling during it today.

This prompted a systematic comparison against `apollo_production/`, which handles an analogous problem (Nifty-index candle fetching + Supertrend signal generation) and has significantly more live-trading history. Every item below is a concrete gap found by reading both codebases side by side, not a general impression.

---

## 1. Candle-fetch retry/backoff resilience

**Apollo** (`apollo.py:334-407`, `apollo_configs.py:159-160`): inner 3-attempt burst (same as Iris) via `_fetch_latest_candle`, but if that fails entirely, escalates to an outer loop — `CANDLE_FETCH_RETRIES = 5` more attempts, `CANDLE_FETCH_RETRY_INTERVAL = 10` seconds apart (up to 18 total attempts over 50+ seconds). Calls `_reset_counters()` before each outer retry — actively resets client-side rate-limit counters rather than retrying into the same throttle window. If still unrecovered, the timestamp goes into a persistent `self._missed_candle_ts_list`, which is actively retried on *every subsequent loop iteration* (not abandoned), and when recovered, the candle is backfilled into the ST series in correct chronological order before the current bar is processed.

**Iris** (`iris_functions.py` → `fetch_candles`): 3 attempts, 1 second apart, then permanently gives up. No counter reset. No backfill queue — `_fetch_candle` returning `None` just skips that cycle forever.

**Recommended:** port Apollo's exact pattern — outer retry loop with counter reset, persistent missed-timestamp queue retried each loop iteration, chronological backfill into `self._df_5m` (and `self._df_15m` where relevant) before processing the current bar.

## 2. 75-min/15-min resampling consistency

**Apollo**: 75-min timeframe is *always* derived by resampling the already-fetched 15-min dataframe (`_compute_75min_st`, `supertrend.py:304`) — zero extra API calls, ever. Necessary in Apollo's case since AngelOne's `getCandleData` has no native 75-min interval.

**Iris**: resamples 5m→15m correctly at seed time (`_resample_to_15m`, `iris_functions.py:142`, used in `seed_st`) — but the live loop doesn't reuse that pattern. `_update_15m_regime()` (`iris.py:407-408`) calls `_fetch_candle(close_ts, REGIME_TF_MIN)`, which sets `interval = 'FIFTEEN_MINUTE'` and makes a **separate, independent API call** every 15 minutes, on top of the `FIVE_MINUTE` call already happening every 5 minutes. Inconsistent with Iris's own seed-time logic, and roughly a third more `getCandleData` traffic than necessary — a direct contributor to today's rate-limit exhaustion.

**Recommended:** make the live loop resample the 15-min regime from `self._df_5m` the same way `seed_st` does, and drop the separate `FIFTEEN_MINUTE` fetch entirely. Superseded in its specifics by §8 below, now that §5 is answered — the resample source needs to be the *reconstructed* 1-min series (chart-matching gap-fill + transition candle, truncated at the last real candle), not the raw 5-min series as originally assumed here.

## 3. On-disk ST cache persistence

**Apollo**: `data/supertrend_cache.csv` (`apollo_configs.py:24`, `_save_st_cache`, `supertrend.py:367-378`), overwritten after every candle close — 15-min OHLC + Supertrend + trend + trend_flip, trimmed to `ST_HISTORY_CANDLES`. Serves both intra-session restart recovery and as a durable, inspectable record.

**Iris**: no cache file at all. No restart recovery without re-seeding from the API, and no way to examine the running signal history except parsing the log file after the fact — exactly what today's whole investigation required.

**Recommended (concretized per item 7 below):** add an equivalent CSV, written after every 5-min *and* 15-min update, holding OHLC + ST_5 + trend_5 + flip_5 + ST_15 + trend_15 + flip_15 per row.

## 4. Slack reporting granularity — general

Neither strategy pings Slack for a "pure" regime flip independent of a resulting trade action currently — flips feed into entry/exit logic, which has its own alerts. Two concrete gaps found on Iris's side specifically:

- Apollo's seed-completion Slack message reports the initial regime state ("*Apollo*: Supertrend seeded (N candles). 75-min trend: bullish/bearish.", `apollo.py:159-161`). Iris's startup message (`iris.py:206-208`) is generic — strategy name + lot count — and doesn't report the initial 5m/15m regime at all.
- Apollo has a **missed-flip-recovery mechanism** at startup (`get_last_completed_flip()`, `supertrend.py:195-213`; used in `apollo.py` around line 550-576): if a flip happened earlier today before Apollo started/restarted and the entry window is still open, it detects this, alerts Slack ("*Apollo*: Missed flip detected at HH:MM..."), and can still act on it. **Iris has no equivalent at all** (confirmed — zero matches for `missed.flip`/`get_last_completed_flip`/`recover` in `iris.py`/`iris_functions.py`). A restart mid-session silently loses any flip that occurred while Iris was down — no detection, no alert, no recovery.

**Recommended:** add regime state to Iris's startup Slack message; port Apollo's missed-flip-recovery mechanism (5m and/or 15m, to be decided).

## 5. Live 5-min/1-min candle integrity during the CAS window — ANSWERED

Investigated post-close 2026-08-07 via a live, single-login AngelOne research session (Iris fully stopped, no rate-limit contention). Pulled raw, uncorrected `getCandleData` for both 2026-08-07 and 2026-08-06, at both `ONE_MINUTE` and `FIVE_MINUTE` intervals, 15:00-15:40 each day, then cross-checked against what AngelOne's own chart displays for the same window and against `data/cas_auction_tracking.csv`.

**What the raw API actually returns (both days, same shape):** continuous real 1-min ticks through 15:14. A single flat row at 15:15 — O=H=L=C all equal, and *not* equal to 15:14's own close, confirming this is the exchange's published VWAP(15:00-15:15) reference price, not a stale-LTP artifact. Then a complete void — zero rows at any granularity — for 12-13 minutes (15:16 through 15:27/15:28, day-dependent). Then a single flat row at the terminal print (15:29 on 08-07, 15:28 on 08-06) — again O=H=L=C all equal, this time to the new settlement value. Nothing at all after that for the rest of the day, at any granularity — confirmed by requesting through 15:40 and getting no rows past the terminal print.

**What the chart shows for the same window, which differs from the raw API in one specific place:** the chart flat-fills every absent minute forward at the 15:15 reference value (matches the raw API's own 15:15 row) — but the candle where the terminal print actually lands is *not* flat on the chart. Its **open equals the carried-forward pre-auction reference value**, not its own close, with high/low spanning the real direction of the move. E.g. 08-07: chart's 15:29 candle is open=24557 (carried forward), close=24570.65 (the print), i.e. real range — where the raw API's own 15:29 row is flat at 24570.65 with no memory of where the price came from. This is standard "frozen price, then one new print" OHLC candle construction, not an AngelOne-specific quirk — and it's exactly the piece the current pipeline logic gets backwards (see §9).

**Resampling confirms it's clean once the 1-min series is correctly reconstructed:** 5-min and 15-min chart views for both days are consistent with straightforward OHLC aggregation *of the chart's own corrected 1-min series* — e.g. 08-07's 5-min "15:25" bucket (spanning 15:25-15:30) shows open=24557/close=24570.65, exactly what aggregating [flat,flat,flat,flat,transition-candle] produces. No separate special-casing needed at coarser timeframes once the 1-min reconstruction is right.

**One more rule, confirmed from the chart directly: the transition candle is the *last* candle of the day for charting/ST purposes.** AngelOne's own chart shows no candles after 15:29 (08-07) / 15:28 (08-06) at all — it does not display the flat-extended 15:30-15:39 candles the data pipeline's `extend_to_day_close` produces for other reasons (see §9). Whatever reconstructs Iris's 1-min series for ST/resampling must truncate at the transition candle, not carry the day out to 15:39 the way the historical pipeline's output currently does. Feeding those extra synthetic candles into `compute_st` would add flat, contentless bars the real chart's ST never sees.

Full implementation design is in §8 (Iris) and §9 (data pipeline, separate codebase, flagged distinctly per the user's request).

## 6. Dedicated Slack reporting for regime changes

Distinct from §4 (which is about missing context in *existing* messages) — this is a new capability. Add explicit Slack notifications on every regime change — both the 5m flip (`_update_5m_st`) and the 15m regime flip (`_update_15m_regime`) in `iris.py` — independent of whether a trade results. Today's 11:15 regime flip only exists in the log file; nothing about it reached Slack, which is part of why this whole investigation needed a log read after the fact rather than being visible live.

## 7. Log/persist ST_5 and ST_15 on every 5-min fetch

Concretizes §3. Two parts:
- At minimum, log both ST_5 and ST_15 on every 5-min cycle — today's log only prints the 5-min bar's own ST value in the "Bar HH:MM" line; the 15-min ST/regime value is logged only when the 15-min boundary itself updates, not every cycle.
- Preferably, persist to CSV the way Apollo does (§3) — this would have made today's entire investigation a CSV read instead of a log-text reconstruction, and doubles as restart-recovery data as a side effect.

## 8. Iris — rebuild the 1-min reconstruction and seeding around a persistent CSV

Supersedes §2's original resampling recommendation now that §5 is answered. This is the actual fix for the CAS-window blind spot, and it subsumes §3/§7's cache-persistence goal rather than sitting alongside it as a separate feature.

**Reconstruction logic** (the chart-matching rules from §5, implemented once and reused everywhere Iris needs a candle series):
1. Real ticks pass through unchanged.
2. Any absent minute gets flat-filled forward at the last known reference value (the pre-auction VWAP print, or whatever the last real/reconstructed value was).
3. The candle where a new real print lands after a gap is reconstructed, not taken as-is from the API: open = the carried-forward flat value, close = the new print, high/low = max/min of the two (direction-aware — CAS can push the print either above or below the pre-auction reference).
4. The reconstructed series **stops at that transition candle** — no flat-extension out to 15:39. That's a data-pipeline-only construct (§9) for a different purpose (row-count alignment with the extended derivatives session) and must not leak into what Iris resamples or computes ST on, per §5's chart-parity rule.

**Daily lifecycle:**
- Seed each day (and on every Iris startup, mid-day restart included) by pulling fresh 1-min history, applying the reconstruction above, resampling to 15m (and any other timeframe needed) from that single corrected series, and computing ST from the start.
- Persist the seeded-and-running series to CSV, appended through the day as each new candle (real or reconstructed) is processed — this is where §3/§7's cache-persistence goal is actually satisfied, as a byproduct of needing a durable record for the reconstruction logic anyway, not a bolt-on.
- Always delete and rebuild this CSV fresh at the start of a new day or on any Iris restart — no cross-restart reuse of a stale cache (deliberately simpler/more conservative than Apollo's restart-recovery-from-cache approach, given everything found in §0/§1 about how easily the live incremental path accumulates silent corruption; reseeding fresh from a correctly-reconstructed historical pull sidesteps that risk rather than trying to detect and repair it after the fact).

**Interaction with §1:** the retry/backoff hardening in §1 still matters for the *normal-hours* rate-limit problem (§0's incident), but does nothing for the CAS-window gap specifically — retrying an exact-timestamp query against a timestamp that will never have data just fails the same way every time (already established in the §5 discussion prior to the raw-pull investigation). §1 and §8 are both needed; they fix different failure modes.

## 9. data_pipeline — CAS gap-fill correction (separate codebase, flagged distinctly)

Same root cause as §5/§8, different repo area (`data_pipeline/data_downloader_angelone.py`), affecting historical/backtest data rather than Iris's live signal — kept as its own item since it's a different codebase with different consumers (any backtest reading `nifty.csv`/`sensex.csv` through a CAS-era day, not just Iris).

**The bug:** `fill_missing_candles`'s current design deliberately leaves the terminal auction print's own OHLC untouched, reasoning (per its own docstring) that forcing its open to the pre-auction close "would produce an invalid candle." §5's chart evidence shows this reasoning doesn't hold — the chart's own transition candle *does* exactly that (open = carried-forward pre-auction value, close = terminal print, high/low direction-aware), and it's a normal, valid OHLC candle, not an invalid one. The pipeline has been producing a flat terminal-print candle where the real chart shows a candle with genuine range, for every CAS-era day since 2026-08-03.

**Three concrete fixes needed:**
1. Fix `fill_missing_candles` itself to reconstruct the transition candle per §5's rule, going forward.
2. **Backfill/correct the existing `nifty.csv` and `sensex.csv`** for every already-downloaded CAS-era day (2026-08-03 onward) — this is retroactive data correction, not just a forward-looking fix, since the CAS-force-close Artemis backtest work from a few days ago (and any other backtest touching this data) consumed the uncorrected version.
3. Confirm/adjust `extend_to_day_close`'s flexibility for variable terminal-print timing — the user's requirement is that it correctly extends flat candles through 15:39 regardless of whether the real terminal print lands at 15:27, 15:28, 15:29, or elsewhere; reading the existing code, this already appears to work off "whatever the last real timestamp actually is" rather than a hardcoded expectation, but needs explicit verification post-fix, since the "last bar" it extends from will now be the *reconstructed* transition candle rather than the raw flat one — the carry-forward value it uses needs to be that candle's `close`, not treat the reconstruction as having changed what "last bar" means structurally.

**Explicitly not the same fix as §8** — §9's flat-extension through 15:39 is correct and necessary for the data pipeline's own purpose (matching the extended derivatives session, row-count alignment for downstream consumers). §8 explicitly does *not* want that extension for Iris's ST calculation. Both are correct simultaneously, for different consumers of the same underlying event.

---

## Open questions for review

- Confirm scope/ordering before implementation starts. §8 (Iris reconstruction/seeding rebuild) is the one that actually fixes the CAS blind spot and subsumes §3/§7's caching goal — likely the highest-value single item. §1 (retry/backoff) fixes a different, also-real failure mode (§0's ordinary rate-limit exhaustion) and is independent of §8. §9 (data_pipeline) is a separate codebase and could land before, after, or in parallel with §8 — they share a root cause but not an implementation.
- §9's backfill (correcting existing `nifty.csv`/`sensex.csv`) needs its own scope decision: every CAS-era day since 2026-08-03, or just re-derive on demand when a specific backtest touches an affected day?
- Confirm CSV schema/filename for §8's persistent series (suggest mirroring Apollo's pattern: `iris_production/data/iris_st_cache.csv` or similar) — one row per candle (real or reconstructed) with a flag distinguishing the two, plus ST_5/trend_5/flip_5/ST_15/trend_15/flip_15 columns per §7.
- Confirm whether §6's regime-change alerts should go to `SLACK_TRADEBOT_CHANNEL` (session lifecycle, matching Apollo's seed message) or a dedicated channel — current Slack channel conventions (`CLAUDE.md`) route entries/exits to `#trade-alerts` and periodic in-trade updates to `#trade-updates`; a pure regime-flip ping without a trade attached doesn't map cleanly to either.
