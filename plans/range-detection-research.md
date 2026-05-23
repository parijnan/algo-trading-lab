# Plan: Index Range Detection — Research & Applications

**Status: EXPLORATORY** — Two timeframes built. 75-min parameter tuning in progress. No use cases frozen. No production changes until backtested.

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

Reads official daily Nifty OHLC from `nifty_daily.csv` (AngelOne weighted-avg close).
`--all` generates one chart per calendar year + `outputs/range_episodes.csv` (103 episodes, 2023–2026).
Visual validation complete.

### `research/range_detection/range_detector_75min.py` — 75-min timeframe

1-min `nifty.csv` resampled to 75-min using Apollo's day-anchored approach (avoids pandas
drift). Exactly 5 bars per session: 09:15, 10:30, 11:45, 13:00, 14:15. Chart uses Plotly
`rangebreaks` to suppress overnight/weekend gaps.
`--all` generates one chart per year + `outputs/range_episodes_75min.csv`.
Visual validation complete (2019–2026).

---

### Full Parameter Reference (75-min script)

| CLI flag | Default | Effect |
|---|---|---|
| `--adx-threshold` | 20 | ADX below this → ranging regime |
| `--breakout-tolerance` | 0.002 (0.2%) | Close must exit episode bounds by this fraction to count as a breakout |
| `--bounds-mode` | `fractal` | How bounds expand: `fractal` = Williams Fractal swings only; `realtime` = any intra-episode new H/L immediately |
| `--breakout-confirm` | 1 | Consecutive closes outside the band required before splitting an episode |
| `--lookback-at-start` | 4 bars | How far back to anchor bounds when a new episode begins |
| `--swing-strength` | 3 bars | Williams Fractal lookahead/lookback window |
| `--years` | (all) | Restrict `--all` output to specific years |
| `--tag` | (none) | Suffix for output filenames — used to distinguish tuning runs |

---

## 75-min Tuning — Progress and Findings

### Round 1 — `adx_threshold` and `breakout_tolerance`

Tested four combinations on 2024 and 2026 (reference years):

| Tag | `adx_threshold` | `breakout_tolerance` | Verdict |
|---|---|---|---|
| (default) | 20 | 0.2% | Too fragmented — many 1-bar episodes throughout |
| `adx15` | 15 | 0.2% | Fewer regime detections; doesn't help fragmentation |
| `bt05` | 20 | 0.5% | **Best of this round** — cleaner but still too aggressive |
| `adx15_bt05` | 15 | 0.5% | Misses genuine ranging periods |

**Conclusion:** `adx_threshold=20`, `breakout_tolerance=0.5%` is the right base to build from.
The remaining problem: episodes still split too readily on single-bar spikes within a genuine range.

### Round 2 — `bounds_mode` and `breakout_confirm` (on top of bt05)

Two new mechanisms added to loosen the split criterion:

- **`bounds_mode=realtime`**: bounds expand on every intra-episode new H/L immediately, rather
  than waiting for Williams Fractal confirmation. Eliminates false splits caused by the 3-bar
  fractal lag — price can reach a new high that isn't yet a confirmed fractal, triggering a
  spurious breakout against stale bounds.

- **`breakout_confirm=N`**: requires N consecutive closes outside the tolerance band before
  splitting. Bounds do not expand while a streak is building; streak resets if price returns
  inside. `N=2` means a single bar overshoot is ignored.

Four charts generated for 2024 and 2026 — **not yet evaluated:**

| Tag | `bounds_mode` | `breakout_confirm` |
|---|---|---|
| `bt05` | fractal | 1 (baseline) |
| `bt05_rt` | realtime | 1 |
| `bt05_conf2` | fractal | 2 |
| `bt05_rt_conf2` | realtime | 2 |

**Next session starts here**: open these four chart pairs, pick the combination that produces
the cleanest multi-bar episodes without hiding genuine breakouts, then lock in as new defaults.

---

## Probable Use Cases (not committed)

- **Entry timing** — wait for spot to revert to range mid before entering (both legs symmetric)
- **Strike selection** — use range bounds as reference alongside delta
- **SL / exit calibration** — tighter stops when near boundary; range-boundary breach as additional trigger
- **Position sizing** — scale by distance from range mid
- **Skip / defer logic** — avoid entering a trending market (ADX ≥ threshold)
- **Trade annotation** — tag historical trades with `range_pct` and `adx` at entry to surface P&L patterns

All applications require backtesting across relevant strategies before any production wiring.

---

## Next Steps

1. **Finish 75-min tuning** ← *resume here*
   - Evaluate the four Round 2 chart pairs (`bt05`, `bt05_rt`, `bt05_conf2`, `bt05_rt_conf2`)
   - Pick the winner; update script defaults; regenerate the full 2019–2026 run and CSV
   - Decide whether to apply the same changes to the daily script

2. **Annotate historical backtest trades** — run existing Athena and Artemis backtests and tag
   each trade with `range_pct`, `adx`, `episode_start` at entry. No strategy logic changes;
   pure observational correlation between position-in-range and outcome.

3. **Decide use cases** — based on annotation results, pick highest-impact applications per
   strategy and design targeted backtests.

---

## Constraints

- No changes to production strategy files at this stage.
- Any implementation must have a dedicated backtest showing improvement before going live.
- `research/range_detection/` is a research module only — not imported by any production code.
