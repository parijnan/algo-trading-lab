# Range Detection

ADX-gated range detection for Nifty. Identifies consolidation episodes by tracking
when ADX drops below a threshold, anchoring episode bounds via Williams Fractal swing
highs/lows, and splitting episodes on confirmed price breakouts.

See [`plans/range-detection-research.md`](../../plans/range-detection-research.md) for
the full research plan, parameter tuning notes, and probable use cases.

## Scripts

| Script | Timeframe | Data Source | Bars/Session |
|---|---|---|---|
| `range_detector.py` | Daily | `nifty_daily.csv` (official weighted-avg close) | 1 |
| `range_detector_75min.py` | 75-min | `nifty.csv` resampled (day-anchored, Apollo method) | 5 |

## Usage

```bash
# Single chart — last N months
python range_detector.py [--months 3]
python range_detector_75min.py [--months 2]

# Full history — one chart per year + episodes CSV
python range_detector.py --all [--no-browser]
python range_detector_75min.py --all [--no-browser]
```

### Key arguments

| Flag | Default | Description |
|---|---|---|
| `--months N` | 3 (daily) / 2 (75-min) | Months to display in single-chart mode |
| `--adx-threshold` | 20 | ADX below this = ranging |
| `--adx-period` | 14 | Wilder ADX period |
| `--swing-strength` | 3 | Williams Fractal lookahead/lookback in bars |
| `--all` | off | Full history, one chart per year |
| `--no-browser` | off | Save HTML without opening |

## Outputs (`outputs/`)

HTML files and CSVs are gitignored — generated locally on demand.

| File | Description |
|---|---|
| `range_chart.html` | Default single-chart (daily) |
| `range_chart_YYYY.html` | Yearly chart (daily, `--all`) |
| `range_chart_75min.html` | Default single-chart (75-min) |
| `range_chart_75min_YYYY.html` | Yearly chart (75-min, `--all`) |
| `range_episodes.csv` | Episode table — daily timeframe |
| `range_episodes_75min.csv` | Episode table — 75-min timeframe |

### Episode CSV columns

`episode_start`, `episode_end`, `days` / `bars`, `range_high`, `range_low`,
`range_mid`, `width_pts`, `width_pct`, `last_close`, `close_pct_in_range`

`close_pct_in_range`: 0 = at range low, 100 = at range high.

## Detection Logic

1. **Regime**: ADX (Wilder, 14-period) < threshold → ranging
2. **Episode start**: when ADX first drops below threshold; bounds anchored 4 bars back
   to capture the consolidation that precedes ADX confirmation lag
3. **Bounds expansion**: episode high/low expand only on confirmed Williams Fractal swings
4. **Episode split**: if close exits bounds by more than `breakout_tolerance` (0.2%) while
   ADX is still low, a new episode starts at the breakout bar
5. **Episode end**: ADX rises back above threshold
