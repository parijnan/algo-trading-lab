# Plan: Index Range Detection — Research & Applications

**Status: §10 steps 1–4 complete; SL aftermath investigation complete and parked (2026-05-30).**
VIX router research is complete (see `plans/vix-router-research.md` §15 — verdict: symmetric
router not supported; containment is the dominant Artemis P&L driver, ρ=0.32 p=0.0001).
Steps 3–4 (lot sizing, strike placement) both found to be non-levers (~₹4k over 7 years).
SL optimisation investigated and closed (§14). Next: Apollo chop-filter annotation (step 5).

---

## 1. Context

The daily Nifty chart regularly shows clear consolidation ranges that are visually obvious but
hard to define algorithmically because their duration is variable (2 days to 2+ weeks). Two
methods were built and compared:

- **ADX method** (`range_detector.py`, `range_detector_75min.py`): ADX < threshold = ranging
  regime; episode bounds anchored via Williams Fractal swing highs/lows.
- **PA method** (`range_detector_pa.py`): range setter candles (closes outside current range)
  define bounds; wick extension allowed; gap openings anchor the inside bound to the previous
  range's near boundary. **Clearly superior on daily data.**

Hybrid ADX+PA combinations were explored and failed — ADX as a gate suppresses good ranges.
Pure PA with a breakout-confirmation filter is the active approach.

---

## 2. The Orthogonal-Axes Finding (central reframe)

After the VIX router work, we tested whether range state is just a VIX-direction proxy. It is
**not**. Measured on the 121 annotated Athena trades:

- **Range direction does not predict the VIX move during the hold.** corr(range=down, ΔVIX)
  = **+0.03** — essentially zero. Up- and down-biased ranges both see VIX roughly flat over
  the ~week hold.
- **Yet down-biased ranges earn 2.5× the P&L** (+25.3 vs +10.2 pts avg), and this holds
  *within* every VIX-ST bucket (both_up: +23 vs +2; mixed: +30 vs +20) with the *same*
  near-zero ΔVIX.
- Therefore the down-bias edge is a **spot-containment / profit-tent effect**, not a vega
  effect — almost certainly the Indian market's structural up-drift: up-biased ranges grind
  spot up and away from the calendar center (CE side tested, parachute deploys); down-biased
  ranges mean-revert toward the strikes.
- corr(range direction, VIX-ST signal) = **+0.46** — they are *correlated at entry* (down
  market ↔ elevated VIX state) but carry independent information about outcome.

**Implication for this plan:**
- Range detection's value is the **spot/containment axis** (where spot goes relative to the
  strikes), which is **independent of vega**. This validates the whole research line.
- Emphasis shifts from range *direction* (entangled with VIX state) to range **bounds &
  containment probability** (pure price, independent).
- Do **not** re-encode range direction as a vega/VIX signal — that double-counts what the
  router already handles.

(Cross-check that also matters for the router: corr(VIX-ST, ΔVIX) = **+0.01** — entry-time
Supertrend has no power to forecast the forward VIX move. The router must use VRP /
mean-reversion validated on full VIX history, not trend signals. Recorded in the router plan.)

**Additional empirical validation (2026-05-26, from VIX router trade-level confirmation):**
The containment axis was directly measured on 150 Artemis-Nifty trades (2019–2025).
Spot-to-nearest-strike distance (min_dist_pct) at entry predicts Artemis P&L at
**ρ=0.32, p=0.0001** — the strongest single signal found in the entire router research.
VRP (the vega axis) showed ρ=-0.084, p=0.31. This empirically confirms that containment
is the dominant Artemis driver and validates the orthogonal-axes framing with hard numbers.
Note: min_dist_pct is *endogenous* (the strategy's delta-based strikes determine it), so
the range-detection research must show that an *exogenous* PA range signal adds incremental
predictive power over the strike geometry already in use — that is the testable hypothesis
for the Artemis variant backtest (§10 step 3).

---

## 3. What Was Built

### `research/range_detection/range_detector.py` — Daily (ADX method)
Reads official daily Nifty OHLC from `nifty_daily.csv`. Validated, set aside in favour of PA.

### `research/range_detection/range_detector_75min.py` — 75-min (ADX method)
1-min data resampled to 75-min. Too noisy on its own — set aside.

### `research/range_detection/resample.py` — Shared resampler
Day-anchored N-minute resampler. Supports any integer timeframe (3, 5, 15, 75 min) plus
`'daily'`. Used by `range_detector_pa.py` and `annotate_athena.py`.

### `research/range_detection/range_detector_pa.py` — All timeframes (PA method)
Price-action range detection. Core rules:
1. Close outside current range → new range setter (its H/L define new bounds)
2. New H or L without close outside → wick extension (range expands, no split)
3. Gap open outside previous range → inside bound anchored to previous range's near boundary
4. `--breakout-confirm N`: require N additional consecutive closes outside before committing.
   If price returns inside within N bars, the candidate is absorbed as a wick extension.

Established ranges are colour-coded by direction:
- **Green** (up-biased): setter broke upward; range_low = key support (thick line)
- **Red** (down-biased): setter broke downward; range_high = key resistance (thick line)
- **Blue** (initial/neutral): first episode, no prior range
- **Grey dashed** (transient): bar_count < min_range_bars

### `research/range_detection/annotate_athena.py` — trade annotation
Tags all 121 Athena trades with PA range state (`ep_direction`, `ep_committed`,
`ep_established`, `ep_entry_spot_pct`, `key_dist_pct`, range bounds/width) and VIX indicators
(`vix_st_*`, `vix_bb_*`). Output `outputs/athena_annotated.csv`; classification grid
`outputs/vix_signal_grid.csv`.

---

## 4. PA Method — Parameter Reference

| CLI flag | Default | Effect |
|---|---|---|
| `--timeframe` | `daily` | `daily` or integer minutes (`75`, `15`, `5`, `3`) |
| `--start-date` | (required) | Initial range setter: `YYYY-MM-DD` or `"YYYY-MM-DD HH:MM"` |
| `--min-range-bars` | 5 | Min bars for established range (drawn solid) |
| `--breakout-confirm` | 1 | Extra closes outside before committing a new setter (0 = immediate) |
| `--hybrid` | `none` | `none` = pure PA, `a` = ADX hard gate (deprecated — didn't work) |
| `--adx-threshold` / `--adx-period` | 20 / 14 | Only used if `--hybrid a` |
| `--months` / `--all` / `--years` | — | Display scope controls |
| `--tag` / `--no-browser` | — | Output filename suffix / headless save |

---

## 5. Breakout Confirmation — Concluded

Three variants on 2023–2026 daily data (`--min-range-bars 3`):

| Tag | `--breakout-confirm` | Episodes | Established |
|---|---|---|---|
| `conf0` | 0 | 301 | 84 |
| `conf1` | 1 | 172 | 78 |
| `conf2` | 2 | 74 | 57 |

**Winner: N=2.** Clean, durable ranges; false breakouts absorbed. Current outputs use N=2.

---

## 6. Lag Analysis

With `--breakout-confirm 2`:

1. **Confirmation lag (2 bars, by design):** setter bar + 2 confirming closes → direction
   not known until day 3.
2. **Bounds instability:** range_high/low keep expanding via wick extension through the
   episode; bounds at commitment are provisional.

**What survives the lag:**
- **Direction** reliable from day 3 (3 consecutive closes one way).
- **Key level** (range_low up / range_high down) anchored to the *previous* range's boundary
  on gap opens — known before the breakout bar, so stable post-commitment.
- **Historical annotation** has no lag issue.

**Refinement adopted for any strategy use (see new-strategy plan): slow-in / fast-out.**
Use the N=2 confirmation for *entry* (be selective), but a *raw* key-level break for *exit*
(the price level is already known — react on the close beyond it; don't wait 2–3 bars to
confirm a new episode or you eat the whole breakout loss).

---

## 7. Validation Gate — PASSED (2026-05-26)

Script: `research/range_detection/validate_gate.py`
Data: 1,808 trading days (2019-01-28 → 2026-05-25); 183 episodes, 107 established.

Pre-declared kill thresholds vs results:

| Criterion | Kill if | Actual | Verdict |
|---|---|---|---|
| P50 bar_count | < 7 | **11** | PASS |
| Artemis hold rate (h=3, bar_count≥6) | < 50% | **88.8%** | PASS |
| Athena hold rate (h=5, bar_count≥8) | < 30% | **73.8%** | PASS |
| Wick breach rate (first 5 bars) | > 70% | **44.9%** | PASS |

**Duration distribution (107 established episodes):**

|  | n | P10 | P25 | P50 | P75 | P90 | mean |
|---|---|---|---|---|---|---|---|
| All | 107 | 5 | 7 | 11 | 19 | 30 | 15.2 |
| Up-biased | 69 | 5 | 7 | 9 | 16 | 24 | 13.0 |
| Down-biased | 38 | 5 | 10 | 15 | 24 | 32 | 19.1 |

**Close-hold rate by horizon:**

| Horizon | All | Up | Down |
|---|---|---|---|
| h=3 (Artemis) | 88.8% | 89.9% | 86.8% |
| h=5 (Athena) | 73.8% | 68.1% | 84.2% |
| h=7 | 60.7% | 49.3% | 81.6% |
| h=10 | 42.1% | 31.9% | 60.5% |

**Notable:** Down-biased ranges are both longer-lived (P50=15 vs 9) and hold better at all
horizons — consistent with the up-drift structural effect. Up-biased ranges are shorter and
fail sooner (particularly at h≥7). Wick breach rate 44.9%: intraday touches occur but close
containment is strong.

**Survival function (fraction still active at bar h after commitment):**
- Up-biased: h=3→90%, h=5→68%, h=7→49%, h=10→32%
- Down-biased: h=3→87%, h=5→84%, h=7→82%, h=10→61%

**GATE PASSED — all containment use cases unblocked (§8).**

---

## 8. Use Cases — Re-Ranked by Fit

Ranked after the orthogonal-axes finding. Range detection owns the **containment/spot** axis;
pair it with the VIX router for the vega axis where relevant.

### Rank 1 — Artemis (Sensex iron condor): strongest, most direct
An iron condor **is** a containment bet with explicit short strikes defining a profit zone.
The detected range bounds map almost 1:1 to strike placement:
- Sell CE above `range_high` (demonstrated resistance), PE below `range_low` (demonstrated
  support). The condor's profit zone = the detected range.
- **Skip** when no established range (trending/initial) or freshly committed (bounds unstable).
- **Directional skew** from the up-drift: in a down-biased range the up-drift fights the
  downtrend so resistance tends to hold short-term (room on CE); in an up-biased range support
  is up-drift-defended (safer PE).
- Pure price — no VIX entanglement (Artemis is already VIX-gated).

**Empirical support (2026-05-26):** spot-to-nearest-strike distance (a containment proxy) predicts
Artemis P&L at ρ=0.32, p=0.0001 on 150 historical trades. This is the strongest quantitative
support for range-based Artemis enhancement. The research question is whether PA range state
at entry (exogenous to strike placement) can improve that containment margin beyond what the
current delta-based selection already achieves.

**To do:** extend `resample.py` for Sensex (trivial path change) and annotate Artemis trades.

### Rank 2 — Apollo (Nifty ITM debit spread): cleanest, fully independent
Inverse use — range detection as a **chop filter** on Apollo's dual-Supertrend signal:
- Signal fires mid-range → likely false (chop) → skip or shrink size.
- Signal coincides with a confirmed **range break** → genuine breakout → take / size up.
- The broken boundary becomes a natural SL (failed breakout = close back inside).
- No VIX overlap, no containment dependency — quickest standalone win. Annotate Apollo trades
  with `ep_entry_spot_pct` and range-break coincidence.

### Rank 3 — Athena (Nifty double calendar): complementary, spot side only
Bounds improve the *spot* axis; direction is redundant with the router (corr 0.46):
- **Strike placement:** center calendars on `range_mid` / lean toward the key level, not blind
  ATM.
- **Up-drift adjustment:** give the CE side more room in up-biased ranges (or skip up-biased
  entries near the upper bound — the down-bias P&L edge is structural).
- **Range-break exit:** spot breaking the range = containment thesis dead = exit, independent
  of the router's vega call.
- Do **not** use range *direction* as an Athena entry/VIX signal.

---

## 9. New Strategy: moved to its own plan

The "Range Anchor" concept from the prior version of this plan is **superseded** by a fuller,
vega-adaptive design in **`plans/range-vega-strategy.md`** (codename *Ares*). Summary of the
verdict that drove the move:
- A fixed short-premium-at-range-bounds strategy is ~80% Artemis. Don't build that standalone.
- The genuinely differentiated idea unifies **both research axes**: anchor to a confirmed
  range (containment) *and* pick the structure's vega sign from the VIX router (vega).
- First test the cheaper hypothesis — **range-anchored strikes as an Artemis variant** — before
  committing to a standalone engine. Full design, dependencies, and validation in the new plan.

---

## 10. Recommended Sequence

VIX router research is complete (§15 of `plans/vix-router-research.md`) — no blocker remains.

1. ~~**Validation gate** (§7): key-level hold rate + duration distribution. Decisive, cheap.~~
   **DONE (2026-05-26)** — gate passed. See §7 for full results.

2. ~~**Apollo chop-filter annotation** + **Artemis annotation**~~ (extend `resample.py` for Sensex).
   **DONE (2026-05-26)** — `resample.py` extended for Sensex; `annotate_artemis.py` written and
   run on both Nifty (296 trades) and Sensex (134 trades).
   Key findings: `min_dist_pct` ρ=+0.32 p=0.0001 confirmed; `key_dist_pct` ρ=-0.17 p=0.043
   (closer to key level → better P&L); down-biased ranges earn 2.5× avg P&L (17.81 vs 5.22 pts).
   Design constraint: no trade filtering — trades are taken every eligible week; optimise the
   trade itself, not the entry decision.

3. ~~**Lot-sizing by direction**~~ **DONE (2026-05-28, finalised same session)** — full sweep
   complete with three correctness fixes applied in sequence:
   - Look-ahead bug: `side='right'` → `side='left'` in annotation (23 trades affected).
   - `ep_committed` filter in `assign_buckets()`: uncommitted ranges (bars_into < 3) no longer
     leak into near/far buckets.
   - `key_dist >= 0` guard: spot already above `range_high` (key_dist < 0) falls through to
     'other' — was incorrectly classified as down_near under the `< 50` threshold alone.
   See §12 for corrected findings. Capital constraint analysis (80/40 or 60/60 only):
   **lot sizing produces no meaningful gain within a fixed capital budget** — CE uplift is
   nearly cancelled by proportional PE reduction regardless of split ratio (80/40, 85/30,
   90/20 all give ~+₹4k over 7 years). Lot sizing is not the lever.

4. ~~**Artemis range-anchored-strike placement**~~ **DONE (2026-05-28, finalised same session).**
   Script: `research/range_detection/step4_strike_counterfactual.py`.
   See §13 for full findings. **Verdict: not a meaningful lever.**

5. **Apollo chop-filter annotation** — most independent, fastest standalone win; not yet done.
   Annotate Apollo trades with `ep_entry_spot_pct` and range-break coincidence.

6. **Athena**: range-break exit + range-anchored strike placement as isolated experiments.

7. Standalone vega-adaptive strategy (*Ares*) only if step 4 shows incremental P&L gain AND
   is uncorrelated with the existing book — see `plans/range-vega-strategy.md`. Note: the
   symmetric VIX router that Ares depended on is not supported; Ares's vega-adaptive
   mechanism would need a different foundation if pursued.

---

## 12. Lot-Sizing Findings (2026-05-28, finalised)

Scripts: `lot_sizing_sweep.py`, `analyze_sizing_rule.py`, `analyze_asymmetric_sizing.py`
Data: 150 traded Artemis-Nifty trades (2019–2026), `artemis_annotated_nifty.csv`.

### Three correctness fixes applied in sequence

1. **Look-ahead bug** (`side='right'` → `side='left'` in `annotate_artemis.py`): 23 Nifty
   trades had direction determined by same-day close. Fixed to Friday's bar. All prior E-adj
   numbers invalidated.

2. **`ep_committed` filter** in `assign_buckets()`: uncommitted entries (bars_into < 3) now
   fall through to 'other' instead of leaking into near/far buckets. Impact: down_near 32→31.

3. **`key_dist >= 0` guard** in `assign_buckets()`: negative key_dist means spot has already
   broken through the key level (above `range_high` for down, below `range_low` for up).
   Previously classified as down_near via `key_dist < 50`; now falls to 'other'.
   Impact: 2 Nifty trades removed (2021-12-13, key_dist=−8.8%; 2022-10-31, key_dist=−10.2%).
   down_near 31→29. The 2022-10-31 trade was a −55 pt CE under ×2.0 that is now correctly
   excluded. Root cause confirmed via 1-min Sensex data for a 2026-05-25 scenario: the PA
   episode's `range_high` is set by the episode setter candle, not the prior transient episode.

### Final structural buckets (Nifty 150 trades, all fixes applied)

| Bucket | n | CE avg | CE win% | PE avg | PE win% |
|---|---|---|---|---|---|
| down_near (down, key_dist 0–50%)  | 29 | +18.34 | 65.5% | +5.38 | 58.6% |
| down_far  (down, key_dist ≥50%)   | 23 | +14.38 | 56.5% | -2.95 | 43.5% |
| up_near   (up, key_dist 0–50%)    | 19 | +6.65  | 42.1% | -1.77 | 42.1% |
| up_far    (up, key_dist ≥50%)     | 39 | -2.07  | 38.5% | +1.05 | 46.2% |

**down_near CE signal survives** — resistance overhead is real (CE avg +18.3, win% 65.5%).
**up_near PE signal gone** — was entirely look-ahead; CE and PE now roughly equal in up_near.

### Final sizing results (Nifty 150 trades)

| Config | Total | Sharpe | MaxDD | Win% |
|---|---|---|---|---|
| Baseline | +1601.4 | 2.263 | −158.6 | 68.7% |
| A: dn_near CE×2.0 | +2133.2 | 2.557 | −157.3 | 69.3% |
| E-adj (M=1.333, rest×0.5) | +1561.9 | 2.494 | −201.9 | 68.7% |
| E-adj-bk (uncommitted→near) | +1664.9 | 2.455 | −221.1 | 68.0% |

Combined Nifty+Sensex (177 trades): Baseline +1.46L, A: dn_near CE×2.0 +1.76L (Sharpe 2.656).

### Capital constraint finding

Artemis is capital-constrained to 80/40 lots (CE/PE) max vs 60/60 baseline. A 2:1 skewed
condor (120/60) costs 1.5× margin; 80/40 = (2/3)×120/60 = capital-neutral. Within a fixed
budget, **any split ratio (80/40, 85/30, 90/20) produces the same ~+₹4k uplift** over 7
years — CE gain is nearly cancelled by proportional PE reduction. Lot sizing is not the lever.

### Conclusion and step 4 direction

The down_near CE structural edge is real but cannot be captured through lot sizing under
a capital constraint. The signal's value lies in strike placement:

- Artemis uses a **fixed expected premium target** (VIX-adaptive: wider strikes at high VIX,
  tighter at low VIX).
- In 31 down_near trades, 15 already have CE strike above `range_high` (high VIX naturally
  places them there); 16 have CE below resistance.
- CE losses in the "CE below resistance" group are predominantly `index_sl` (combined
  position stop, not CE-specific) — moving the CE strike would not have saved them.
- **Step 4 direction:** model P&L if CE is always anchored at `range_high + buffer` for
  down_near, using `ce_sell_entry` / `ce_buy_entry` premiums from the annotated CSV to
  estimate entry credit at the counterfactual strike.

---

## 13. Strike Placement Findings (2026-05-28, finalised)

Script: `research/range_detection/step4_strike_counterfactual.py`
Output: `research/range_detection/outputs/step4_counterfactual_nifty.csv`

**Counterfactual rule:** CE sell always placed at the first 100-pt strike strictly above
`range_high` (resistance-anchored), CE buy at +300. Actual options data used for entry/exit
premiums; scaling applied for 10 of 29 trades where the current options files differ from
the data used in the original backtest (verified by matching actual entry prices).

### Breach analysis prerequisite

Weekly high from 1-min Nifty data (entry → expiry):
- **Held (16/29, 55%):** resistance did not break → CE expires worthless regardless of strike.
- **Breached (13/29, 45%):** resistance broke. Median overshoot = 126 pts above `range_high`.
  CF strike gap to resistance = 1–100 pts. **12/13 breach trades: CF CE also gets hit.**
  Strike placement cannot protect against a breach; overshoot is almost always large enough
  to clear the resistance-anchored strike as well.

### Results

| Group | n | Δ entry credit/trade | Δ CE P&L/trade | Δ total (7yr) |
|---|---|---|---|---|
| CE below RH (16 trades) | 16 | −16.75 | +1.15 | +18 pts |
| CE above RH (13 trades) | 13 | +18.22 | +10.11 | +131 pts |
| All down_near | 29 | −1.08 | +5.17 | **+150 pts = ₹3,746** |

**CE below RH:** Moving CE from near-ATM (targeting fixed premium) up to just-above-resistance
loses significant entry credit (−16.75/trade avg). Gains some protection, but since 8/8 breach
trades in this group have overshoot > CF gap, the CE is still hit on breach. Net near-neutral.

**CE above RH:** When VIX is high, Artemis goes very far OTM (e.g. 350+ pts above resistance
in Feb 2024) to keep the sell premium at target. Moving down to just-above-resistance captures
substantially more premium (+18.22/trade avg). For held trades, this becomes realised profit.
For breached trades, the closer-to-money CF CE is now hit worse (e.g. 2024-04-22: −30.85 delta).

### VIX split

| Group | n | Breach% | CE win% | CE avg | CF delta |
|---|---|---|---|---|---|
| High VIX (≥14.8) | 10 | 40% | 90% | +21.7 | +11.2/trade |
| Mid/Low VIX (<14.8) | 19 | 47% | 53% | +9.0 | +2.0/trade |

### Verdict

**₹3,746 incremental over 7 years from 29 trades.** Not a meaningful lever.

The fundamental conclusion: the down_near CE structural edge (+18 pts/trade, 65.5% win rate)
comes from **resistance holding more often than not in down-biased ranges** — a structural
property of the PA episode definition. Strike placement does not change whether resistance
holds; it only changes how much premium is collected or lost when it does.

Every lever explored in steps 3–4 (lot sizing under capital constraint, resistance-anchored
strike placement) produced improvements of at most ~₹4k over 7 years against a ~₹1.46L
baseline. The rigid base rule — "trade Artemis on down_near weeks" — delivers the edge;
parameter optimisation cannot meaningfully extend it within the existing trade structure.

**VIX observation (not actioned):** High VIX down_near (n=10) has 90% CE win rate.
Filtering to high VIX only would skip 19 profitable trades (avg +22.3 pts each, ₹10.6k total
foregone). VIX gate sharpens per-trade quality but degrades total P&L.

---

## 14. SL Aftermath Analysis (2026-05-30, investigated and parked)

Script: `research/range_detection/analyze_sl_aftermath.py`
Output: `research/range_detection/outputs/sl_aftermath_analysis.csv`

### What was investigated

For every stopped Artemis trade (Nifty + Sensex), computed the counterfactual P&L if the
original position had been held to expiry using intrinsic value at expiry spot. Classified
each trade by range bucket, stop type, and day of first exit. Specifically investigated
whether Thursday `index_sl` cases where spot had broken the PA range could be treated
differently — either via conditional SL suppression or re-entry.

### Overall SL verdict

Stops are net beneficial:
- All stopped trades: ~55% premature by count, but saved P&L exceeds cost of premature exits.
- `index_sl` and `option_sl` are doing their designed job. Not candidates for removal.
- **ELM is regulatory capital management (SEBI-mandated), not an optimisation lever.**
  Only `index_sl` and `option_sl` are candidates for any future SL research.

### Thursday range-broken index_sl cases (the specific angle investigated)

8 trades (across Nifty + Sensex) where `index_sl` fired on Thursday (expiry day) and spot
was outside the PA range at the time of the SL. All 8 expired profitably if held — 100%
in-sample win rate. Detailed grid:

```
#  Date         Inst    Leg  Strike  Entry    Exit    Spot@SL  RangeHi  RangeLo  MaxAdv   SL_time  MaxPremAfterSL
1  2023-07-03   nifty   CE   19500   25.15   21.30   19450    19201    18886    19512    10:14     25.00
2  2024-07-15   nifty   CE   24800   22.55   18.05   24754    24635    24331    24838    13:53     61.20
3  2024-09-16   nifty   CE   25500   41.85   82.20   25543    25433    24753    25612    09:16    136.00
4  2024-09-23   nifty   CE   26200   30.15   51.90   26168    25956    25427    26251    14:37     62.50
5  2025-08-04   nifty   PE   24300   34.85   15.55   24347    25010    24535    24344    13:38     15.55
6  2025-10-20   sensex  CE   85100  118.50  215.95   85036    84172    82727    85290    09:16    279.90
7  2025-11-03   sensex  PE   83200  113.00   64.15   83439    85290    83906    83238    10:13     76.00
8  2025-11-17   sensex  CE   85600  114.65   66.45   85402    84919    84029    85802    10:41    209.40
```

**MaxAdv**: max high (CE) or min low (PE) reached that day — worst intraday spot for the leg.
**MaxPremAfterSL**: highest the sold option's premium reached after the SL fired (from
individual option files in `data_pipeline/`).

### Why conditional SL suppression was rejected

- Cases 3, 6, 8: option continued materially higher after exit (136 vs 82 exit; 280 vs 216;
  209 vs 66). MTM would have gone deeply negative before recovering.
- 8 trades over 7 years is too thin a sample to conclude mean reversion is reliable.
- Risk of suppressing a stop on a genuine breakout is unacceptable given the option upside.

### Why immediate re-entry was rejected

- **No surviving leg on expiry day.** In all 8 cases, the other leg was already closed before
  Thursday's index_sl fired — via ELM (Wednesday 15:16) in 6 cases, via option_sl (Wednesday)
  in 1 case. Re-entry means opening a brand new single-leg spread from scratch.
- **Strike placement is premium-based.** Artemis selects strikes to hit an expected premium
  target, not anchored to PA range bounds. After spot spikes and reverts, the OTM options
  near the original strike have collapsed to near zero. To hit the premium target, the new
  sell strike would have to be significantly closer to spot — more ATM, on expiry morning,
  after a demonstrated volatile spike.
- **Premium collapse by confirmation time.** A decision tree needs spot to re-enter the range
  and hold for confirmation bars. By that point, expiry-day theta has crushed the option
  premium. Re-entering at a fraction of the original premium does not justify the transaction
  costs and the degraded risk profile.
- **This is a different trade.** The original CE edge came from selling into a premium-target
  structure with the full weekly time value. The re-entry CE is a short expiry-day position
  entered after a volatile morning — structurally and probabilistically different.

### Verdict

**Parked.** The SLs are working. These 8 cases are the cost of having protective stops — a
cost that is outweighed by the cases where stops correctly prevent larger losses. No viable
optimisation lever was found in this direction.

---

## 11. Constraints

- No changes to production strategy files at this stage.
- Any implementation must have a dedicated backtest showing improvement before going live.
- `research/range_detection/` is a research module only — not imported by production code.
- Validation gate (§7) must pass before any containment-based build.
