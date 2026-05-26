# Plan: Hestia — Range-Anchored, Vega-Adaptive Premium Strategy

**Codename: Hestia** (placeholder — rename freely; chosen to fit the Greek-deity family:
Athena / Artemis / Apollo. Hestia = hearth/center, fitting a containment strategy.)

**Status: Concept / design. Downstream of two upstream research threads — does not start until
both validate.**

**Supersedes** the "Range Anchor" concept previously embedded in
`plans/range-detection-research.md`.

---

## 1. Why this strategy exists (and why it isn't a clone)

Two independent edges have emerged from the research, on **orthogonal axes**:

- **Containment / spot axis** (range detection): an established range tells you *where spot is
  likely to stay*. Validated as independent of vega (corr(range direction, ΔVIX) ≈ 0).
- **Vega axis** (VIX router): a forward-VIX-direction forecast tells you *which way implied
  vol is likely to move*.

The existing book uses these only partially and rigidly:

| Strategy | Containment | Vega | Strikes |
|---|---|---|---|
| Athena | implicit (calendar tent) | **fixed long** | delta-based, ~ATM |
| Artemis | implicit (condor zone) | **fixed short** | delta-based |
| Apollo | inverse (wants breakout) | n/a (directional) | — |

**Hestia's thesis:** anchor short/long premium to a *confirmed* range's key level
(containment edge), and **choose the structure's vega sign from the VIX router** (vega edge).
That makes it differentiated:
- **vs Artemis** — Artemis is permanently short vega. Hestia flips to long vega when the
  router forecasts rising VIX.
- **vs Athena** — Athena is permanently long vega and delta/ATM-anchored. Hestia only deploys
  when a range is confirmed and anchors strikes to the proven key level.
- **vs Apollo** — Apollo bets on the range breaking; Hestia bets on it holding (opposite
  regime — they are natural complements, never both firing on the same read).

In short: Hestia is the **synthesis** of the two research threads, not a fourth flavour of the
same bet.

---

## 2. Hard dependencies (do NOT start before these pass)

Hestia is downstream of both threads. Building it before they validate is wasted effort.

1. **Range-detection validation gate** (`plans/range-detection-research.md` §7):
   - Key-level hold rate must be high enough that anchoring strikes to the key level has a real
     containment edge.
   - Duration distribution must show enough bars remaining after the 2–3 bar confirmation lag.
2. **VIX router Phase 1** (`plans/vix-router-research.md`):
   - A forward-VIX-direction forecast (VRP / mean-reversion) must validate on full VIX history
     with stable, better-than-base-rate skill — otherwise the vega-adaptation has no signal and
     Hestia collapses back into "Artemis with range strikes."
3. **Artemis range-anchored-strike variant** (`plans/range-detection-research.md` §10 step 3):
   - This is the *cheaper* test of half of Hestia (containment-anchored short premium). If
     range-anchored strikes don't beat delta-based strikes for Artemis, the containment-strike
     premise is weak and Hestia's structure needs rethinking before any standalone build.

**Gate rule:** all three must show signal. If only containment validates (router fails),
build the Artemis variant and stop — do not build Hestia. If only the router validates
(containment weak), the edge belongs in the router/strategy routing, not a new strategy.

---

## 3. Core design

### 3.1 Regime detection (entry precondition)
- An **established** PA range (≥3 bars, N=2 confirmed), direction known (up/down — skip
  initial/neutral).
- Sufficient expected bars remaining (threshold informed by the duration distribution; e.g.
  only enter if median remaining ≥ the hold horizon).
- Key level identified: `range_low` (up-biased = support) or `range_high` (down-biased =
  resistance).

### 3.2 Vega-adaptive structure selection (the differentiator)
Read the VIX router's forward-VIX forecast for the hold horizon:

| Router forecast | Vega sign | Structure (anchored to key level) |
|---|---|---|
| VIX likely **falls** | **short vega** | Iron condor / credit spread; shorts just *beyond* the range bounds (sell the edges the market respects) |
| VIX likely **rises** | **long vega** | Calendar / diagonal at the key level (sell near-term, buy further-dated) |
| Ambiguous | default | Short-vega theta harvest, or **skip** (decide via mutual-exclusivity choice in router plan §9) |

Confidence-asymmetric, per the router's design: take the short-vega (VIX-falls) side on
moderate confidence (more forecastable); require higher confidence for the long-vega (VIX-rises)
side.

### 3.3 Directional / up-drift skew
The Indian market's structural up-drift defends different boundaries depending on bias:
- **Up-biased range:** `range_low` (support) is up-drift-defended → favour put-side selling at
  / just below it; give the call side more room (up-drift can push through resistance).
- **Down-biased range:** up-drift fights the downtrend, so `range_high` (resistance) tends to
  hold short-term → call-side selling above it is reasonable, but tighten on reversal signs.

Encode as **asymmetric strike distances** from the key level, not symmetric offsets.

### 3.4 Entry timing
- Enter at the established-range confirmation (day 3+). Slow, selective.
- Single entry per range episode (no pyramiding in v1).

### 3.5 Exit — slow-in / fast-out
- **Primary (fast):** raw key-level break — spot *closes* beyond the key level → exit
  immediately. Do **not** wait for a new-episode N=2 confirmation; the price level is known,
  reacting late means eating the breakout loss.
- **Profit target:** 50–60% of max premium on credit structures.
- **Time:** close the near-term leg 1 day before its expiry; never carry into expiry day.
- **Vega-structure-specific:** for the long-vega calendar variant, also exit if the router
  flips its forecast materially mid-hold (the vega thesis is gone).

---

## 4. What to test for (validation before any live use)

1. **Does vega-adaptation add value over fixed vega?** The critical experiment. Backtest three
   variants on the same range-anchored entries/strikes:
   - (a) always short vega (≈ Artemis-at-range-bounds)
   - (b) always long vega (≈ Athena-at-range-bounds)
   - (c) **router-selected** vega sign (Hestia)
   If (c) does not beat both (a) and (b) out-of-sample, the vega-adaptation — Hestia's whole
   reason to exist — is not real. Kill it and keep the better of (a)/(b) as an enhancement to
   the existing strategy.
2. **Does range-anchoring beat delta-anchoring?** Compare range-anchored strikes vs the
   standard delta-based strikes (this overlaps the Artemis-variant test in the range plan).
3. **Does the up-drift skew help?** Asymmetric vs symmetric strike distances.
4. **Fast-exit value:** raw key-level-break exit vs holding to time stop — quantify the
   breakout losses avoided vs whipsaw costs incurred.
5. **Capital efficiency vs the book:** does Hestia earn enough incremental, *uncorrelated*
   P&L to justify a separate engine, or is it better folded into Artemis/Athena as a mode?

---

## 5. Backtest design

- **Engine:** start by adapting the Artemis (Sensex) backtest engine for the short-vega
  variant and the Athena (Nifty) calendar engine for the long-vega variant, sharing a common
  range+router signal layer. Avoid a brand-new engine until variants (a)/(b)/(c) prove out.
- **Signal layer:** reuse `annotate_*`-style precomputation — PA ranges + the router forecast
  keyed by (date, horizon). No lookahead (previous daily bar; intraday bars complete at entry).
- **Underlying:** decide Nifty vs Sensex per vega side, or test both. Sensex (Thursday expiry)
  aligns with the post-Sep Artemis infra; Nifty aligns with Athena.
- **Baselines to beat:** Artemis baseline, Athena baseline, and variants (a)/(b) above.
- **Metrics:** P&L, win-rate, R:R, max consecutive losses, and **correlation of returns with
  the existing book** (uncorrelated P&L is the real justification for a 4th strategy).
- **Regime split:** report pre/post-Sep-2025 separately (expiry-day + underlying change).

---

## 6. Risks & failure modes

- **Double dependency.** Needs *both* upstream threads to succeed. Highest-risk item in the
  research book — but also the only idea that monetises both edges at once.
- **Router reliability.** If the VIX forecast is only weakly skilled, flipping vega structures
  on it adds noise, not edge. Variant (c)-vs-(a)/(b) test is the guard.
- **Entry lag.** 2–3 bar confirmation eats early range duration; the duration-distribution gate
  must show enough bars remain.
- **Fast-exit whipsaw.** Reacting to raw key-level breaks will catch false breaks; test 4 must
  show net positive.
- **Strategy proliferation.** If incremental P&L is correlated with the existing book, the
  right answer is a *mode* inside Artemis/Athena, not a standalone strategy. Decide on the
  correlation metric, not on novelty.

---

## 7. Phased path

1. **Wait on dependencies** (§2). Track the two upstream plans.
2. **Build the combined signal table:** PA range state + router forecast per candidate entry
   date, with the chosen structure per regime. Pure annotation, no trading.
3. **Variant backtest (a)/(b)/(c)** on range-anchored entries — the decisive vega-adaptation
   test (§4.1).
4. **Skew + fast-exit refinements** (§4.3–4.4) only if (c) wins.
5. **Correlation / capital-efficiency check** (§4.5) → decide standalone vs mode.
6. **Production design** only after a dedicated backtest beats all baselines on uncorrelated
   P&L.

---

## 8. Data & infrastructure reference

- PA ranges: `research/range_detection/range_detector_pa.py`, `resample.py`.
- Router forecast: `plans/vix-router-research.md` (signal layer, once built).
- Engines: `artemis_backtest/backtest.py` (Sensex, short vega), `athena_backtest/backtest.py`
  (Nifty calendar, long vega).
- Data: `data_pipeline/data/indices/` — `nifty.csv`, `india_vix.csv`, `sensex.csv` (+ daily).
- Gotchas (inherited): VIX 1-min must be loaded with `.dt.tz_localize(None)` (IST, not UTC);
  no-lookahead (previous daily bar at entry; intraday bars complete at/before entry); slow-in /
  fast-out on range signals.

---

## 9. When to call back

- If variants (a)/(b)/(c) show (c) does **not** beat both fixed-vega variants → Hestia is
  dead; fold the better fixed variant into the existing strategy.
- If incremental P&L is **correlated** with the existing book → don't build standalone; make
  it a mode.
- If both upstream threads validate and (c) wins on uncorrelated P&L → proceed to production
  design and come back to scope the live engine.
