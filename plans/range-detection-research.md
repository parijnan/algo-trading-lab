# Plan: Index Range Detection — Research & Applications

**Status: EXPLORATORY** — Prototype built and validated. No use cases frozen. No production changes until backtested.

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

`research/range_detection/range_detector.py` — standalone prototype:

| Function | Purpose |
|---|---|
| `load_daily(months_back)` | Resamples 1-min Nifty CSV to daily OHLC with 2-month ADX warm-up buffer |
| `compute_adx(df, period=14)` | Pure numpy Wilder ADX (sum-seed for TR/DM, mean-seed for ADX step) |
| `find_swings(df, strength=3)` | Williams Fractals — marks confirmed swing highs/lows |
| `compute_ranges(...)` | ADX-gated episodes; price-breakout splits a ranging episode when close exits bounds ± tolerance; lookback anchoring captures range that forms before ADX confirms |
| `plot()` | Plotly dark-theme candlestick + ADX chart → `outputs/range_chart.html` |
| `print_summary()` | Terminal episode table with position-in-range assessment |

**Key parameters:**

| Parameter | Default | Effect |
|---|---|---|
| `adx_threshold` | 20 | ADX below this → ranging regime |
| `lookback_at_start` | 4 bars | How far back to anchor range start when ADX first drops |
| `breakout_tolerance` | 0.2% | How far close must exit bounds to split an episode |
| `swing_strength` | 3 bars | Williams Fractal window (±3 bars) |

**Validation (3-month backtest vs visual assessment):**
- Feb 12–26 range, Feb 27 breakout — matches
- Apr 21–May 11 range, May 12–present range — correctly split on breakout

---

## Probable Use Cases (not committed)

Knowing the current range state and position-in-range is potentially applicable to:

- **Entry timing** — wait for spot to revert to range mid before entering (both legs symmetric)
- **Strike selection** — use range bounds as reference alongside delta (e.g. don't sell beyond a confirmed range extreme)
- **SL / exit calibration** — tighter stops when near boundary, range-boundary breach as additional trigger
- **Position sizing** — scale by distance from range mid
- **Skip / defer logic** — avoid entering a trending market (ADX ≥ threshold), wait for next ranging episode
- **Trade annotation** — tag historical trades with `range_pct` and `adx` at entry to surface P&L patterns

All applications require backtesting across relevant strategies before any production wiring.

---

## Next Steps

1. **Annotate historical backtest trades** — run existing Athena and Artemis backtests and tag
   each trade with `range_pct`, `adx`, `episode_start` at the entry candle. No strategy logic
   changes; pure observational correlation between position-in-range and outcome.

2. **Parameter sensitivity sweep** — sweep `ADX_THRESHOLD` (18/20/22), `lookback_at_start`
   (3/4/5), `breakout_tolerance` (0.1%/0.2%/0.5%) to verify episode detection is stable.

3. **Extend to 1-min resolution** — daily candles give the regime; 1-min data needed to
   evaluate intraday behaviour at decision points (e.g. first touch of range_mid).

4. **Decide use cases** — based on annotation results, pick highest-impact applications per
   strategy and design targeted backtests.

---

## Constraints

- No changes to production strategy files at this stage.
- Any implementation must have a dedicated backtest showing improvement before going live.
- `research/range_detection/` is a research module only — not imported by any production code.
