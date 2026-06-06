# Plan: TRIPLE_CONFIRM Integration — Artemis and Athena

## Status: Artemis PC1–PC4 Complete (+5.8% improvement) · Athena PC1–PC3 Complete (+11.1% improvement after lookahead fix, outlier-driven)

---

## Context

TRIPLE_CONFIRM is a three-layer alignment signal developed in `iris_backtest/`:
- **Layer 1 (day filter):** ORB_75 has already broken in direction D (market has committed)
- **Layer 2 (regime):** ST_FAST has flipped in direction D within ±15 min (5-min+15-min trend change)
- **Layer 3 (entry):** ST_RAPID flips in direction D (3-min+9-min confirmation)

Quality on Nifty 1-min (2019–2026, 134 fires / 18.3 per year):
- WR 72.9% at 5-min, **83.1% at 15-min**, 71.7% at 30-min
- RR 3.50 at 5-min, **4.53 at 15-min**, 3.70 at 30-min
- Median close +6.7 pts at 5-min, **+13.2 pts at 15-min**
- Move completes within 60 min — WR drops to 50% at 120-min

The signal is too infrequent for a standalone scalping strategy (18/year) but its directional
quality makes it a candidate for pre-emptive adjustment triggers within Artemis and Athena,
which are already actively managed strategies with defined adjustment mechanisms.

Validation on Sensex is in `iris_backtest/research/validate_sensex.py`.

---

## Application to Artemis

### Current behaviour
Artemis adjusts reactively — when `index_sl` is breached, it exits the tested side and rolls
the other side inward. Adjustment happens after the move has already started; the exit price
on the tested side is worse than if the exit had been made earlier.

### Proposed use of TRIPLE_CONFIRM
When TRIPLE_CONFIRM fires **against** an open Artemis leg during a live session:
- Fire is bullish + CE sell strike is at risk → exit CE spread early, before `index_sl` fires
- Fire is bearish + PE sell strike is at risk → exit PE spread early, before `index_sl` fires

The signal fires ~18 times/year; Artemis runs ~40 sessions/year. In any given session (Mon
entry → Thu expiry), TRIPLE_CONFIRM may fire 0-2 times. On days when it fires in a direction
that threatens an open leg, acting on it should produce a better exit price than waiting for
the SL to trigger.

### Key backtest question
Does pre-emptive exit on TRIPLE_CONFIRM produce better average P&L per adjusted trade than
the current reactive SL-triggered exit? Needs Artemis backtest with TRIPLE_CONFIRM wired as
an additional exit path alongside the existing SL logic.

### Caveats
- TRIPLE_CONFIRM is calibrated on **Nifty**. Artemis trades **Sensex**. High correlation
  but not identical — the signal must be validated on Sensex data before production use.
  Initial validation in `validate_sensex.py`.
- 17% false-signal rate at 15-min: roughly 1 in 6 TRIPLE_CONFIRM fires will not produce the
  expected directional move. An early exit on a false signal = unnecessary adjustment P&L drag.
- Does not replace `index_sl` — it is a supplementary early-warning trigger only.

---

## Application to Athena

### Terminology

| Term | Definition |
|---|---|
| **PE safety wing** | 0.05-delta monthly PE bought at entry; held to ELM. Guards against gap-down events on the PE leg. |
| **CE parachute** | 0.35-delta monthly CE bought when `spot > ce_sell_strike + 150`. Guards against extreme upward trends. Exits on reversal (`spot <= ce_sell_strike`). Max 1 attempt. |
| **PE parachute** | 0.35-delta monthly PE. **Does not exist in the baseline.** TC track introduces it as a downside trend hedge, triggered only by bearish TC. Distinct from the PE wing — guards against a sustained downward trending move, not just gap-down events. |

### Current behaviour (baseline)

**CE side:** CE parachute triggers reactively when `spot > ce_sell_strike + 150`. Exits
reactively when `spot <= ce_sell_strike`. Max 1 attempt. No CE wing.

**PE side:** PE safety wing (0.05-delta monthly PE) bought at entry, held to ELM. No PE
parachute — backtests showed no benefit to a reactive spot-triggered PE parachute analogous
to the CE one.

### Proposed use of TRIPLE_CONFIRM

TC introduces two new mechanics — one additive (CE parachute early trigger), one entirely new
(PE parachute):

---

**Bullish TC fire — CE parachute early entry**

If no CE parachute is active: enter CE parachute before spot crosses the +150 threshold. Entry
premium is lower (spot hasn't moved yet). Reactive spot trigger remains as fallback — if spot
crosses first, enter normally.

*False signal exit:* same mechanisms as the reactive path — exit when `spot <= ce_sell_strike`,
OR exit pre-emptively when a bearish TC fires while the CE parachute is active (symmetric with
the entry trigger). In both cases the attempt cap (`EMERGENCY_MAX_ATTEMPTS = 1`) still applies.

---

**Bearish TC fire — PE parachute entry + wing close**

Enter a 0.35-delta monthly PE parachute. This is the **only** trigger for the PE parachute —
there is no spot-based reactive PE parachute in the TC track.

When the PE parachute is entered, the PE safety wing becomes redundant: the parachute covers
both the trending-move risk and the gap-down risk simultaneously. Close the PE safety wing at
current market price (likely at a small gain — spot has moved down, wing premium has increased).

*False signal exit:* if the bearish TC turns out to be wrong and spot reverses upward, a
subsequent bullish TC fires → exit the PE parachute AND buy back the PE safety wing (restoring
gap-down protection). As an additional safety, exit the PE parachute and rebuy the wing if
`spot >= pe_sell_strike` (symmetric with the CE parachute's `spot <= ce_sell_strike` exit).

### Key design decisions for the backtest

1. **CE parachute: TC supplements, does not replace the spot trigger.** Whichever fires first
   (TC or spot crossing +150) enters the parachute. This mirrors the Artemis pattern.

2. **PE parachute: TC only.** No spot-based reactive PE parachute. Bearish TC is the sole
   entry trigger.

3. **Wing close/rebuy is atomic with PE parachute entry/exit.** When PE parachute enters,
   wing closes in the same candle. When PE parachute exits (TC or spot reversal), wing is
   rebought in the same candle.

4. **Attempt cap applies to both parachutes independently.** `EMERGENCY_MAX_ATTEMPTS = 1` per
   side per trade. A TC-triggered CE parachute entry consumes the CE attempt; a TC-triggered
   PE parachute entry has its own independent counter.

5. **PE wing rebuy after PE parachute exit uses the same delta target (0.05) as the original
   entry** — the nearest available monthly PE at 0.05 delta at the time of rebuy.

### Key backtest question
Does TC-timed parachute management — earlier CE parachute entry on upward trends, and a new
TC-only PE parachute on downward trends — improve overall Athena P&L vs the baseline?

Expected benefit on CE side: lower entry premium on true bullish TC signals.
Expected cost on CE side: unnecessary CE parachute entries on false bullish TC signals.

Expected benefit on PE side: on true bearish TC signals, the PE parachute captures trending
downside gains that the baseline cannot (the PE wing provides only gap protection, not
trend protection). Wing close at a gain partially offsets the parachute cost.
Expected cost on PE side: false bearish TC → PE parachute entered and exited at a loss, wing
closed and rebuyed incurring round-trip slippage.

### Caveats
- TRIPLE_CONFIRM is calibrated on Nifty; Athena trades Nifty — direct applicability, no
  cross-instrument adjustment needed.
- False-signal downside is bounded: each parachute has its own attempt cap, and the PE wing
  rebuy mechanism restores gap-down protection after any PE parachute exit.
- The PE parachute + wing interaction adds execution complexity (3 orders in one candle on
  PE parachute entry: buy PE parachute, sell PE wing; 2 orders on exit: sell PE parachute,
  buy PE wing). Must be reflected accurately in the backtest execution model.
- Athena sessions run ~40/year. TC fires ~18/year on Nifty. Signal funnel analysis will
  quantify how many fires fall inside active sessions.

---

## Known Limitation: Does Not Catch Sudden Midday Moves

TRIPLE_CONFIRM **requires ORB_75 to have fired first** — meaning the market must have
already broken its 09:15–10:29 opening range in the signal direction before the triple
alignment can occur. On days where Sensex ranges quietly through the morning and then spikes
sharply in the afternoon (e.g., 2026-06-02 — sudden CE SL hit at 12:50 after a quiet
morning), TRIPLE_CONFIRM will not fire. The ORB_75 confirmation either never precedes the
ST signals, or fires simultaneously with them during the spike itself.

This is structural, not a parameter issue. The signal is designed for **gradual,
well-telegraphed trending days** where directional commitment builds through the morning
session. It misses sudden midday reversals and gap-continuation sessions.

**Implication for Artemis:** TRIPLE_CONFIRM is not a substitute for the index_sl. It is an
early-warning signal on specifically trending days. The sudden-move scenario (June 2 type)
still requires the existing reactive SL mechanism. TRIPLE_CONFIRM adds value on trending days
as a pre-emptive trigger; it provides no additional protection on sudden-move days.

---

## Pre-conditions Before Implementation (either strategy)

### Artemis — All Complete

1. **Sensex validation complete** (for Artemis): `validate_sensex.py` must show TRIPLE_CONFIRM
   on Sensex fires in the same situations as on Nifty and with comparable quality.
   ✅ **DONE** — PC1 cleared. Sensex WR 76.9%, RR 4.26 @ 15min, 13.9 fires/year.

2. **Strategy-level backtest**: run TRIPLE_CONFIRM as an additional trigger through the full
   Artemis backtest and compare cumulative P&L vs baseline.
   ✅ **DONE** — PC2 Artemis complete. Results below.

3. **Timing analysis**: quantify how early TRIPLE_CONFIRM fires before a typical SL trigger.
   ✅ **DONE** — PC3 complete. Results below.

4. **False-signal cost**: model the P&L impact of the ~17–23% false-signal rate.
   ✅ **DONE** — PC4 complete. Results below.

### Athena — In Progress

1. **Instrument validation**: TC is calibrated on Nifty; Athena trades Nifty.
   ✅ **DONE** — direct applicability, no cross-instrument adjustment needed.

2. **Signal funnel analysis**: of the ~18 Nifty TC fires/year, how many fall inside an
   active Athena session?
   ✅ **DONE** — 43 TC fires across 121 matched trades (2020–2026): 24 CE-early, 23 PE-triggered,
   4 both. ~7 TC-fired trades/year.

3. **Strategy-level backtest**: run `athena_backtest/backtest_tc.py` with TC as an additional
   parachute trigger; compare total P&L, parachute entry/exit prices, and false-signal cost
   vs baseline `backtest.py`.
   ✅ **DONE** — `backtest_tc.py` run 2026-06-05. Results: see PC2 Athena section below.

4. **Timing analysis (PC3-equivalent)**: Athena TC chutes are held to ELM unless bearish/bullish
   TC fires — no "lead time before reactive threshold" concept applies (TC exit is TC-only).
   ✅ **DONE** — N/A for Athena design; CE chutes from reactive spot trigger still use baseline exit.

5. **False-signal cost (PC4-equivalent)**: 37.2% of TC-fired trades worse than baseline; avg drag
   per losing TC trade: −57.5 pts. Offset by avg gain on improving trades: +82.1 pts.
   ✅ **DONE** — see PC2 Athena section below.

### PC3 Timing Analysis Results

TC lead time before index_sl/option_sl, categorised across all TC-fired weeks:

**Nifty (18 TC fires, 8 true signals where SL would have fired)**

Lead time distribution is **bimodal** — no middle ground:

| Cluster | Count | Lead time | What it means |
|---------|-------|-----------|---------------|
| Same-day | 4 | 13–53 min | TC fires intraday during a trending session, 13–53 min before index_sl triggers |
| Cross-day | 4 | 19–22 hrs | TC fires Monday/Tuesday afternoon; SL would have fired at next morning's 09:15 gap-open |

True signal P&L delta: **+153.60 pts** across 8 weeks (TC exits early → adjustment captures more)  
False signal (10 fires, no SL would have fired): 4 better / 6 worse, net **−61.34 pts**

**Sensex (2 TC fires, both true signals, both cross-day)**

| Expiry | Lead time | Delta |
|--------|-----------|-------|
| 2025-11-13 | 42.9 hrs (Mon → Wed 09:15) | −84.32 pts |
| 2025-12-18 | 19.9 hrs (Tue → Wed 10:06) | +166.60 pts |

**Signal funnel — why only ~20 of 110 signals apply to Artemis:**

| Filter | Nifty (98) | Sensex (12) | Combined |
|--------|-----------|-------------|----------|
| Outside holding window (Thu/Fri/pre-entry) | 41 (42%) | 8 (67%) | 49 (45%) |
| VIX-skipped week | 22 (22%) | 0 (0%) | 22 (20%) |
| Eligible (inside traded week window) | 35 (36%) | 4 (33%) | 39 (35%) |
| — threatened side already closed by prior SL | 12 | 2 | 14 (36% of eligible) |
| — **truly actionable** | **23** | **2** | **25 (64% of eligible)** |

The "already closed" cases are weeks where index_sl or option_sl fired on the threatened side earlier the same session — TC's three-layer alignment completes too late for those hard intraday moves, consistent with the known limitation.

**Key finding:** TC lead time is never marginal. It either fires 13–53 min before an intraday SL (meaningful same-session advantage) or fires the prior afternoon before a gap-open SL (overnight protection). The cross-day cluster is where the largest P&L swings occur — both the biggest win (+166.60) and biggest loss (−84.32).

The false-signal drag (−61.34 pts Nifty) is the cost of TC exiting a leg pre-emptively when the market doesn't follow through. This is PC4 territory.

### PC4 False-Signal Cost Analysis (Nifty, 18 TC fires)

**Per-fire averages:**

| Category | Fires | Avg delta | Win rate | Net total |
|----------|-------|-----------|----------|-----------|
| True signal (SL would have fired) | 8 | **+19.20 pts** | 88% (7/8) | +153.60 pts |
| False signal — survived to expiry | 7 | +2.04 pts | 3/7 | +14.29 pts |
| False signal — closed at ELM | 3 | −25.21 pts | 1/3 | −75.63 pts |
| **All false signals** | **10** | **−6.13 pts** | 4/10 | **−61.34 pts** |
| **All TC fires** | **18** | **+5.13 pts** | — | **+92.26 pts** |

True/false ratio: **3.13×** — true signal gain per fire is 3x the false signal cost per fire.
Even at a 50% false rate the strategy is net positive: 0.5 × 19.20 − 0.5 × 6.13 = +6.54 pts/fire.

**Key finding — "closed at ELM" is the costly false signal class:**
- TC exits a threatened leg early, but the market partially reverses before ELM
- The leg would have recovered time-decay value by ELM; early exit locks in a worse price
- Average cost: −25.21 pts/fire vs nearly neutral "survived to expiry" cases (+2.04 pts/fire)

**"Survived to expiry" false signals are nearly neutral (+2.04 avg):** TC exits early and
adjusts the surviving leg — the adjustment partially or fully compensates for the unnecessary
early exit, leaving the net delta close to zero.

**Overall verdict:** TC is net positive on Artemis Nifty at +5.13 pts per TC fire, with true
signal gain dominating false signal drag by 3:1 on a per-fire basis.

### PC2 Artemis Results (matched-week comparison, expiry-merged)

| Instrument | Matched weeks | Baseline P&L | TC P&L | Net delta | TC fires | TC better | TC worse |
|------------|--------------|-------------|--------|-----------|----------|-----------|----------|
| Nifty      | 150          | 1601.43 pts | 1693.67 pts | **+92.26 pts** | 18 | 11/18 | 7/18 |
| Sensex     | 27           | 2090.35 pts | 2172.63 pts | **+82.28 pts**  |  2 |  1/2  |  1/2  |

All delta comes from TC-fired weeks (non-fired weeks are identical to baseline by construction).
Sensex sample is too small (2 fires) to be statistically meaningful.
Nifty: 11/18 (61%) TC better on fired weeks — directionally positive but PC3/PC4 required
before concluding the signal is net-beneficial in production.

---

### PC2 Athena Results (matched-week comparison, 2020–2026)

> **⚠ Lookahead fix applied 2026-06-06**: Original date-keyed signal map allowed candles to see TC
> signals that hadn't fired yet on that date. Fixed to timestamp-keyed (same day + signal_ts ≤ ts).
> Results below reflect the corrected backtest.

**Overall (124 trades, baseline re-run 2026-06-05):**

| Metric | Baseline | TC (corrected) | Delta |
|--------|----------|----------------|-------|
| Total P&L (₹) | +149,130 | +165,620 | **+₹16,490 (+11.1%)** |

*Previous (lookahead-inflated) figure was +₹82,966 (+55.6%) — overstated by ~5×.*

**TC fires: 36 / 124 trades**
- CE early entries: 20 · PE chute triggered: 17
- TC-fired win rate (delta positive): **15/36 = 41.7%** — TC worse on majority of fired trades

**Concentration (corrected):**
- Jan 27 2021 entry: +₹30,628 delta (CE early, Budget rally — TC fired 2021-02-01 12:48)
- Jan 19 2022 entry: +₹20,907 delta (PE chute, Jan selloff — TC fired 2022-01-20 13:00)
- Top 2 subtotal: **+₹51,535** (312% of total delta)
- All other 34 fires: **−₹35,045** (net-negative)

**Verdict for Athena (corrected):** TC is marginal and outlier-driven. The non-outlier fires are net-negative
(−₹35,045 across 34 trades), meaning the signal is a net drag on most trades it fires on. The two macro-event
trades (Budget 2021, Jan 2022 selloff) alone generate more than the total improvement. Live edge is not
established. Further investigation required before any production consideration.

---

## Implementation Sketch

### Backtest: `athena_backtest/backtest_tc.py`

Isolated parallel track — same constraint as Artemis: never modifies `backtest.py` or
`configs.py`. Imports all helpers from `backtest.py`. Writes output to `data_tc/` (gitignored).

**TC signal loading** (same pattern as Artemis):
```python
from configs import REPO_ROOT
import os, pandas as pd

TC_SIGNALS_CSV = os.path.join(REPO_ROOT, 'iris_backtest', 'data',
                               'TRIPLE_CONFIRM_excursions.csv')
TC_DATA_DIR    = os.path.join(os.path.dirname(__file__), 'data_tc')
TC_SUMMARY     = os.path.join(TC_DATA_DIR, 'trade_summary_tc.csv')
BASELINE_SUMMARY = os.path.join(os.path.dirname(__file__), 'data',
                                 'trade_summary.csv')   # read-only reference

def _build_tc_signal_map(tc_csv):
    df = pd.read_csv(tc_csv, parse_dates=['time'])
    df['date'] = df['time'].dt.date
    return dict(zip(df['date'], df['direction']))   # date → 'bullish' | 'bearish'
```

**`check_tc_ce()` and `check_tc_pe()` — one function per side**:
```python
def check_tc_ce(ts, tc_signal_map, ce_chute_active, ce_chute_attempts, max_attempts):
    tc_dir = tc_signal_map.get(ts.date())
    if tc_dir == 'bullish' and not ce_chute_active and ce_chute_attempts < max_attempts:
        return 'ce_chute_enter'
    if tc_dir == 'bearish' and ce_chute_active:
        return 'ce_chute_exit'
    return None

def check_tc_pe(ts, tc_signal_map, pe_chute_active, pe_chute_attempts, max_attempts):
    tc_dir = tc_signal_map.get(ts.date())
    if tc_dir == 'bearish' and not pe_chute_active and pe_chute_attempts < max_attempts:
        return 'pe_chute_enter'   # buy PE parachute + close PE wing
    if tc_dir == 'bullish' and pe_chute_active:
        return 'pe_chute_exit'    # sell PE parachute + rebuy PE wing
    return None
```

Note: simultaneous CE and PE actions on the same TC fire are theoretically possible but
occurred at most once in 6 years of data (2021-11-25/26, conditional on an overnight
parachute staying active). Not worth special-casing — the two functions are independent and
the main loop handles each result separately in the same candle.

**Main loop modifications** (inside the candle-by-candle iteration):
```python
exec_ts = ts + pd.Timedelta(minutes=1)

ce_action = check_tc_ce(ts, tc_signal_map, ce_chute_active, ce_chute_attempts, EMERGENCY_MAX_ATTEMPTS)
if ce_action == 'ce_chute_enter':
    ce_chute_active = True; ce_chute_attempts += 1  # buy CE parachute
elif ce_action == 'ce_chute_exit':
    ce_chute_active = False                          # sell CE parachute

pe_action = check_tc_pe(ts, tc_signal_map, pe_chute_active, pe_chute_attempts, EMERGENCY_MAX_ATTEMPTS)
if pe_action == 'pe_chute_enter':
    pe_chute_active = True; pe_wing_active = False; pe_chute_attempts += 1  # buy PE parachute + sell wing
elif pe_action == 'pe_chute_exit':
    pe_chute_active = False; pe_wing_active = True                          # sell PE parachute + rebuy wing

# Reactive CE spot check still runs if no TC CE action this candle:
if not ce_chute_active and ce_chute_attempts < EMERGENCY_MAX_ATTEMPTS:
    if spot > ce_sell_strike + EMERGENCY_TRIGGER_OFFSET:   # +150 pts
        # normal CE parachute entry ...

# PE spot-based parachute exit (if spot reverses past pe_sell_strike):
if pe_chute_active and spot >= pe_sell_strike:
    pe_chute_active = False; pe_wing_active = True  # sell PE parachute + rebuy wing
```

**Output columns added to `trade_summary_tc.csv`** (on top of baseline schema):
- `ce_chute_trigger` — `tc` | `spot` | `none`
- `pe_chute_trigger` — `tc` | `none`
- `ce_chute_entry_premium` / `pe_chute_entry_premium` — parachute entry premiums
- `pe_wing_close_premium` — PE wing sell price when PE parachute entered
- `pe_wing_rebuy_premium` — PE wing buy price when PE parachute exited (if applicable)
- `delta_pts` — net P&L difference TC vs baseline for this trade

**Comparison CSV** (`comparison.csv`): inner join on sell_expiry; columns:
`sell_expiry, baseline_pl, tc_pl, delta_pts, ce_chute_trigger, pe_chute_trigger,
 baseline_ce_chute_triggered, tc_ce_chute_triggered, tc_pe_chute_triggered`.

---

### Production wiring (post-backtest, if validated)

**Athena** (`athena_engine.py`):
- CE parachute: add bullish TC as a second entry condition alongside the reactive
  `spot > ce_sell_strike + 150` check; add bearish TC as a second exit condition
  alongside `spot <= ce_sell_strike`
- PE parachute (new): add bearish TC entry → buy 0.35-delta monthly PE + sell PE wing;
  bullish TC or `spot >= pe_sell_strike` exit → sell PE parachute + rebuy PE wing
- Slack notifications should identify the trigger as `TRIPLE_CONFIRM` and distinguish
  CE parachute events from PE parachute/wing events

**Artemis** (`iron_condor.py`):
- In the main monitoring loop, after fetching LTPs: call `triple_confirm.detect_live()`
- If fire direction threatens an open leg: write `TRIPLE_CONFIRM:ce` or `TRIPLE_CONFIRM:pe`
  to `SLACK_COMMAND.flag`, same mechanism as manual adjustment
- The existing `adjust_spread()` path handles execution

In both cases, a Slack notification should identify the trigger as TRIPLE_CONFIRM.

---

## Files

| File | Purpose |
|---|---|
| `iris_backtest/signals/triple_confirm.py` | Signal detection (backtest mode) |
| `iris_backtest/research/validate_sensex.py` | Sensex validation script |
| `iris_backtest/data/TRIPLE_CONFIRM_excursions.csv` | Nifty TC signals (Feb 2019 – present, 134 fires) |
| `iris_backtest/data/TRIPLE_CONFIRM_sensex_excursions.csv` | Sensex TC signals (Jul 2024 – present, 26 fires) |
| `artemis_backtest/backtest_tc.py` | Artemis TC parallel backtest — instrument-aware, never overwrites baseline |
| `artemis_backtest/data_tc/trade_summary_tc_nifty.csv` | TC backtest output — Nifty (gitignored) |
| `artemis_backtest/data_tc/trade_summary_tc_sensex.csv` | TC backtest output — Sensex (gitignored) |
| `artemis_backtest/data_tc/comparison_nifty.csv` | Matched baseline vs TC — Nifty (gitignored) |
| `artemis_backtest/data_tc/comparison_sensex.csv` | Matched baseline vs TC — Sensex (gitignored) |
| `artemis_backtest/data_tc/pc3_timing_nifty.csv` | PC3 timing analysis — Nifty (gitignored) |
| `artemis_backtest/data_tc/pc3_timing_sensex.csv` | PC3 timing analysis — Sensex (gitignored) |
| `athena_backtest/backtest_tc.py` | Athena TC parallel backtest — never overwrites baseline (**to be created**) |
| `athena_backtest/data_tc/trade_summary_tc.csv` | TC backtest output (gitignored) |
| `athena_backtest/data_tc/comparison.csv` | Matched baseline vs TC (gitignored) |
| `athena_backtest/data_tc/pc3_timing.csv` | TC lead time before spot crosses 150-pt trigger (gitignored) |
