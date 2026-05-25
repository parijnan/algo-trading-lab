# Plan: Athena Entry Filter — VIX Signal Research

**Status: Research phase complete. Backtest implementation next.**

---

## Context

Started as part of range-detection research (see `plans/range-detection-research.md`): after
annotating 121 historical Athena trades with PA range state, the up-biased cohort (61 trades,
50.8% win) was significantly weaker than the down-biased cohort (60 trades, 65% win). The
investigation into why led to VIX behaviour analysis, which is now the primary active thread.

---

## Key Finding: Asymmetric VIX Signal

Athena calendars are **net long vega** (far-month buys carry more time than near-month sells).
This means:

- **Down-biased market + rising VIX**: both forces work in the same direction — spot moving
  toward the PE calendar sweet spot while IV expands. The strongest trades in the dataset are
  in this regime.
- **Up-biased market + mid-band rising VIX**: the danger zone. When VIX is trending up on
  both timeframes but not yet stretched to the BB upper band, there is room for VIX to reverse
  downward as the market continues higher. IV crush on the PE calendar is the primary loss
  mechanism in this cohort.

The same VIX signal (`both_up` Supertrend) means **opposite things** depending on market
direction:

| Direction | VIX ST | Result |
|---|---|---|
| Down-biased | both_up + upper_zone | **77.8% win, +598 pts** (18 trades) — elite |
| Down-biased | both_up + mid_zone | 55.6% win, +251 pts — acceptable |
| Up-biased | both_up + upper_zone | 46.2% win, +244 pts — marginal |
| Up-biased | both_up + mid_zone | **22.2% win, −194 pts** (9 trades) — skip candidate |

---

## Signal Definition

Two VIX indicators computed at entry (10:30 Wednesday), using only information available at
that moment:

### 1. Dual-Timeframe VIX Supertrend
- **Daily** (`vix_st_daily`): previous completed trading day's bar. Period = 7, multiplier = 3.0.
  Direction = `'up'` if close ≥ Supertrend value, else `'down'`.
- **75-min** (`vix_st_75m`): 09:15→10:29 bar on entry day (complete before 10:30 entry).
  Period = 10, multiplier = 3.0.
- **Combined** (`vix_st_signal`): `'both_up'` | `'mixed'` | `'both_down'`.

### 2. VIX Bollinger Bands %B
- **Daily** (`vix_bb_pct`): %B = (close − lower_band) / (upper_band − lower_band).
  Period = 20, 2 standard deviations. Uses previous completed daily bar.
- **Zone** (`vix_bb_zone`): `above_upper` (>1.0) | `upper_zone` (0.7–1.0) | `mid_zone`
  (0.3–0.7) | `lower_zone` (0.0–0.3) | `below_lower` (<0.0).

Both signals are already computed in `research/range_detection/annotate_athena.py` and
stored as annotation columns in `outputs/athena_annotated.csv`.

---

## Proposed Entry Skip Condition

```
skip if:
    ep_direction == 'up'          # market in an up-biased range at entry
    AND vix_st_signal == 'both_up' # VIX trending up on both daily + 75-min
    AND vix_bb_zone  == 'mid_zone' # VIX not yet stretched (has room to reverse)
```

**Impact on 121-trade baseline:**

| | Trades | Win% | Total pts |
|---|---|---|---|
| Baseline | 121 | 57.9% | +2,141.55 |
| With filter | 112 | ~60% | ~+2,336 (estimated) |
| Removed (skip bucket) | 9 | 22.2% | −194.35 |

The 9 removed trades average −21.6 pts each; the retained set improves per-trade quality
without removing any good trades.

**Why not apply symmetrically to down-biased trades?**
Down-biased + both_up is strongly positive (VIX rising as market falls = long vega + spot
direction aligned). Applying the filter to down-biased trades would remove +251 pts of solid
trades, netting a worse result than the baseline.

---

## VIX Behaviour During Trade Hold (the 25 up-biased + both_up trades)

Trade-level VIX analysis (entry vs exit) confirmed the mechanism:
- **Winners** (10): avg VIX change **+1.28** — VIX kept rising during the hold
- **Losers** (15): avg VIX change **−0.94** — VIX reversed and fell (IV crush)

The Supertrend captures the trend direction at entry but not whether the trend is exhausted.
The BB %B adds the "room to run vs stretched" dimension:
- `mid_zone` at entry = VIX still mid-rally, vulnerable to reversal if market continues up
- `upper_zone` at entry = VIX already elevated, tends to stay elevated or revert slowly

---

## Next Steps

1. **Implement filter in `backtest.py`** (Phase 2.2)
   - Load 1-min VIX data; compute daily ST and BB at entry time (no lookahead: prev day's bar)
   - Load 75-min VIX data; compute ST on the 09:15→10:29 bar of entry day
   - Add skip condition as described above
   - Run full backtest and verify: target ~112 trades, better per-trade stats
   - Do NOT change any other strategy logic — isolated experiment

2. **Parameter sensitivity**
   - BB period: 15 / 20 / 25 — does the mid_zone boundary shift significantly?
   - ST parameters: daily (7,3) and 75m (10,3) are conventional; check (10,3) daily and (7,2) 75m
   - BB std: 1.5 / 2.0 / 2.5 — does the zone classification change material outcomes?

3. **Sample size validation**
   - 9 trades is a small sample for the skip bucket. The effect size (−194 pts, 22% win) is
     strong, and the mechanism is coherent. But confirming with Artemis data (if applicable)
     or waiting for more data would strengthen the case before production wiring.

4. **Production wiring** (after backtest confirms)
   - Add VIX signal checks to the live entry logic
   - Compute signals from live 1-min VIX feed at the time of entry decision

---

## Constraints

- No production changes until Phase 2.2 backtest confirms the finding in the live engine.
- The research signal is already at correct timing (no lookahead) — the implementation
  translation is direct, not an approximation.
- Keep the filter as a named config flag (`ENABLE_VIX_ENTRY_FILTER = True/False`) so it
  can be toggled for comparison in future runs.
