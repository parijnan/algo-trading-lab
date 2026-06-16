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

**Status: Not started.**

Decompose each trade's realized P&L into delta, gamma, theta, and vega contributions.

Method: at each 1-min bar, compute net position Greeks. Attribute bar-to-bar P&L change as:
- Delta: Δspot × net_delta × lot_size
- Gamma: ½ × Δspot² × net_gamma × lot_size
- Theta: Δt × net_theta
- Vega: ΔIV × net_vega × lot_size
- Residual: unexplained (should be small)

Key questions:
- What fraction of Athena P&L comes from theta vs vega?
- On losing trades, which Greek drives the loss — directional (delta/gamma) or vol (vega)?
- Does the attribution pattern differ early (2020–22) vs recent (2023+)?

Output: `data/pnl_attribution.csv` — one row per trade.

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

*No results yet — research not started.*

---

## Pipeline

```bash
# 1. Build shared engine and validate on a single trade
python research/greek_analysis/greek_engine.py --validate

# 2. Branch 1 — P&L attribution (full backtest, cold run ~20 min)
python research/greek_analysis/pnl_attribution/run.py

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
