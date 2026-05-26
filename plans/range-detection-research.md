# Plan: Index Range Detection — Research & Applications

**Status: RESEARCH PHASE ACTIVE — validation gate is the immediate next step.**
VIX router research is complete (see `plans/vix-router-research.md` §15 — verdict: symmetric
router not supported; containment is the dominant Artemis P&L driver, ρ=0.32 p=0.0001).
Range detection is **unblocked and re-prioritised** as the primary research direction.
Next gate: key-level hold rate + duration distribution (§7).

---

## 1. Context

The daily Nifty chart regularly shows clear consolidation ranges that are visually obvious but
hard to define algorithmically because their duration is variable (2 days to 2+ weeks). Two
methods were built and compared:

- **ADX method** (`range_detector.py`, `range_detector_75min.py`): ADX < threshold = ranging
  regime; episode bounds anchored via Williams Fractal swing highs/lows.
- **PA method** (`range_detector_pa.py`): range setter candles (closes outside current range)
  define bounds; wick extension allowed; gap openings anchor the inside bound to the previous
  range's near boundary. **Clearly superior on daily data.**

Hybrid ADX+PA combinations were explored and failed — ADX as a gate suppresses good ranges.
Pure PA with a breakout-confirmation filter is the active approach.

---

## 2. The Orthogonal-Axes Finding (central reframe)

After the VIX router work, we tested whether range state is just a VIX-direction proxy. It is
**not**. Measured on the 121 annotated Athena trades:

- **Range direction does not predict the VIX move during the hold.** corr(range=down, ΔVIX)
  = **+0.03** — essentially zero. Up- and down-biased ranges both see VIX roughly flat over
  the ~week hold.
- **Yet down-biased ranges earn 2.5× the P&L** (+25.3 vs +10.2 pts avg), and this holds
  *within* every VIX-ST bucket (both_up: +23 vs +2; mixed: +30 vs +20) with the *same*
  near-zero ΔVIX.
- Therefore the down-bias edge is a **spot-containment / profit-tent effect**, not a vega
  effect — almost certainly the Indian market's structural up-drift: up-biased ranges grind
  spot up and away from the calendar center (CE side tested, parachute deploys); down-biased
  ranges mean-revert toward the strikes.
- corr(range direction, VIX-ST signal) = **+0.46** — they are *correlated at entry* (down
  market ↔ elevated VIX state) but carry independent information about outcome.

**Implication for this plan:**
- Range detection's value is the **spot/containment axis** (where spot goes relative to the
  strikes), which is **independent of vega**. This validates the whole research line.
- Emphasis shifts from range *direction* (entangled with VIX state) to range **bounds &
  containment probability** (pure price, independent).
- Do **not** re-encode range direction as a vega/VIX signal — that double-counts what the
  router already handles.

(Cross-check that also matters for the router: corr(VIX-ST, ΔVIX) = **+0.01** — entry-time
Supertrend has no power to forecast the forward VIX move. The router must use VRP /
mean-reversion validated on full VIX history, not trend signals. Recorded in the router plan.)

**Additional empirical validation (2026-05-26, from VIX router trade-level confirmation):**
The containment axis was directly measured on 150 Artemis-Nifty trades (2019–2025).
Spot-to-nearest-strike distance (min_dist_pct) at entry predicts Artemis P&L at
**ρ=0.32, p=0.0001** — the strongest single signal found in the entire router research.
VRP (the vega axis) showed ρ=-0.084, p=0.31. This empirically confirms that containment
is the dominant Artemis driver and validates the orthogonal-axes framing with hard numbers.
Note: min_dist_pct is *endogenous* (the strategy's delta-based strikes determine it), so
the range-detection research must show that an *exogenous* PA range signal adds incremental
predictive power over the strike geometry already in use — that is the testable hypothesis
for the Artemis variant backtest (§10 step 3).

---

## 3. What Was Built

### `research/range_detection/range_detector.py` — Daily (ADX method)
Reads official daily Nifty OHLC from `nifty_daily.csv`. Validated, set aside in favour of PA.

### `research/range_detection/range_detector_75min.py` — 75-min (ADX method)
1-min data resampled to 75-min. Too noisy on its own — set aside.

### `research/range_detection/resample.py` — Shared resampler
Day-anchored N-minute resampler. Supports any integer timeframe (3, 5, 15, 75 min) plus
`'daily'`. Used by `range_detector_pa.py` and `annotate_athena.py`.

### `research/range_detection/range_detector_pa.py` — All timeframes (PA method)
Price-action range detection. Core rules:
1. Close outside current range → new range setter (its H/L define new bounds)
2. New H or L without close outside → wick extension (range expands, no split)
3. Gap open outside previous range → inside bound anchored to previous range's near boundary
4. `--breakout-confirm N`: require N additional consecutive closes outside before committing.
   If price returns inside within N bars, the candidate is absorbed as a wick extension.

Established ranges are colour-coded by direction:
- **Green** (up-biased): setter broke upward; range_low = key support (thick line)
- **Red** (down-biased): setter broke downward; range_high = key resistance (thick line)
- **Blue** (initial/neutral): first episode, no prior range
- **Grey dashed** (transient): bar_count < min_range_bars

### `research/range_detection/annotate_athena.py` — trade annotation
Tags all 121 Athena trades with PA range state (`ep_direction`, `ep_committed`,
`ep_established`, `ep_entry_spot_pct`, `key_dist_pct`, range bounds/width) and VIX indicators
(`vix_st_*`, `vix_bb_*`). Output `outputs/athena_annotated.csv`; classification grid
`outputs/vix_signal_grid.csv`.

---

## 4. PA Method — Parameter Reference

| CLI flag | Default | Effect |
|---|---|---|
| `--timeframe` | `daily` | `daily` or integer minutes (`75`, `15`, `5`, `3`) |
| `--start-date` | (required) | Initial range setter: `YYYY-MM-DD` or `"YYYY-MM-DD HH:MM"` |
| `--min-range-bars` | 5 | Min bars for established range (drawn solid) |
| `--breakout-confirm` | 1 | Extra closes outside before committing a new setter (0 = immediate) |
| `--hybrid` | `none` | `none` = pure PA, `a` = ADX hard gate (deprecated — didn't work) |
| `--adx-threshold` / `--adx-period` | 20 / 14 | Only used if `--hybrid a` |
| `--months` / `--all` / `--years` | — | Display scope controls |
| `--tag` / `--no-browser` | — | Output filename suffix / headless save |

---

## 5. Breakout Confirmation — Concluded

Three variants on 2023–2026 daily data (`--min-range-bars 3`):

| Tag | `--breakout-confirm` | Episodes | Established |
|---|---|---|---|
| `conf0` | 0 | 301 | 84 |
| `conf1` | 1 | 172 | 78 |
| `conf2` | 2 | 74 | 57 |

**Winner: N=2.** Clean, durable ranges; false breakouts absorbed. Current outputs use N=2.

---

## 6. Lag Analysis

With `--breakout-confirm 2`:

1. **Confirmation lag (2 bars, by design):** setter bar + 2 confirming closes → direction
   not known until day 3.
2. **Bounds instability:** range_high/low keep expanding via wick extension through the
   episode; bounds at commitment are provisional.

**What survives the lag:**
- **Direction** reliable from day 3 (3 consecutive closes one way).
- **Key level** (range_low up / range_high down) anchored to the *previous* range's boundary
  on gap opens — known before the breakout bar, so stable post-commitment.
- **Historical annotation** has no lag issue.

**Refinement adopted for any strategy use (see new-strategy plan): slow-in / fast-out.**
Use the N=2 confirmation for *entry* (be selective), but a *raw* key-level break for *exit*
(the price level is already known — react on the close beyond it; don't wait 2–3 bars to
confirm a new episode or you eat the whole breakout loss).

---

## 7. Validation Gate (do FIRST — kills or greenlights everything downstream)

Every containment use case depends on ranges actually containing price. Answer these two from
the episodes CSV before building anything:

1. **Key-level hold rate** — across established ranges (2019–2026), what fraction stay inside
   the key level (range_low for up / range_high for down) for ≥5 bars after commitment? If
   ranges don't hold, stop — there is no containment edge.
2. **Duration distribution** — bar_count P25/P50/P75 for established episodes. Sets the
   realistic theta window and how much range remains after the 2–3 bar confirmation lag.

Both are cheap (pure pandas on `range_episodes_pa_daily.csv`) and decisive.

---

## 8. Use Cases — Re-Ranked by Fit

Ranked after the orthogonal-axes finding. Range detection owns the **containment/spot** axis;
pair it with the VIX router for the vega axis where relevant.

### Rank 1 — Artemis (Sensex iron condor): strongest, most direct
An iron condor **is** a containment bet with explicit short strikes defining a profit zone.
The detected range bounds map almost 1:1 to strike placement:
- Sell CE above `range_high` (demonstrated resistance), PE below `range_low` (demonstrated
  support). The condor's profit zone = the detected range.
- **Skip** when no established range (trending/initial) or freshly committed (bounds unstable).
- **Directional skew** from the up-drift: in a down-biased range the up-drift fights the
  downtrend so resistance tends to hold short-term (room on CE); in an up-biased range support
  is up-drift-defended (safer PE).
- Pure price — no VIX entanglement (Artemis is already VIX-gated).

**Empirical support (2026-05-26):** spot-to-nearest-strike distance (a containment proxy) predicts
Artemis P&L at ρ=0.32, p=0.0001 on 150 historical trades. This is the strongest quantitative
support for range-based Artemis enhancement. The research question is whether PA range state
at entry (exogenous to strike placement) can improve that containment margin beyond what the
current delta-based selection already achieves.

**To do:** extend `resample.py` for Sensex (trivial path change) and annotate Artemis trades.

### Rank 2 — Apollo (Nifty ITM debit spread): cleanest, fully independent
Inverse use — range detection as a **chop filter** on Apollo's dual-Supertrend signal:
- Signal fires mid-range → likely false (chop) → skip or shrink size.
- Signal coincides with a confirmed **range break** → genuine breakout → take / size up.
- The broken boundary becomes a natural SL (failed breakout = close back inside).
- No VIX overlap, no containment dependency — quickest standalone win. Annotate Apollo trades
  with `ep_entry_spot_pct` and range-break coincidence.

### Rank 3 — Athena (Nifty double calendar): complementary, spot side only
Bounds improve the *spot* axis; direction is redundant with the router (corr 0.46):
- **Strike placement:** center calendars on `range_mid` / lean toward the key level, not blind
  ATM.
- **Up-drift adjustment:** give the CE side more room in up-biased ranges (or skip up-biased
  entries near the upper bound — the down-bias P&L edge is structural).
- **Range-break exit:** spot breaking the range = containment thesis dead = exit, independent
  of the router's vega call.
- Do **not** use range *direction* as an Athena entry/VIX signal.

---

## 9. New Strategy: moved to its own plan

The "Range Anchor" concept from the prior version of this plan is **superseded** by a fuller,
vega-adaptive design in **`plans/range-vega-strategy.md`** (codename *Ares*). Summary of the
verdict that drove the move:
- A fixed short-premium-at-range-bounds strategy is ~80% Artemis. Don't build that standalone.
- The genuinely differentiated idea unifies **both research axes**: anchor to a confirmed
  range (containment) *and* pick the structure's vega sign from the VIX router (vega).
- First test the cheaper hypothesis — **range-anchored strikes as an Artemis variant** — before
  committing to a standalone engine. Full design, dependencies, and validation in the new plan.

---

## 10. Recommended Sequence

VIX router research is complete (§15 of `plans/vix-router-research.md`) — no blocker remains.

1. **Validation gate** (§7): key-level hold rate + duration distribution. Decisive, cheap.
2. If pass → **Apollo chop-filter annotation** (most independent, fastest win) **and Artemis
   annotation** (extend `resample.py` for Sensex).
3. **Artemis range-anchored-strike variant** backtest vs delta-based baseline. This is the
   highest-priority use case — containment is empirically the dominant Artemis P&L driver
   (ρ=0.32), and an exogenous range signal could improve it further.
4. **Athena**: range-break exit + range-anchored strike placement as isolated experiments.
5. Standalone vega-adaptive strategy (*Ares*) only if step 3 shows incremental P&L gain AND
   is uncorrelated with the existing book — see `plans/range-vega-strategy.md`. Note: the
   symmetric VIX router that Ares depended on is not supported; Ares's vega-adaptive
   mechanism would need a different foundation if pursued.

---

## 11. Constraints

- No changes to production strategy files at this stage.
- Any implementation must have a dedicated backtest showing improvement before going live.
- `research/range_detection/` is a research module only — not imported by production code.
- Validation gate (§7) must pass before any containment-based build.
