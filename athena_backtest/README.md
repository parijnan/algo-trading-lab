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

- **Reactive PE Wing (`backtest_wing_reactive.py`):** Replaces the static always-on 0.05-delta PE wing with a
  conditional hedge. Wing is bought only when spot drops a threshold below `entry_spot`; sold when
  `spot > entry_spot`. No overnight lock — wing held naturally through overnight/weekends until spot recovers.
  `entry_spot` is the 10:30 spot at trade entry and is fixed for the entire trade.
  Supports two trigger modes via CLI:
  - `--offset N` — fixed points below entry_spot (e.g. `--offset 250`)
  - `--pct P` — percentage of entry_spot (e.g. `--pct 1.75`); self-normalises as spot level changes

- **EOD variant (`backtest_wing_eod.py`):** Tested daily buy at 15:15 + morning sell at 9:20.
  Worse (₹116,015, −939 pts wing drag, 437 transactions). Not pursued further.

- **Fixed-offset sweep (50–300 pts):** Peak at 250 pts (₹181,795). Fixed offsets are not spot-normalised —
  250 pts = 2.3% at 2020 Nifty levels but only 1.0% at 2024 levels. Superseded by pct sweep.

- **Percentage-offset sweep (0.5%–2.0%, June 2026):** Trigger = `entry_spot × pct`. Wing fires only once
  spot has moved the same *relative* distance regardless of the index level.

  | Config | Total P&L (₹) | Win Rate | R:R | Wing P&L (pts) | Wing Txs | Max Loss (₹) |
  |---|---|---|---|---|---|---|
  | Baseline (always-on) | 149,130 | 58.9% | 1.65 | −455 | ~228 | −8,229 |
  | 0.5% | 168,009 | 64.5% | 1.50 | −165 | 139 | — |
  | 0.75% | 171,561 | 62.9% | 1.63 | −110 | 106 | — |
  | 1.0% | 176,706 | 64.5% | 1.55 | −31 | 82 | — |
  | 1.25% | 175,721 | 63.7% | 1.58 | −46 | 71 | — |
  | 1.5% | 179,777 | 63.7% | 1.59 | +16 | 55 | — |
  | **1.75%** | **179,764** | **62.9%** | **1.66** | **+16** | **48** | **−8,489** |
  | 2.0% | 183,401 | 64.5% | 1.56 | +72 | 39 | −9,230 |

  Wing P&L turns net positive at 1.5%. P&L improves monotonically through 2.0%.
  Rows 0.5%–1.5% use pre-fix execution timing; 1.75% and 2.0% re-run after timing fix (see below).

- **Timing fix (June 2026):** The original code detected the wing trigger at bar T's close but executed
  at bar T's open — a 1-bar lookahead. Fixed by introducing `wing_buy_pending` / `wing_sell_pending`
  flags: trigger is detected at close of bar T, execution deferred to open of bar T+1. Impact is small
  (−₹679 for 1.75%, −₹2,379 for 2.0%) and does not change the production candidate selection.

- **Sustainability analysis — 1.75% vs 2.0% (post-fix):**
  Both configs share the same median P&L (₹1,202) and max consecutive losses (4).
  1.75% is the more sustainable choice:
  - Skewness: 0.649 vs 0.537 (better upside tail)
  - Downside deviation: ₹2,159 vs ₹2,283
  - Sortino-like ratio: 0.671 vs 0.648
  - Max loss: −₹8,489 vs −₹9,230 (2.0% exceeds the always-on baseline max loss)
  The 2.0% config earns only ₹30/trade more in mean P&L by accepting a heavier left tail.

- **Per-trade logs:** `data_wing_reactive_pct_NNN/trade_logs/trade_NNNN_YYYY-MM-DD.csv` — one file per trade
  with 1-min resolution: spot, VIX, `entry_spot`, `wing_trigger_level`, wing state, and cumulative P&L.
  Gitignored (generated output).

- **Status (June 2026):** 1.75% pct-offset is the leading production candidate — beats baseline by +₹30,634
  (+21%), R:R 1.66 (above baseline 1.65), max loss −₹8,489 (within range of baseline −₹8,229). Post-fix
  numbers confirmed. Ready for production port pending paper session validation.

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

### Jun 30 Buy-Expiry Cycle Results (May–Jun 2026)

| Entry | Buy Expiry | Actual P&L | Backtest P&L | Notes |
|-------|------------|-----------|-------------|-------|
| Apr 20 | May 26 | +₹5,129 | +₹5,129 | ICICI data |
| Apr 27 | May 26 | +₹722 | +₹722 | ICICI data |
| May 4 | May 26 | ~+₹2,300 | ~+₹2,300 | ICICI data |
| May 11 | Jun 30 | −₹9,000 | **SKIPPED** | Angel One missing Jun30 24300ce from May 11 |
| May 18 | Jun 30 | −₹9,000 | −₹4,186 | Old stale run showed −₹12,750 (deleted temp data) |
| May 25 | Jun 30 | −₹2,500 | −₹4,937 | Manually managed; backtest assumes mechanical exit |
| Jun 1 | Jun 30 | +₹234 (Artemis) | +₹4,573 hypothetical | Routed to Artemis; snapshot at Jun 5 close only |

**Structural finding:** May 26 buy-expiry cycle (Apr–May, Nifty ~24k, VIX 17–20) → 3 winners.
Jun 30 buy-expiry cycle (May–Jun, Nifty ~23k volatile) → 3 mechanical losers.
Monthly 50+ DTE buy leg moves more than weekly on intraweek Nifty trends → vega trap in trending markets.

### Data Quality Warnings

**Stale temp data shadows ICICI:** `load_option_data` checks `temp/` before `ICICI options/`. If a temp
directory exists for an expiry that is already fully covered by ICICI (after contract expiry), it will
silently shadow the correct ICICI data. **Delete temp directories for expired contracts** once ICICI data
is confirmed present. Stale temp files can produce entirely wrong strike selections and P&L figures.

**Angel One returns garbage on market holidays:** The API returns fabricated candles for non-trading days
(e.g., all Saturday rows). These show wildly incorrect option prices and will corrupt P&L calculations.
Strip all rows timestamped on market holidays after each download.

**Corrupt files from concurrent writes:** Running multiple download processes that target the same output
file produces split/merged rows in the CSV. Symptom: `ValueError: time data "IFTY" doesn't match format`.
Fix: drop any row where the timestamp field is not a valid `YYYY-MM-DD HH:MM:SS` datetime.

**Missing strikes use proxy files:** When Angel One has no data for a required strike (e.g., 24300ce),
copy the nearest available strike's CSV and update the `strike_price` column via sed. This only works if
the proxy strike was active from the same entry date. If not (e.g., 24350ce starts May 13 but entry is
May 11), the trade cannot be modelled and will be skipped by the backtest.

**May 11 is permanently unmodelable:** Angel One has no historical data for Jun30 24300ce starting May 11
(token appears from May 13 onwards). The trade was entered in production at −₹9,000 but cannot be
replicated in the realtime backtest.

---
*Note: Phase 1 legacy configurations are preserved in `configs_phase1.py` and `backtest_phase1.py` for reference.*
