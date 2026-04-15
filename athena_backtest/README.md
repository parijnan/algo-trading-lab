# Athena — Nifty Double Calendar Spread

Part of the **Algo Trading Lab** project.

## Strategy Overview

A market-neutral, theta-positive double calendar spread on Nifty weekly options. Sells 20-delta CE and PE on the near-term Tuesday expiry and buys the same strikes on the monthly expiry (last Tuesday of the current month). The long-vega profile benefits from IV expansion; the time decay differential between the two expiries drives profit in stable markets.

| Parameter | Value |
|---|---|
| Instrument | Nifty weekly options |
| Structure | Double calendar (CE + PE, near-term sell / monthly buy) |
| Entry | Monday 10:30 AM |
| Sell expiry | Next Tuesday from entry (~7 DTE) |
| Buy expiry | Last Tuesday of current month; rolled to next month if DTE < 14 |
| Strike selection | 20-delta sell leg, rounded to nearest 100 points |
| Delta computation | mibian Black-Scholes (IV backed out from market price) |

## Exit Mechanisms

All four legs always exit simultaneously. Priority order (checked on every 1-min candle):

| Priority | Mechanism | Default | Toggle |
|---|---|---|---|
| 1 | Pre-expiry exit | 15:15 Monday before sell expiry | Always active |
| 2 | Spread SL | Combined P&L ≤ −75% of net premium | `ENABLE_SPREAD_SL` |
| 3 | Index SL | Spot within 50 pts of either sell strike | `ENABLE_INDEX_SL` |
| 4 | Option SL | Sell LTP > 2× sell entry (either side) | `ENABLE_OPTION_SL` |
| 5 | Profit target | Combined P&L ≥ 50% of max theoretical profit | `ENABLE_PROFIT_TARGET` |

## Adjustment Logic

When a SL fires within the cutoff window (Wednesday 15:00), the position closes and a fresh one-sided calendar is entered on the breached side:

- Breached side: spot above entry → CE; spot below entry → PE
- New entry: current 20-delta strike at next 1-min open, same buy expiry
- Evaluated independently — own P&L vs own max theoretical profit
- Maximum one adjustment per trade week

## Max Theoretical Profit

Approximated at entry using mibian: IV is backed out from the sell leg's market price, then used to project the buy leg's theoretical value at sell expiry. This adapts the profit target to IV conditions at the time of entry.

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

No precompute step required — Athena reads 1-min data directly.

## Output

**`data/trade_summary.csv`** — one row per trade week:
- Entry details: time, spot, VIX, strikes, deltas, net premium per side, max theoretical profit
- Exit details: time, reason, per-side and total P&L
- Adjustment details: side, strikes, entry/exit prices, P&L, exit reason

**`data/trade_logs/trade_NNNN_YYYY-MM-DD.csv`** — one row per minute while in trade:
- Spot, VIX, all four leg LTPs
- Per-side and combined unrealised P&L
- Index SL and option SL reference levels for each side

## Key Parameters

| Parameter | Default | Notes |
|---|---|---|
| `ENTRY_TIME` | `10:30` | Monday entry |
| `DELTA_TARGET` | `0.20` | Sell leg abs delta |
| `STRIKE_STEP` | `100` | Rounding interval |
| `BUY_LEG_MIN_DTE` | `14` | Roll threshold |
| `PROFIT_TARGET_PCT` | `0.50` | % of max theoretical profit |
| `INDEX_SL_OFFSET` | `50` | Points before sell strike goes ATM |
| `OPTION_SL_MULTIPLIER` | `2.0` | × sell entry |
| `SPREAD_SL_PCT` | `0.75` | % of total net premium |
| `SLIPPAGE_POINTS` | `1.0` | Per leg |

## Status

- [x] configs.py
- [x] backtest.py
- [ ] Backtest run and Phase 1 calibration
- [ ] Optimisation
- [ ] Live execution module