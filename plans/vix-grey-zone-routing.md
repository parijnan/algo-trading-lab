# Plan: VIX Grey Zone Routing — Lower Boundary to 15

**Status: TESTING — Option C (widen Athena to VIX 15) under backtest evaluation.**

---

## 1. Background

The current hard routing gate is:
- VIX < 16 → Artemis (`VIX_THRESHOLD = 16.0` in `artemis_backtest/configs.py`)
- VIX 16–25 → Athena (`VIX_FILTER_LOW = 16.0` in `athena_backtest/configs.py`)

Two weeks of live sessions surfaced the boundary problem: VIX sitting near 16 at entry
caused either (a) Athena entry with subsequent VIX fall → vega bleed losses, or (b) manual
override to Artemis when VIX then spiked → nervous management. Neither outcome is clean.

---

## 2. Grey Zone Research (2026-06-06)

**Script:** `athena_backtest/analyze_vix_grey_zone.py`
**Method:** Pure VIX analysis, 360 Wednesday entries (2019-01-30 → 2026-05-27). No strategy P&L.

### Key findings

**Frequency:** VIX lands in 15–17 at entry on 18% of Wednesdays (66/360). Post-2020 rate:
8–25% per year, roughly 7–10 entries/year.

**Base rate — coin flip:**
When VIX is 15–17 at entry, it rises the following week 44% of the time and falls 56%.
Average change: −0.07 pts. Median: −0.12 pts. The zone has essentially no directional bias.

**No signal works:**

| Signal | Rose (%) | Fell (%) |
|--------|----------|----------|
| 5d momentum UP | 43% | 57% |
| 5d momentum DOWN | 44% | 56% |
| VIX above 20d MA | 40% | 60% |
| VIX below 20d MA | 48% | 52% |
| BB upper_zone | 40% | 60% |
| BB mid_zone | 45% | 55% |
| BB lower_zone | 46% | 54% |

The only weak signal is prior-day candle direction (yesterday-up → 34% rose; yesterday-down → 53%
rose), but with n=32 per bucket this is not actionable.

**VIX persistence:** 52% of grey-zone entries still have VIX in 15–17 the next Wednesday.
29% drop to 13–15, 15% rise to 17–19.

**Conclusion:** VIX in 15–17 is genuinely unforecastable. The routing choice at 16 is a coin
flip; no direction signal changes this meaningfully. The problem cannot be solved by adding a
smarter signal.

---

## 3. Three Options

| Option | Description | Trade-off |
|--------|-------------|-----------|
| A | Keep hard 16 boundary as-is | Accept ~50% misroutes in grey zone |
| B | Skip entirely when VIX 15–17 | Forfeit ~15% of annual entries; no misroutes but less P&L |
| **C** | **Lower boundary to 15** (Athena gets 15–25, Artemis gets <15) | Reduces friction at boundary; grey zone trades shift to Athena |

**Why test Option C:**
When VIX is in 15–17, VIX is slightly more likely to fall (56%) than rise. From a vega
standpoint, falling VIX hurts Athena (long vega) and benefits Artemis (short vega). But
lowering the boundary to 15 puts the entire grey zone in Athena, making the routing decision
cleaner: the 15–16 band no longer creates a near-boundary routing ambiguity.

The empirical test is whether the grey zone trades are better run as Athena than as Artemis
over the historical period. The grey zone is a coin flip for VIX direction, but the *strategy
payoff profile* for each type of week (calm vs. volatile) may still favour one strategy.

Option C is also the simplest code change (one constant each in two config files) and is
easy to revert.

---

## 4. Test Design

### Baselines (committed)

| Strategy | Period | n | P&L (₹) | Win Rate | R:R |
|----------|--------|---|---------|----------|-----|
| Athena | 2020-01–2026-05 | 124 | +149,129 | 57.9% | 1.66 |
| Artemis Nifty | 2019-12–2025-08 | 150 | +104,092 | — | — |
| Artemis Sensex | 2025-09–2026-03 | 27 | +41,806 | — | — |
| **Combined** | | **301** | **+295,027** | | |

### Config changes for Option C

```python
# athena_backtest/configs.py
VIX_FILTER_LOW = 15.0   # was 16.0

# artemis_backtest/configs.py
VIX_THRESHOLD = 15.0    # was 16.0
```

### Scripts

- `athena_backtest/backtest_vix15.py` — runs Athena with VIX_LOW=15, saves to `data_vix15/`
- `artemis_backtest/backtest_vix15.py` — runs Artemis Nifty then Sensex with VIX_MAX=15,
  saves to `data_vix15/`

### Success criteria (pre-committed)

Option C is worth adopting if **combined P&L improves** with neither strategy suffering a
meaningful individual degradation:
- Combined P&L ≥ ₹295,027 (current combined baseline)
- Neither strategy's win rate falls by more than 3pp
- Neither strategy's R:R falls by more than 0.15

If only combined improves but one strategy's metrics deteriorate significantly, investigate
the trade-level composition before deciding.

---

## 5. Running the test

```bash
# From repo root:
python athena_backtest/backtest_vix15.py
python artemis_backtest/backtest_vix15.py
```

Each script prints a comparison table vs. its baseline at the end.

---

## 6. If Option C passes

1. Update `athena_backtest/configs.py`: `VIX_FILTER_LOW = 15.0`
2. Update `artemis_backtest/configs.py`: `VIX_THRESHOLD = 15.0`
3. Update `leto_config.py`: `VIX_ARTEMIS_MAX = 15.0` and `VIX_ATHENA_MIN = 15.0`
4. Commit with a clear "routing boundary change" message
5. Paper-trade for at least 2 sessions before going live

## 7. If Option C fails

Revisit Option B (skip 15–17 entirely). The skip approach eliminates the routing problem
at the cost of ~15% of annual entries. The P&L impact of those entries can be estimated from
the trade logs of both strategies by tagging entry_vix in the 15–17 range.

---

*Research scripts: `athena_backtest/analyze_vix_grey_zone.py`, `athena_backtest/analyze_vix_vega.py`*
*Supersedes: nothing — complements `plans/vix-router-research.md` (which tested VRP signals;
this plan tests a simpler boundary shift).*
