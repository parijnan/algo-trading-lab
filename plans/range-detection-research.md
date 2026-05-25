# Plan: Index Range Detection — Research & Applications

**Status: EXPLORATORY** — PA method validated on daily data. Breakout confirmation N=2
selected. Directional bias visualisation implemented. Use cases designed; none backtested yet.

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
suppresses good ranges. Pure PA with a breakout confirmation filter is the active approach.

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

Established ranges are colour-coded by direction:
- **Green** (up-biased): range setter broke upward; range_low = key support (thick line)
- **Red** (down-biased): range setter broke downward; range_high = key resistance (thick line)
- **Blue** (initial/neutral): very first episode, no prior range to break
- **Grey dashed** (transient): bar_count < min_range_bars

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

## Breakout Confirmation — Concluded

Three variants compared on 2023–2026 daily data (`--min-range-bars 3`):

| Tag | `--breakout-confirm` | Episodes | Established |
|---|---|---|---|
| `conf0` | 0 (no filter) | 301 | 84 |
| `conf1` | 1 | 172 | 78 |
| `conf2` | 2 | 74 | 57 |

**Winner: N=2.** Conf2 produces clean, durable ranges. False breakouts absorbed correctly.
Comparison charts archived to `outputs/archive/`. Current outputs use N=2.

---

## Lag Analysis

With `--breakout-confirm 2`, two types of lag exist:

**1. Confirmation lag (2 bars, by design):** The range setter bar (day 1) plus 2 confirming
closes = you don't know the new range direction until day 3. During the pending period, the
chart shows the breakout bars as wick extensions of the old range — no visible signal that
a new range is forming.

**2. Bounds instability:** Even after commitment, range_high/range_low keep expanding via wick
extension throughout the episode. The bounds at commitment are provisional.

**What survives the lag:**
- **Direction** is reliable from day 3 — 3 consecutive closes in one direction is a strong read.
- **Key level** (range_low for up-biased, range_high for down-biased) is anchored to the
  *previous* range's near boundary when there's a gap open. That level was known before the
  breakout bar opened, so it doesn't move much after commitment.
- **Historical annotation** has no lag issue at all.

**Practical implication:** Don't trade the range setter bar or the confirmation bars. By day 3
the direction and key level are known, and most ranges have 5–25+ bars remaining — enough to
work with.

---

## Hybrid ADX Exploration — Concluded

Options A, B, C were explored. All approaches that use ADX as a gate suppress genuine ranges
(ADX lags and can stay elevated well into an established consolidation). Pure PA is superior.
The `--hybrid` flag remains in the script for reference but is not the active direction.

---

## Use Cases in Existing Strategies

### Athena (Nifty double calendar) — strongest fit

Double calendars profit from the underlying staying in a range — the strategy and the signal
are structurally aligned.

- **Entry filter**: only enter when an established range (≥3 bars, confirmed direction) is
  active. Skip transient episodes and freshly committed episodes (bounds not yet stable).
- **Strike placement**: centre the two calendars around range_mid rather than ATM. In an
  up-biased range shift slightly above mid; in a down-biased range slightly below mid.
- **Exit trigger**: committed range break (new episode transition) as a hard exit — the
  condition that justified the trade no longer holds.
- **Width calibration**: use `width_pct` from the episode CSV to size strikes relative to
  range width.

### Apollo (Nifty ITM debit spread) — directional filter

Apollo already has a directional signal (dual Supertrend). Range detection adds context.

- **False signal filter**: a Supertrend signal with close_pct_in_range between 30–70% is more
  likely noise. The cleanest Apollo setups are breakouts from a range, not chop mid-range.
- **Conviction amplifier**: new up-biased range committed + Supertrend bullish = double
  confirmation. Candidate for sizing up.
- **Range-based SL**: in an up-biased range, range_low is key support. A close below it
  invalidates the bias — cleaner SL than a fixed offset.

### Artemis (Sensex iron condor)

`sensex_daily.csv` is already in the data pipeline — the range detector can run on Sensex
directly. No proxy needed.

- **Directional skew**: in a down-biased Sensex range, give more room on the CE side
  (resistance respected), tighter on the PE side. Reverse for up-biased.
- **Skip condition**: if a new Sensex range was committed within the last 2 days (bounds
  unstable), skip that week's Artemis entry.
- **Key level as reference**: range_high (down-biased) or range_low (up-biased) as an
  additional anchor for short strike placement beyond delta alone.
- **To do**: extend `resample.py` to support Sensex as a data source (trivial — same file
  format, just a different path constant).

---

## New Strategy Concept: Range Anchor

**Thesis:** After a range is committed, one boundary (range_low for up-biased, range_high for
down-biased) acts as the key level the market has demonstrated it respects. Sell time value
anchored to that level while the range holds; exit when the range breaks.

**Entry conditions:**
- New range episode committed (day 3+, N=2 confirmation)
- Direction confirmed (up or down — skip initial/neutral)
- Episode has sufficient bars remaining (target ≥5 bars left)

**Core structure — diagonal calendar at the key level:**

*Up-biased range (key support = range_low):*
- Sell near-term (weekly) PE at or slightly below range_low
- Buy further-dated (monthly) PE 100–150 pts below range_low as protection
- Net: put calendar/diagonal that collects theta while support holds

*Down-biased range (key resistance = range_high):*
- Sell near-term CE at or slightly above range_high
- Buy further-dated CE 100–150 pts above range_high as protection
- Net: call calendar/diagonal that collects theta while resistance holds

**Optional income overlay:**
- Sell a credit spread on the far side of the range (PE spread below range_low for up-biased,
  or CE spread above range_high for down-biased). Generates additional premium without
  conflicting with the directional bias.

**Exit rules:**
- **Primary:** new range episode committed (breakout confirmed, N=2) — structural basis gone
- **Secondary:** price closes beyond the far side of the range (theta on near-term leg has
  collapsed or gone ITM, close early)
- **Time:** close near-term leg 1 day before expiry; don't carry into expiry day
- **Theta target on credit overlay:** close at 50–60% of max premium

**Why the lag is acceptable here:**
- Not trying to catch the range setter bar — entering after establishment
- Key level is derived from the previous range boundary, which was known before the breakout
- A 5–25+ bar range has plenty of theta to collect even after a 2-day lag at entry

---

## Validation Required Before Any Live Use

These questions need quantitative answers from the episode CSV + backtests:

1. **Key level hold rate**: across all established ranges 2023–2026, what fraction of the time
   did price not close beyond the key level (range_low for up / range_high for down) for at
   least 5 bars after commitment?
2. **Range duration distribution**: what does the bar_count distribution look like for
   established episodes? P25/P50/P75 durations set realistic expectations for theta decay.
3. **Strike buffer**: is the key level itself the right anchor or does it need a buffer of N
   points? (key level is derived from previous range boundary + wick, not always a round number)
4. **Income overlay risk**: does the far-OTM credit spread add Sharpe or just add tail risk?
5. **Existing strategy annotation**: run Athena and Apollo backtests, tag each trade with
   `range_pct`, `direction`, `episode_start` at entry. Purely observational — see whether
   range state at entry correlates with P&L outcome.

---

## Next Steps

1. **Quantify key level hold rate** ← *resume here*
   - From the episodes CSV, for each established episode calculate: did price close beyond
     the key level at any point during the episode?
   - This answers whether Range Anchor has a structural edge before touching options pricing

2. **Annotate historical Athena and Apollo backtest trades**
   - Tag each trade with `range_pct`, `direction`, `episode_start` at entry
   - Purely observational — identify whether range state at entry correlates with P&L

3. **Range duration distribution**
   - From the episodes CSV: bar_count distribution for established episodes (P25/P50/P75)
   - Informs realistic theta collection window for Range Anchor

4. **Decide use cases**
   - Based on above findings, decide which of the identified use cases to pursue first
   - Design targeted backtests for each chosen application

---

## Constraints

- No changes to production strategy files at this stage.
- Any implementation must have a dedicated backtest showing improvement before going live.
- `research/range_detection/` is a research module only — not imported by any production code.
