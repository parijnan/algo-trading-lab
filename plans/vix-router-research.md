# Plan: VIX-Direction Router — Athena ⇄ Artemis

**Status: RESEARCH COMPLETE — VERDICT: symmetric VIX-direction router is not supported.**
See §15 for the full findings chain. The hard VIX-level gate remains in production unchanged.
Supersedes the routing scope of `plans/athena-entry-filter.md` (which remains valid for the
annotation infrastructure and the corrected VIX-signal findings).

---

## 1. Objective

Replace the **hard VIX-level gate** that currently decides between Athena and Artemis with a
**forward-VIX-direction forecast**. The two strategies are opposite-vega bets on the same
variable:

- **Athena** (Nifty double calendar) is **net long vega** — profits when VIX *rises*.
- **Artemis** (Sensex iron condor, post-Sep-2025) is **net short vega** — profits when VIX
  *falls / stays crushed*.

If we can forecast the sign of the VIX move over the trade's vega-exposure window with
better-than-coinflip, mechanism-grounded accuracy:

- VIX expected to **fall** → deploy **Artemis**
- VIX expected to **rise** → deploy **Athena**
- Ambiguous → fall back to the current hard gate, or skip

**Optimise as far as the data honestly supports, without overfitting.** The guardrails in
§10 are not optional.

---

## 2. Background: strategies, lifecycles, and the Sep-2025 regime split

There is a hard regime break at **Sep 2025** driven by the Nifty expiry-day change
(Thursday → Tuesday) and the Sensex expiry settling on Thursday.

| | Pre-Sep-2025 | Post-Sep-2025 |
|---|---|---|
| **Athena** underlying | Nifty | Nifty |
| Athena entry | Wednesday ~10:30 | **shared window** (Mon-ish) ~10:30 |
| Athena hold | ~5 trading sessions (Wed→Wed) | confirm from trade summary |
| **Artemis** underlying | **Nifty** | **Sensex** (switched; Sensex expiry = Thursday) |
| Artemis entry | own Nifty schedule | **same window as Athena** ~10:30 |
| Artemis hold | weekly | ~Mon→Wed/Thu; confirm from trade summary |

**Key consequence:** **post-Sep-2025 Athena and Artemis enter at the same window**, so the
router becomes a genuine **binary either/or decision at a single point in time** — exactly the
clean routing problem we want. Pre-Sep-2025 they ran on separate schedules, so for that era
the forecast is applied to each strategy's own entry independently.

**Current hard gate (to be replaced):**
- VIX < 16 → Artemis (`VIX_THRESHOLD = 16.0` in `artemis_backtest/configs.py`)
- VIX 16–25 → Athena (`VIX_FILTER_LOW/HIGH` in `athena_backtest/configs.py`)
- VIX > 25 → neither

**Why the level gate is crude:** a VIX *level* is a weak proxy for VIX *direction*. The whole
point of this project is that the same VIX level can precede very different forward paths.

---

## 3. Prerequisite cleanup (do first, separately)

- **Entry-time bug**: Artemis logs entry at `10:31` (e.g. `2025-09-01 10:31:00`). The
  intended entry is **10:30** — use the **open of the 10:31 one-minute candle**, matching
  Athena's convention (`get_1min_value(..., 'open')` at the entry timestamp). Fix in
  `artemis_backtest/backtest.py` and re-baseline before any routing comparison, so both
  engines read VIX/spot at the identical instant. Keep this as its own commit — it is not part
  of the router research.

---

## 4. Core idea: one forecast, consumed by both strategies

Build a **single strategy-agnostic VIX-direction forecast** keyed by (date, horizon). Both
strategies read the same forecast at their entry; the routing rule (§9) interprets it through
each strategy's vega sign. Do **not** build two separately-tuned signals — that doubles the
overfitting surface for no reason.

---

## 5. The anti-overfitting backbone: decouple forecast from trade P&L

**This is the most important methodological decision in the plan.**

We have ~121 Athena trades but **~1,800 daily VIX observations (2019→2026)**. Tuning a signal
against trade P&L fits to 121 noisy outcomes contaminated by strikes, spot path, and the
parachute → guaranteed overfit.

Instead, **validate the forecast on the full daily VIX series, independent of any strategy:**

1. For every day *t*, compute candidate signals (§6).
2. Compute the realised forward VIX change over each horizon *h* (§5.1).
3. Score predictive power with **rank statistics** (§7) — near-zero free parameters.
4. Only signals stable across the **full sample AND sub-periods** graduate to routing (§9).
5. The trade backtest (§8, last phase) is a **confirmation step, not the fitting step.**

This gives ~15× the sample, strips strategy noise, and decouples signal validation from the
thing being optimised.

### 5.1 Forecast target

```
fwd_vix_chg_h(t) = VIX_close(t + h) − VIX_close(t)
```
- Primary label: `sign(fwd_vix_chg_h)` and the magnitude.
- Secondary (vega-path nuance): `fwd_max_up = max(VIX[t..t+h]) − VIX[t]` and
  `fwd_max_dn = VIX[t] − min(VIX[t..t+h])`. Calendars/condors carry vega for much of the
  hold, so the *path* matters, not just the endpoint — but start with the endpoint sign; it
  is the cleanest, most robust target.

### 5.2 Horizons

Match the **vega-exposure window** of each strategy. Derive the exact modal hold from the
trade summaries (`entry_time → exit_time` deltas in trading sessions) rather than assuming:

- `h_athena` ≈ 5 sessions pre-Sep (confirm post-Sep value from the summary).
- `h_artemis` ≈ 2–3 sessions (confirm from `trade_summary_sensex.csv`).

If post-Sep holds are similar, a single ~weekly horizon can serve both; if not, validate at
each horizon separately.

---

## 6. Candidate signals — ranked by *mechanism*, not by fit

State the economic rationale **before** testing each. Two well-grounded signals beat ten
fitted ones.

### 6.1 Variance Risk Premium (VRP) — **top priority**
Realised Nifty vol vs implied (VIX). When VIX ≫ realised, the premium is rich and tends to
compress (VIX falls → Artemis); when realised catches up to / exceeds VIX, VIX tends to rise
(→ Athena). Strongest theoretical basis; fully computable from data on hand.

```
r_t      = ln(close_t / close_{t-1})                      # daily Nifty log returns
RV_n(t)  = std(r over trailing n sessions) * sqrt(252) * 100   # annualised %, comparable to VIX
VRP(t)   = VIX_close(t) − RV_n(t)                         # vol points  (also try ratio VIX/RV)
```
- Try `n ∈ {10, 20}` (cap the parameter search — two values, not a sweep).
- Optional refinement: Parkinson / Garman-Klass realised vol from OHLC (more efficient than
  close-to-close) — only if close-to-close shows promise.

### 6.2 VIX Bollinger %B / relative position — **active lead**
Already computed in `annotate_athena.py` (`vix_bb_pct`, `vix_bb_zone`). The corrected
finding that **`lower_zone` is consistently weak for Athena** is the first concrete
hypothesis: it may mark a calm/decaying regime where VIX grinds lower — i.e. an **Artemis**
signal. Test directly (§8 step 2).

### 6.3 VIX z-score vs longer MA
`z(t) = (VIX(t) − SMA_m(VIX)) / STD_m(VIX)`, `m ∈ {20, 50}`. A second mean-reversion framing
independent of band width. Use only if it adds orthogonal signal to VRP/%B.

### 6.4 VIX term structure (futures contango/backwardation) — **data check first**
The gold standard for forward VIX. Contango → spot VIX tends to rise toward futures /
backwardation → VIX expected to fall. **Action: check whether India VIX futures data exists
anywhere in the pipeline.** Liquidity is likely thin — if unavailable, drop this route.

### 6.5 Supertrend / ROC momentum — **demoted to minor feature**
We already proved ST at entry does not predict VIX over the hold (winners +1.28, losers −0.94,
both flagged `both_up`). ST is lagging and trend-following; VIX is mean-reverting. Keep ST as
at most a small auxiliary feature; it must not anchor the forecast.

### 6.6 The asymmetry that shapes everything
VIX **spikes up violently, grinds down slowly**. The grind-down is far more forecastable than
spikes (which need unforecastable catalysts). Therefore:
- The "VIX will fall → Artemis" side is the **easier, more reliable** bet.
- The "VIX will rise → Athena" side is **harder**.
This is a *design constraint*, not a fitted parameter — see §9.

---

## 7. Validation methodology

**Tools:** `pandas`, `numpy`, `scipy.stats` (`spearmanr`, optionally `pointbiserialr`),
optional `matplotlib`/`plotly` for decile charts. Reuse `research/range_detection/resample.py`
(`resample_daily`, `resample_intraday`) and `apollo_backtest/technical_indicators.py`
(`SupertrendIndicator`).

For each candidate signal × horizon, on the **full daily VIX series**:

1. **Rank correlation** — `spearmanr(signal(t), fwd_vix_chg_h(t))`. Report ρ and p-value.
2. **Decile monotonicity** — `pd.qcut(signal, 10)`; per decile report mean `fwd_vix_chg`,
   median, and **sign hit-rate**. A usable signal shows a near-monotone gradient across
   deciles, not just a significant ρ.
3. **Directional hit-rate** — fraction of days where `sign(signal-implied direction)` matches
   `sign(fwd_vix_chg)`. Compare against the base rate (VIX falls slightly more often than it
   rises — establish the base rate first).
4. **Walk-forward / sub-period stability** — compute ρ per calendar year (2019…2026). The
   sign of ρ must be **stable**; a signal that flips sign across years is overfit noise.
5. **Regime conditioning** — repeat within the gate-relevant bands (VIX < 16 for
   Artemis-relevant days, 16–25 for Athena-relevant days), since the live decision only
   happens inside those bands.

**Kill criterion:** if `|ρ| < ~0.10` on the full sample **and** the per-year sign is unstable,
drop the signal. Do not rescue it by adding parameters.

---

## 8. Phased research routes (ordered; each cheap and falsifiable)

Each phase is a self-contained script under `research/` (suggest a new
`research/vix_router/` directory). Do not touch production strategy code until the final
phase.

**Phase 0 — Horizons & base rates.**
Derive modal holds for both strategies (pre/post Sep) from the trade summaries. Establish the
unconditional base rate: P(VIX falls over h) for each horizon. *Output:* the horizons to use
and the coinflip benchmark every signal must beat.

**Phase 1 — VRP forecast validation (full VIX history).**
Build VRP (§6.1), run the full §7 battery at `h_athena` and `h_artemis`.
*Success:* stable ρ with monotone deciles and hit-rate clearly above base rate.
*Kill:* fails §7 kill criterion → VRP is not the signal; proceed to Phase 2 leads only.

**Phase 2 — `lower_zone` / BB %B hypothesis.**
Test directly whether `vix_bb_zone == 'lower_zone'` (and the continuous `vix_bb_pct`) precedes
flat-to-falling VIX over both horizons, full history. *Success:* confirms BB position as an
Artemis signal and explains the Athena `lower_zone` weakness mechanistically.

**Phase 3 — Combine (only if ≥2 signals survive).**
If VRP and BB %B both survive and are **orthogonal** (low mutual correlation), combine by
**rank-average** (not a fitted regression — keeps parameter budget near zero). Re-run §7 on
the blend. Reject the blend if it does not beat the best single signal out-of-the-box.

**Phase 4 — Term-structure route (conditional on §6.4 data check).**
Only if India VIX futures data is found. Test contango/backwardation as a forward-VIX signal.

**Phase 5 — Routing logic + backtest (last).**
Translate the surviving forecast into the router (§9). Backtest routing vs the current hard
gate. **Post-Sep-2025 is the clean shared-window regime** but a short sample (~8 months) —
report it separately from the reconstructed pre-Sep per-strategy application. Compare on
P&L, win-rate, R:R, and max consecutive losses, per strategy and combined.

---

## 9. Routing logic design

**Translate forecast → decision through each strategy's vega sign, with built-in asymmetry.**

Let `p_fall` be the forecast probability/score that VIX falls over the horizon.

- **Post-Sep-2025 (shared entry window — binary choice):**
  - `p_fall` high (≥ a *moderate* threshold) → **Artemis** (the easier, more reliable side).
  - `p_fall` low, i.e. confident VIX rises (≤ a *stricter* threshold) → **Athena**.
  - In between → fall back to the current hard VIX-level gate, or skip.
  - **Confidence asymmetry is deliberate:** Artemis clears a lower bar because VIX falls are
    more forecastable; Athena (betting on the harder "rise") must clear a higher bar. This is
    grounded in §6.6, not tuned to P&L.
  - **Open design question for the user:** do you want *strict mutual exclusivity* (exactly
    one strategy per window) or is "neither" / "both on different underlyings" acceptable?
    This decides whether the two thresholds must partition the line or can leave a gap.

- **Pre-Sep-2025 (separate schedules):** apply the same forecast at each strategy's own entry
  and horizon, replacing only that strategy's VIX-level gate. No binary contention.

Thresholds are calibrated on the **forecast distribution** (e.g. score quantiles), **not**
searched against trade P&L.

---

## 10. Overfitting guardrails (hard rules)

- Forecaster validated on **VIX history**, never tuned on trade P&L.
- Every signal needs a **stated economic mechanism before** testing.
- **Walk-forward / per-year sign stability** required — not just full-sample significance.
- **Parameter budget ≤ 2–3** across the entire forecaster. No grid sweeps of lookbacks/zones.
- **Confidence-asymmetric routing by design**, not by search.
- Combine signals by **rank-average**, not fitted weights, unless a regression is justified by
  a much larger validated sample.
- Report the **post-Sep routing backtest separately** and treat its short sample with
  appropriate skepticism.

---

## 11. Data & infrastructure reference

**Data files (`data_pipeline/data/indices/`):**
- `nifty.csv` — 1-min Nifty (2019-01-28→), tz-aware (+05:30).
- `india_vix.csv` — 1-min VIX, tz-aware (+05:30).
- `nifty_daily.csv`, `india_vix_daily.csv`, `sensex.csv`, `sensex_daily.csv` — daily series.

**Trade summaries:**
- `athena_backtest/data/trade_summary.csv`
- `artemis_backtest/data/trade_summary_sensex.csv` (post-Sep Sensex era)

**Reusable code:**
- `research/range_detection/resample.py` — `resample_daily`, `resample_intraday` (day-anchored).
- `research/range_detection/annotate_athena.py` — reference for VIX signal computation and the
  no-lookahead lookup pattern; already emits `vix_bb_pct`/`vix_bb_zone`/`vix_st_*`.
- `apollo_backtest/technical_indicators.py` — `SupertrendIndicator(period, multiplier)`
  (expects `High/Low/Close` capitalised; returns `Supertrend` column).

**Critical gotchas (learned the hard way):**
- **VIX timezone:** load 1-min VIX with `pd.to_datetime(...).dt.tz_localize(None)` — **NOT**
  `tz_convert(None)`. The CSV timestamps are IST (+05:30); `tz_convert(None)` shifts them to
  UTC (09:15 IST → 03:45), which silently corrupts any intraday resample. This bug previously
  turned the 75-min VIX Supertrend into a second daily ST.
- **75-min bar alignment:** the day-anchored 09:15 bar spans 09:15→10:29 and is complete
  *before* the 10:30 entry — correct to use at entry. Verify the bar timestamp, don't assume.
- **No lookahead:** daily signals must use the **previous** completed daily bar at a 10:30
  entry (the entry-day daily bar closes at 15:30, unavailable at entry). Intraday signals use
  bars that complete at/before entry. This is non-negotiable for an honest backtest.

---

## 12. Tools to employ — summary

| Task | Tool |
|---|---|
| Data load / resample | `pandas`, `research/range_detection/resample.py` |
| Realised vol, VRP, z-scores | `numpy` |
| Rank correlation, hit-rate | `scipy.stats.spearmanr`, `pointbiserialr` |
| Decile analysis | `pandas.qcut` |
| Supertrend (aux feature only) | `apollo_backtest/technical_indicators.py` |
| Decile / path charts (optional) | `matplotlib` or `plotly` |
| Final routing backtest | `athena_backtest/backtest.py`, `artemis_backtest/backtest.py` |

---

## 13. Where to resume / when to call back

Research is complete. No further phases needed.
See §15 for the full findings chain and verdict.

The natural successor is `plans/range-detection-research.md` §7 (validation gate), which is
now the unblocked next research priority. Artemis containment has been empirically confirmed
as the dominant P&L driver (§15.4); range state is the exogenous signal to test.

---

## 14. Implementation Architecture (addendum)

The research phases are exploratory — don't over-engineer throwaway analysis scripts. But
**one interface is durable**: the VIX forecast is consumed twice (Phase 1 validation *and* the
Phase 5 routing backtest, later Ares). Pin it down now so Phase 1 doesn't produce code that
Phase 5 rebuilds. This section specifies only the durable parts: directory layout, the forecast
interface, and per-phase output contracts. Everything else is implementer's discretion.

### 14.1 Directory layout — `research/vix_router/`

```
research/vix_router/
  data_layer.py     # load + resample VIX/Nifty; tz_localize(None); daily + intraday bars
  signals.py        # pure signal functions: vrp(), bb_pct(), zscore() — each date-indexed
  forecast.py       # THE durable interface (§14.2): build_forecast() + forecast_at()
  validate.py       # Phases 0–2: rank-stat battery vs forward-VIX target; emits reports
  outputs/          # generated CSVs/JSON (gitignored, like research/range_detection/outputs)
```
Reuse `research/range_detection/resample.py` (`resample_daily`, `resample_intraday`) and
`apollo_backtest/technical_indicators.py` (`SupertrendIndicator`) rather than reimplementing.

### 14.2 The forecast interface (durable — design once)

**Discipline:** all no-lookahead logic lives *inside* `build_forecast`, so consumers cannot
cheat. Each output row is labelled by the entry date and uses only the previous completed
daily bar + intraday bars complete by the entry time.

```python
# forecast.py
def build_forecast(vix_1m: pd.DataFrame, nifty_1m: pd.DataFrame,
                   horizon_days: int, config: dict) -> pd.DataFrame:
    """
    One row per trading day, indexed by entry date. Columns:
      vrp, bb_pct, zscore, ...   # raw signals (prev-day bar; no lookahead)
      score                      # combined rank-percentile in [0,1] (rank-average of signals)
      p_fall                     # optional calibrated P(VIX falls over horizon); else = score
      direction                  # 'fall' | 'rise' | 'neutral' (from score vs §9 thresholds)
    """

def forecast_at(forecast_df: pd.DataFrame, entry_date) -> dict | None:
    """Point lookup for the backtest — returns the row for entry_date, or None if absent."""
```

The **forward-VIX target is separate and validation-only** — it uses *future* bars and must
never feed back into `build_forecast`:

```python
# validate.py
def forward_vix_change(vix_daily: pd.DataFrame, horizon_days: int) -> pd.Series:
    """fwd(t) = VIX_close(t + h) - VIX_close(t), date-indexed. Future bars — validation only."""
```

Keep `config` tiny (≤2–3 params total, per the §10 guardrails): signal lookbacks and the two
asymmetric direction thresholds. No grid sweeps.

### 14.3 Per-phase output contracts (so phases compose)

| Phase | Emits | Consumed by |
|---|---|---|
| 0 | `outputs/horizons.json` — modal holds per strategy/era + base-rate P(VIX falls) | all phases (sets `horizon_days`, the coinflip benchmark) |
| 1–2 | `outputs/signal_validation_h{N}.csv` — per signal: Spearman ρ, p, per-year ρ, decile table, hit-rate | the go/kill decision (§7) |
| build | `outputs/vix_forecast_h{N}.csv` — the date→forecast table from §14.2 (**the durable artifact**) | Phase 5 backtest, later Ares |
| 5 | routing backtest reads `vix_forecast_h{N}.csv` (point lookup via `forecast_at`) | comparison vs hard gate |

### 14.4 Consumption boundary

Per the no-production-import constraint (§11 of `range-detection-research.md`): the **backtest**
phase may import `research/vix_router/forecast.py` directly (it's a backtest, like
`annotate_athena.py`). **Production wiring** (Leto router) is a later, separate concern — it
will either read a precomputed `vix_forecast` table or re-implement `build_forecast` against
the live feed; do not wire research code into production.

---

## 15. Research Findings and Verdict (2026-05-26)

All phases were executed in `research/vix_router/validate.py`. Regenerate outputs with
`python research/vix_router/validate.py` (outputs gitignored, rebuilds from 1-min data).

### 15.1 Phase 0 — Horizons and base rates

- **h_athena = 5 trading sessions** (modal, pre- and post-Sep consistent; n=121 trades).
- **h_artemis = 3 trading sessions** (modal, Mon→Thu expiry; n=26 Sensex trades).
- Base rate P(VIX falls) over h=3: **52.2%**; over h=5: **52.3%**. Near-coinflip — every
  signal must beat this meaningfully.

### 15.2 Phase 1 — VRP signal validation (full VIX history, 2019–2026, n≈1,800 days)

**Full-sample results** (Spearman ρ, VRP vs forward VIX change):

| Signal | h=3 ρ (p) | h=5 ρ (p) | Sign-stable full-sample? |
|---|---|---|---|
| VRP n=10 | -0.009 (0.71) | -0.020 (0.39) | No |
| VRP n=20 | -0.021 (0.38) | -0.056 (0.018) | No |
| BB %B | -0.009 (0.69) | -0.040 (0.093) | No |
| z-score 50 | -0.044 (0.062) | **-0.073 (0.002)** | Mostly |

All signals fail or are marginal on the full-sample kill criterion (§7). The full-sample
failure is caused by **regime dilution**: VRP's sign *flips* mechanistically across regimes
(negative <16, ~0 in 16–25, **positive** +0.29 to +0.36 in >25 — coherent mean-reversion in
calm, momentum in stress). Pooling all three cancels to near zero.

**Regime-conditioned results** (within VIX<16, Artemis-relevant band):

| Signal | h=5 ρ (p) | Per-year sign stable? |
|---|---|---|
| VRP n=10 | -0.168 (p≈0) | Mostly (2020 flips, n=31) |
| VRP n=20 | **-0.213 (p≈0)** | **Yes — all 8 years negative** |

VRP n=20 within VIX<16 passes the §7 walk-forward sign-stability test (2019–2026 all
negative), with ρ≈-0.21 at h=5. **Athena zone (16–25): no signal** (ρ≈0 for all signals,
well-powered null at n≈700 days). Symmetrically, no Athena-side routing edge exists.

**Band-crossing analysis** (h=3, Artemis's actual hold):
- Base rate P(VIX crosses 16 in next 3 sessions): **9.3%**.
- Low VRP (Q1): 14.1% cross. High VRP (Q5): **4.7%** cross. Ratio ≈ 3×.
- Even at the most bearish quintile, 86% of the time VIX stays <16. This rate is far too
  low to justify routing capital to Athena on a VIX-direction signal. The signal is a
  **skip/caution filter at most**, not a router.

### 15.3 Phase 1 — Trade-level confirmation (Artemis Nifty history, n=150 trades, 2019–2025)

VRP computed at entry (VRP n=20, prev-day close, no lookahead) attached to all 150 traded
Artemis-Nifty weeks.

**Key results:**
- Spearman VRP vs total_pl_points: **ρ=-0.084, p=0.31** — not statistically significant.
- Quintile win rates: Q1 76.7%, Q2 73.3%, Q3 70.0%, Q4 56.7%, Q5 66.7% — **non-monotone**.
  The expected monotone decreasing relationship (high VRP = high confidence Artemis) does not
  materialise. Q4 is the outlier trough; Q5 partially recovers.
- Low VRP (Q1+Q2) 75.0% win, avg P&L +15.4. High VRP (Q4+Q5) 61.7% win, avg P&L +6.6.
  Direction opposite to hypothesis, but effect is **not statistically significant**.

**Confounder identified (key methodological finding):**
Low VRP = high recent realised vol (Q1 rv20 median = 15.0 vs Q5 = 8.0). High realised vol
→ delta-based strike placement lands **wider** (Q1: 2.12% width, 0.76% min-breach distance;
Q5: 1.78% width, 0.58% min-breach distance). The apparent "low-VRP-wins" pattern is partly
an artifact of strike geometry, not an independent VRP signal.

**The dominant Artemis P&L driver:**
Spot-to-nearest-strike distance (min_dist_pct): **ρ=0.32, p=0.0001** — an order of magnitude
more significant than VRP. This is the spot-containment axis, not the vega axis, and it
directly validates `plans/range-detection-research.md` as the correct research direction for
Artemis enhancement.

### 15.4 Verdict

**The symmetric VIX-direction router is not supported.** Two independent failure modes:

1. **Athena side (VIX 16–25):** no VIX-direction forecasting edge exists (solid null, n≈700
   days). Hard level gate is as good as any forecast here.
2. **Artemis side (VIX <16):** VRP predicts VIX *direction* within the band (ρ≈-0.21,
   sign-stable 2019–2026), but VIX direction at this magnitude does not translate to condor
   P&L. The dominant P&L driver is spot containment (min_dist ρ=0.32, p=0.0001), not VIX
   ticks. A 3-day condor over a ±0.3 VIX drift is driven almost entirely by whether Nifty
   stays inside the short strikes.

**The hard VIX-level gate remains in production unchanged.** No modification is warranted.

**The research unblocked a higher-value finding:** containment is empirically the dominant
Artemis axis. The correct next research direction is `plans/range-detection-research.md` §7
(validation gate), followed by an Artemis range-anchored-strike variant backtest (§8 Rank 1,
§10 step 3). Range state is an *exogenous* containment signal that could improve strike
placement beyond what delta-based selection already encodes — that is the testable hypothesis.
