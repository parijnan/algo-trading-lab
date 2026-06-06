# Athena Backtest Suite

Backtesting and optimization for the Nifty Double Calendar Condor strategy.

## Strategy: Phase 2 (Optimized)

The current "Phase 2" configuration represents a 67% improvement over the baseline by addressing the inherent downside skew of the Indian market while maintaining high theta efficiency.

### Core Mechanics
- **Structure:** 4-leg Double Calendar (Sell 0.30 Delta Weekly, Buy same strike Monthly).
- **Strike Selection:** Matching Strikes (CE Long = CE Short, PE Long = PE Short) for maximum time decay capture.
- **DTE Guard:** Buy legs are rolled to the next month if the monthly expiry is < 16 days away.

### Risk Management (The "Smart Parachute")
Phase 2 introduces asymmetric risk management to protect against runaway rallies while saving on static hedge costs.

1. **PE-Only Safety Wing:** A 0.05 Delta monthly PE is bought at entry. CE wings are disabled to reduce net debit and margin.
2. **Emergency CE Hedge (Smart Parachute):** 
   - **Entry:** If `Spot >= CE Sell Strike + 150 pts`, a **0.35 Delta Monthly CE** is bought immediately.
   - **Exit (Salvage):** If the market reverses and `Spot <= CE Sell Strike`, the hedge is sold to preserve core profit.
   - **Limit:** Maximum 1 attempt per trade to limit whipsaw costs.

### Performance (Lot Size 65 | VIX Filter 16-25 | 2020–2026)
- **Total P&L:** +₹149,130 (on 1 lot, 124 trades)
- **Win Rate:** 57.9%
- **Reward:Risk:** 1.66
- **Max Drawdown (Consec):** 4 losses

### Verification Note
As of May 2026, the backtesting engine accounts for the **PE Safety Wing** cost/gain end-to-end. Previous
results (₹157k) omitted the wing cost. The baseline was re-run in June 2026 after May 2026 option data
became available, adding 3 trades and revising the total from ₹139,201 to ₹149,130.

## Usage

### 1. Precompute Data
Generate the resampled 75-minute and 15-minute caches used for indicator checks:
```bash
python athena_backtest/precompute.py
```

### 2. Run Backtest
Execute the main backtest engine (uses parameters defined in `configs.py`):
```bash
python athena_backtest/backtest.py
```

### 3. Review Results
Results are saved to `athena_backtest/data/trade_summary.csv`.

## Future Research

### Phase 2.2 — Entry Filter: VIX Signal (research in progress)

121 historical trades tagged with PA range state and VIX indicators
(`research/range_detection/annotate_athena.py`). Signals computed at entry with no lookahead:

- **Dual-TF VIX Supertrend** (`vix_st_signal`): daily (p=7, m=3.0, prev day's bar) +
  75-min (p=10, m=3.0, 09:15→10:29 bar on entry day). States: `both_up` | `mixed` | `both_down`.
- **VIX Bollinger Bands %B** (`vix_bb_zone`): 20-day, 2σ, prev day's bar.
  Zones: `above_upper` (>1.0) | `upper_zone` (0.7–1.0) | `mid_zone` (0.3–0.7) | `lower_zone` (0–0.3).

**Findings so far:**
- Down-biased + `both_up` VIX (upper_zone/above_upper): ~70–75% win, strongest cluster —
  falling market + rising VIX = long vega working both directions simultaneously.
- `lower_zone` BB is the weakest column across all combinations: 31 trades, 52% win, +3.6 avg.
- No single clean skip condition confirmed yet. Full 3-way grid at
  `research/range_detection/outputs/vix_signal_grid.csv`.

**Next step:** Identify a defensible filter condition from the corrected data, then implement
as `ENABLE_VIX_ENTRY_FILTER` flag in `backtest.py`. See `plans/athena-entry-filter.md`.

**Broader direction:** the VIX work has evolved from a standalone Athena filter into a
**VIX-direction router** between Athena (long vega) and Artemis (short vega) — see
`plans/vix-router-research.md`. Separately, range state is an *orthogonal* spot-containment
signal (not a VIX proxy): useful for Athena strike placement and range-break exits, but range
*direction* should not be re-encoded as a VIX signal (corr 0.46 with VIX state). See
`plans/range-detection-research.md`.

### Phase 2.1 — Tactical Adjustments
- **PE Wing Salvage (`backtest_wing_salvage.py`):** Automatically exiting the redundant PE wing when the CE Parachute triggers. 
- **Results:** Improved win rate (64.2%) and R:R (1.37), though absolute profit was slightly lower due to exit slippage.

- **PE Parachute (`backtest_pe_chute.py`):** Symmetric downside hedge — when spot falls below the PE sell strike,
  buy a 0.35-delta monthly PE and close the PE safety wing. Sweep tested trigger offsets from −50 to +100 pts.
- **Results:** Degraded performance at every offset. No configuration improved on baseline.
- **Verdict (June 2026):** Structurally broken. Downside spikes in Indian equity markets snap back quickly unlike
  the sustained momentum seen in upside breakouts (budget rallies, election moves). Buying expensive put premium
  into elevated IV at the spike low rarely recovers its cost before the market reverses.

- **CE Buy Roll Adjustment (`backtest_ce_adj.py`):** When spot falls CE_ADJ_TRIGGER pts below the PE sell strike,
  close the monthly CE buy and reopen it CE_ADJ_VALUE pts lower. 4-cell cross sweep: triggers {50, 150} at value
  100, values {50, 150} at trigger 100. Pre-committed gate: WR ≥ 57.9% AND R:R ≥ 1.66 AND P&L ≥ ₹149,130.
- **Results:** All four cells fail the gate. WR ticks up (+1–2 pp) on three cells but R:R drops to 1.38–1.54.
  Rolling down the CE buy shaves the large winning trades (months where market partially recovers from the dip),
  dragging average winner down without reducing average loss.
- **Verdict (June 2026):** No winning configuration. WR and R:R move in opposite directions across all parameter
  combinations — the adjustment trades one for the other but cannot improve both simultaneously.

### Phase 3 — ML-Adaptive Routing (`backtest_ml_adaptive.py`)
- **Dynamic Parachute:** Scaling the emergency trigger offset based on ML confidence.
- **Preemptive Pivot:** Proactively closing the tested side based on stealth trend detection.
- **Results:** Consistently underperformed Phase 2. Final results (5-year coverage): **₹105,329 P&L, 60.8% Win Rate**.
- **Conclusion:** ML proactivity introduced a "Complexity Trap." Tightening triggers too early increased insurance costs and slippage without providing a commensurate reduction in risk. 

### Experiment: Adaptive Exit Timing (`backtest_adaptive_exit.py`)
- **Logic:** Shifted entry to 15:15. Implemented VIX-based exit: 10:25 AM if VIX < 16 (handoff to Artemis), otherwise 15:10 PM.
- **Results:** **₹102,000 P&L, 50% Win Rate, 1.70 R:R**.
- **Verdict:** Underperformed production spec. While Reward:Risk improved, the sharp drop in Win Rate and absolute P&L makes it unsuitable for production.

**VERDICT:** Phase 2 (Static 150-pt trigger) remains the definitive production version for institutional scaling.

## VIX Grey Zone Research (June 2026)

Scripts: `analyze_vix_grey_zone.py`, `analyze_vix_vega.py`

**Problem:** When VIX is near 16 at entry, the routing boundary between Artemis and Athena
produces ~50% misroutes — VIX at 15–17 is genuinely unforecastable in direction.

**Findings (pure VIX analysis, 360 Wednesday entries, 2019–2026):**
- VIX 15–17 at entry fires on 18% of Wednesdays (7–10/year post-2020)
- Base rate: 44% rose / 56% fell the following week — essentially a coin flip
- No signal (5d momentum, BB zone, MA position) changes this meaningfully
- 52% of grey-zone entries still have VIX in 15–17 the following week

**Intraday intervention track (closed):** early VIX drop on day 1 does not predict subsequent
P&L decline (r = −0.016, p = 0.86). VIX and P&L are the same option prices measured twice —
no predictive lead time exists for intraday management.

**100% of Athena losses are pre_expiry exits** — calm grinding weeks where VIX drifts down.
Zero SL-triggered losses in the baseline. The loss mechanism is entry-level, not intraday.

**Next step:** See `plans/vix-grey-zone-routing.md` — testing Option C (lower routing
boundary from 16 to 15, giving Athena the full 15–25 VIX range).

## TRIPLE_CONFIRM Parallel Research Track

`backtest_tc.py` is an isolated parallel track exploring the effect of adding TC-triggered parachutes to
the Athena double calendar. It imports all helpers from `backtest.py` without modifying them and writes
output to `data_tc/` (gitignored).

**Modifications vs baseline:**
- **CE parachute:** TC-bullish fires an early entry before spot crosses the +150 threshold. A TC-bearish
  signal exits a TC-triggered parachute (spot-triggered entries still use the reactive spot exit).
- **PE parachute (new):** TC-bearish only trigger — buys a 0.35-delta monthly PE and simultaneously
  closes the PE safety wing. TC-bullish exits the PE parachute and rebuys the PE wing.

**Results (2020–2026, 124 trades, timestamp-keyed signals — no lookahead):**
- Baseline: ₹149,130 → TC: ₹165,620 — delta **+₹16,490 (+11.1%)**
- 36 of 124 trades had TC fire; 15 better / 21 worse (41.7% win rate on fired trades)
- Result is outlier-driven: top 2 trades (Budget rally Feb 2021, Jan 2022 selloff) = +₹51,535;
  remaining 34 fires combined = −₹35,045

**Note:** An earlier version using a date-keyed signal map produced an inflated +55.6% figure due to
intraday lookahead (candles could see a signal that fired later the same day). Fixed 2026-06-06.

**Status:** Live integration deferred — edge is not robust. See `plans/tc-live-integration.md`.

To run:
```bash
python athena_backtest/backtest_tc.py
```

## Real-Time Backtesting

A separate sandbox is provided for backtesting currently open or recently closed trades before the official ICICI Breeze data is available (which only happens after contract expiry).

- **Data Source:** Angel One (`NFO` segment) normalised to match Breeze schema.
- **Engine:** `athena_backtest/backtest_realtime.py`
- **Config:** `athena_backtest/configs_realtime.py`
- **Results:** `athena_backtest/data_realtime/`

To run a real-time backtest:
1. Download data: `python data_pipeline/angel_nifty_backtest_data.py`
2. Execute backtest: `python athena_backtest/backtest_realtime.py`

---
*Note: Phase 1 legacy configurations are preserved in `configs_phase1.py` and `backtest_phase1.py` for reference.*
