# Plan: Index Range Detection — Research & Applications

**Status: EXPLORATORY** — Two detection methods built and visually validated on daily data.
Hybrid combination in progress. No use cases frozen. No production changes until backtested.

---

## Context

The daily Nifty chart regularly shows clear consolidation ranges that are visually obvious but
hard to define algorithmically because their duration is variable (2 days to 2+ weeks). Two
complementary methods have been built and compared:

- **ADX method** (`range_detector.py`, `range_detector_75min.py`): ADX < threshold = ranging
  regime; episode bounds anchored via Williams Fractal swing highs/lows.
- **PA method** (`range_detector_pa.py`): range setter candles (closes outside current range)
  define bounds; wick extension allowed; gap openings anchor the inside bound to the previous
  range's near boundary.

Visual comparison on daily 2025 data shows: PA method produces better bounds most of the time;
ADX method outperforms in trending stretches by correctly gating out non-ranges.

**Core insight**: PA logic for bound definition + ADX for regime gating is the natural
combination.

---

## What Was Built

### `research/range_detection/range_detector.py` — Daily timeframe (ADX method)

Reads official daily Nifty OHLC from `nifty_daily.csv` (AngelOne weighted-avg close).
`--all` generates one chart per calendar year + `outputs/range_episodes.csv`.
Visual validation complete.

### `research/range_detection/range_detector_75min.py` — 75-min timeframe (ADX method)

1-min `nifty.csv` resampled to 75-min using Apollo's day-anchored approach.
Exactly 5 bars per session: 09:15, 10:30, 11:45, 13:00, 14:15.
Round 2 parameter tuning (bounds_mode, breakout_confirm) generated but not evaluated —
superseded by the PA approach which is being pursued instead.

### `research/range_detection/resample.py` — Shared resampler module

Day-anchored N-minute resampler used by `range_detector_pa.py`. Supports any integer
timeframe (3, 5, 15, 75 min) plus `'daily'`. Extracted for reuse across scripts.

### `research/range_detection/range_detector_pa.py` — All timeframes (PA method)

Price-action range detection: range setter candles define bounds; wick extension; gap logic.
Timeframe-agnostic via `--timeframe daily|N` (N = minutes).
`--all` generates one chart per year + CSV. CSV always exported on every run.
Visual validation complete on daily 2023–2026.
75-min timeframe too noisy to be useful on its own — focus is daily.

---

## PA Method — Full Parameter Reference

| CLI flag | Default | Effect |
|---|---|---|
| `--timeframe` | `daily` | `daily` or integer minutes (e.g. `75`, `15`, `5`, `3`) |
| `--start-date` | (required) | Initial range setter: `YYYY-MM-DD` or `"YYYY-MM-DD HH:MM"` |
| `--min-range-bars` | 5 | Min bars for a range to be considered established (drawn solid) |
| `--months` | (all from start) | Months to display in single-chart mode |
| `--all` | off | Full history, one chart per year |
| `--years` | (all) | Restrict `--all` to specific years |
| `--tag` | (none) | Suffix for output filenames |
| `--no-browser` | off | Save HTML without opening |

---

## Hybrid Combination — In Progress

PA method for bound definition + ADX for regime gating. Three options to evaluate
sequentially on the daily chart:

### Option A — ADX as a hard gate on PA ranges
A PA episode is only displayed as established if ADX was below threshold during it.
In trending periods PA tracks range setters internally but nothing is drawn solid.
Simplest implementation.

### Option B — ADX upgrades/downgrades PA episodes (recommended starting point)
Each PA episode gets an ADX reading. If ADX < threshold → established (solid).
If ADX ≥ threshold → transient (dashed), regardless of bar count.
min_range_bars stays as a secondary condition (must satisfy both).
Directly addresses the cases where ADX outperforms PA standalone.

### Option C — Two-layer structure
ADX defines the macro ranging regime (outer band). Within each ADX regime,
PA logic tracks sub-ranges (inner levels). Most information-rich, most visually complex.

**Next session starts here**: implement Option A, evaluate on 2023–2026 daily charts,
then B, then C. Pick the winner; freeze defaults; consider annotating backtest trades.

---

## Probable Use Cases (not committed)

- **Entry timing** — wait for spot to revert to range mid before entering
- **Strike selection** — use range bounds as reference alongside delta
- **SL / exit calibration** — tighter stops when near boundary; boundary breach as exit trigger
- **Position sizing** — scale by distance from range mid
- **Skip / defer logic** — avoid entering a trending market
- **Trade annotation** — tag historical trades with range_pct and adx at entry

All applications require backtesting across relevant strategies before any production wiring.

---

## Next Steps

1. **Implement hybrid options A, B, C** ← *resume here*
   - Implement and chart each on 2023–2026 daily data
   - Pick the winner; freeze as new default in `range_detector_pa.py`

2. **Annotate historical backtest trades** — run existing Athena and Artemis backtests and tag
   each trade with `range_pct`, `adx`, `episode_start` at entry. Pure observational.

3. **Decide use cases** — based on annotation results, design targeted backtests.

---

## Constraints

- No changes to production strategy files at this stage.
- Any implementation must have a dedicated backtest showing improvement before going live.
- `research/range_detection/` is a research module only — not imported by any production code.
