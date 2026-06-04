# Plan: TRIPLE_CONFIRM Integration — Artemis and Athena

## Status: PC3 Artemis Complete — PC4 (false-signal cost) pending

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
Athena's CE parachute triggers reactively when `spot > ce_sell_strike + PARACHUTE_OFFSET`.
The parachute is a hedge buy; entering it after the spot has already moved means paying a
higher premium on the hedge.

### Proposed use of TRIPLE_CONFIRM
When TRIPLE_CONFIRM fires **bullish** during an active Athena session with no CE parachute:
- Pre-emptively enter the CE parachute before spot crosses the trigger threshold
- Entry at lower hedge premium than reactive trigger would produce

When TRIPLE_CONFIRM fires **bearish** during an active Athena session with a live CE parachute:
- Pre-emptively exit the parachute before spot retreats through the exit threshold
- Exit at higher hedge premium than reactive exit would produce

### Key backtest question
Does TRIPLE_CONFIRM-timed parachute entry/exit improve overall Athena P&L vs the current
spot-triggered mechanism? Needs Athena backtest with TRIPLE_CONFIRM wired alongside the
existing parachute entry/exit logic.

### Caveats
- TRIPLE_CONFIRM is calibrated on Nifty; Athena also trades Nifty — direct applicability,
  no cross-instrument adjustment needed.
- Athena sessions run ~40/year. TRIPLE_CONFIRM will fire in a meaningful subset of them.
- The parachute is already optional (manual trigger exists) — TRIPLE_CONFIRM could start as
  an automated pre-trigger for the existing manual Slack path before full automation.

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

1. **Sensex validation complete** (for Artemis): `validate_sensex.py` must show TRIPLE_CONFIRM
   on Sensex fires in the same situations as on Nifty and with comparable quality.
   ✅ **DONE** — PC1 cleared. Sensex WR 76.9%, RR 4.26 @ 15min, 13.9 fires/year.

2. **Strategy-level backtest**: run TRIPLE_CONFIRM as an additional trigger through the full
   Artemis/Athena backtest and compare cumulative P&L vs baseline.
   ✅ **DONE** — PC2 Artemis complete. Results below.

3. **Timing analysis**: quantify how early TRIPLE_CONFIRM fires before a typical SL/parachute
   trigger. If the typical lead time is <5 min, the benefit is marginal.
   ✅ **DONE** — PC3 complete. Results below.

4. **False-signal cost**: model the P&L impact of the ~17–23% false-signal rate (unnecessary
   adjustment triggered by a TRIPLE_CONFIRM that reverses). Must be outweighed by the P&L
   gain from earlier exits on true signals.
   ⏳ **PENDING**

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

**Key finding:** TC lead time is never marginal. It either fires 13–53 min before an intraday SL (meaningful same-session advantage) or fires the prior afternoon before a gap-open SL (overnight protection). The cross-day cluster is where the largest P&L swings occur — both the biggest win (+166.60) and biggest loss (−84.32).

The false-signal drag (−61.34 pts Nifty) is the cost of TC exiting a leg pre-emptively when the market doesn't follow through. This is PC4 territory.

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

## Implementation Sketch (when ready)

**Artemis** (`iron_condor.py`):
- In the main monitoring loop, after fetching LTPs: call `triple_confirm.detect_live()`
  (a streaming version that checks if all three conditions are met on current bars)
- If fire direction threatens an open leg: write `TRIPLE_CONFIRM:ce` or `TRIPLE_CONFIRM:pe`
  to `SLACK_COMMAND.flag`, same mechanism as manual adjustment
- The existing `adjust_spread()` path handles execution — no new order logic needed

**Athena** (`athena_engine.py`):
- In parachute check: add TRIPLE_CONFIRM bullish as an additional entry condition alongside
  the existing spot > threshold check
- The existing parachute entry/exit path handles execution

In both cases, a Slack notification should identify the trigger as TRIPLE_CONFIRM so it is
distinguishable from SL-triggered and manual adjustments in the logs.

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
