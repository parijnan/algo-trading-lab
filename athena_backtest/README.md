# Athena — Nifty Double Calendar Spread

Part of the **Algo Trading Lab** project.

## Strategy Overview

A market-neutral, theta-positive double calendar spread on Nifty weekly options. Sells near-delta CE and PE on the next Tuesday expiry (~8 days out) and buys the same strikes on the monthly expiry. The long-vega profile benefits from IV expansion; the time decay differential between the two expiries drives profit in range-bound weeks.

| Parameter | Value |
|---|---|
| Instrument | Nifty weekly options |
| Structure | Double calendar (CE + PE, near-term sell / monthly buy) |
| Entry | Day before previous Tuesday expiry, 10:30 AM |
| Exit | Day before sell expiry, 10:25 AM (pre-expiry exit) |
| Sell expiry | Next Tuesday from entry (~8 DTE) |
| Buy expiry | Nearest monthly expiry with DTE ≥ 16; rolls to next month if below threshold |
| Strike selection | Delta-targeted sell leg, rounded to nearest 100 points |
| Delta computation | mibian Black-Scholes (IV backed out from market price) |
| VIX filter | Entry only when 16 ≤ India VIX ≤ 25 |

## Delta Targeting

Delta target is VIX-conditional:

| VIX Band | Target Delta |
|---|---|
| 16–22 | 0.25 |
| 22–25 | 0.30 |

## Exit Mechanisms

All four legs always exit simultaneously. Only the pre-expiry exit is active in the current production baseline. Additional mechanisms are under calibration.

| Priority | Mechanism | Status |
|---|---|---|
| 1 | Pre-expiry exit at 10:25 AM day before sell expiry | Always active |
| 2 | Spread SL — combined P&L ≤ −N points | Under calibration |
| 3 | Adjustment — winning side tightening | Under development |

## Adjustment Logic

Under active development. Preliminary design based on trade log analysis:

- Trigger window: day 2–3 of the trade
- Condition: losing side unrealised P&L ≤ −30 pts AND momentum confirmed
- Action: tighten the winning side's sell strike closer to current spot (roll sell leg only; buy leg unchanged)
- Constraint: new sell strike must remain ≥ 150 pts from current spot
- Maximum one adjustment per trade week
- Losing side is never touched

## Project Structure

```
athena_backtest/
├── configs.py          # All parameters — edit here only
├── backtest.py         # Main backtest loop and trade log generation
└── data/
    ├── trade_summary.csv       (generated — gitignored)
    └── trade_logs/             (generated — gitignored)
        └── trade_NNNN_YYYY-MM-DD.csv
```

## Setup

1. Ensure the Nifty options data pipeline has run (laptop cron, Tuesday nights)
2. Update `BACKTEST_START_DATE` / `BACKTEST_END_DATE` in `configs.py` if needed
3. Run: `python backtest.py`

## Output

**`data/trade_summary.csv`** — one row per trade week:

| Column group | Contents |
|---|---|
| Entry | time, spot, VIX, strikes, deltas, net debit per side, max theoretical profit, target delta used |
| Exit | time, reason, per-side and total P&L |
| Intraweek | max/min spot, max/min VIX, max/min unrealised P&L with timestamps |
| SL context | triggered side, trigger time/spot/day, untouched side values at SL, days remaining at SL |
| Adjustment | side, strikes, entry/exit prices, P&L, exit reason, entry spot, days remaining |

**`data/trade_logs/trade_NNNN_YYYY-MM-DD.csv`** — one row per minute while in trade:
- Spot, VIX, all four leg LTPs
- Per-side and combined unrealised P&L
- Index SL and option SL reference levels for each side

## Key Parameters

| Parameter | Current value | Notes |
|---|---|---|
| `ENTRY_TIME` | `10:30` | Monday equivalent entry |
| `EXIT_TIME` | `10:25` | Day before sell expiry |
| `VIX_FILTER_LOW` | `16.0` | No entry below this |
| `VIX_FILTER_HIGH` | `25.0` | No entry above this |
| `VIX_DELTA_BANDS` | `[(22.0, 0.25), (25.0, 0.30)]` | VIX-conditional delta |
| `STRIKE_STEP` | `100` | Rounding interval |
| `BUY_LEG_MIN_DTE` | `16` | Roll threshold |
| `SLIPPAGE_POINTS` | `1.0` | Per leg |
| `SPREAD_SL_POINTS` | `None` | Absolute point floor — under calibration |
| `ENABLE_ADJUSTMENT` | `False` | Pending development |

## Relationship to Artemis and Apollo

Athena is the third strategy in the Algo Trading Lab. It complements Artemis (Sensex iron condor, short vega, low VIX) and Apollo (Nifty directional debit spread, high VIX). At 10:30 AM every Monday, the system reads VIX and routes to the appropriate strategy:

| VIX | Strategy |
|---|---|
| < 16 | Artemis |
| 16–25 | Athena |
| > 25 | Apollo (if trending) |

Artemis always closes by Thursday. Athena closes by Monday 10:25 AM. The margin handoff is clean with no overlap.

## Status

- [x] configs.py
- [x] backtest.py
- [x] VIX filter (16–25)
- [x] VIX-conditional delta targeting
- [x] Configurable entry/exit time
- [x] Spread SL (absolute points)
- [x] Max/min P&L with timestamps in trade summary
- [ ] Adjustment mechanism (spec written, pending implementation)
- [ ] Final parameter calibration
- [ ] Live execution module