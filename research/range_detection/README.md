# Range Detection

Two approaches to identifying Nifty/Sensex consolidation ranges. PA method is validated and
active; ADX method is retained for reference. Athena and Artemis trade annotation complete.

**Range state and VIX direction are *orthogonal* axes of a premium trade.** Range detection
owns the **spot-containment** axis; the VIX router owned the *vega* axis (research now
complete — symmetric router not supported; containment confirmed as dominant). They are
complementary, not competing — corr(range direction, ΔVIX over hold) ≈ 0, yet down-biased
ranges earn 2.5× the P&L via spot containment (the market's up-drift), independent of vega.

**Status (2026-05-30):** §7 gate passed. Steps 3–4 (lot sizing, strike placement) both
finalised as non-levers. SL aftermath investigation complete and parked: SLs are net
beneficial overall; 8 Thursday range-broken index_sl cases examined — conditional suppression
and re-entry both rejected (see §14 of plan and SL Aftermath section below).

Plans:
- [`plans/range-detection-research.md`](../../plans/range-detection-research.md) — research,
  §7 gate results (**PASSED**), and ranked use cases (Artemis steps 3–4 active).
- [`plans/vix-router-research.md`](../../plans/vix-router-research.md) — **[COMPLETE]** VIX router
  research. Verdict: hard gate unchanged; containment is dominant Artemis driver (ρ=0.32).
- [`plans/range-vega-strategy.md`](../../plans/range-vega-strategy.md) — *Ares*: proposed
  range-anchored strategy (downstream of steps 3–4; router verdict changes scope).
- [`plans/athena-entry-filter.md`](../../plans/athena-entry-filter.md) — annotation infra + VIX-signal findings.
- [`plans/trend-overlay-strategy.md`](../../plans/trend-overlay-strategy.md) — *Poseidon*: **[SHELVED]**
  trend/crisis-alpha overlay that would have reused the validated PA range-breakout signal
  (`range_detector_pa.py`) as its entry trigger. Killed at Step 0 (MTM equity-curve gate weak) —
  see [`research/mtm_equity/`](../mtm_equity/) and [`research/iris_threshold/`](../iris_threshold/).

---

## Scripts

| Script | Purpose | Data Source |
|---|---|---|
| `range_detector.py` | ADX-gated daily ranges (set aside; PA superior) | `nifty_daily.csv` |
| `range_detector_75min.py` | ADX-gated 75-min ranges (set aside) | `nifty.csv` resampled |
| `range_detector_pa.py` | PA range detection — daily or any N-min | `nifty_daily.csv` or `nifty.csv` resampled |
| `resample.py` | Day-anchored N-minute resampler; supports `nifty` and `sensex` | `nifty.csv` / `sensex.csv` / daily CSVs |
| `validate_gate.py` | §7 validation gate — hold rate + duration on full 2019–2026 history | `nifty.csv` (1-min) |
| `annotate_athena.py` | Tags Athena trades with range state + VIX signals | `trade_summary.csv` + `nifty.csv` + `india_vix.csv` |
| `annotate_artemis.py` | Tags Artemis trades with range state + endogenous containment proxies | `trade_summary_{nifty,sensex}_rerun.csv` + index CSVs |
| `lot_sizing_sweep.py` | Symmetric lot-sizing sweep across range-state conditions + two-sided sweep | `artemis_annotated_{nifty,sensex}.csv` |
| `analyze_sizing_rule.py` | Deep analysis of the ×2.0/×0.75 symmetric rule — bucket profiles, SL breakdown, year-by-year | `artemis_annotated_nifty.csv` |
| `analyze_asymmetric_sizing.py` | Asymmetric leg sizing sweep — scale protected leg per bucket; capital-adjusted for 1.5× margin | `artemis_annotated_nifty.csv` |
| `analyze_sl_aftermath.py` | For every stopped trade: intrinsic-at-expiry counterfactual P&L, cost of stop, range bucket, day-of-exit breakdown | `artemis_annotated_{nifty,sensex}.csv` + index CSVs |

---

## Usage

### ADX method (daily)
```bash
python range_detector.py [--months 3]
python range_detector.py --all [--no-browser]
```

### ADX method (75-min)
```bash
python range_detector_75min.py [--months 2]
python range_detector_75min.py --all [--no-browser]
```

### PA method
```bash
# Daily — single chart (last N months)
python range_detector_pa.py --timeframe daily --start-date 2023-05-23 [--months 6]

# Daily — full history
python range_detector_pa.py --timeframe daily --start-date 2023-05-23 --all [--no-browser]

# Intraday (e.g. 75-min)
python range_detector_pa.py --timeframe 75 --start-date "2024-01-02 09:15" [--months 2]
```

### Key PA arguments

| Flag | Default | Description |
|---|---|---|
| `--timeframe` | `daily` | `daily` or integer minutes (`75`, `15`, `5`, `3`) |
| `--start-date` | (required) | Initial range setter: `YYYY-MM-DD` or `"YYYY-MM-DD HH:MM"` |
| `--min-range-bars` | 5 | Min bars for established range (drawn solid; below = dashed) |
| `--breakout-confirm` | 1 | Extra closes required outside range before committing a new setter (0 = immediate) |
| `--months N` | all from start | Months to display in single-chart mode |
| `--all` | off | Full history, one chart per year |
| `--no-browser` | off | Save HTML without opening |

---

## Annotation: `annotate_athena.py`

Tags each historical Athena trade with its PA range state and VIX signal state at entry.

```bash
python annotate_athena.py
```

Output: `outputs/athena_annotated.csv` (gitignored).

### Annotation columns (Athena)

| Column | Description |
|---|---|
| `ep_direction` | `'up'` \| `'down'` \| `'initial'` — range bias at entry |
| `ep_bars_into` | Bars elapsed in the current range episode at entry |
| `ep_committed` | True if episode confirmed (bars_into > 2) |
| `ep_established` | True if committed AND bars_into ≥ 3 |
| `ep_entry_spot_pct` | Spot position in range: 0 = at low, 100 = at high |
| `ep_range_high/low/mid` | Range bounds and midpoint at entry |
| `ep_width_pct` | Range width as % of midpoint |
| `key_dist_pct` | Distance from the directional key level: down → (high−spot)/width×100; up → (spot−low)/width×100 |
| `vix_st_daily` | Daily VIX Supertrend direction at entry (`'up'`/`'down'`); p=7, m=3.0; prev day's bar |
| `vix_st_75m` | 75-min VIX Supertrend at entry (`'up'`/`'down'`); p=10, m=3.0; 09:15→10:29 bar on entry day |
| `vix_st_signal` | `'both_up'` \| `'mixed'` \| `'both_down'` |
| `vix_bb_pct` | VIX %B — position in 20-day Bollinger Bands (p=20, std=2); prev day's bar |
| `vix_bb_zone` | `'above_upper'` (>1.0) \| `'upper_zone'` (0.7–1.0) \| `'mid_zone'` (0.3–0.7) \| `'lower_zone'` (0–0.3) \| `'below_lower'` (<0) |

### Key findings — Athena (as of 2026-05-26)

Range state is the spot-containment axis, independent of vega. Down-biased ranges earn 2.5×
the P&L (+25.3 vs +10.2 avg) with the same near-zero ΔVIX — a structural up-drift effect.

---

## Annotation: `annotate_artemis.py`

Tags each historical Artemis trade with PA range state and endogenous containment proxies.
Covers both Nifty and Sensex instruments.

```bash
python annotate_artemis.py              # both instruments
python annotate_artemis.py nifty        # single instrument
python annotate_artemis.py sensex
```

Outputs: `outputs/artemis_annotated_{nifty,sensex}.csv` (gitignored).

### Annotation columns (Artemis)

Same `ep_*` and `key_dist_pct` columns as Athena, plus:

| Column | Description |
|---|---|
| `pe_dist_pct` | `(spot − pe_sell_strike) / spot × 100` — PE side clearance |
| `ce_dist_pct` | `(ce_sell_strike − spot) / spot × 100` — CE side clearance |
| `min_dist_pct` | `min(pe_dist_pct, ce_dist_pct)` — endogenous containment proxy |

### Key findings — Artemis Nifty (2026-05-26, n=296 trades 2019–2025)

| Signal | ρ | p | n |
|---|---|---|---|
| `min_dist_pct` (endogenous) | +0.32 | 0.0001*** | 150 |
| `key_dist_pct` (exogenous range) | −0.17 | 0.043* | 150 |

Closer to key level (lower `key_dist_pct`) predicts better P&L — the range bound acts as
demonstrated support/resistance, not a breakout threat. Within min_dist quartiles, key_dist
shows the same directional sign (ρ≈−0.25 to −0.29) but doesn't reach per-quartile significance.

**Direction dominates:** down-biased ranges avg +17.81 pts vs up-biased +5.22 pts (2.5× gap),
consistent with the up-drift structural effect (down ranges mean-revert against the drift).

**Design constraint:** trades are taken every eligible week — no filtering. Optimisation path
is trade-level: (1) lot-sizing by range direction, (2) range-anchored strike placement.

---

## Lot-Sizing Analysis (2026-05-28, corrected)

Scripts: `lot_sizing_sweep.py` → `analyze_sizing_rule.py` → `analyze_asymmetric_sizing.py`

### Look-ahead bug found and fixed (2026-05-28)

`annotate_artemis.py` was using `side='right'` in `price_idx.searchsorted(entry_date)`.
For daily bars indexed at midnight, this returned **Monday's bar** for a Monday 10:31am entry
— a bar whose close (which determines breakout direction) isn't known at entry time.
Fixed to `side='left'`, returning **Friday's bar** (last complete daily bar before entry).

Impact: 23 Nifty trades had their direction determined by the same-day close (look-ahead).
After fix: those 23 resolve to their prior established range. All prior E-adj numbers are
invalidated.

### Three correctness fixes applied (2026-05-28)

1. **Look-ahead bug** (`side='right'` → `side='left'`): 23 Nifty trades had direction set by
   same-day close. Invalidated all prior E-adj numbers.
2. **`ep_committed` filter** in `assign_buckets()`: uncommitted entries no longer leak into
   near/far buckets. down_near 32→31.
3. **`key_dist >= 0` guard**: spot already past the key level (negative key_dist) falls to
   'other'. Two Nifty trades removed. down_near 31→29.

### Final structural buckets (Nifty 150 trades, all fixes applied)

| Bucket | n | CE avg | CE win% | PE avg | PE win% |
|---|---|---|---|---|---|
| down_near (down, key_dist 0–50%)  | 29 | +18.34 | 65.5% | +5.38 | 58.6% |
| down_far  (down, key_dist ≥50%)   | 23 | +14.38 | 56.5% | −2.95 | 43.5% |
| up_near   (up, key_dist 0–50%)    | 19 | +6.65  | 42.1% | −1.77 | 42.1% |
| up_far    (up, key_dist ≥50%)     | 39 | −2.07  | 38.5% | +1.05 | 46.2% |

**down_near CE signal survives** — resistance overhead is real (CE avg +18.3, win% 65.5%).
**up_near PE signal gone** — was entirely look-ahead; CE and PE now roughly equal.

### Final sizing results (Nifty 150 trades)

| Config | Total | Sharpe | MaxDD | Win% |
|---|---|---|---|---|
| Baseline | +1601.4 | 2.263 | −158.6 | 68.7% |
| A: dn_near CE×2.0 | +2133.2 | 2.557 | −157.3 | 69.3% |
| E-adj (M=1.333, rest×0.5) | +1561.9 | 2.494 | −201.9 | 68.7% |
| E-adj-bk (uncommitted→near) | +1664.9 | 2.455 | −221.1 | 68.0% |

Combined Nifty+Sensex (177 trades): Baseline +1.46L, A: dn_near CE×2.0 +1.76L.

**Capital constraint:** Artemis is limited to 80/40 lots max. Within a fixed budget any
split (80/40, 85/30, 90/20) gives the same ~+₹4k uplift over 7 years — CE gain cancelled
by PE reduction. Lot sizing is not the lever.

### Step 4: Resistance-anchored CE strike placement (2026-05-28, finalised)

Script: `step4_strike_counterfactual.py`. Counterfactual: CE sell always at first 100-pt
strike above `range_high`; actual options data used (with scaling for 10 data-mismatched
trades). **Result: +150 pts = ₹3,746 over 7 years. Not a meaningful lever.**

Key findings:
- **Breach gates the outcome, not strike placement.** Resistance held 16/29 (55%) weeks;
  CE wins 94% when held, 31% when breached. Median overshoot when breached = 126 pts, so
  12/13 breach trades would hit the CF strike too — anchoring to resistance doesn't protect.
- **CE below RH (16 trades): Δ = +18 pts total.** Moving CE up to resistance loses premium;
  breach protection is illusory since overshoot clears the CF strike in almost all cases.
- **CE above RH (13 trades): Δ = +131 pts total.** High VIX pushes Artemis far OTM (350+
  pts above resistance in Feb 2024); moving down to just-above-resistance captures more credit.
- **VIX remains the sharpest predictor:** high VIX (≥14.8) → 90% CE win, 40% breach.
  Filtering to high VIX only would skip 19 profitable trades (avg +22.3 pts, ₹10.6k foregone).

**Overall conclusion (steps 3–4):** Every parameter lever explored adds at most ~₹4k over
7 years against a ₹1.46L baseline. The down_near CE edge is a structural property of
resistance holding in down-biased ranges. Rigid rules deliver it; optimisation cannot
meaningfully extend it within the existing 4-day weekly trade structure.

---

## SL Aftermath Analysis (2026-05-30, parked)

Script: `analyze_sl_aftermath.py`. For every stopped Artemis trade, computes the
counterfactual P&L if held to expiry (intrinsic at expiry spot), the cost of the stop
(positive = premature), and cross-references range bucket and day of first exit.

**Overall verdict:** stops are net beneficial — the P&L saved by correct stops outweighs the
cost of premature ones. `index_sl` and `option_sl` are working as designed.

**Thursday range-broken investigation:** 8 trades where `index_sl` fired on Thursday (expiry
day) with spot outside the PA range; all 8 expired profitably if held. Two approaches to
exploit this were evaluated and rejected:

- **Conditional SL suppression:** cases 3, 6, 8 saw the option go 50–90% higher after the SL
  before reverting. Holding through that MTM drawdown on a sample of 8 is not justified.
- **Re-entry:** on expiry day the other leg is already closed (ELM/option_sl on Wednesday).
  Re-entry means a new single-leg spread entered after a volatile morning. Strike placement
  is premium-based — re-entry premium would be a fraction of the original, and the sell strike
  would have to be closer to spot to hit the premium target. Decision-tree confirmation time
  further collapses the available premium. Not viable.

See §14 of `plans/range-detection-research.md` for the full grid and reasoning.

---

## Outputs (`outputs/`)

HTML files and CSVs are gitignored — generated locally on demand.

| File | Script | Description |
|---|---|---|
| `range_chart.html` | ADX daily | Default single chart |
| `range_chart_YYYY.html` | ADX daily | Yearly chart (`--all`) |
| `range_chart_75min.html` | ADX 75-min | Default single chart |
| `range_chart_75min_YYYY.html` | ADX 75-min | Yearly chart (`--all`) |
| `range_chart_pa_{tf}.html` | PA | Default single chart |
| `range_chart_pa_{tf}_{YYYY}.html` | PA | Yearly chart (`--all`) |
| `range_episodes.csv` | ADX daily | Episode table |
| `range_episodes_75min.csv` | ADX 75-min | Episode table |
| `range_episodes_pa_{tf}.csv` | PA | Episode table (always exported) |

### Episode CSV columns (PA method)

`episode_id`, `episode_start`, `episode_end`, `bar_count`, `is_transient`, `direction`
(`up`/`down`/`initial`), `gap_open`, `range_high`, `range_low`, `range_mid`,
`width_pts`, `width_pct`

---

## PA Detection Logic

1. **Bootstrap**: the candle at `--start-date` is the first range setter; its H/L are the
   initial range bounds.
2. **Wick expansion**: if a subsequent candle makes a new H or L but *closes* inside the
   current bounds, the range expands to absorb the wick.
3. **New range setter**: if a candle *closes* outside the current bounds, it becomes a
   *candidate* range setter. With `--breakout-confirm N`, the next N closes must also stay
   outside before the candidate is committed. If price returns inside first, the candidate
   bar is absorbed as a wick extension and the range continues unchanged.
4. **Gap logic**: if the committed range setter's open is already outside the previous range
   (gap open), the inside bound is anchored to the previous range's near boundary rather
   than the candle's own wick.
5. **Established vs transient**: episodes with `bar_count < min_range_bars` are drawn dashed
   (transient); longer episodes are drawn solid (established).
6. **Directional bias**: established episodes are colour-coded by direction. Green = up-biased
   (setter broke upward; `range_low` = key support, drawn with a thicker line). Red =
   down-biased (`range_high` = key resistance). Blue = initial episode (no prior range).
   Grey dashed = transient regardless of direction. The bottom annotation includes the bias
   label and key level price.

## ADX Detection Logic

1. **Regime**: ADX (Wilder, 14-period) < threshold → ranging
2. **Episode start**: when ADX first drops below threshold; bounds anchored 4 bars back
3. **Bounds expansion**: episode high/low expand only on confirmed Williams Fractal swings
4. **Episode split**: close exits bounds by more than `breakout_tolerance` while ADX still low
5. **Episode end**: ADX rises back above threshold
