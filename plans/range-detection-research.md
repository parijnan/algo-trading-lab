# Plan: Index Range Detection — Research & Applications

**Status: EXPLORATORY** — PA method validated on daily data. Breakout confirmation
filter under evaluation. No use cases frozen. No production changes until backtested.

---

## Context

The daily Nifty chart regularly shows clear consolidation ranges that are visually obvious but
hard to define algorithmically because their duration is variable (2 days to 2+ weeks). Two
methods were built and compared:

- **ADX method** (`range_detector.py`, `range_detector_75min.py`): ADX < threshold = ranging
  regime; episode bounds anchored via Williams Fractal swing highs/lows.
- **PA method** (`range_detector_pa.py`): range setter candles (closes outside current range)
  define bounds; wick extension allowed; gap openings anchor the inside bound to the previous
  range's near boundary. **Clearly superior on daily data.**

Hybrid ADX+PA combinations (Options A, B, C) were explored. All failed — ADX as a gate
suppresses good ranges. Pure PA with a breakout confirmation filter is the current direction.

---

## What Was Built

### `research/range_detection/range_detector.py` — Daily timeframe (ADX method)

Reads official daily Nifty OHLC from `nifty_daily.csv`. Validated, set aside in favour of PA.

### `research/range_detection/range_detector_75min.py` — 75-min timeframe (ADX method)

1-min data resampled to 75-min. 75-min timeframe too noisy on its own — set aside.

### `research/range_detection/resample.py` — Shared resampler module

Day-anchored N-minute resampler. Supports any integer timeframe (3, 5, 15, 75 min) plus
`'daily'`. Used by `range_detector_pa.py`.

### `research/range_detection/range_detector_pa.py` — All timeframes (PA method)

Price-action range detection. Core rules:
1. Close outside current range → new range setter (its H/L define new bounds)
2. New H or L without close outside → wick extension (range expands, no split)
3. Gap open outside previous range → inside bound anchored to previous range's near boundary
4. `--breakout-confirm N`: require N additional consecutive closes outside before committing.
   If price returns inside within N bars, the potential range setter is absorbed as a wick
   extension and the range continues unchanged.

Established (solid blue) = `bar_count >= min_range_bars`.
Transient (dashed grey) = `bar_count < min_range_bars`.

---

## PA Method — Full Parameter Reference

| CLI flag | Default | Effect |
|---|---|---|
| `--timeframe` | `daily` | `daily` or integer minutes (e.g. `75`, `15`, `5`, `3`) |
| `--start-date` | (required) | Initial range setter: `YYYY-MM-DD` or `"YYYY-MM-DD HH:MM"` |
| `--min-range-bars` | 5 | Min bars for established range (drawn solid) |
| `--breakout-confirm` | 1 | Extra closes required outside range before committing a new range setter (0 = immediate) |
| `--hybrid` | `none` | Hybrid ADX mode: `none` = pure PA, `a` = ADX hard gate (deprecated — didn't work) |
| `--adx-threshold` | 20 | ADX threshold (only used if `--hybrid a`) |
| `--adx-period` | 14 | Wilder ADX period (only used if `--hybrid a`) |
| `--months` | (all from start) | Months to display in single-chart mode |
| `--all` | off | Full history, one chart per year |
| `--years` | (all) | Restrict `--all` to specific years |
| `--tag` | (none) | Suffix for output filenames |
| `--no-browser` | off | Save HTML without opening |

---

## Breakout Confirmation — Current Comparison Run

Three variants generated for 2023–2026 daily data (`--min-range-bars 3`):

| Tag | `--breakout-confirm` | Episodes | Established |
|---|---|---|---|
| `conf0` | 0 (no filter) | 301 | 84 |
| `conf1` | 1 | 172 | 78 |
| `conf2` | 2 | 74 | 57 |

Charts in `outputs/` (conf0/conf1/conf2 × 2023–2026). Older outputs archived to
`outputs/archive/`.

**Next session starts here**: evaluate the three chart sets and pick the winner.
Then decide on final defaults for `--min-range-bars` and `--breakout-confirm`.

---

## Hybrid ADX Exploration — Concluded

Options A, B, C were explored. All approaches that use ADX as a gate suppress genuine ranges
(ADX lags and can stay elevated well into an established consolidation). Pure PA is superior.
The `--hybrid` flag remains in the script for reference but is not the active direction.

---

## Probable Use Cases (not committed)

- **Entry timing** — wait for spot to revert to range mid before entering
- **Strike selection** — use range bounds as reference alongside delta
- **SL / exit calibration** — tighter stops when near boundary; boundary breach as exit trigger
- **Position sizing** — scale by distance from range mid
- **Skip / defer logic** — avoid entering a trending market
- **Trade annotation** — tag historical trades with range_pct at entry

All applications require backtesting across relevant strategies before any production wiring.

---

## Next Steps

1. **Pick breakout confirmation winner** ← *resume here*
   - Evaluate conf0 / conf1 / conf2 chart sets for 2023–2026
   - Lock in final `--min-range-bars` and `--breakout-confirm` defaults

2. **Annotate historical backtest trades** — run existing Athena and Artemis backtests and tag
   each trade with `range_pct`, `episode_start` at entry. Pure observational.

3. **Decide use cases** — based on annotation results, design targeted backtests.

---

## Constraints

- No changes to production strategy files at this stage.
- Any implementation must have a dedicated backtest showing improvement before going live.
- `research/range_detection/` is a research module only — not imported by any production code.
