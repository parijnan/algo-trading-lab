# Plan: TRIPLE_CONFIRM Integration — Artemis and Athena

## Status: Artemis PC1–PC4 Complete (+5.8% improvement) · Athena track — in progress

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

### Current behaviour
Athena's CE parachute (Emergency Hedge) triggers reactively when
`spot >= ce_sell_strike - 150 pts` (`EMERGENCY_TRIGGER_OFFSET = -150`).
The parachute is a 0.35-delta monthly CE buy; entering it after the spot has already moved
150 pts past the sell strike means paying elevated IV and a higher absolute premium on the
hedge. The parachute exits reactively when `spot <= ce_sell_strike + 0 pts`
(`EMERGENCY_EXIT_OFFSET = 0`), and the attempt cap (`EMERGENCY_MAX_ATTEMPTS = 1`) limits it
to one parachute cycle per trade.

There is no PE parachute — Athena buys a static 0.05-delta monthly PE safety wing at entry
and holds it to ELM. The TC track affects only the CE parachute.

### Proposed use of TRIPLE_CONFIRM

**Bullish TC fire + no active parachute → pre-emptive parachute entry**  
Enter the CE parachute before spot crosses the 150-pt trigger threshold. The hedge premium
will be lower (spot hasn't moved yet); if the TC is a true signal, the position is protected
from the move. If it's a false signal, the parachute is exited via the normal spot-reversal
exit (`spot <= ce_sell_strike`) or held to ELM.

**Bearish TC fire + active parachute → pre-emptive parachute exit**  
Exit the CE parachute before spot retreats through the 0-pt exit threshold. The hedge premium
will be higher (spot hasn't retreated yet), capturing more of the parachute's gain on
reversal. If it's a false signal, the parachute was already at risk of a deteriorating
position (market moving down with a live CE hedge).

### Key design decisions for the backtest

1. **TC as supplementary trigger (not replacement)**: if TC fires before spot crosses
   the 150-pt trigger, enter early. If spot crosses the trigger before TC fires, enter
   normally via the existing reactive path. Whichever fires first wins.
   This is the same pattern as Artemis (TC supplements, doesn't replace the SL).

2. **Attempt cap interaction**: `EMERGENCY_MAX_ATTEMPTS = 1` limits the parachute to one
   cycle. A TC-triggered early entry consumes that attempt. If TC fires a second time in
   the same trade (after the first parachute has been exited), no re-entry is attempted —
   same constraint as reactive path.

3. **Bearish TC exit only fires if a parachute is active**: if no parachute is live, a
   bearish TC fire has no effect on Athena (the PE wing is static and is not touched).

### Key backtest question
Does TC-timed parachute entry/exit improve overall Athena P&L vs the current spot-triggered
mechanism? Expected benefit: lower hedge cost on entry (true signal case) and higher exit
capture on reversal. Expected cost: false-signal parachute entries that are either exited at
a loss or held to ELM at diminished value.

### Caveats
- TRIPLE_CONFIRM is calibrated on Nifty; Athena also trades Nifty — direct applicability,
  no cross-instrument adjustment needed.
- The parachute already has a maximum-1-attempt cap, which bounds the false-signal
  downside to a single unnecessary parachute cost per trade.
- Unlike Artemis (which permanently alters the position on TC), Athena's parachute is a
  temporary hedge: false-signal cost is bounded (parachute expires or exits at spot reversal)
  rather than open-ended.
- Athena sessions run ~40/year. TC fires ~18/year on Nifty.
  Signal funnel analysis needed (analogous to Artemis PC3 funnel) to quantify how many
  of those 18 fires fall inside an active Athena session with the CE side still unhedged.

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
   active Athena session with the CE side unhedged? Athena sessions run ~40/year and the
   sell leg is typically open from entry Monday until ELM Wednesday — roughly 3 days.
   TC fires are distributed across all 5 weekdays. Expected ~10–12 TC fires inside
   Athena holding windows per year, but need actual count from data.
   ⬜ **TODO** — run against `TRIPLE_CONFIRM_excursions.csv` and Athena trade dates.

3. **Strategy-level backtest**: run `athena_backtest/backtest_tc.py` with TC as an additional
   parachute trigger; compare total P&L, parachute entry/exit prices, and false-signal cost
   vs baseline `backtest.py`.
   ⬜ **TODO** — implement backtest_tc.py (see Implementation Sketch below).

4. **Timing analysis (PC3-equivalent)**: quantify how early TC fires before spot crosses the
   150-pt trigger threshold. If lead time is typically <5 min, benefit is marginal.
   ⬜ **TODO** — derive from TC fire timestamp vs candle-by-candle spot in Athena trade logs.

5. **False-signal cost (PC4-equivalent)**: decompose unnecessary parachute entries (TC fired,
   spot never crossed threshold) by outcome — parachute exited at spot-reversal exit vs held
   to ELM worthless.
   ⬜ **TODO** — derive from backtest_tc.py output.

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

**`check_tc_parachute()` — mirrors the emergency hedge check in `backtest.py`**:
```python
def check_tc_parachute(trade: dict, ts: pd.Timestamp,
                        tc_signal_map: dict, hedge_active: bool,
                        hedge_attempts: int, max_attempts: int) -> str | None:
    tc_dir = tc_signal_map.get(ts.date())
    if tc_dir is None:
        return None
    if tc_dir == 'bullish' and not hedge_active and hedge_attempts < max_attempts:
        return 'tc_enter'
    if tc_dir == 'bearish' and hedge_active:
        return 'tc_exit'
    return None
```

**Main loop modifications** (inside the candle-by-candle iteration):
```python
# Before the reactive spot check:
tc_action = check_tc_parachute(trade, ts, tc_signal_map,
                                hedge_active, hedge_attempts,
                                EMERGENCY_MAX_ATTEMPTS)
if tc_action == 'tc_enter':
    # enter parachute at next-candle open (same execution model as reactive path)
    ...
    hedge_active = True; hedge_attempts += 1; tc_triggered = True

elif tc_action == 'tc_exit' and hedge_active:
    # exit parachute at next-candle open
    ...
    hedge_active = False; tc_triggered = True

# Reactive spot check still runs if TC hasn't fired:
if not hedge_active and hedge_attempts < EMERGENCY_MAX_ATTEMPTS:
    if spot >= ce_sell_strike - abs(EMERGENCY_TRIGGER_OFFSET):
        # normal parachute entry ...
```

**Output columns added to `trade_summary_tc.csv`** (on top of baseline schema):
- `tc_parachute_entered` — bool: TC triggered early entry
- `tc_parachute_exited` — bool: TC triggered early exit
- `tc_fire_date` — date TC fired (if applicable)
- `tc_entry_premium` — parachute entry premium under TC path
- `baseline_entry_premium` — parachute entry premium under reactive path (from baseline)
- `delta_pts` — net P&L difference TC vs baseline for this trade

**Comparison CSV** (`comparison.csv`): inner join on sell_expiry between baseline and TC
summary; one row per trade; columns: `sell_expiry, baseline_pl, tc_pl, delta_pts,
tc_triggered, tc_action, baseline_parachute_triggered, tc_parachute_triggered`.

---

### Production wiring (post-backtest, if validated)

**Athena** (`athena_engine.py`):
- In the parachute check block: add TRIPLE_CONFIRM bullish as an additional entry condition
  alongside the existing `spot >= ce_sell_strike - 150` check
- The existing `_enter_emergency_hedge()` path handles execution — no new order logic
- A Slack notification should identify the trigger as `TRIPLE_CONFIRM` so it is
  distinguishable from reactive and manual triggers in the logs

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
