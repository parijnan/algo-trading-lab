# Plan: VIX-Band Parameter Optimisation — Artemis, Athena, Iris

**Status: RESEARCH PLANNED — not started**

---

## 1. Objective

Each strategy now has a fixed routing band. Parameters were tuned globally — across all
VIX levels — during initial research. This plan explores whether band-specific parameter
values improve performance in each strategy's deployed range:

- **Artemis** — VIX < 16
- **Athena** — VIX 16–25
- **Iris** — VIX > 25

---

## 2. Methodology — overfitting guards (non-negotiable)

Per-band trade counts are small (~26–37 per sub-band for Athena/Artemis, N=77 for
Iris VIX > 25). Sweeping a multi-dimensional parameter grid against N=30 will
manufacture spurious winners. This section defines the guardrails for all experiments.

### 2.1 Train/test split

- **Training period**: 2020–2023 (tune here)
- **Validation period**: 2024–present (confirm here, untouched during tuning)
- A parameter change is only accepted if it holds in the validation period.
  Fits that only appear in-sample are rejected regardless of headline improvement.

### 2.2 One parameter family at a time

Change one parameter family per experiment (e.g., index_sl only). Hold all others at
their current values. Attributing performance to a parameter requires isolating it.

### 2.3 Minimum-N gate

Do not tune a parameter band if that band has fewer than 30 trades in the training
period. Below 30, results are noise. Record the N and note it explicitly in findings.

### 2.4 Robustness-across-years check

For any candidate parameter change, break down P&L year-by-year. If the improvement
is driven by one or two years only (the Apollo VIX < 11 lesson), reject it.

### 2.5 Mechanistic rationale required

No parameter change ships on backtest delta alone. Every candidate must have a
mechanistic reason stated first (e.g., "at VIX < 12, intraday range compresses,
so a tighter index SL avoids exiting on normal noise"). The backtest confirms or
refutes the hypothesis; it does not generate the hypothesis.

---

## 3. Artemis — VIX < 16

### 3.1 Instrument ambiguity (resolve first)

Artemis is currently live on **Sensex** (since Sep 2025). The Sensex dataset has only
27 trades total — per-band parameter tuning is not viable at that N. The Nifty
dataset has ~150 valid trades (2020–2025) but Nifty is being retired as the live
instrument.

**Decision gate before any Artemis parameter work:**
- If Sensex N reaches ~100+ trades (mid-2026 at current pace), proceed with Sensex.
- Until then, Nifty parameter changes cannot be applied to the live instrument.
  Nifty tuning is academic only — do not deploy Nifty-optimised params to Sensex
  without Sensex-level validation.

Current recommendation: **defer Artemis parameter optimisation** until Sensex N ≥ 100.
File as future work.

### 3.2 Parameter inventory (when data is sufficient)

The VIX-band-aware infrastructure is already wired in `artemis_backtest/configs.py`.
`INDEX_SL_OFFSETS` and `SL_DTE_MULTIPLIERS` are per-band dicts, all currently
initialised to the same default. No structural code changes required — only values.

| Parameter | Config key | Current | Hypothesis | Priority |
|---|---|---|---|---|
| Index SL offset | `INDEX_SL_OFFSETS[band]` | 200 all bands (Sensex) | Tighter at VIX < 12 (range compresses); looser at VIX 14–16 (more movement) | High |
| Option SL multiplier | `SL_DTE_MULTIPLIERS[band][dte]` | 2.66/2.33/2.00/1.66/1.33 all bands | Looser at VIX 14–16 (options carry more premium — need more room) | Medium |
| Hedge width | `HEDGE_POINTS` | 300 Nifty / 1000 Sensex | Tighter at VIX < 12 (move potential is lower) | Medium |
| Min gap from spot | `MINIMUM_GAP` | 350 Nifty / 1000 Sensex | Narrower in low-VIX (spot range smaller) | Low |
| Entry premium filter | `EXPECTED_PREMIUM` | 30 Nifty / 120 Sensex | Skip days where available premium is too thin for the SL ratio to work | Low |

### 3.3 Sweep approach (when authorised)

1. Filter dataset to Artemis routing band (VIX 11–16 for Nifty analysis; full range
   for Sensex once N ≥ 100).
2. Run `INDEX_SL_OFFSETS` sweep first (single parameter, highest hypothesised impact).
3. Validate against train/test split. Only proceed to `SL_DTE_MULTIPLIERS` if index SL
   improvement is confirmed in validation period.

---

## 4. Athena — VIX 16–25

### 4.1 Data sufficiency

Athena has **302 trades** across VIX 16–25 (all Nifty), which is the most usable
surface. Sub-band breakdown from routing research:

| Sub-band | N |
|---|---|
| VIX 16–18 | 31 |
| VIX 18–20 | 30 |
| VIX 20–22 | 37 |
| VIX 22–25 | 26 |

Sub-band tuning is marginal at these Ns. Start with full-band (16–25) experiments.
Only move to sub-band if full-band results justify it and N per sub-band permits.

### 4.2 Parameter inventory

#### Entry — delta targeting
`VIX_DELTA_BANDS` is currently flat at 0.30 across all bands.

| Parameter | Config key | Current | Hypothesis | Priority |
|---|---|---|---|---|
| Sell delta | `VIX_DELTA_BANDS` | 0.30 all | Lower VIX → sell closer (0.30 justified); higher VIX → sell further out (0.25) to reduce assignment risk when vol is elevated | Medium |

Candidate: `[(18, 0.30), (20, 0.28), (22, 0.26), (25, 0.25)]`
Counter-candidate: `[(18, 0.32), (20, 0.30), (22, 0.30), (25, 0.28)]` (sell closer when vol is high to collect more premium, accept higher delta exposure)
These are contradictory hypotheses — the backtest resolves them.

#### Exit — index SL (currently disabled)
`ENABLE_INDEX_SL = False`. This is a genuine unexplored surface.

| Parameter | Config key | Current | Hypothesis | Priority |
|---|---|---|---|---|
| Index SL | `ENABLE_INDEX_SL` + `INDEX_SL_OFFSET` | Disabled | Enable with a wide offset (100–150 pts) to catch directional breaks without over-trading. At VIX 22–25, wider offset needed since spot oscillates more. | High |

Candidate offsets: 50 / 75 / 100 / 150 pts. Run full-band first; sub-band only if confirmed.

#### Exit — option SL (currently disabled)
`ENABLE_OPTION_SL = False`.

| Parameter | Config key | Current | Hypothesis | Priority |
|---|---|---|---|---|
| Option SL multiplier | `ENABLE_OPTION_SL` + `OPTION_SL_MULTIPLIER` | Disabled | 3× is loose enough to avoid whipsaw but caps runaway loss when one leg moves strongly | Medium |

Candidate multipliers: 2.0 / 2.5 / 3.0 / 4.0. Note: enabling both index SL and option SL
simultaneously inflates the search space — test independently first.

#### Safety wings
`SAFETY_WING_DELTA = 0.05` — active.

| Parameter | Config key | Current | Hypothesis | Priority |
|---|---|---|---|---|
| Wing delta | `SAFETY_WING_DELTA` | 0.05 | At VIX 22–25, tighter wing (0.07) gives more protection; at VIX 16–18, wider wing (0.03) is cheaper with little additional risk | Low |

#### Emergency hedge (CE parachute)
Active with fixed params. These interact with TRIPLE_CONFIRM integration (see
`plans/tc-live-integration.md`) — do not change until TC integration scope is settled.

| Parameter | Config key | Current | Hypothesis | Priority |
|---|---|---|---|---|
| CE trigger offset | `EMERGENCY_TRIGGER_OFFSET` | −150 pts past CE strike | At VIX 16–18, 150pt move is rare — trigger fires too late; tighten to −100. At VIX 22–25, −200 avoids chasing whipsaws. | Medium |
| CE hedge delta | `EMERGENCY_HEDGE_DELTA` | 0.35 | Higher delta (0.40) at VIX 22–25 for more protection when vol is already elevated | Low |
| PE trigger offset | `PE_EMERGENCY_TRIGGER_OFFSET` | −150 pts past PE strike | Symmetric to CE — same sub-band logic applies | Medium |

#### Calendar roll threshold
| Parameter | Config key | Current | Hypothesis | Priority |
|---|---|---|---|---|
| Buy leg min DTE | `BUY_LEG_MIN_DTE` | 16 | At VIX 22–25, rolling to 21-DTE month earlier captures more theta spread from the wider vol surface | Low |

### 4.3 Recommended experiment sequence

1. `ENABLE_INDEX_SL` with 100pt offset — highest unexplored surface, clean hypothesis
2. `VIX_DELTA_BANDS` — entry parameter, independent of SL
3. `ENABLE_OPTION_SL` — only after index SL result is known; test independently
4. `EMERGENCY_TRIGGER_OFFSET` — only after TC integration scope is resolved
5. `SAFETY_WING_DELTA` / `BUY_LEG_MIN_DTE` — low priority, defer

---

## 5. Iris — VIX > 25

### 5.1 Data sufficiency

Iris operates as a single band (VIX > 25), not subdivided. N=77 in the routing
research backtest. The existing sweep infrastructure in
`iris_backtest/research/run_strategy_backtest.py` ran across all VIX levels. The
key question is whether the optimal params differ when filtered to VIX > 25 only.

### 5.2 Parameter inventory

#### Exit — stop/target/max-hold (sweep already exists)

| Parameter | Script var | Current | Hypothesis | Priority |
|---|---|---|---|---|
| Stop loss | `STOP_GRID` | 20% (from prior sweep) | VIX > 25 options are expensive — wider SL (25–30%) may be appropriate to avoid premature stop-outs on fast intraday moves | High |
| Profit target | `TARGET_GRID` | 30% (from prior sweep) | Target may need to move up at high VIX (options can run further in trending moves) | High |
| Max hold | `MAX_HOLD_GRID` | 60 min | High-VIX trend moves can be fast — a 30-min cap may improve expectancy by cutting indecisive trades | Medium |

Action: add a `vix_filter` argument to `run_strategy_backtest.py` so the sweep can
be run against `entry_vix > 25` trades only. Compare optimal params to the
all-VIX baseline; if they diverge materially, apply VIX-gated config to production.

#### Entry — ITM depth

| Parameter | Hardcoded location | Current | Hypothesis | Priority |
|---|---|---|---|---|
| ITM depth | `_itm150_strike()` in `run_strategy_backtest.py` | 150 pts (3 × STRIKE_STEP) | At VIX > 25, deeper ITM (200 pts = 4×) reduces gamma risk on fast moves; shallower ITM (100 pts = 2×) collects more on successful trades | Medium |

Action: parameterise `_itm150_strike()` with an `itm_depth` argument and add it to
the sweep grid. Test 100 / 150 / 200 pts.

#### Entry — time-of-day dead zone

`SKIP_ENTRY_WINDOWS = [('10:45', '11:30')]` was derived from all-VIX data.

| Parameter | Config key | Current | Hypothesis | Priority |
|---|---|---|---|---|
| Skip windows | `SKIP_ENTRY_WINDOWS` | 10:45–11:30 | High-VIX days may have a different dead zone — post-opening vol can remain elevated longer, or the dead zone may not apply at all | Low |

Action: run time-of-day WR breakdown (same analysis that produced the current window)
filtered to VIX > 25 only. If the dead zone is different, update config.

### 5.3 Recommended experiment sequence

1. VIX > 25 filtered stop/target/max-hold sweep — lowest code change, highest impact
2. ITM depth sweep — parameterise `_itm150_strike()` and add to grid
3. Time-of-day dead zone check — diagnostic only, one script run

---

## 6. Sequencing across strategies

| Order | Strategy | Experiment | Blocker |
|---|---|---|---|
| 1 | Iris | VIX > 25 filtered stop/target/max-hold sweep | None — run now |
| 2 | Iris | ITM depth parameterisation + sweep | Minor code change |
| 3 | Athena | Enable index SL — full-band sweep | None |
| 4 | Athena | Delta band tuning | None |
| 5 | Athena | Option SL — full-band sweep | Run after index SL result |
| 6 | Artemis | All parameter work | Blocked: wait for Sensex N ≥ 100 |

---

## 7. Files and outputs

| File | Purpose |
|---|---|
| `iris_backtest/research/run_strategy_backtest.py` | Add `vix_filter` + `itm_depth` params |
| `iris_backtest/data/strategy_sweep_vix25.csv` | VIX > 25 sweep output |
| `athena_backtest/configs.py` | `ENABLE_INDEX_SL`, `VIX_DELTA_BANDS` updates |
| `athena_backtest/data/trade_summary_index_sl_sweep.csv` | Index SL sweep output |
| `artemis_backtest/configs.py` | `INDEX_SL_OFFSETS`, `SL_DTE_MULTIPLIERS` updates (deferred) |

---

## 8. Go/no-go gates per experiment

For each experiment, the result is accepted only if:

- [ ] Improvement holds in validation period (2024–present)
- [ ] Year-by-year breakdown shows improvement is not concentrated in 1–2 years
- [ ] Mechanistic rationale stated before the run and confirmed (not contradicted) by results
- [ ] N in the relevant band is ≥ 30 in training period
- [ ] Change is tested in isolation (other params held fixed)
