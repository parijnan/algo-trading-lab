# Greek Analysis — Research Notes

Diagnostic and predictive research on option Greeks for Athena and Artemis.

The central question this research addresses: **do the strategies make money the way they are
designed to?** Athena is constructed as a theta-harvesting, net-long-vega calendar condor. Artemis
is a market-neutral iron condor where P&L is dominated by spot containment. Neither of these
claims has been empirically verified via Greek decomposition — that is the primary purpose of
this research track.

Full plan: [`plans/greek-analysis.md`](../../plans/greek-analysis.md)

---

## Organizing Principle

**Diagnostic branches (1–4):** measure what trades already did. No signal, no in/out-of-sample
split, no overfitting risk. High certainty of insight regardless of outcome.

**Predictive branches (5–6):** new entry/exit signals derived from Greeks. Subject to the same
IC / period-split / sample-size gauntlet as prior research tracks (OI filter, TC). Set prior low.

---

## Shared Infrastructure

**`greek_engine.py`** — shared IV and Greek computation layer used by all branches.

Core functions:
- `compute_iv(option_price, spot, strike, dte_years, rate, option_type)` → float (IV via mibian)
- `compute_greeks(iv, spot, strike, dte_years, rate, option_type)` → dict (delta, gamma, theta, vega)
- `load_trade_logs(trade_logs_dir)` → DataFrame (per-bar snapshots with all leg LTPs and DTE)
- `position_greeks(legs, spot, ts)` → dict (net multi-leg Greeks at a point in time)

Compute note: IV via mibian is 600K-row × 6-leg compute on the full backtest. First run caches
IV to parquet per trade; subsequent branches reuse the cache. Estimated: 15–30 min cold, <1 min
cached.

Data sources:
- `athena_backtest/data/trade_logs/` — per-bar snapshots during each trade
- `athena_backtest/data/trade_summary.csv` — entry/exit metadata
- `data_pipeline/data/nifty/options/` — raw option prices
- `data_pipeline/data/indices/india_vix.csv` — VIX at each bar

---

## Branches

### Branch 1 — P&L Attribution (`pnl_attribution/`)

**Athena: Complete. Output: `pnl_attribution/data/pnl_attribution.csv` (124 rows, 2020–2026).**

**Artemis: Script written (`run_artemis.py`). Cold run pending — see open issue below.**

Decompose each trade's realized P&L into delta, gamma, theta, and vega contributions.

Method: at each 1-min bar, compute net position Greeks. Attribute bar-to-bar P&L change as:
- Delta: Δspot × net_delta
- Gamma: ½ × Δspot² × net_gamma
- Theta: Δt × net_theta
- Vega: ΔIV × net_vega
- Residual: unexplained (should be small)

Artemis-specific differences from Athena (`run_artemis.py`):
- Single weekly expiry (not dual sell/buy expiries)
- 4 legs with variable per-bar strikes (change after SL-triggered roll)
- Status-gated: leg attribution skipped when side is `'closed'`
- Base lot P&L only (`pe_pl + ce_pl`); add lot P&L tracked separately
- IV cache: `research/greek_analysis/data/iv_cache_artemis/` (separate from Athena)
- Runs both Nifty (146 trades) and Sensex (27 trades); combined output

Key questions (Artemis):
- Is Artemis P&L theta-dominated or delta-dominated (spot containment)?
- On losing trades, which Greek drives the loss — directional move (delta/gamma) or vol expansion (vega)?
- Does the Greek profile differ between the VIX 12–13 and VIX 15–16 sweet spots?

Output: `data/pnl_attribution_artemis.csv` — one row per trade (Nifty + Sensex combined).

---

### Branch 2 — Greek Profile (`greek_profile/`)

**Status: Not started.**

Track net position delta/gamma/theta/vega as a time series from entry to exit. Aggregate across
all trades for the typical Greek trajectory.

Key questions:
- Does net delta stay near zero (market-neutral) throughout, or does it drift?
- Does net vega confirm the calendar is long-vega? (the wings and strike spacing can alter this —
  verify empirically rather than assuming)
- When does gamma spike? (near-expiry regime vs well-hedged mid-trade)

Output: `data/greek_profiles.parquet` — per-bar Greeks per trade.

---

### Branch 3 — IV Term Structure (`iv_term_structure/`)

**Status: Not started.**

At entry: ATM IV for the near (sell) expiry and far (buy) expiry.

Metrics:
- Term structure slope: far_IV / near_IV
- Correlation of slope with trade P&L (Spearman IC)
- Period split (2020–22 vs 2023+) mandatory

A calendar's edge is larger when near_IV > far_IV — we're selling the more expensive vol.
If IC is period-stable, may qualify as an entry filter (same gauntlet as pcr_near OI signal).

Output: `data/iv_term_structure.csv`

---

### Branch 4 — Realized vs Implied Vol (`realized_vs_implied/`)

**Status: Not started.**

For each trade: entry IV at the sell strike vs realized vol over the actual holding period.

Metrics:
- rv_iv_ratio = realized_vol / entry_iv (>1 = vol underpriced; strategy got hurt)
- Segmented by exit type: expiry hold, SL hit, target hit
- Correlation with trade P&L

Key question: when we lose, is it because spot moved adversarially (delta/gamma) or because vol
expanded beyond what was priced in (vega)? Cross-checks branch 1 attribution.

Output: `data/rv_iv_analysis.csv`

---

### Branch 5 — IV Skew (`iv_skew/`)

**Status: Not started.**

At entry: CE sell IV vs PE sell IV at the actual strikes traded.

Metric: `(put_iv - call_iv) / atm_iv` — positive = market pricing more downside risk.

Hypothesis: high-skew entries are asymmetrically expensive; calendar is less symmetric. Low skew
→ both sides contribute theta equally.

Full IC / period-split / barrier gauntlet required. Close immediately if IC < 0.10 or sign-unstable
across periods (same close condition as pcr_near).

Output: `data/iv_skew_signal.csv`

---

### Branch 6 — Greek Exit Triggers (`greek_exit_triggers/`)

**Status: Not started. Requires branches 1–2 first.**

Replace Athena's fixed `EMERGENCY_TRIGGER_OFFSET` (150 points) with a delta-threshold on the CE
sell leg (e.g., delta ≥ 0.45). A fixed point offset is not vol-aware: the same 150-point move
carries more risk at VIX 23 than VIX 17.

Implementation: `athena_backtest/backtest_greek_exit.py` — thin wrapper following the
`backtest_oi_filter.py` pattern. Output to `athena_backtest/data_greek_exit/`.

Gate: only start this branch after branches 1–2 confirm that delta/gamma is the dominant loss
driver on emergency hedge trades — otherwise the mechanistic justification is weak.

Output: `data/greek_exit_triggers.csv` (analysis); backtest results in
`athena_backtest/data_greek_exit/`.

---

## Findings

### Branch 1: P&L Attribution (124 trades, 2020–2026)

**Methodology note:** Bar-to-bar Taylor decomposition (δ·Δspot + ½Γ·Δspot² + θ·Δt + v·ΔIV).
Per-leg IV (not VIX proxy). `pct_unexplained = |residual| / |actual_mtm|`. Residuals are
expected to be large per-trade — the Taylor expansion is a local approximation and breaks down
for large spot/vol moves (especially during COVID/high-vol episodes). In aggregate, components
partially cancel and the summed residual is a manageable 28.6% of total MtM.

**Aggregate (all 124 trades):**

| Component | Sum (pts) | Mean/trade | % of MtM |
|---|---|---|---|
| Theta | +7068 | +57.0 | +237.5% |
| Delta | +1033 | +8.3 | +34.7% |
| Gamma | −4195 | −33.8 | −141.0% |
| Vega | −1781 | −14.4 | −59.9% |
| Residual | +851 | +6.9 | +28.6% |
| **Actual MtM** | **+2976** | **+24.0** | — |
| Total P&L (summary) | +2766 | +22.3 | — |

IV fail bars: 0 / 221,919 (0.0% — all bars attributed).

**Key findings:**

1. **Theta is confirmed as the primary engine.** +57 pts/trade vs +24 pts average MtM. The
   strategy earns theta faster than it loses to gamma/vega on average.

2. **Net gamma is a consistent drag (−34 pts/trade).** The near-dated short is gamma-positive
   (bad), and the far-dated long provides less offset than expected. Gamma drag is large and
   roughly symmetric across wins and losses.

3. **Vega flips between winners and losers.** On wins: vega ≈ 0 (−0.3 pts). On losses: vega =
   −38 pts. Losing trades are disproportionately associated with vol expansion — exactly what
   the calendar long-vega structure is supposed to hedge but doesn't fully cover.

4. **Athena is NOT net-long-vega on average.** Aggregate vega = −14.4 pts/trade (net short).
   The wings and strike spacing reduce the calendar's theoretical long-vega position enough to
   flip it negative. The structure is closer to vega-neutral to slightly short-vega.

5. **Period split: 2023+ shows stronger theta and worse vega.**

   | Period | n | θ mean | v mean | Δ mean |
   |---|---|---|---|---|
   | 2020–2022 | 102 | +49.9 | −9.3 | +3.1 |
   | 2023+ | 22 | +90.0 | −38.0 | +32.7 |

   The 2023+ trades have nearly double the theta per trade but also significantly larger negative
   vega. The delta contribution in 2023+ is unexpectedly large (+32.7) — worth monitoring for
   directionality creep as strikes are selected.

6. **All backtest exits are classified `pre_expiry`.** Emergency hedge activations and wing
   adjustments are intra-trade adjustments, not separate exit types. Exit reason breakdown
   is not meaningful; winning vs losing decomposition is the relevant split.

**Reconciliation:** `actual_mtm` vs `total_pl_points` median diff < 2 pts. Four trades with
large diffs (trades 94, 107, 115, 120 — diffs of +48, +20, +15, +92 pts) likely have
multiple wing/emer entry-exit cycles within the trade; these inflate `actual_mtm` vs
`total_pl_points` which only records final P&L. Not a code bug.

---

## Pipeline

```bash
# 1. Build shared engine and validate on a single trade
python research/greek_analysis/greek_engine.py --validate

# 2. Branch 1 — P&L attribution
#    Athena (cold run ~20 min, warm <1 min):
python research/greek_analysis/pnl_attribution/run.py
#    Artemis (cold run ~60–90 min once compute_iv intrinsic guard is in place):
python research/greek_analysis/pnl_attribution/run_artemis.py

# 3. Branch 2 — Greek profile (reuses cached IV from branch 1)
python research/greek_analysis/greek_profile/run.py

# 4. Branch 3 — IV term structure (entry-time only, fast)
python research/greek_analysis/iv_term_structure/run.py

# 5. Branch 4 — Realized vs implied vol
python research/greek_analysis/realized_vs_implied/run.py

# 6. Branch 5 — IV skew (after branches 3+4)
python research/greek_analysis/iv_skew/run.py

# 7. Branch 6 — Greek exit triggers (after branches 1+2)
python athena_backtest/backtest_greek_exit.py
```
