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

### Performance (Lot Size 65 | VIX Filter 16-25)
- **Total P&L:** +139,201 ₹ (on 1 lot, 5-year backtest)
- **Win Rate:** 57.9%
- **Reward:Risk:** 1.66
- **Max Drawdown (Consec):** 4 losses

### Verification Note
As of May 2026, the backtesting engine has been updated to strictly account for the **PE Safety Wing** cost/gain from entry to exit. Previous results (₹157k) omitted the wing cost from the summary totals. The current ₹139k figure represents the true net performance with all 5/6 legs (Base 4 + PE Wing + CE Parachute) correctly netted.

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

### Phase 2.2 — Entry Filter: VIX Signal (research complete, backtest pending)

Analysis of 121 historical trades tagged with PA range state and VIX indicators found a
structural weak spot: **up-biased range + VIX both-timeframe uptrend + VIX mid Bollinger-Band
position** produces 22% win rate and −194 pts combined across 9 trades.

**Signal definition (all computed from data available at 10:30 entry, no lookahead):**
- Dual-TF VIX Supertrend: daily (p=7, m=3.0, previous day's bar) + 75-min (p=10, m=3.0,
  09:15→10:29 bar on entry day). Combined signal: `both_up` | `mixed` | `both_down`.
- VIX Bollinger Bands %B: 20-day, 2σ, previous day's daily close.
  Zones: `above_upper` (>1.0) | `upper_zone` (0.7–1.0) | `mid_zone` (0.3–0.7) | `lower_zone` | `below_lower`.

**Proposed skip condition:**
```
ep_direction == 'up' AND vix_st_signal == 'both_up' AND vix_bb_zone == 'mid_zone'
```

**Estimated impact:** 112 trades, ~+2,336 pts vs baseline 121 trades, +2,142 pts.

**Critical asymmetry:** Down-biased + both_up VIX is a strongly positive signal (77% win,
+819 pts for 26 trades) — rising VIX with a falling market benefits the long-vega calendars.
The filter is up-biased only.

**Next step:** Implement as `ENABLE_VIX_ENTRY_FILTER` flag in `backtest.py` and run full
backtest to verify. See `plans/athena-entry-filter.md` for the full research and next steps.

**Research artefact:** `research/range_detection/annotate_athena.py` — regenerates
`outputs/athena_annotated.csv` with all range + VIX annotation columns.

### Phase 2.1 — Tactical Adjustments
- **PE Wing Salvage (`backtest_wing_salvage.py`):** Automatically exiting the redundant PE wing when the CE Parachute triggers. 
- **Results:** Improved win rate (64.2%) and R:R (1.37), though absolute profit was slightly lower due to exit slippage.

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
