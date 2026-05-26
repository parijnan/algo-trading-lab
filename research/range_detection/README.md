# Range Detection

Two approaches to identifying Nifty consolidation ranges. PA method is validated and active;
ADX method is retained for reference. Athena trade annotation is complete.

**Range state and VIX direction are *orthogonal* axes of a premium trade.** Range detection
owns the **spot-containment** axis; the VIX router owned the *vega* axis (research now
complete — symmetric router not supported; containment confirmed as dominant). They are
complementary, not competing — corr(range direction, ΔVIX over hold) ≈ 0, yet down-biased
ranges earn 2.5× the P&L via spot containment (the market's up-drift), independent of vega.

**VIX router research complete (2026-05-26):** spot-to-strike distance (containment proxy)
predicts Artemis P&L at ρ=0.32, p=0.0001 on 150 historical trades. This is the empirical
foundation for range-based Artemis enhancement. Validation gate (§7) is the immediate next step.

Plans:
- [`plans/range-detection-research.md`](../../plans/range-detection-research.md) — research,
  validation gate (**next**), and re-ranked use cases (Artemis strongest, Apollo cleanest, Athena complementary).
- [`plans/vix-router-research.md`](../../plans/vix-router-research.md) — **[COMPLETE]** VIX router
  research. Verdict: hard gate unchanged; containment is dominant Artemis driver.
- [`plans/range-vega-strategy.md`](../../plans/range-vega-strategy.md) — *Hestia*: proposed
  range-anchored strategy (downstream of range validation gate + router; router verdict changes scope).
- [`plans/athena-entry-filter.md`](../../plans/athena-entry-filter.md) — annotation infra + VIX-signal findings.

---

## Scripts

| Script | Purpose | Data Source |
|---|---|---|
| `range_detector.py` | ADX-gated daily ranges (set aside; PA superior) | `nifty_daily.csv` |
| `range_detector_75min.py` | ADX-gated 75-min ranges (set aside) | `nifty.csv` resampled |
| `range_detector_pa.py` | PA range detection — daily or any N-min | `nifty_daily.csv` or `nifty.csv` resampled |
| `resample.py` | Day-anchored N-minute resampler shared by all scripts | `nifty.csv` / `india_vix.csv` |
| `annotate_athena.py` | Tags historical Athena trades with range + VIX signals | `trade_summary.csv` + `nifty.csv` + `india_vix.csv` |

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
Run from the `research/range_detection/` directory:

```bash
python annotate_athena.py
```

Output: `outputs/athena_annotated.csv` (gitignored — regenerate locally).

### Annotation columns

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

### Key findings (as of 2026-05-26)

**Range state is the spot-containment axis, independent of vega.** corr(range direction,
ΔVIX over hold) ≈ +0.03, yet down-biased ranges earn 2.5× the P&L (+25.3 vs +10.2 avg) with
the *same* near-zero VIX move — a spot-containment effect (up-drift), not a vega effect.
This validates range detection as complementary to the VIX router. Emphasis is therefore on
range **bounds & containment** (pure price), not range *direction* (corr 0.46 with VIX state —
the router's job).

**VIX-signal findings (vega axis, see router plan):** down-biased + both_up VIX is the
strongest cluster; `lower_zone` BB is the consistently weakest column. No single clean VIX
skip condition confirmed yet. Full 3-way grid at `outputs/vix_signal_grid.csv`.

Note: an earlier version used `.dt.tz_convert(None)` when loading 1-min VIX, converting IST to
UTC (09:15 → 03:45) and corrupting the 75-min resample. Fixed (`tz_localize(None)`); all
numbers reflect corrected data.

**Next gate:** key-level hold rate + range duration distribution — see
`plans/range-detection-research.md` §7. These greenlight or kill all containment use cases.

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
