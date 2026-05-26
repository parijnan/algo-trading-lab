# Plan: Athena Entry Filter — VIX Signal Research

**Status: Research in progress. No filter condition confirmed yet.**
**Routing scope moved → see `plans/vix-router-research.md` (Athena⇄Artemis VIX-direction
router). This file remains the reference for the annotation infrastructure and the corrected
VIX-signal findings, which the router work builds on.**

---

## Context

Started as part of range-detection research: after annotating 121 historical Athena trades
with PA range state, the up-biased cohort (61 trades, 50.8% win) was significantly weaker
than the down-biased cohort (60 trades, 65% win). Investigation into VIX behaviour during
the trade lifecycle led to a dual-timeframe VIX analysis.

---

## Signal Infrastructure (complete)

Two VIX indicators computed at entry (10:30 Wednesday), using only information available at
that moment — no lookahead:

### Dual-Timeframe VIX Supertrend (`vix_st_signal`)
- **Daily** (`vix_st_daily`): previous completed trading day's bar. Period = 7, multiplier = 3.0.
- **75-min** (`vix_st_75m`): 09:15→10:29 bar on entry day (complete before 10:30 entry).
  Period = 10, multiplier = 3.0.
- **Combined**: `'both_up'` | `'mixed'` | `'both_down'`.

### VIX Bollinger Bands %B (`vix_bb_zone`)
- 20-day SMA ± 2σ on daily VIX close. Uses previous completed daily bar.
- Zones: `above_upper` (>1.0) | `upper_zone` (0.7–1.0) | `mid_zone` (0.3–0.7)
  | `lower_zone` (0.0–0.3) | `below_lower` (<0.0).

Both signals are computed in `research/range_detection/annotate_athena.py` and stored in
`outputs/athena_annotated.csv`. The full 3-way classification grid (bias × ST × BB) is at
`outputs/vix_signal_grid.csv`.

---

## Implementation Note: Timezone Bug (fixed 2026-05-26)

An early version of the analysis loaded 1-min VIX using `.dt.tz_convert(None)`, which
converts IST timestamps to UTC (09:15 IST → 03:45). The day-anchored 75-min resampler
then applied a market-hours filter (09:15–15:30) against these UTC times, passing only the
last ~45 minutes of each trading day instead of the opening 75 minutes. This made the
75-min ST effectively a second daily ST rather than a genuine intraday signal, producing a
heavily skewed signal distribution (only 12 `mixed` trades out of 121).

Fix: `.dt.tz_localize(None)` keeps local (IST) time. The corrected distribution is
56 `both_up` / 49 `mixed` / 16 `both_down` — the two timeframes genuinely disagree ~40%
of the time, as expected from independent indicators.

All findings below are based on the corrected data.

---

## Findings (corrected data, 121 trades)

### What's clear

**Down-biased + `both_up` VIX is Athena's structural edge:**

| Bucket | N | Win% | Avg | Total |
|---|---|---|---|---|
| down + both_up + above_upper | 8 | 75.0% | +27.6 | +221 |
| down + both_up + upper_zone | 17 | 70.6% | +30.1 | +511 |
| down + both_up + mid_zone | 13 | 61.5% | +20.9 | +272 |

40 of 60 down-biased trades have `both_up` VIX. Falling market + rising VIX on both
timeframes = long vega working from both directions simultaneously. This is the mechanism
that makes Athena profitable.

**`lower_zone` BB is the weakest column across all combinations:**
31 trades, 52% win, +3.6 avg. Every other BB zone averages +15 to +25 pts. The weakness
holds for both up-biased and down-biased trades and all ST signals.

**`both_down` VIX overall is nearly flat:** 16 trades, 50% win, +4.95 total. The VIX
filter (16–25) already constrains the regime — sustained falling VIX rarely co-exists with
the VIX being above 16 for a full week.

### What's not clear yet

No single combination from the corrected 3-way table produces a clean skip signal with
sufficient sample size (N ≥ 10) and consistent negative performance. The cells are too
thinly populated for confident filtering: most interesting cross-sections have 5–10 trades.

---

## Next Steps

1. **`lower_zone` filter evaluation**
   - The weakest consistent signal across the entire grid. 31 trades, 52% win, +111 total.
   - Test: skip entries where `vix_bb_zone == 'lower_zone'` regardless of direction/ST.
   - Quantify: how much of the +111 comes from a few outlier winners? Check the distribution.

2. **Accumulate more data**
   - Current 5-year sample (121 trades) produces ~5–15 trades per 3-way cell. Most filter
     candidates need 20+ trades to be actionable.
   - Re-run annotation and grid after each new quarter of backtest data.

3. **`both_down` investigation**
   - 16 trades, 50% win, +5 total — these are the "tailwinds missing" trades. Worth
     understanding why they're entered (VIX filter passes 16–25 but both TF trending down)
     and whether they share a structural pattern.

4. **Implement filter infrastructure in `backtest.py`** (when a condition is confirmed)
   - `ENABLE_VIX_ENTRY_FILTER` toggle flag in `configs.py`
   - Precompute VIX daily ST + BB and 75-min ST at startup
   - Apply skip condition in the entry loop, after existing VIX filter
   - Target: isolated experiment — no other strategy logic changes

---

## Constraints

- No production changes until a filter condition is confirmed with adequate sample size.
- The research signal is correctly timed (no lookahead) — implementation translation will
  be direct when the time comes.
- The annotation script (`annotate_athena.py`) is the canonical source for all signal
  values — any filter condition should be verifiable against the annotated CSV first.
