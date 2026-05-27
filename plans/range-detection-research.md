# Plan: Index Range Detection — Research & Applications

**Status: §7 VALIDATION GATE PASSED (2026-05-26) — proceed to §8 use cases.**
VIX router research is complete (see `plans/vix-router-research.md` §15 — verdict: symmetric
router not supported; containment is the dominant Artemis P&L driver, ρ=0.32 p=0.0001).
Next: Apollo chop-filter annotation + Artemis annotation (§10 step 2), then Artemis
range-anchored-strike variant backtest (§10 step 3).

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

3. ~~**Lot-sizing by direction**~~ **DONE (2026-05-27)** — full sweep complete; capital-adjusted
   asymmetric leg sizing validated. See §12 for full findings. Leading candidate: **E-adj**
   (CE×1.33/PE×0.67 on down_near; PE×1.33/CE×0.67 on up_near; both×0.5 on rest).
   Concrete lot structure: down_near (CE 80, PE 40), up_near (CE 40, PE 80), rest (CE 30, PE 30).
   Open questions remain on the sizing model before moving to strike anchoring.

4. **Artemis range-anchored-strike variant** backtest vs delta-based baseline.
   Use range bounds (`range_high`/`range_low`) to anchor short strikes instead of pure delta.
   Requires modifying the backtest engine's strike selection and re-running on full history.
   Test hypothesis: does anchoring to demonstrated support/resistance improve containment
   (higher min_dist_pct equivalent) beyond what the current delta-based selection achieves?

5. **Apollo chop-filter annotation** — most independent, fastest standalone win; not yet done.
   Annotate Apollo trades with `ep_entry_spot_pct` and range-break coincidence.

6. **Athena**: range-break exit + range-anchored strike placement as isolated experiments.

7. Standalone vega-adaptive strategy (*Ares*) only if step 4 shows incremental P&L gain AND
   is uncorrelated with the existing book — see `plans/range-vega-strategy.md`. Note: the
   symmetric VIX router that Ares depended on is not supported; Ares's vega-adaptive
   mechanism would need a different foundation if pursued.

---

## 12. Lot-Sizing Findings (2026-05-27)

Scripts: `lot_sizing_sweep.py`, `analyze_sizing_rule.py`, `analyze_asymmetric_sizing.py`
Data: 150 traded Artemis-Nifty trades (2019–2025), `artemis_annotated_nifty.csv`.

### Four structural buckets

| Bucket | n | % | CE avg | CE win% | PE avg | PE win% |
|---|---|---|---|---|---|---|
| down_near (down-biased, key_dist<50%) | 37 | 25% | +29.31 | 73% | -2.07 | 43% |
| down_far (down-biased, key_dist≥50%) | 28 | 19% | +3.05 | 54% | +2.29 | 57% |
| up_near (up-biased, key_dist<50%)    | 31 | 21% | -6.29 | 29% | +10.88 | 61% |
| up_far  (up-biased, key_dist≥50%)    | 54 | 36% | +2.66 | 54% | +2.93 | 52% |

`down_near`: CE is structurally protected by demonstrated resistance — scale CE.
`up_near`: CE has only 29% win rate; PE is protected by support + up-drift — scale PE.

### Symmetric sweep (lot_sizing_sweep.py)

Best symmetric result: `down+kd<50% ×2.0 / up(any) ×0.75` →
Sharpe 2.754 (+0.491), MaxDD -127.8, total +2498 (+56% vs baseline).

However, this result is **over-capitalised**: it assumes doubling lots on down_near trades,
which the broker (Sensibull-verified) prices at **1.5× the margin** of a standard balanced
iron condor. Cannot double lots if already at max capital.

### Asymmetric leg sizing (analyze_asymmetric_sizing.py)

Formula: `trade_pl = ce_factor × ce_comp + pe_factor × pe_comp`
where `ce_comp = ce_pl + ce_add_pl/lots`, `pe_comp = pe_pl + pe_add_pl/lots`.

**Capital adjustment:** 2× skewed trade (CE×2/PE×1) costs 1.5× capital of a balanced trade.
Under max-capital constraint: effective multipliers = 2/1.5 = **1.333** (protected leg) and
1/1.5 = **0.667** (unprotected leg).

### Capital-adjusted results vs baseline (+1601.4 pts, Sharpe 2.263, MaxDD -158.6)

| Config | Lot structure | Total | Sharpe | MaxDD |
|---|---|---|---|---|
| Baseline | all CE 60 + PE 60 | +1601.4 | 2.263 | -158.6 |
| sym-renorm (dn=1, rest=0.5) | dn 60+60, rest 30+30 | +1304.7 | 2.715 | -73.9 |
| C-adj (asym legs, rest=1×) | dn CE80/PE40, un CE40/PE80, rest 60+60 | +2165.7 | 2.699 | -155.8 |
| **E-adj (asym legs, rest=0.5×)** | **dn CE80/PE40, un CE40/PE80, rest 30+30** | **+1940.1** | **2.857** | **-90.9** |
| E-adj-0.75 (asym legs, rest=0.75×) | dn CE80/PE40, un CE40/PE80, rest 45+45 | +2052.9 | 2.804 | -120.2 |

**Leading candidate: E-adj rest=0.5×**
- Sharpe **2.857** (+0.594 vs baseline)
- MaxDD **-90.9** (43% reduction from baseline -158.6)
- Total +21% vs baseline
- Win rate 70.7% (vs 68.7%)
- Concrete lots: down_near (CE 80, PE 40), up_near (CE 40, PE 80), rest (CE 30, PE 30)

Key property: the MaxDD improvement is large (+68 pts better than baseline) while still
adding total P&L — unusual combination. Driven by: the 0.5× rest reduction absorbs the
big loss events (down_far/up_far choppy weeks), while the asymmetric leg routing on
favoured trades adds P&L without adding drawdown (capital goes to the structurally
protected leg, not gross exposure).

### Caveat

down_near = 37 trades, up_near = 31 trades across 6 years. Structural logic is sound and
CE/PE win rates are clean, but these are small-ish samples for the buckets where the
asymmetric edge is concentrated. Open questions remain before treating E-adj as final.

---

## 11. Constraints

- No changes to production strategy files at this stage.
- Any implementation must have a dedicated backtest showing improvement before going live.
- `research/range_detection/` is a research module only — not imported by production code.
- Validation gate (§7) must pass before any containment-based build.
