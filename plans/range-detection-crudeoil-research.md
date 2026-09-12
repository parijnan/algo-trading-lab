# Plan: PA Range Detection — Adapted to CRUDEOILM 15-min (exploratory)

**Status: exploratory research only, no known outcome, no production changes.** This adapts the
Nifty-validated PA range-detection method (`plans/range-detection-research.md`) to CRUDEOILM on
Prometheus's own 15-min timeframe, to see whether it can supplement, replace, or reject part of
the existing ST_15 Supertrend signal — or point toward a differently-structured strategy
entirely. Per `research/range_detection/README.md`'s own constraint: this directory is research
only, never imported by production code; any implementation needs a dedicated backtest first.

---

## 1. Method

Reused the PA algorithm's core (`compute_pa_ranges` from `range_detector_pa.py`) unmodified,
imported directly rather than editing the shared Nifty/Sensex module. Data: CRUDEOILM
front-month-stitched 1-min series (`prometheus_backtest/data_loader.load_futures_1min` — same
weekend-drop, opening-bar-fix, front-month de-dup the live signal already relies on), resampled
to 15-min via the same day-anchored `resample_ohlcv` used by `backtest_p3.py`. 8,979 bars,
2026-01-30 to 2026-09-10 (~7 months). Cross-referenced against the live signal's own 389 raw
ST_15 trend flips (`prometheus_backtest/phase3/data_sweep/mult_2.0/trade_summary.csv`).

Script: `research/range_detection/crudeoil_15min_pilot.py`.

**Two real lookahead bugs were caught by advisor before any finding was trusted, across three
consultation rounds.**

Round 1: `compute_pa_ranges`'s `_commit()` retroactively rewrites `bar_rh`/`bar_rl`/`bar_ep` for
the pending-breakout window once a breakout confirms (lines 225-236 of the shared script) — so
reading `episode_id`/`close_pct_in_range` off the full-history result at a flip's own bar
reflects what was only knowable `breakout_confirm` bars LATER, not what was known in real time.
Same bug class as this research line's own §12 fix #1 (`side='right'`→`'left'`, 23 contaminated
Nifty trades). Fixed by adding a point-in-time-correct (walk-forward) read: for each flip,
`compute_pa_ranges` is rerun on ONLY the data through that bar (`df_15m.iloc[:i+1]`), and the
last bar's own state is used. §2.2 and §2.3 report BOTH numbers so the size of that leak is on
record, not silently absorbed.

Round 3 (after §2.4's "replace" test had already been through one correction pass — entry-guard
parity and cost haircut — and looked settled): a second, more subtle timing bug remained. The
replace test and composite filled every PA-trend trade at `start_idx + 1` regardless of
`breakout_confirm`, but a commit is not actually *knowable* until `breakout_confirm` bars after
`start_idx` have closed still outside the range — the earliest honest fill is
`start_idx + breakout_confirm + 1`. At `breakout_confirm > 0` this filled trades one-to-three
bars *during* the still-unconfirmed confirmation window, which is by selection biased toward the
breakout direction (that's what "still outside" means) — the always-in-market chain rode the new
direction through bars structurally guaranteed to move that way, and stopped riding the old
direction through the same bars. The signature: `breakout_confirm=0` (the one row immune to this
bug, since `_commit_immediate` fires at the setter bar itself) was also the *only* row where PA
lost to ST_15 — the apparent edge tracked the lookahead exactly. The fade engine had a mirror
problem: its exit compared the full-history (retroactively-rewritten) `episode_id` against the
entry bar's id, so a setter that later got *denied* still showed the old id (letting the fade
ride a false breakout it would have exited in real time), and a setter that later *confirmed* let
the fade ride a real breakout against itself for up to 96 bars before the id changed. Both fixed:
entries/exits use `start_idx + breakout_confirm + 1`; the fade now exits on the PIT range bounds
captured at entry (`close` crossing `range_high`/`range_low`), never on `episode_id`. §2.4-§2.6
below are the corrected numbers — the previous version of this plan reported the pre-fix figures
and is superseded.

---

## 2. Findings

### 2.1 Structural transfer — episodes form, but decay much faster than Nifty daily

| min_range_bars | breakout_confirm | Episodes | Established % | Duration P50 (bars/hrs) | Duration P90 |
|---|---|---|---|---|---|
| 5  | 1 | 1114 | 41.9% | 11 / 2.5h | 36 / 15.5h |
| 5  | 2 | 499  | 67.7% | 12 / 2.8h | 55 / 23.2h |
| 10 | 1 | 1114 | 23.9% | 18 / 4.2h | 52 / 22.9h |
| 10 | 2 | 499  | 43.5% | 21 / 5.2h | 73 / 28.7h |
| 20 | 2 | 499  | 23.6% | 40 / 13.4h | 94 / 51.0h |
| 20 | 3 | 372  | 32.5% | 42 / 13.5h | 97 / 61.2h |

Ranges form and establish at broadly similar rates to Nifty daily (23-68% established depending
on params, vs. Nifty's ~60% at its own settled params). But **hold-rate decay is much faster**
relative to bar count than Nifty's daily validation gate (§7 of the parent plan): at
(10, 2), corrected for the tautology traps below, hold rate is 67.7% at h=4 bars (~1h),
25.1% at h=32 (~8h), 4.2% at h=96 (~24h). Nifty's daily gate held 73.8-88.8% at h=3-5 *days*
and still 42-60% at h=7-10 *days*. Even the widest params tested (20, 3) only reach 38.0% at
h=32 and 7.6% at h=96. **CRUDEOILM 15-min ranges are structurally much less durable than Nifty
daily ranges relative to their own bar-count scale** — consistent with a genuinely more
trend-prone instrument at this granularity, not a parameter-tuning artifact (the decay pattern
holds across the whole grid).

*(Two measurement traps were caught during this analysis, both now fixed in the script: an
initial "close-hold rate" read the range's own already-expanded final bounds, which is
tautologically 100% by construction; and horizons were originally measured from an episode's
raw start rather than from when it became established, which is tautologically 100% for any
h <= min_range_bars. A separate "trendiness" metric (time spent near range extremes) was found
circular against a trailing range and dropped from the write-up entirely.)*

### 2.2 Range POSITION at entry — much weaker than it first looked, near-zero after the fix

Naive (contaminated) read suggested a large effect: flips firing mid-range hugely outperformed
flips firing near a demonstrated range edge (total P&L +14,479 vs +488 pts, avg +51.0 vs +4.7
pts/trade). **The corrected, point-in-time read shrinks this dramatically**: mid-range total
+9,936 vs near-edge +5,031 (roughly 2x, not ~30x), avg +43.4 vs +31.6 pts/trade, and the
correlation between "how extreme the range position is" and P&L flips from -0.033 to +0.018 —
both trivially close to zero. **Verdict: range position at entry does not meaningfully predict
raw ST_15 trade outcome on this data.** A "skip flips that aren't at a confirmed range edge"
filter — the natural analogue of the Apollo rank-2 chop-filter idea from the parent plan — is
NOT supported here, and the apparent support in the naive numbers was substantially a lookahead
artifact.

### 2.3 Range DIRECTION at entry — partially survives, but asymmetric (not the naive read)

Naive read: trading WITH the active PA episode's own directional bias clearly beat fighting it,
symmetrically in both up- and down-biased ranges. **Corrected read tells a different, more
specific story**:

| Range dir | Trade dir | Relation | n (naive→PIT) | avg pnl (naive→PIT) | win% (PIT) |
|---|---|---|---|---|---|
| up   | bullish | AGREES | 82→69   | +84.3→+81.5 | 49.3% |
| up   | bearish | FIGHTS | 98→102  | +3.5→+9.8   | 38.2% |
| down | bullish | FIGHTS | 83→92   | +15.9→+33.9 | 42.4% |
| down | bearish | AGREES | 70→53   | +32.7→+34.2 | 37.7% |

The up-range effect survives the correction essentially intact (agree +81.5 vs fight +9.8 — an
~8x gap, n=69/102). **The down-range effect does NOT survive** — agree (+34.2) and fight
(+33.9) are statistically indistinguishable after the fix. So the real finding is narrower than
it first looked: **trading with an established up-biased range's own bias has real value here;
trading with a down-biased range's bias does not, at least on this window.** Given the small
subgroup sizes (53-102) and this single ~7-month window's own realized drift (crude was net
bullish over Jan-Sep 2026 — up-biased ranges earned more total P&L than down-biased, +7,254 vs
+3,603, unlike Nifty's down-bias edge which has a structural mechanism, index up-drift × option
geometry), **this asymmetry should be treated as unproven until checked on a differently-trending
window** — it may just be "the trend-following direction that happened to work this period,"
not a durable structural property the way Nifty's down-bias edge is.

### 2.4 The "replace" test — corrected: PA-trend does NOT beat ST_15 once the fill-timing bug is fixed

**This finding reversed from the previous version of this plan.** The version reported earlier
(PA beating ST_15 by ~27% at breakout_confirm=1) was itself still contaminated by the Round-3
fill-timing lookahead described in §1 — entries/exits filled at `start_idx + 1` for every
`breakout_confirm`, when the earliest honest fill is `start_idx + breakout_confirm + 1`. With
that fixed, entry guard and 2-point cost haircut unchanged from before:

| breakout_confirm | n trades (after guard) | win% | avg pnl NET | total pnl NET | opening-bar commits |
|---|---|---|---|---|---|
| 0 | 2124 | 64.5% | +2.6  | +5,540 | 120/2157 |
| 1 | 1097 | 60.4% | +7.5  | +8,194 | 95/1113 |
| 2 | 493  | 64.9% | +15.9 | +7,855 | 70/498 |
| 3 | 367  | 64.6% | +21.7 | +7,949 | 66/371 |
| **ST_15 (ref)** | **388** | **42.0%** | **+36.6** | **+14,191** | — |

**PA-as-its-own-signal loses to ST_15 at every `breakout_confirm` setting**, by a wide margin
(best PA cell +8,194 vs ST_15's +14,191, a ~42% shortfall). This is a clean, useful negative
result, not a wash: it says the Nifty method's breakout-confirmation delay costs real edge on a
trend-prone instrument at this granularity — PA's higher win rate (60-65% vs ST_15's 42%) comes
from many small, cheap wins that don't compensate for missing ST_15's few large trend-following
wins. **PA-trend, standalone, is not a replacement for ST_15 on this data.** (See §2.6 for
whether it still has value as a filter/overlay rather than a standalone signal.)

### 2.5 The "bold" test — corrected: the fade engine is net-negative, not modestly profitable

The fade engine's exit had its own Round-3 bug (§1): it compared the retroactively-rewritten
`episode_id` at each bar against the entry bar's id, which let fades ride through denied false
breakouts (still showing the "old" episode) and ride against confirmed real breakouts for up to
96 bars (before the entry bar's own pending id resolved). Fixed to exit purely on the PIT range
bounds captured at entry (`close` crossing `range_high`/`range_low`) — causal, no `episode_id`
lookup at all.

**Result: 1,613 naive candidates → 394 PIT-confirmed, non-overlapping fade trades (up from 246 —
the corrected exit trips sooner, freeing the non-overlap tracker for more trades). Win rate
26.6%, net total −1,286 pts (n=394, avg −3.3/trade).** The fade engine is a net loser, not
modestly profitable as the pre-fix version showed. The tail did shrink in the direction the fix
predicted — **worst 5 trades are now −477, −419, −407, −349, −297** vs the pre-fix −932, −673,
−478, −419, −407 — consistent with the mechanism advisor diagnosed: the worst pre-fix losers were
fades riding a false-breakout-then-denial or a confirmed breakout the stale `episode_id` hadn't
caught up to yet, and both of those specific failure modes are gone. But the corrected engine is
simply a losing strategy at these settings (74% loss rate) — **fading an established range,
using the same detector's own bounds as the sole exit signal, does not work standalone on
CRUDEOILM 15-min in this window.**

*(Caveat, advisor-flagged: the candidate prefilter (`cand_idx`) still scans the full-history
`mid_result`, not a PIT-correct one — a bar that was genuinely PIT-valid at its own close but
later got retroactively reassigned to a pending window is never offered to the PIT-verification
pass. Those missed bars sit just before breakouts, the worst possible fade entries, so this
prefilter is a FAVORABLE subset of the true candidate set, not the full one. The fade loses money
even on the easier subset — not rebuilt, since the verdict can only get worse, not better.)*

### 2.6 Composite — corrected: PA-trend substitutes for ST_15, doesn't complement it, and the fade side is a net drag

Combined PA-trend (confirm=1) with the fade engine and compared against ST_15 alone on the
identical window, plus weekly-binned P&L correlation:

| | Net total (pts) | n |
|---|---|---|
| PA-trend engine (confirm=1) | +8,194 | 1,097 |
| Fade engine | −1,286 | 394 |
| **Composite (trend+fade)** | **+6,908** | 1,491 |
| ST_15 alone | +14,191 | 389 |

- **corr(weekly PA-trend, weekly fade) = −0.40** — meaningfully negative, the direction you'd
  want for a complementary pair, but neither piece is individually profitable enough on its own
  for the diversification to matter (§2.4, §2.5).
- **corr(weekly PA-trend, weekly ST_15) = +0.63** — high, and now corroborated directly: a
  bar-level overlap check (does a same-direction PA-trend commit fire within ±2 bars of each
  ST_15 flip, using the same honestly-timed commit bar as §2.4) finds **192/389 ST_15 flips
  (49.4%) have a matching PA-trend commit within ±2 bars.** Roughly half of ST_15's own trend
  changes are independently re-detected by PA within a 30-minute window — strong direct evidence,
  not just a correlation coefficient, that PA-trend is substantially the *same underlying signal*
  (a trend/regime-change detector on the same 15-min closes) rather than an independent second
  engine. **Both the "replace" framing and the "supplement via a second parallel engine" framing
  are now unsupported for the trend side** — PA-trend neither beats ST_15 standalone (§2.4) nor
  offers real diversification once combined (its weekly P&L moves with ST_15's). The fade side
  remains the more genuinely differentiated piece by correlation, but is itself unprofitable
  standalone (§2.5) — so as tested, neither half of this composite adds value over ST_15 alone.

**The last door — PA-trend as a confirmation FILTER on ST_15, rather than a standalone signal or
parallel engine — is also closed.** The ±2-bar overlap above admits PA commits that hadn't
happened yet at ST_15's own entry (fine for "is this the same signal," not for "would this work
as a filter"). A causally-correct split — a PA commit only counts if its confirmation bar closed
at or before ST_15's own signal bar, within a 4-bar (~1h) lookback window — separates ST_15's 389
(CRUDEOILM) / 410 (CRUDEOIL) flips into those with and without a causally-knowable same-direction
PA co-detection:

| | CRUDEOILM matched | CRUDEOILM unmatched | CRUDEOIL matched | CRUDEOIL unmatched |
|---|---|---|---|---|
| n | 72 | 316 | 81 | 328 |
| win% | 43.1% | 41.8% | 39.5% | 39.0% |
| avg pnl | +58.3 | +34.1 | +49.0 | +33.4 |
| total pnl | +4,195 | +10,772 | +3,965 | +10,954 |

If PA-trend had value as a confirmation gate, unmatched flips should have been the weak ones —
flat or net-negative. On both instruments they're not: unmatched flips are still solidly
profitable (avg +34.1 / +33.4, win% barely different from matched), just modestly below the
matched subgroup's average. **This rules out "PA co-detection as an entry filter" too** — it
would discard roughly 80% of ST_15's total P&L (the unmatched 316/328 trades carry ~72% of the
total) to chase a subgroup that's only ~1.5-1.7x better per trade, not a case where the
unfiltered trades are actually bad.

### 2.7 CRUDEOIL cross-validation — full-size contract confirms every corrected finding

Same pipeline (`PILOT_SYMBOL=CRUDEOIL research/range_detection/crudeoil_15min_pilot.py`), full
cross-reference against CRUDEOIL's own 410 ST_15 flips (`phase3_crudeoil/data_sweep/mult_2.0/`).
8,967 15-min bars, same 2026-01-30 to 2026-09-10 window:

| | CRUDEOILM | CRUDEOIL |
|---|---|---|
| PA-trend (confirm=1) net total | +8,194 (n=1097) | +5,155 (n=1123) |
| ST_15 (ref) net total | +14,191 (n=388) | +14,101 (n=409) |
| Fade engine net total | −1,286 (n=394) | −2,024 (n=422) |
| Composite (trend+fade) | +6,908 | +3,131 |
| corr(weekly trend, weekly fade) | −0.40 | −0.35 |
| corr(weekly trend, weekly ST_15) | +0.63 | +0.61 |
| Direct overlap (±2 bars, same dir) | 49.4% (192/389) | 47.3% (194/410) |

Every headline number lands within a few percentage points of the mini contract's own figure.
**This is not a single-instrument artifact** — PA-trend underperforms ST_15 standalone on both
contracts, the fade engine loses money on both, and the substitution relationship (high weekly
correlation, ~half of ST_15 flips matched within ±2 bars) holds on both. The remaining "single
~7-month window, one regime" caveat (§3) applies equally to both instruments, since they share
almost the same underlying price history by construction (MCX crude futures, mini vs full-size).

---

## 3. Open questions / what this does NOT yet show

**Resolved since the previous version of this plan:**
- ~~CRUDEOIL cross-validation not yet run~~ — done, §2.7. Confirms every corrected finding on the
  full-size contract.
- ~~Trade overlap between PA-trend and ST_15 beyond correlation~~ — done, §2.6/§2.7 (~half of
  ST_15 flips have a matching PA-trend commit within ±2 bars, on both instruments).
- ~~The "replace" test's headline number~~ — reversed on correction (§2.4): PA-trend loses to
  ST_15 standalone, on both instruments. Both §2.4's prior "PA beats ST_15" framing and §2.6's
  prior "supplement" framing for the trend side are now understood to have been reading a
  fill-timing lookahead, not a real edge.
- ~~PA-trend as a confirmation filter on ST_15~~ — tested directly (§2.6, causal co-detection
  split) and rejected: unmatched ST_15 flips are still solidly profitable (avg +34.1/+33.4 pts),
  not the weak subgroup a working filter would need. All three of "replace," "supplement as a
  parallel engine," and "filter/gate" are now closed for the PA-trend side specifically.

**Still open:**
- **All four framings from the original ask (replace/supplement/reject/differently-structured)
  have been tested for PA-trend-as-signal and PA-fade-as-signal specifically, and both are
  rejected as tested (§2.4, §2.5, §2.6, §2.7)** — the honest bottom line is that neither piece
  currently adds value over ST_15 alone, on either instrument, in this window. The positive
  finding is structural rather than a new signal: §2.1's fast hold-rate decay (4% survival at
  24h) and the fade engine's 26-30% win rate are two independent measurements pointing the same
  way — CRUDEOILM/CRUDEOIL at 15-min is a genuinely trend-prone instrument at this granularity,
  which is corroborating evidence (not proof) that ST_15's own always-in-market trend-following
  design fits the instrument, rather than a gap this line of research can fill.
- **Two structurally different, NOT-yet-tested ideas from advisor's last round, distinct from
  everything tested above:**
  - *Range duration as a conviction layer, not a signal*: split PA-trend commits by the PRIOR
    episode's own `bar_count` (i.e., did a breakout out of a long-established range follow through
    better than a breakout out of a short/choppy one?). This is a context/weighting question on
    top of ST_15's existing entries, not a new signal or filter, so it isn't covered by §2.2's
    (near-zero position effect) or §2.6's (filter rejected) nulls above.
  - *Compression-before-expansion, the inverse of the fade hypothesis*: §2.5 tested fading a
    range that's holding (mean-reversion). The untested inverse, on the same detector, is using a
    long-established, narrow, low-`bar_count`-growth range as a signal that expansion is
    imminent — a volatility-regime bet, not a directional one. Different enough from both §2.4
    (trend) and §2.5 (fade) that neither result speaks to it.
- **Fade engine's tail risk is still unmodeled beyond the 5 worst trades shown** — no stop-loss on
  the fade itself (only midpoint-or-break exit, capped at 96 bars). A hard SL on a failed fade
  wasn't tested; given the fade is now net-negative even before adding a hard SL's own cost, this
  is lower priority than it was.
- **Single ~7-month window, one regime, confirmed on two instruments that share the same
  underlying price history** — §2.7's CRUDEOIL cross-check rules out "mini-contract-specific
  artifact" but does NOT rule out "this specific 7-month window's regime." Every finding above,
  especially §2.3's direction-bias asymmetry, needs re-checking on a differently-trending period
  before being trusted as structural.
- **Advisor's proposed exit/SL tests (not yet built)**: range-derived exit targets (opposing
  bound as an alternative to fixed `TARGET1_PCT`/`TARGET2_FLAT_PCT`); range-derived SL (broken
  boundary vs. fixed 2.2%); a range-half-life-based time stop (Prometheus has no time-based exit
  today); conviction-weighted sizing (agree/fight as a `_calculate_units()` multiplier rather
  than a hard filter, since §2.3 showed the direction effect is asymmetric and unproven for
  down-ranges specifically). These target ST_15's own exit mechanics rather than PA-as-signal, so
  they're still live questions even though §2.4-§2.6 closed the door on PA-trend/fade as
  standalone replacements.

---

## 4. Constraints

- No changes to `prometheus_production/` at this stage.
- `research/range_detection/` stays research-only; `crudeoil_15min_pilot.py` does not modify
  the shared Nifty/Sensex scripts.
- Any implementation path needs its own dedicated backtest (slippage-adjusted, cross-checked
  against a second window) before any production consideration — this plan is Step 0, not a
  decision.
