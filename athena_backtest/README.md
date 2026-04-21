# Athena — Nifty Double Calendar Spread

Part of the **Algo Trading Lab** project.

## Strategy Overview

A market-neutral, theta-positive double calendar spread on Nifty weekly options. Sells near-delta CE and PE on the next Tuesday expiry (~8 days out) and buys the same strikes on the monthly expiry. The long-vega profile benefits from IV expansion; the time decay differential between the two expiries drives profit in range-bound weeks.

| Parameter | Value |
|---|---|
| Instrument | Nifty weekly options |
| Structure | Double calendar (CE + PE, near-term sell / monthly buy) |
| Entry | Day before previous Tuesday expiry, 10:30 AM |
| Exit | Day before sell expiry, 10:25 (pre-expiry exit) |
| Sell expiry | Next Tuesday from entry (~8 DTE) |
| Buy expiry | Last Tuesday of current month; rolls to next month if DTE < 16 |
| Strike selection | Delta-targeted sell leg, rounded to nearest 100 points |
| Delta computation | mibian Black-Scholes (IV backed out from market price) |
| VIX filter | Configurable; currently enabled (trades when VIX is >= 16 and <=25) |

## Delta Targeting

Delta target is VIX-conditional:

| VIX Band | Target Delta |
|---|---|
| ≤ 22 | 0.30 |
| 22–25 | 0.30 |

## Exit Mechanisms

All four legs always exit simultaneously. Priority order — first to trigger wins.

| Priority | Mechanism | Status | Config |
|---|---|---|---|
| 1 | Pre-expiry exit at 10:25 day before sell expiry | Always active | `ELM_EXIT_TIME` |
| 2 | Spread SL — combined P&L ≤ −N points | Inactive | `SPREAD_SL_POINTS = 100` |
| 3 | Index SL — spot within N pts of either sell strike | Inactive | `INDEX_SL_OFFSET = 50` |
| 4 | Option SL — sell leg LTP > N × entry | Inactive | `OPTION_SL_MULTIPLIER = 2.0` |
| 5 | Trail stop — P&L falls N pts from peak | Inactive | `TRAIL_ACTIVATION_POINTS = 20`, `TRAIL_POINTS = 10` |
| 6 | Profit target — combined P&L ≥ N% of net debit | Inactive | `PROFIT_TARGET_PCT_NET_DEBIT = 0.20` |

## Adjustment Logic

Spot-proximity triggered roll of the non-threatened sell leg. Maximum one adjustment per trade.

**Trigger A:** `spot >= ce_sell_strike - ADJUSTMENT_TRIGGER_OFFSET` → roll PE sell leg  
**Trigger B:** `spot <= pe_sell_strike + ADJUSTMENT_TRIGGER_OFFSET` → roll CE sell leg

- The triggered side (the one spot is approaching) is **never touched**
- The **other** side's sell leg is bought back; a new sell is placed at `existing_sell_strike ± ADJUSTMENT_NEW_STRIKE_DISTANCE` (stepped toward spot)
- CE rolls down: `new_ce_sell = ce_sell_strike - ADJUSTMENT_NEW_STRIKE_DISTANCE`
- PE rolls up: `new_pe_sell = pe_sell_strike + ADJUSTMENT_NEW_STRIKE_DISTANCE`
- New sell must be OTM — abort if safety check fails
- If `ADJUST_BUY_LEG = True`: buy leg also rolled to match new sell strike, same buy expiry. Entire adjustment aborts if new buy leg data not found.
- Adjustment cannot fire on days listed in `ADJUSTMENT_EXCLUDED_DAYS` (calendar days from entry, day 0 = entry day)

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
| Entry | time, spot, VIX, sell/buy expiry, strikes, deltas, net debit per side, max theoretical profit, target delta used |
| Exit | time, reason, per-side P&L (ce_pl_points, pe_pl_points), total_pl_points, total_pl_rupees |
| Intraweek | max/min spot, max/min VIX, max/min unrealised P&L with timestamps, trail_activation_reached |
| SL context | triggered side, trigger time/spot/day, untouched side values at SL, days remaining at SL |
| Adjustment | adj_side, new sell strikes, new sell entry/exit, sell buyback prices, buy leg prices (if ADJUST_BUY_LEG), adj_pl_points, adj_entry_spot, adj_days_remaining, adj_trigger_day |

**`data/trade_logs/trade_NNNN_YYYY-MM-DD.csv`** — one row per minute while in trade:
- Spot, VIX, all four leg LTPs, per-side and combined unrealised P&L
- Index SL and option SL reference levels for each side
- realised_pl_pts / realised_pl_rs on final row only

## Key Parameters

| Parameter | Current value | Notes |
|---|---|---|
| `ENTRY_TIME` | `10:30` | Entry time on day before prior expiry |
| `ELM_EXIT_TIME` | `10:25` | Pre-expiry exit time, holiday-adjusted |
| `ENABLE_VIX_FILTER` | `True` | Currently enabled |
| `VIX_FILTER_LOW` | `16.0` | Entry floor when filter enabled |
| `VIX_FILTER_HIGH` | `25.0` | Entry ceiling when filter enabled |
| `VIX_DELTA_BANDS` | `[(18,0.30),(20,0.30),(22,0.30),(25,0.30)]` | VIX-conditional delta |
| `STRIKE_STEP` | `100` | Rounding interval |
| `BUY_LEG_MIN_DTE` | `16` | Roll threshold for buy expiry |
| `SLIPPAGE_POINTS` | `1.0` | Per leg (not applied on pre-expiry exit) |
| `SPREAD_SL_POINTS` | `100` | Combined point floor |
| `INDEX_SL_OFFSET` | `50` | Points before sell strike reaches ATM |
| `OPTION_SL_MULTIPLIER` | `2.0` | Sell leg LTP multiple |
| `TRAIL_ACTIVATION_POINTS` | `20` | Trail arms at this peak P&L |
| `TRAIL_POINTS` | `10` | Trail fires if P&L drops this far from peak |
| `PROFIT_TARGET_PCT_NET_DEBIT` | `0.20` | 20% of total net debit paid |
| `ENABLE_ADJUSTMENT` | `True` | Spot-proximity roll |
| `ADJUST_BUY_LEG` | `False` | Also roll buy leg when adjusting |
| `ADJUSTMENT_TRIGGER_OFFSET` | `-300` | pts from sell strike (positive = OTM) |
| `ADJUSTMENT_NEW_STRIKE_DISTANCE` | `400` | Step distance from existing sell strike |
| `ADJUSTMENT_EXCLUDED_DAYS` | `(6,7)` | Allows adjustment on days 3, 4, 5 only |

## Relationship to Artemis and Apollo

Athena is the third strategy in the Algo Trading Lab. It complements Artemis (Sensex iron condor, short vega, low VIX) and Apollo (Nifty directional debit spread, high VIX). At 10:30 AM every Monday, the system reads VIX and routes to the appropriate strategy:

| VIX | Strategy |
|---|---|
| < 16 | Artemis |
| 16–25 | Athena |
| > 25 | Apollo (if trending) |

Artemis always closes by Thursday. Athena closes by Monday 10:25. The margin handoff is clean with no overlap.

## Status

- [x] configs.py
- [x] backtest.py
- [x] VIX filter (configurable, currently disabled)
- [x] VIX-conditional delta targeting
- [x] Configurable entry/exit times
- [x] Spread SL, Index SL, Option SL, Trail stop, Profit target
- [x] Max/min P&L with timestamps in trade summary
- [x] Adjustment mechanism (spot-proximity trigger, sell leg roll)
- [x] ADJUST_BUY_LEG option (roll buy leg alongside sell leg)
- [ ] Adjustment parameter calibration (in progress)
- [ ] Final exit mechanism calibration
- [ ] Live execution module