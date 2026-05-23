# Plan: Index Range Detection — Research & Applications

**Status: EXPLORATORY** — Two timeframes built and validated. No use cases frozen. No production changes until backtested.

---

## Context

The daily Nifty chart regularly shows clear consolidation ranges that are visually obvious but
hard to define algorithmically because their duration is variable (2 days to 2+ weeks). A fixed
lookback window does not work. ADX-gated range detection solves this by using market state
(ADX < 20 = consolidating) rather than a fixed window, and tracks episode bounds adaptively via
confirmed swing highs/lows.

The core insight: knowing *where* spot is within an established range, and *whether* a range
exists at all, is potentially useful information for multiple strategy decisions across the
entire system — not just entry timing, and not restricted to any single strategy.

---

## What Was Built

### `research/range_detection/range_detector.py` — Daily timeframe

| Function | Purpose |
|---|---|
| `load_daily(months_back)` | Reads official daily Nifty OHLC from `nifty_daily.csv` (AngelOne weighted-avg close). 2-month ADX warm-up buffer. |
| `compute_adx(df, period=14)` | Pure numpy Wilder ADX |
| `find_swings(df, strength=3)` | Williams Fractals — confirmed swing highs/lows |
| `compute_ranges(...)` | ADX-gated episodes; price-breakout splits a ranging episode when close exits bounds ± tolerance; lookback anchoring captures range that forms before ADX confirmation lag |
| `plot(from_date, to_date, label)` | Plotly dark-theme candlestick + ADX chart |
| `print_summary(from_date, to_date)` | Terminal episode table with position-in-range |
| `export_episodes_csv(result, path)` | One row per episode: start, end, bounds, width, close position |

**`--all` flag:** Runs detection once on full 3-year history, generates one HTML chart per
calendar year + `outputs/range_episodes.csv` (103 episodes, 2023–2026).

**Visual validation complete** — 2023 through 2026 charts inspected.

---

### `research/range_detection/range_detector_75min.py` — 75-min timeframe

Same detection logic. Data source: 1-min `nifty.csv` resampled to 75-min using the same
day-anchored approach as Apollo (avoids pandas drift on non-clock-aligned intervals).
Guarantees exactly 5 bars per session: 09:15, 10:30, 11:45, 13:00, 14:15.

Chart uses Plotly `rangebreaks` to suppress overnight and weekend gaps.

**`--all` flag:** Generates one chart per year + `outputs/range_episodes_75min.csv`
(613 episodes, 2019–2026).

**Visual validation complete** — 2019 through 2026 charts inspected.

---

### Key Parameters (both scripts)

| Parameter | Default | Effect |
|---|---|---|
| `adx_threshold` | 20 | ADX below this → ranging regime |
| `lookback_at_start` | 4 bars | How far back to anchor when ADX first drops |
| `breakout_tolerance` | 0.2% | Close must exit bounds by this much to split an episode |
| `swing_strength` | 3 bars | Williams Fractal window (±3 bars) |

---

## Pending Parameter Tuning (75-min)

The 75-min run at default parameters produces many short (1–3 bar) episodes, especially
during the 2024 bull grind and other low-ADX-but-directional periods. Two changes are
worth evaluating before the annotation step:

| Parameter | Current | Candidate | Rationale |
|---|---|---|---|
| `adx_threshold` | 20 | 15 | ADX responds faster intraday; 20 may be too lenient and let slow trending periods qualify as ranging |
| `breakout_tolerance` | 0.2% | 0.5% | 0.2% fires splits too easily on normal intraday volatility — at 24,000 spot, 0.2% is ~48 pts, easily covered in one candle |

Validate visually on 2–3 representative years before changing defaults. Apply the same
candidates to the daily script for consistency.

---

## Probable Use Cases (not committed)

Knowing the current range state and position-in-range is potentially applicable to:

- **Entry timing** — wait for spot to revert to range mid before entering (both legs symmetric)
- **Strike selection** — use range bounds as reference alongside delta
- **SL / exit calibration** — tighter stops when near boundary; range-boundary breach as additional trigger
- **Position sizing** — scale by distance from range mid
- **Skip / defer logic** — avoid entering a trending market (ADX ≥ threshold)
- **Trade annotation** — tag historical trades with `range_pct` and `adx` at entry to surface P&L patterns

All applications require backtesting across relevant strategies before any production wiring.

---

## Next Steps

1. **Parameter sensitivity (75-min)** — evaluate the `adx_threshold=15` and
   `breakout_tolerance=0.5%` candidates visually. Confirm or adjust defaults before
   annotation runs.

2. **Annotate historical backtest trades** — run existing Athena and Artemis backtests and
   tag each trade with `range_pct`, `adx`, `episode_start` at entry. No strategy logic
   changes; pure observational correlation between position-in-range and outcome.

3. **Decide use cases** — based on annotation results, pick highest-impact applications per
   strategy and design targeted backtests.

---

## Constraints

- No changes to production strategy files at this stage.
- Any implementation must have a dedicated backtest showing improvement before going live.
- `research/range_detection/` is a research module only — not imported by any production code.
