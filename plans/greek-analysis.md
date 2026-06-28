# Plan: Greek Analysis — Diagnostic and Predictive Research

**Status: Branch 1 (pnl_attribution) COMPLETE for both Athena (124 trades) and Artemis (173 trades). Branch 2 (greek_profile) COMPLETE for both. Branch 3 (iv_term_structure) COMPLETE for Athena (2026-06-23). Branch 4 (realized_vs_implied) COMPLETE for Athena and Artemis (2026-06-23). Branch 5 (iv_skew) CLOSED (2026-06-23): IC=+0.022, sign-unstable across periods — both close conditions triggered. Branch 6 (greek_exit_triggers) CLOSED for both Athena (2026-06-23) and Artemis (2026-06-24): Athena — vol-aware thesis invalid (p=0.67), early-warning (delta=0.45) fires 56 vs 21 trades but Δmean=−0.45 pts full-sample, −0.69 pts recent — both periods degraded; Artemis — closed at diagnostic: trigger delta=0.38 median is already the current offset's natural exposure (50 pts OTM = ~delta 0.38), VIX effect p=0.47 n.s., no backtest run. Branch 7 (exit_timing) CLOSED for Artemis (2026-06-24): no gamma/theta crossover at any DTE — position remains theta-positive throughout; breakeven LARGEST at expiry day (11.2 pts vs median realized 3.2 pts, 18.7% pct_gt); surviving-leg post-CE-roll is closest to crossover at 42% — still theta-positive. Losses are directional (delta), not DTE-timing. Branch 7 (exit_timing) CLOSED for Athena (2026-06-28): no vega/theta crossover at any DTE — tv_mean positive at all observed DTE buckets (1–8d+); vega reverses sign by DTE (near-expiry positive due to near-leg vega collapse, early-trade negative due to term-structure slope). Losses are event-driven vol spikes, not DTE-structured. All branches complete for both strategies.**

---

## 1. Motivation

Every backtest and live session is summarized as total P&L points. We have never empirically
verified whether the strategies make money *the way they are designed to*. The central questions:

- Does Athena (calendar condor) harvest theta and benefit from vega, or are wins actually driven
  by vol crush and accidental direction?
- Does Artemis (iron condor) P&L track spot containment as expected, and is the dominant Greek
  the delta of the untested leg?
- Are our fixed-point exit triggers (EMERGENCY_TRIGGER_OFFSET, OPTION_SL_MULTIPLIER) appropriate,
  or does their effectiveness vary with vol regime?

The TC and OI research tracks were both predictive signals. Both died the same death: in-sample
appearance, period-split collapse, small-n fragility. This research track leads with *diagnostic*
work — measurement that cannot overfit — and only enters predictive territory where there is a
mechanistic justification (Greek-threshold exit triggers).

---

## 2. Organizing Principle

**Diagnostic** (branches 1–4): measure what our existing trades already did. No signal, no
in/out-of-sample distinction, no overfitting risk. High certainty of insight.

**Predictive** (branches 5–6): new entry/exit signals derived from Greeks. Subject to the full
IC / period-split / sample-size gauntlet. Treat with low prior.

---

## 3. Shared Infrastructure — `greek_engine.py`

All branches share a common Greek computation layer at `research/greek_analysis/greek_engine.py`.

### Core functions

```python
def compute_iv(option_price, spot, strike, dte_years, rate, option_type) -> float:
    """Back out IV from market price using mibian (Black-Scholes)."""

def compute_greeks(iv, spot, strike, dte_years, rate, option_type) -> dict:
    """Return delta, gamma, theta, vega from mibian."""

def load_trade_logs(trade_logs_dir) -> pd.DataFrame:
    """Load per-bar trade logs from the backtest trade_logs/ directory.
    Returns one row per (trade_id, bar_ts) with columns:
      spot, ce_sell_ltp, ce_buy_ltp, pe_sell_ltp, pe_buy_ltp,
      ce_wing_ltp, pe_wing_ltp, dte_near, dte_far, vix."""

def position_greeks(legs: list[LegParams], spot, ts) -> dict:
    """Compute net delta/gamma/theta/vega for a multi-leg position at a point in time.
    legs is a list of (strike, expiry, option_type, direction, qty) tuples."""
```

### Compute cost note

The full backtest is 600K rows × 462 expiries. IV computation via mibian per row per leg is
expensive. Cache IV to parquet per trade at first run; subsequent branches reuse the cache.
Estimated runtime: 15–30 min first pass; <1 min on cached runs.

### Data requirements

- `athena_backtest/data/trade_logs/` — per-bar snapshots during each trade
- `athena_backtest/data/trade_summary.csv` — entry/exit metadata
- `data_pipeline/data/nifty/options/` — raw option price data (already used by backtest)
- `data_pipeline/data/indices/india_vix.csv` — VIX at each bar

---

## 4. Branch Descriptions

### Branch 1: P&L Attribution (`pnl_attribution/`)

**Type:** Diagnostic. **Priority:** Highest.

Decompose each trade's realized P&L into delta, gamma, theta, and vega contributions.

Method:
1. For each bar in the trade, compute net Greeks at the position level.
2. P&L change between bar t and t+1 splits as:
   - Delta contribution: Δspot × net_delta × lot_size
   - Gamma contribution: ½ × Δspot² × net_gamma × lot_size
   - Theta contribution: Δt × net_theta (time decay, expected to be positive for Athena)
   - Vega contribution: ΔIV × net_vega × lot_size
   - Residual: unexplained (should be small if Greeks are well-estimated)
3. Sum contributions over the full trade lifetime.

Output: `data/pnl_attribution.csv` — one row per trade with columns:
  `trade_id, entry_date, total_pl, delta_contrib, gamma_contrib, theta_contrib, vega_contrib, residual`

Key questions:
- What fraction of Athena P&L comes from theta vs vega?
- On losing trades, which Greek drives the loss (delta/gamma = direction; vega = vol expansion)?
- Does the attribution pattern differ between early 2020-22 and recent 2023+?

Entry point: `research/greek_analysis/pnl_attribution/run.py`

**Findings (Athena, 124 trades):** Theta +57 pts/trade (primary engine). Gamma −33.8 (consistent
drag). Vega −14.4 avg; flips on losses (−38 on losers vs 0 on winners) — losing trades are
vega-driven. Athena is net short-vega, not long-vega as designed.

Entry point: `research/greek_analysis/pnl_attribution/run_artemis.py`

**Findings (Artemis, 173 trades):** Theta +48.8 pts/trade (primary engine). Gamma −34.0 (identical
drag to Athena). Vega −24.5 (larger drag than Athena — iron condor is structurally short-vega).
Losses are delta-driven (Δ=−27.8 on losers vs +13.9 on winners) — spot breaks directionally past
sell strike. Athena and Artemis have opposite loss mechanisms: Athena loses on vol expansion,
Artemis loses on spot movement.

---

### Branch 2: Greek Profile (`greek_profile/`)

**Type:** Diagnostic. **COMPLETE (2026-06-18).**

Track net position Greek *levels* at each bar from entry to exit. Distinct from Branch 1's
contributions (exposure × Δmarket). Long-vega exposure with falling IV posts negative vega P&L.

**Findings (Athena):** Athena IS structurally long-vega (+20.3 pts/vol-pt at entry, 100% of
bars). Corrects Branch 1 finding #4 — the negative vega *contribution* (−14.4 pts/trade) means
IV fell during trades, not that the position was short-vega. Net delta = +0.064 (stable).
Theta and vega both grow as dte_sell → 0 (calendar becomes more long-vega and faster-decaying
near sell expiry). Win/loss entry profiles identical — outcomes determined by market moves.

**Findings (Artemis):** Structurally short-vega (−6.9 Nifty, −28.2 Sensex pts/vol-pt). Short-vega
collapses near expiry (3–5d: −6.7, 1–2d: −3.1). Net delta ≈ 0 (market-neutral confirmed).
Gamma ~constant across DTE range (buy legs offset). Win/loss profiles identical at entry.

Cross-check vs Branch 1: WARNs on delta/gamma/theta are expected — due to wing-leg activation
bar boundary (Branch 2 includes wing at first active bar; Branch 1 skips bars where IV failed
at either endpoint). Scale and sign confirmed correct.

Output: `greek_profile/data/greek_profiles_athena.parquet`, `greek_profiles_artemis.parquet`

Entry points: `research/greek_analysis/greek_profile/run.py` (Athena),
`research/greek_analysis/greek_profile/run_artemis.py` (Artemis)

---

### Branch 3: IV Term Structure (`iv_term_structure/`)

**Type:** Diagnostic first, then optionally predictive. **COMPLETE (2026-06-23).**

Athena only (single-expiry Artemis has no term structure to measure). Uses traded-strike IVs —
both CE/PE sell/buy legs share the same strike across expiries, so slope = far_IV/near_IV is
a clean single-strike term-structure ratio (no skew contamination).

**Findings (n=124):**
- slope IC vs P&L = −0.327 (p=0.0002 ***). spread IC = +0.321 (p=0.0003 ***).
- Tercile P&L: low-spread (contango) +1.5 pts, mid +31.3, high-spread (backwardation) +33.8 pts.
- NOT a VIX proxy: VIX IC = −0.087 (p=0.34); VIX-controlled slope IC unchanged at −0.331.
- Period split: 2020–22 (n=102) IC = −0.347 ***; 2023+ (n=22) IC = −0.185 n.s. (CI ±0.43).
  Period stability is inconclusive — n=22 too small to distinguish IC=0 from IC=−0.33.

**Signal verdict:** IC ≈ 0.33 is material. Warrants barrier/quintile analysis and out-of-sample
test before live use. More 2023+ data needed to establish period-stability.

Output: `iv_term_structure/data/iv_term_structure.csv` (124 rows, one per trade).

Entry point: `research/greek_analysis/iv_term_structure/run.py`

---

### Branch 4: Realized vs Implied Vol (`realized_vs_implied/`)

**Type:** Diagnostic. **COMPLETE (2026-06-23).**

QV estimator: `rv_ann = sqrt(Σ rᵢ² / T_years) × 100`. Overnight gaps included. `rv_iv_ratio = rv_ann / near_iv`.

**Findings (Athena, n=124):**
- Vol overpriced at entry across all trades: mean ratio = 0.82. Entry IV > realized vol.
- Winners ratio = 0.81, Losers ratio = 0.86 — tiny difference, not significant (IC = −0.10, p=0.26).
- rv_iv_ratio has NO predictive power for Athena. Entry IV being "right" or "wrong" is not the loss driver.
- Branch 1 cross-check confirmed: Athena loses because IV falls DURING the trade (long-vega
  calendar marks down with vol compression), not because entry IV was set too low.

**Findings (Artemis Nifty, n=146):**
- Confounded by exit timing: early SL exits produce inflated annualized RV (short window captures vol spike).
  Winners (ratio=1.87) > Losers (ratio=1.03) is an artifact of this confound, not a vol signal.
- ELM exits (all wins, n=26): ratio = 0.85 — regulatory exits before large moves, vol correctly priced.
- index_sl losses: ratio = 1.08 (spot moved just beyond entry IV) — consistent with directional loss.
- option_sl losses: ratio = 0.98 — price-based exit at moderate vol, not large spot move.
- IC = +0.07 (p=0.40, not significant).

Outputs: `realized_vs_implied/data/rv_iv_athena.csv`, `rv_iv_artemis.csv`

Entry point: `research/greek_analysis/realized_vs_implied/run.py`

---

### Branch 5: IV Skew (`iv_skew/`)

**Type:** Predictive. **CLOSED (2026-06-23).** Both close conditions triggered.

Skew metric: `(pe_near_iv - ce_near_iv) / near_iv`. Pre-registered hypothesis: negative IC.

Findings: IC_raw = +0.022 (< 0.10, wrong sign). Sign-unstable: 2020–22 IC = +0.15, 2023+ IC = −0.19.
Tercile P&L is non-monotonic. No signal survives VIX or slope partial correlation. Structural note:
94.4% of trades enter with positive skew — equity market puts are structurally more expensive than
calls, so there is little variation in skew *direction*, only magnitude.

Output: `iv_skew/data/iv_skew_signal.csv`

Entry point: `research/greek_analysis/iv_skew/run.py`

---

### Branch 6: Greek Exit Triggers (`greek_exit_triggers/`)

**Type:** Predictive. **CLOSED for both Athena (2026-06-23) and Artemis (2026-06-24).**

Hypothesis: replace fixed offset trigger with sell-leg delta threshold for vol-aware activation.

**Athena findings (run.py):**
- Offset trigger fires at CE sell delta = 0.77 median (DTE 1-3 days, deep ITM). Vol-aware thesis
  DOES NOT HOLD — trigger delta constant across VIX (p=0.67, n.s.).
- Backtest (backtest_greek_exit.py): full-sample Δ=−0.45 pts, recent Δ=−0.69 pts. Neither
  improves. Close condition triggered.
- Structural conclusion: Athena's hedge is a late-stage parachute firing at delta=0.77. Earlier
  activation at delta=0.45 imposes unrecoverable cost on near-miss winners.

**Artemis findings (run_artemis.py — 80 index_sl events, Nifty + Sensex, 2020–2026):**
- Offset trigger fires at sell delta = 0.38 median (near-ATM, 50 pts OTM for Nifty). DTE 0–3d.
  Vol-aware thesis DOES NOT HOLD — VIX effect p=0.47 (n.s.). Std=0.082 is DTE-noise, not vol.
- A delta threshold of 0.38 would be EQUIVALENT to the current 50-pt offset — same exposure by
  construction. No backtest run (would be redundant). Close condition triggered at diagnostic.
- Contrast with Athena: Athena fired deep ITM (delta=0.77) so threshold tested different behaviour;
  Artemis already calibrated at near-ATM (delta=0.38) — no improvement axis exists.

Entry points: `research/greek_analysis/greek_exit_triggers/run.py` (Athena analysis);
`research/greek_analysis/greek_exit_triggers/run_artemis.py` (Artemis analysis);
backtest variant at `athena_backtest/backtest_greek_exit.py` (Athena only — Artemis not needed).

---

### Branch 7: Gamma/Theta Exit Timing (`exit_timing/`)

**Type:** Diagnostic. **CLOSED for Artemis (2026-06-24).** Athena pending.

**Artemis question:** At what DTE does rising gamma overwhelm theta for the full iron condor?
Is there an optimal exit before the position becomes gamma-dominated?

**Method:** Vectorized BS greeks (scipy/numpy) for all 4 legs at each 1-min bar, 173 trades.
Breakeven spot move = √(2·θ_net·Δt/|γ_net|). Compare realized 1-min move distribution by DTE.
Surviving-leg analysis: split by pre/post-adjustment for index_sl trades.

**Artemis findings:**
- No crossover at any DTE (0–4d). Position theta-positive throughout.
- Breakeven INCREASES at expiry: 5.6 pts (3–4d) → 11.2 pts (0–0.5d). Theta accelerates
  faster than gamma near expiry (θ ∝ 1/√T).
- %realized_gt_breakeven peaks at 37.9% (2.0–2.5d) — never reaches 50%.
- Surviving-leg: post-CE-roll closest at 42%, still theta-positive.
- Close condition: no crossover. Losses are directional (delta from Branch 1), not DTE-timing.

**Athena question (pending):** At what DTE does falling Vega overtake Theta gains?
Breakeven IV expansion = Θ_net/|V_net| — vol pts of IV rise that neutralize one unit of theta.
Compare against realized ΔIV distribution by DTE on losing vs winning trades.

---

## 5. Execution Sequence

1. **Build `greek_engine.py`** — shared IV/Greek computation + trade log loader. Validate on
   a single trade before full-sample run.
2. **Branch 1 (pnl_attribution)** — most diagnostic value, establishes compute feasibility.
3. **Branch 2 (greek_profile)** — reuses same per-bar Greek data; run immediately after.
4. **Branch 3 (iv_term_structure)** — entry-time only, cheaper compute; diagnostic first.
5. **Branch 4 (realized_vs_implied)** — validates / cross-checks branch 1 findings.
6. **Branch 5 (iv_skew)** — only if 3 and 4 suggest vol structure is meaningfully predictive.
7. **Branch 6 (greek_exit_triggers)** — only if branch 1 confirms delta/gamma is the dominant
   loss driver on emergency hedge trades; confirms the mechanism before changing behavior.

---

## 6. Success Criteria

| Branch | Success | Close condition |
|---|---|---|
| pnl_attribution | Attribution explains ≥ 80% of P&L; theta + vega confirmed as dominant | Always completes (diagnostic) |
| greek_profile | Net vega sign confirmed; gamma trajectory documented | Always completes (diagnostic) |
| iv_term_structure | IC computed; period split assessed | IC unstable → no entry filter; diagnostic finding retained |
| realized_vs_implied | rv_iv_ratio distribution by exit type documented | Always completes (diagnostic) |
| iv_skew | IC > 0.15, period-stable | IC < 0.10 or sign-unstable → close immediately |
| greek_exit_triggers | Full-sample + recent-period improvement vs fixed offset | Both periods must improve; else close — Athena CLOSED (2026-06-23): Δ=−0.45 full, −0.69 recent. Artemis CLOSED (2026-06-24): trigger delta=0.38 ≡ offset, VIX p=0.47 n.s. |
| exit_timing (Branch 7) | Find crossover DTE where gamma overwhelms theta | No crossover → close. Artemis CLOSED (2026-06-24): no crossover at any DTE (max 37.9% pct_gt, breakeven highest at expiry). Athena pending. |

---

## 7. Files

| Path | Purpose |
|---|---|
| `research/greek_analysis/greek_engine.py` | Shared IV/Greek computation |
| `research/greek_analysis/pnl_attribution/run.py` | Branch 1 |
| `research/greek_analysis/greek_profile/run.py` | Branch 2 |
| `research/greek_analysis/iv_term_structure/run.py` | Branch 3 |
| `research/greek_analysis/realized_vs_implied/run.py` | Branch 4 |
| `research/greek_analysis/iv_skew/run.py` | Branch 5 |
| `research/greek_analysis/greek_exit_triggers/run.py` | Branch 6 analysis |
| `athena_backtest/backtest_greek_exit.py` | Branch 6 backtest variant |
| `research/greek_analysis/README.md` | Running findings log |
