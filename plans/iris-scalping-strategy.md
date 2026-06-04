# Plan: Iris — Auto-Entry Scalping Strategy (Manual Arm/Disarm)

## Status: Track A COMPLETE · Track B Backtest COMPLETE · Paper mode — in progress (first session 2026-06-04)

---

## Context

A directional scalping strategy that monitors for high-conviction trend signals and
auto-enters on signal without per-trade approval. The trader arms/disarms the watchdog
via Slack; once armed, the algo trades autonomously. Independent of Leto's VIX routing.

---

## Calibrated Parameters (from full backtest — `iris_backtest/`)

| Parameter | Value | Source |
|---|---|---|
| Signal | ST_FAST — 5-min ST flip aligned with 15-min regime | Track A signal comparison |
| Instrument | Nifty weekly options, ITM-150, nearest expiry | Track B options fill sim |
| Lot size | 65 (Nifty standard) | Fixed |
| Profit target | 10% of entry premium | Strategy backtest sweep |
| Stop loss | 25% of entry premium | Strategy backtest sweep |
| Max hold | 30 minutes per trade | Strategy backtest sweep |
| Daily cutoff | 15:00 | Hard limit |
| Skip windows | 10:45–11:30 (dead zone) | Time-of-day analysis |

**Backtest result (7.3 years, 1,172 trades):**
WR 59.8% · Avg ₹229/lot · Median ₹414/lot · Total ₹268,798 (1 lot)

---

## Track A — Signal Research ✓ COMPLETE

### Outcome
Eight signal candidates compared on Nifty 1-min (2019–2026). **ST_FAST selected.**

ST_FAST (5-min ST flip + 15-min regime alignment):
- 200 signals/year (~1/trading day)
- WR 55.6% at 15-min, RR 1.42 — highest quality of all 8 candidates
- Positive median close at every horizon (5–120 min)
- Only signal where win rate, RR, and median close are all positive and consistent

Eliminated: 15m+75m supertrend (too slow — Apollo already covers this), EMA_CROSS
and ROC_BURST (RR ≈ 1.0, no edge), RANGE_BREAK daily (insufficient signals/year).

### Files
- `iris_backtest/signals/` — 8 signal detectors
- `iris_backtest/data/signal_comparison.csv` — full comparison table
- `iris_backtest/research/run_all.py` — regenerate signal excursions
- `iris_backtest/research/compare.py` — rebuild comparison table

---

## Track B — Execution Harness ✓ BACKTEST COMPLETE · Paper mode pending

### Instrument selection
- **ITM-150 Nifty call/put**, nearest weekly expiry
- Delta ~0.70 mean (not futures-like — genuine optionality retained)
- Median entry premium: ~₹13,871/lot (213 pts × 65)
- Liquidity confirmed: 370+ active bars/day even at ITM-150 depth
- Slippage confirmed negligible: median close→next-open gap = 0.0 pts

### Exit bucket structure (calibrated params)

| Bucket | N | WR% | Avg ₹/lot | Notes |
|---|---|---|---|---|
| 1. Profit target | 478 | 100% | +₹1,444 | 10% target — 41% of all trades |
| 2. Stop loss | 24 | 0% | -₹3,718 | 25% stop — 2% of trades, tail insurance only |
| 3. Trend flip | 42 | 9.5% | -₹1,412 | 5-min ST reversal — nearly always a confirmed loss |
| 4. Max hold | 628 | 34.9% | -₹436 | 30-min timer — median MFE only 2.8% within hold |

Bucket 4 (54% of trades) is the structural drag. Within-hold MFE median is 2.8% — these
trades genuinely don't move enough. No exit mechanism (trailing stops tested at 15–30%)
improves Bucket 4; trailing stops make it worse. VIX at entry has no correlation with
bucket outcomes.

### Time-of-day insight
09:15 window (first 15 min): 360 trades, WR 67.8%, 52% profit-target rate.
Contributes 48% of total P&L on 30% of trades. Iris is a morning strategy.

Skip window 10:45–11:30: three consecutive dead-zone windows (WR 31–43%, near-zero
target rate) during post-opening-move settling. 11:30 recovers sharply to 72.7% WR.

### Production harness
- `iris_production/iris.py` — main loop (standalone login, ST_FAST live signal, 4 exits + market-close auto-shutdown)
- `iris_production/configs.py` — all calibrated params
- `iris_production/state.py` — IrisState (idle/watching/in_trade)
- `iris_production/functions.py` — SupertrendIndicator, order placement, guardian check
- `iris_production/README.md` — full execution flowchart and module documentation
- **DRY_RUN=True** — set to False only after paper parity confirmed

### Guardian check
Iris refuses to start if Apollo, Athena, or Artemis has an open position.
Angel One allows only one active session — a new Iris login would invalidate the
running strategy session.

### Pending before live deployment
1. ~~Confirm Angel One serves `FIVE_MINUTE` interval from `getCandleData`~~ ✓ (first paper session 2026-06-04)
2. Run paper mode for 2–3 weeks and diff live ST_FAST flips against backtest signal list
3. Calibrate stop/target after observing live P&L distribution
4. Clean start/stop: `python iris_production/iris.py` to start; delete `iris_active.flag` to stop cleanly

---

## Build Sequence

1. ~~Track A — Signal comparison~~ ✓
2. ~~Track B — Options fill sim (ATM/ITM depths)~~ ✓
3. ~~Track B — Strategy backtest (4-condition exits, parameter sweep)~~ ✓
4. ~~Track B — Full backtest with per-trade logs~~ ✓
5. ~~Track B — Time-of-day analysis + skip window calibration~~ ✓
6. ~~Track B — Execution harness skeleton (paper mode)~~ ✓
7. **Paper trading** (2–3 weeks, verify parity) ← in progress (started 2026-06-04)
8. Live deployment (small lot count, monitored)

---

## Constraints

- No live orders until paper parity is confirmed (DRY_RUN guard in configs.py).
- No changes to Leto, Artemis, Athena, or Apollo during Iris paper/live sessions.
- Futures trading excluded — no broker-side enabling done; options infrastructure proven.
