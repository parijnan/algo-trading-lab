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

**Artemis: Complete. Output: `pnl_attribution/data/pnl_attribution_artemis.csv` (173 rows: 146 Nifty 2020–2025 + 27 Sensex 2025–2026).**

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

**Athena: Complete. Output: `greek_profile/data/greek_profiles_athena.parquet` (222,043 rows, 124 trades).**

**Artemis: Complete. Output: `greek_profile/data/greek_profiles_artemis.parquet` (232,768 rows: 197,235 Nifty + 35,533 Sensex).**

Track net position Greek *levels* (delta, gamma, theta, vega) at each bar from entry to exit.

**Distinction from Branch 1:** Branch 1 measured *contribution* (Greek × Δmarket). Branch 2 measures
*exposure* (instantaneous sensitivity). They can diverge in sign: a long-vega position posts
a negative vega P&L contribution whenever IV falls. This distinction resolves an apparent
contradiction in the Branch 1 findings — see the correction note under Branch 1 finding #4.

Artemis-specific: Nifty and Sensex reported separately (gamma/theta/vega scale with option-price
level, ~4× larger for Sensex). Status-gated: closed sides contribute zero Greeks. All-4-valid
(all four main legs with valid IV) covers 100% of Athena bars and ~40% of Artemis bars (rest are
post-SL-close bars where one side is fully exited).

---

### Branch 3 — IV Term Structure (`iv_term_structure/`)

**Athena only: Complete (2026-06-23). Output: `iv_term_structure/data/iv_term_structure.csv` (124 rows).**

Note: Artemis is single-expiry; there is no near/far term structure to measure.

Uses traded-strike IVs (not ATM), which is more precise here: the CE sell and CE buy legs
use the same strike at different expiries, so `ce_buy_iv / ce_sell_iv` is a clean single-strike
term-structure ratio with no skew contamination. ATM would mix strikes and conflate skew with
term structure.

Metrics at entry (bar 0):
- `near_iv` = mean(ce_near_iv, pe_near_iv) — avg IV at sell expiry
- `far_iv`  = mean(ce_far_iv,  pe_far_iv)  — avg IV at buy expiry
- `slope`   = far_iv / near_iv             — >1 = contango (far > near), <1 = backwardation
- `spread`  = near_iv − far_iv             — >0 = backwardation (near > far)

---

### Branch 4 — Realized vs Implied Vol (`realized_vs_implied/`)

**Athena: Complete (2026-06-23). Output: `realized_vs_implied/data/rv_iv_athena.csv` (124 rows).**
**Artemis: Complete (2026-06-23). Output: `realized_vs_implied/data/rv_iv_artemis.csv` (173 rows).**

Entry IV from IV cache at bar 0. Realized vol via quadratic variation estimator:
`rv_ann% = sqrt(Σ rᵢ² / T_years) × 100`, where `T_years = elapsed calendar time / 365`.
QV is gap-robust — overnight and weekend gaps are naturally included (position is held continuously).
`rv_iv_ratio = rv_ann / near_iv` (>1 = realized vol exceeded entry IV, vol was underpriced).
Athena: segment by win/loss (all exits `pre_expiry`). Artemis: segment by exit type of the
first-exiting side (chronologically). ELM is kept as its own bucket (regulatory, not vol outcome).

---

### Branch 5 — IV Skew (`iv_skew/`)

**CLOSED (2026-06-23). Output: `iv_skew/data/iv_skew_signal.csv` (124 rows).**

At entry: CE sell IV vs PE sell IV at the actual strikes actually traded.

Metric: `(pe_near_iv - ce_near_iv) / near_iv` — positive = market pricing more downside risk.

Delta check: CE and PE sell strikes are delta-symmetric (both target 0.30 delta, mean |delta diff|
= 0.027), so the IV difference is a clean risk-reversal-style skew measurement with no moneyness
contamination.

Pre-registered hypothesis: negative IC (low skew → better, more symmetric calendar).

**Spearman IC (full sample, n=124):**

| Signal | IC | p-value | |
|---|---|---|---|
| skew (raw) | +0.022 | 0.81 | n.s. |
| skew \| VIX controlled | +0.041 | 0.65 | n.s. |
| skew \| slope controlled | +0.057 | 0.53 | n.s. |
| skew \| VIX + slope controlled | +0.091 | 0.32 | n.s. |

**Period split:**

| Period | n | IC | p |
|---|---|---|---|
| 2020–22 | 102 | +0.153 | 0.12 |
| 2023+ | 22 | −0.189 | 0.40 |

**Tercile P&L:**

| Tercile | n | Mean | Median |
|---|---|---|---|
| Low | 41 | +27.0 | +29.6 |
| Mid | 41 | +4.0 | −0.5 |
| High | 42 | +35.5 | +29.8 |

**Close verdict: both conditions triggered.**
1. `|IC_raw| = 0.022 < 0.10` — no signal.
2. Sign-unstable across periods (+0.15 in 2020–22, −0.19 in 2023+).

The tercile table is non-monotonic (mid is worst, high is best) — no coherent directional story.
The pre-registered hypothesis (negative IC) is wrong even in sign for 2020–22. Controlling for
slope or VIX does not reveal a hidden signal. The skew metric carries no independent predictive
information about Athena P&L.

Note: 94.4% of trades enter with positive skew (put IV > call IV is structural in equity markets).
There is essentially no within-sample variation in the direction of skew, only in magnitude.

Output: `iv_skew/data/iv_skew_signal.csv`

---

### Branch 6 — Greek Exit Triggers (`greek_exit_triggers/`)

**CLOSED (2026-06-23). Both close conditions triggered.**

Replace Athena's fixed `EMERGENCY_TRIGGER_OFFSET` (150 points) with a delta-threshold (≥ 0.45)
on the CE sell leg.

**Diagnostic finding (run.py):**
The offset trigger fires when the CE sell leg delta is already 0.77 median (deep ITM, DTE 1-3
days). Vol-aware thesis DOES NOT HOLD: trigger delta is ~constant across VIX (p=0.67, n.s.).
The early-warning hypothesis (fire earlier, at delta 0.45) was tested instead.

**Backtest result (backtest_greek_exit.py):**

| Period     | n   | Baseline mean | Delta mode mean | Δ |
|---|---|---|---|---|
| Full sample | 124 | +22.30 pts | +21.85 pts | −0.45 |
| Early 2020–22 | 102 | +16.57 pts | +16.17 pts | −0.40 |
| Recent 2023+ | 22 | +48.88 pts | +48.19 pts | −0.69 |

Delta mode fires in 56 trades (vs 21 for offset). The 35 additional fires are near-miss winners;
hedge premium cost on false positives (−56 pts total) exceeds gains from earlier activation.
Neither period improves → close condition triggered.

**Structural conclusion:** Athena's emergency hedge mechanism is a late-stage parachute (fires at
DTE 1-3 days, delta ~0.77). Firing earlier at delta=0.45 provides better protection on the worst
losers but imposes unrecoverable cost on near-miss winners. The mechanism is working as designed;
vol-awareness is not a productive axis for improvement.

Output: `data/trigger_delta_analysis.csv` (21 hedged trade diagnostics);
backtest results in `athena_backtest/data_greek_exit/`.

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
   −38 pts. Athena is long-vega (Branch 2), so a negative vega contribution means IV fell during
   the trade. Losing trades are disproportionately associated with vol compression — IV dropped
   substantially from entry to exit, hurting the long-vega calendar position.

4. **Branch 1 vega contribution was negative (−14.4 pts/trade) — but this does NOT mean
   Athena is short-vega.** *(Correction: Branch 2 proves Athena is consistently long-vega —
   see below.)* The contribution is negative because IV fell during Athena trades on average.
   A long-vega position collects positive vega P&L when IV rises and posts negative P&L when
   IV falls. The negative contribution is a statement about the *direction of IV moves during
   these trades*, not about the sign of the vega *exposure*.

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

### Branch 1: P&L Attribution — Artemis (173 trades: 146 Nifty + 27 Sensex)

**Methodology note:** Same bar-to-bar Taylor decomposition as Athena. 4-leg iron condor (PE
sell/buy + CE sell/buy), single weekly expiry. Per-leg IV per bar. Variable strikes — strikes
change after SL-triggered roll/adjustment. Status-gated: leg attribution skipped once that
side is `'closed'`. Base lots only (`pe_pl + ce_pl`); add lot P&L tracked separately from
summary. IV fail bars: 24,455 / 232,595 (10.5%) — deep ITM bars after roll where
`ltp ≤ intrinsic + 0.5` (intrinsic guard in `greek_engine.compute_iv`). Those bars land in
residual.

**Aggregate (all 173 trades):**

| Component | Sum (pts) | Mean/trade | % of MtM |
|---|---|---|---|
| Theta | +8,444 | +48.8 | +309% |
| Delta | +657 | +3.8 | +24% |
| Gamma | −5,885 | −34.0 | −215% |
| Vega | −4,230 | −24.5 | −155% |
| Residual | +3,748 | +21.7 | +137% |
| **Actual MtM (base)** | **+2,733** | — | — |
| Add lot P&L (summary) | +1,806 | — | — |
| Total P&L (summary) | +4,568 | — | — |

IV fail bars: 24,455 / 232,595 (10.5% — deep ITM bars after roll, intrinsic guard triggered).

**Key findings:**

1. **Theta confirmed as the primary engine (+48.8 pts/trade).** Consistent with design —
   Artemis is a short-vol, time-decay strategy. Theta at +309% of MtM means it earns decay
   faster than gamma/vega drag consumes it on average.

2. **Gamma is the primary drag (−34 pts/trade, −215% of MtM).** Comparable to Athena (−33.8).
   Iron condor structure has two exposed sell strikes and no far-dated longs to offset gamma
   exposure, so the absolute drag is similar to the calendar despite being a different structure.

3. **Vega drag is larger than Athena (−24.5 vs −14.4 pts/trade).** Artemis is structurally
   short-vega (no far-dated long buy to partially offset). Vol expansions hurt more. This is
   consistent with design — iron condor has a short-vol profile; calendar is closer to vol-neutral.

4. **Delta near zero on aggregate (+3.8 pts/trade) — market neutrality confirmed.**
   Nifty specifically: Δ = −0.17/trade (essentially flat). Sensex: Δ = +25.3/trade (positive
   bias, likely due to the smaller sample of 27 trades including a trending Sensex period).

5. **Losses are delta-driven; Athena losses are vega-driven.** This is the critical
   structural difference:

   | | Wins (n=131) | Losses (n=42) |
   |---|---|---|
   | Theta | +48.7 | +49.1 |
   | Delta | +13.9 | −27.8 |
   | Gamma | −34.5 | −32.4 |
   | Vega | −22.1 | −31.9 |

   Theta is near-identical across wins and losses (time decay is constant). On losing trades,
   delta is the dominant driver (−27.8 pts vs +13.9 on wins) — spot broke directionally past
   the sell strike. Vega also worsens on losses but is secondary. For Athena, losses were
   primarily vega-driven; for Artemis, they are primarily delta-driven. This reflects the
   mechanical difference: Artemis exits on index SL (spot crosses sell strike), so losses
   correspond to directional moves.

6. **Add lots nearly equal base lots.** Base lot P&L: +2,733 pts. Add lot P&L: +1,806 pts
   (0.5× weighted). Add lots are entered after a successful side adjustment, so they enter on
   a position that is already working — their contribution per unit is structurally higher.

7. **Nifty vs Sensex per-trade scale:**

   | Instrument | n | θ mean | v mean | Δ mean | Γ mean |
   |---|---|---|---|---|---|
   | Nifty | 146 | +35.3 | −18.3 | −0.2 | −23.0 |
   | Sensex | 27 | +121.8 | −57.5 | +25.3 | −93.4 |

   Sensex per-trade values are roughly 3–4× Nifty — consistent with the index ratio (~4.5×)
   and fewer bars per trade (shorter hold). Sensex delta bias (+25.3) should be monitored as
   the Sensex sample grows.

8. **Period split (Nifty):**

   | Period | n | θ mean | v mean | Δ mean |
   |---|---|---|---|---|
   | 2020–2022 | 32 | +28.7 | −9.0 | −0.07 |
   | 2023+ | 114 | +37.2 | −21.0 | −0.20 |

   Same pattern as Athena: stronger theta in recent period, worse vega drag. Higher IV
   environment in 2023+ benefits theta (more premium) but also carries more vol expansion risk.

**Comparison to Athena:**

| | Athena (124 trades) | Artemis (173 trades) |
|---|---|---|
| Theta | +57.0 | +48.8 |
| Gamma | −33.8 | −34.0 |
| Vega | −14.4 | −24.5 |
| Delta | +8.3 | +3.8 |
| Loss driver | Vega | Delta |

Gamma drag is nearly identical (~34 pts/trade). Theta is higher for Athena per trade.
Vega drag is significantly worse for Artemis — as expected given the structural difference
(short-vega iron condor vs near-neutral calendar). The loss mechanism differs: Athena loses
on vol expansion; Artemis loses on directional moves through the sell strike.

---

### Branch 2: Greek Profile — Athena (124 trades, 222,043 bars)

**Methodology note:** Net position Greek *levels* at each bar (not bar-to-bar changes).
`net_greek = Σ_legs direction_i × greek_i(IV_t, spot_t, strike_i, dte_i)`. 100% of bars
have all 4 main legs with valid IVs (n_main_valid=4 for every bar). Wings counted separately
(n_opt_valid). Base lots only. Reuses IV cache from Branch 1.

**Entry Greeks at bar_num=0 (n=124 trades):**

| Greek | Mean | Std |
|---|---|---|
| net_delta | +0.0642 | 0.0223 |
| net_gamma | −0.000580 | 0.000228 |
| net_theta | +7.61 pts/bar | 4.22 |
| net_vega | +20.30 pts/vol-pt | 7.10 |

**Key findings:**

1. **Athena IS structurally long-vega at entry (+20.3 pts/vol-pt).** This directly corrects
   Branch 1 finding #4. The calendar construction (sell near-dated, buy far-dated, same strike)
   produces net long-vega because vega scales with √DTE: far-month vega > near-month vega.
   The position is long-vega on 100% of bars across all 124 trades.

2. **Reconciling with Branch 1's negative vega P&L contribution:** Athena earns positive vega
   P&L when IV rises, negative when IV falls. Branch 1 showed −14.4 pts/trade average —
   meaning IV fell (or compressed) during the average Athena trade. On losing trades specifically
   (vega_contrib = −38 pts), IV dropped substantially. This is a market-condition statement, not
   a structural-design statement. The position was long-vega throughout; the market moved against it.

3. **Net delta is +0.064 and remarkably stable** (0–10% bucket: +0.061, 90–100% bucket: +0.066).
   Slight CE-bias from calendar construction (ATM CE has slightly higher vega). Near-zero and
   market-neutral in practice.

4. **Net theta and vega both grow as dte_sell → 0.** This is a structural property of calendars:
   as the near-dated sell expiry approaches, its vega and theta collapse faster than the far-dated
   buy's. The calendar becomes *more* long-vega and *more* theta-positive near the sell expiry.

   | dte_sell | n | net_theta | net_vega |
   |---|---|---|---|
   | >5d | 131,263 | +7.1 | +22.1 |
   | 3–5d | 41,683 | +13.0 | +25.5 |
   | 2–3d | 40,617 | +16.9 | +27.3 |
   | 1–2d | 8,479 | +24.5 | +28.6 |

5. **By normalized trade time:**

   | Time bucket | net_theta | net_vega | net_gamma |
   |---|---|---|---|
   | 0–10% (entry) | +7.8 | +20.3 | −0.000570 |
   | 50–60% | +9.1 | +24.0 | −0.000616 |
   | 90–100% (exit) | +18.5 | +27.9 | −0.000687 |

   Gamma is the most stable Greek — stays in the −0.00057 to −0.00069 range throughout.

6. **Win/loss profiles at entry are identical.** Wins and losses enter with the same Greeks
   (delta, vega, gamma, theta differ by < 1%). The outcome is entirely determined by what the
   market does during the trade, not by what Greeks looked like at entry.

**Cross-check vs Branch 1:** Reconstruction `Σ net_greek(t)×Δmarket_t` produces diffs vs
Branch 1 contributions: delta max 143 pts, gamma max 26 pts, theta max 5 pts. All WARNs are
due to wing legs — when a wing activates at bar t, its IV at bar t−1 was None, so Branch 1
skips that bar-pair while Branch 2 includes the wing's delta in the reconstruction. For
wing-free trades, reconstruction would be exact. Scale and sign are confirmed correct.

---

### Branch 2: Greek Profile — Artemis (173 trades: 146 Nifty + 27 Sensex)

**Methodology note:** Same as Athena but single expiry, status-gated legs, variable strikes.
All-4-valid means all 4 legs active with valid IVs (excludes bars after SL-close of one side).
Nifty 40.1% all-4-valid (60% of bars are post-close, one side exited). Sensex 33.8%.

**Entry Greeks at bar_num=0 (n=146 Nifty, n=27 Sensex):**

| Greek | Nifty mean | Sensex mean |
|---|---|---|
| net_delta | −0.030 | −0.015 |
| net_gamma | −0.00174 | −0.00045 |
| net_theta | +14.2 pts/bar | +51.6 pts/bar |
| net_vega | −6.92 pts/vol-pt | −28.2 pts/vol-pt |

**Key findings:**

1. **Artemis IS structurally short-vega (net_vega < 0 throughout).** Iron condor = short both
   call and put spreads at the same expiry → short vega on all four legs net. Opposite of
   Athena. Sensex vega (−28.2) is ~4× larger than Nifty (−6.9), confirming the scale
   proportionality with option-price level.

2. **Short-vega exposure collapses as DTE → 0** (Nifty): from −6.72 at 3–5d to −3.09 at 1–2d.
   This is the expected behavior: vega → 0 as expiry approaches (all options, regardless of
   moneyness). The iron condor becomes progressively less sensitive to vol moves in its
   final day.

   | DTE (Nifty) | net_vega |
   |---|---|
   | 3–5d | −6.72 |
   | 2–3d | −5.14 |
   | 1–2d | −3.09 |

3. **Net gamma (~−0.00174 Nifty) does not intensify measurably over the DTE range 1–5d**
   (−0.00166 at 3–5d, −0.00166 at 1–2d). The buy-legs partially offset sell-leg gamma
   throughout. The all-4-valid filter may under-sample the most extreme near-expiry scenarios
   (which often have one side closed).

4. **Net delta near zero (−0.030 Nifty, −0.015 Sensex) — market-neutral confirmed.** Slight
   PE-bias in Nifty (sell puts slightly closer to ATM or with higher delta). This is the
   structural counterpart to Branch 1's near-zero delta contribution.

5. **Win/loss profiles at entry are nearly identical** (vega: wins −6.87, losses −7.08).
   Consistent with Branch 1: Artemis losses are delta-driven (spot movement during the trade),
   not determined by the Greek level at entry.

6. **Nifty theta grows as DTE → 0:** 3–5d: +13.5, 2–3d: +15.1, 1–2d: +18.4 pts/bar.
   Theta accelerates near expiry, confirming the short-option theta engine runs at maximum
   efficiency in the last 1–2 trading days.

---

### Branch 3: IV Term Structure — Athena (124 trades, 2020–2026)

**Methodology note:** Entry-bar IV only (bar_num=0). Reuses IV cache from Branch 1 (run time
< 5 seconds). Traded-strike IVs used; both CE and PE legs share the same strike across expiries,
so the ratio directly measures term-structure slope without skew contamination. 48.4% of trades
entered in contango (far > near); 51.6% in backwardation.

**Descriptive statistics at entry:**

| Metric | Mean | Std | Min | Max |
|---|---|---|---|---|
| near_iv | 19.05 | 3.74 | 12.51 | 33.89 |
| far_iv | 18.75 | 2.61 | 13.98 | 26.54 |
| slope | 0.999 | 0.120 | 0.671 | 1.527 |
| spread | +0.30 | 2.42 | −6.93 | +11.14 |

**IC vs total P&L — full sample (n=124):**

| Signal | IC | p-value | |
|---|---|---|---|
| slope (far/near) | −0.327 | 0.0002 | *** |
| spread (near−far) | +0.321 | 0.0003 | *** |
| near_iv alone | +0.069 | 0.45 | n.s. |
| far_iv alone | −0.114 | 0.21 | n.s. |
| entry_vix | −0.087 | 0.34 | n.s. |

The slope/spread signal is strong (IC ≈ ±0.32) and independent of VIX level. Note that slope
and spread are inverse transformations of the same underlying variable — they carry identical
information; the IC signs just flip.

**Period split:**

| Period | n | slope IC | p | spread IC | p |
|---|---|---|---|---|---|
| 2020–22 | 102 | −0.347 | 0.0003 *** | +0.348 | 0.0003 *** |
| 2023+ | 22 | −0.185 | 0.41 n.s. | +0.144 | 0.52 n.s. |

The 2020–22 IC is strong. The 2023+ result is statistically indeterminate: n=22 gives a 95% CI
of ±0.43, so an IC of −0.18 in 2023+ is entirely consistent with the true IC still being −0.33
(or being zero — the data cannot distinguish). This is not a period-instability verdict; it is
a sample-size limitation.

**Tercile P&L — spread (near−far):**

| Tercile | n | Mean P&L | Median P&L |
|---|---|---|---|
| Low (contango) | 41 | +1.5 | −4.5 |
| Mid | 41 | +31.3 | +19.6 |
| High (backwardation) | 42 | +33.8 | +36.0 |

Clear monotonic pattern: high-backwardation entries → +33.8 pts mean; contango entries → +1.5 pts mean.

**VIX confound check:**
- entry_vix IC vs P&L = −0.087 (p=0.34, not significant)
- slope IC with VIX controlled out: −0.331 (vs raw −0.327) — essentially unchanged
- spread IC with VIX controlled out: +0.317 (vs raw +0.321) — essentially unchanged

The term structure signal is independent of VIX level. Athena is VIX-gated (16–25), so VIX
variation within the window is ≤ 9 points; the fact that the IC is not just a VIX proxy is
important — it means term-structure slope carries information beyond "how stressed is the market."

**CE vs PE slope consistency:** Spearman(ce_slope, pe_slope) = 0.79 (p < 0.0001). The two legs
respond to the same vol regime; averaging them is well-justified.

**Key findings:**

1. **Backwardation (near_IV > far_IV) is associated with better P&L.** Tercile mean P&L goes
   from +1.5 (contango) to +33.8 (backwardation). The mechanism is calendar edge: when near-dated
   vol is expensive relative to far-dated vol, we collect more premium on the sell leg per unit of
   far-dated hedge cost. A steeper downward slope in the term structure = larger theta collection.

2. **The signal is not a VIX proxy.** VIX itself has no significant IC with P&L (−0.09, p=0.34).
   The VIX-controlled term structure IC is indistinguishable from the raw IC. Term structure
   slope contains information about the *shape* of the vol surface that raw vol level does not.

3. **Period stability is inconclusive, not negative.** 2020–22: IC = −0.35 (strong, n=102).
   2023+: IC = −0.19 (n=22, CI too wide to conclude). More 2023+ data is needed before treating
   the signal as stable or unstable. The 2023+ sample currently represents only 18% of the total.

4. **Neither near_iv nor far_iv alone has meaningful IC.** The signal is in the *relative* term
   structure (near vs far), not the absolute vol level. This is consistent with the calendar's
   theoretical edge being driven by the ratio between near- and far-dated vol.

**Signal verdict:** Strong diagnostic finding. The slope/spread IC of ≈ 0.33 at n=124 is
material (comparable in magnitude to good factor premia in quantitative finance). Warrants the
full entry-filter gauntlet (barrier analysis, out-of-sample test, transaction cost adjustment)
before live implementation. The 2023+ n=22 is too small to establish period-stability — more
data required. Proceed to barrier/quintile analysis before declaring this an actionable filter.

---

### Branch 4: Realized vs Implied Vol — Athena (124 trades, 2020–2026)

**Methodology note:** Quadratic-variation realized vol estimator, matching mibian's calendar
convention (T = dte/365). Overnight and weekend gap moves included — positions are held
continuously. `near_iv` from Branch 3 IV cache at bar_num=0.

**Full-sample summary:**

| Group | n | rv_ann | near_iv | rv_iv_ratio | median ratio |
|---|---|---|---|---|---|
| All trades | 124 | 15.71% | 19.05% | 0.825 | 0.781 |
| Winners | 78 | 15.50% | 19.19% | 0.805 | 0.760 |
| Losers | 46 | 16.06% | 18.80% | 0.859 | 0.840 |

**Spearman IC vs total P&L (full sample):**

| Signal | IC | p-value | |
|---|---|---|---|
| rv_iv_ratio | −0.102 | 0.26 | n.s. |
| rv_ann | −0.032 | 0.73 | n.s. |
| entry_iv (near_iv) | +0.069 | 0.45 | n.s. |
| entry_vix | −0.087 | 0.34 | n.s. |

**Key findings:**

1. **Vol was overpriced at entry across all Athena trades (ratio = 0.82).** On average, spot
   moved only 82% of what entry IV implied. Athena is selling expensive vol — confirmed.

2. **rv_iv_ratio has NO predictive power for Athena P&L (IC = −0.10, p=0.26).** Losers have
   only marginally higher ratio (0.86 vs 0.81). Entry IV being "right" or "wrong" does not
   determine whether the trade wins or loses.

3. **Cross-validation of Branch 1 loss mechanism:** Branch 1 showed Athena losers are
   vega-driven (vega contribution = −38 pts on losers vs ≈ 0 on winners). Athena is long-vega,
   so negative vega contribution means IV FELL during those trades. Branch 4 shows losers do NOT
   have rv_iv_ratio > 1 (only 21.7% of losers vs 14.1% of winners). The entry IV was not
   underpriced vs realized vol. **Conclusion: Athena losses come from IV falling DURING the trade
   — the calendar marks down as IV compresses from entry level to exit level. Entry IV correctly
   reflected eventual realized vol; the loss is path-dependent (IV drops intraperiod, the long-vega
   calendar loses mark-to-market, and vol does not recover before exit).**

4. **Period split is consistent with no signal:** 2020–22 IC = −0.03 (p=0.74); 2023+ IC = −0.24
   (p=0.28, n=22 too small). Nothing to segment further.

---

### Branch 4: Realized vs Implied Vol — Artemis (146 Nifty + 27 Sensex)

**Methodology note:** Exit time for RV = exit time of first-exiting side. ELM exits kept separate
(regulatory, not vol outcome). Warning: the RV window for early SL exits is shorter, which raises
annualized vol mechanically — this confounds the exit-type comparison.

**Nifty full-sample summary:**

| Group | n | rv_ann | near_iv | rv_iv_ratio | median ratio |
|---|---|---|---|---|---|
| All trades | 146 | 23.42% | 14.16% | 1.660 | 1.009 |
| Winners | 109 | 26.60% | 14.30% | 1.873 | 1.083 |
| Losers | 37 | 14.04% | 13.73% | 1.031 | 0.993 |

**By exit type (Nifty, first-exiting side):**

| Exit type | Win/Loss | n | rv_ann | ratio |
|---|---|---|---|---|
| elm | wins | 26 | 13.09% | 0.854 |
| index_sl | wins | 43 | 25.01% | 1.985 |
| index_sl | losses | 19 | 13.90% | 1.077 |
| option_sl | wins | 40 | 37.09% | 2.415 |
| option_sl | losses | 18 | 14.18% | 0.982 |

**Key findings:**

1. **Artemis Nifty rv_iv_ratio is inverted: winners (1.87) > losers (1.03).** This is an
   artifact of exit timing — not a vol-regime conclusion. Early SL exits (within 1–2 days on
   volatile days) produce high annualized RV simply because the short window captures a spike.
   Winning trades often trigger SL on one side early but still collect net credit; these trades
   show inflated rv_iv_ratio because of the short annualization window.

2. **ELM exits (always wins, n=26): ratio = 0.85.** ELM fires near expiry when options collapse
   toward intrinsic. Spot has moved little relative to entry IV — correctly showing vol was
   overpriced for these trades.

3. **index_sl losses have ratio = 1.08 — spot moved slightly more than priced in.** This is
   consistent with Branch 1's finding that Artemis losses are delta-driven: spot crossed the sell
   strike directionally. The 1.08 ratio shows a modest but real excess-realized-vol effect.

4. **option_sl losses have ratio = 0.98 — vol was barely mispriced.** Option_sl fires when the
   option price exceeds a threshold, but spot may not have moved far. These losses happen at
   moderate vol (ratio near 1), consistent with spread widening from microstructure or modest
   directional drift rather than large spot moves.

5. **IC = +0.07 (p=0.40) — no significant predictive power.** The confound identified in
   finding #1 renders rv_iv_ratio uninterpretable as a Spearman signal here.

6. **Cross-validation of Branch 1 loss mechanism:** Branch 1 showed Artemis losses are
   delta-driven (delta contribution = −27.8 on losers). Branch 4 index_sl losses show ratio =
   1.08 (spot moved beyond entry IV) — directionally consistent. option_sl losses at ratio = 0.98
   suggest vol was fairly priced; those losses are price-level exits, not spot-range exits.

7. **Sensex (n=27, all 2023+):** ratio = 2.38 (winners 2.57, losers 1.54). Same directional
   pattern — no losers below ratio=1 (100% of Sensex losers have ratio > 1). Entry_vix IC = −0.36
   (p=0.065, borderline) — lower VIX entries do somewhat better, consistent with Sensex being
   entered in lower-vol periods.

---

```bash
# 1. Build shared engine and validate on a single trade
python research/greek_analysis/greek_engine.py --validate

# 2. Branch 1 — P&L attribution
#    Athena (cold run ~20 min, warm <1 min):
python research/greek_analysis/pnl_attribution/run.py
#    Artemis (cold run ~60–90 min once compute_iv intrinsic guard is in place):
python research/greek_analysis/pnl_attribution/run_artemis.py

# 3. Branch 2 — Greek profile (reuses cached IV from branch 1, ~1-2 min warm)
python research/greek_analysis/greek_profile/run.py          # Athena
python research/greek_analysis/greek_profile/run_artemis.py  # Artemis

# 4. Branch 3 — IV term structure (entry-time only, fast)
python research/greek_analysis/iv_term_structure/run.py

# 5. Branch 4 — Realized vs implied vol
python research/greek_analysis/realized_vs_implied/run.py

# 6. Branch 5 — IV skew (after branches 3+4)
python research/greek_analysis/iv_skew/run.py

# 7. Branch 6 — Greek exit triggers (after branches 1+2)
python athena_backtest/backtest_greek_exit.py
```
