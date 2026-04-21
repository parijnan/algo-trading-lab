"""
configs.py — Athena Backtest Configuration
All parameters in one place. Tweak here, nothing else needs to change.

Strategy: Double calendar spread on Nifty weekly options.
Sell 20-delta CE and PE on next Tuesday's expiry (7 DTE from Monday entry).
Buy same strikes on last Tuesday of current month ("monthly" expiry).
Entry: Monday 10:30 AM.
"""

import os

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT           = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINE_DATA       = os.path.join(REPO_ROOT, "data_pipeline", "data")

NIFTY_INDEX_FILE    = os.path.join(PIPELINE_DATA, "indices", "nifty.csv")
VIX_INDEX_FILE      = os.path.join(PIPELINE_DATA, "indices", "india_vix.csv")
NIFTY_OPTIONS_PATH  = os.path.join(PIPELINE_DATA, "nifty", "options")
CONTRACT_LIST_FILE  = os.path.join(REPO_ROOT, "data_pipeline", "config", "options_list_nf.csv")

TRADE_LOGS_DIR      = os.path.join(os.path.dirname(__file__), "data", "trade_logs")
TRADE_SUMMARY_FILE  = os.path.join(os.path.dirname(__file__), "data", "trade_summary.csv")

# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
ENTRY_TIME              = '10:30'       # Entry time on the day before the prior expiry (see backtest.py) (HH:MM)
STRIKE_STEP             = 100           # Nifty strike rounding interval — liquidity constraint
BUY_LEG_MIN_DTE         = 16            # Roll buy leg to next month if DTE below this at entry

# VIX-conditional delta targeting
# List of (vix_upper_bound, delta_target) tuples in ascending order of VIX.
# At entry, the first band where entry_vix <= vix_upper_bound is selected.
# Falls back to the last band's delta if entry_vix exceeds all bounds.
# To use a flat delta across all VIX levels, set all bands to the same value.
VIX_DELTA_BANDS         = [
    (18.0, 0.30),   # VIX up to 18:   0.25 delta
    (20.0, 0.30),   # VIX 18–20:      0.25 delta
    (22.0, 0.30),   # VIX 20–22:      0.25 delta
    (25.0, 0.30),   # VIX 22–25:      0.30 delta
]

# ---------------------------------------------------------------------------
# Entry — VIX filter
# Enter trades only when VIX_FILTER_LOW <= entry_vix <= VIX_FILTER_HIGH.
# Set ENABLE_VIX_FILTER = False to trade across all VIX regimes.
# ---------------------------------------------------------------------------
ENABLE_VIX_FILTER       = True
VIX_FILTER_LOW          = 16.0          # Skip entry if VIX below this
VIX_FILTER_HIGH         = 25.0          # Skip entry if VIX above this

# ---------------------------------------------------------------------------
# Exit — profit target
# Denominator is total net debit paid (ce + pe combined) — the capital at risk.
# ---------------------------------------------------------------------------
ENABLE_PROFIT_TARGET            = False
PROFIT_TARGET_PCT_NET_DEBIT     = 0.20      # Exit when combined P&L >= 20% of total net debit paid

# ---------------------------------------------------------------------------
# Exit — index SL
# CE side: exit when spot >= ce_sell_strike - INDEX_SL_OFFSET  (approaching from below)
# PE side: exit when spot <= pe_sell_strike + INDEX_SL_OFFSET  (approaching from above)
# Both sides exit simultaneously on trigger.
# ---------------------------------------------------------------------------
ENABLE_INDEX_SL         = False
INDEX_SL_OFFSET         = 50            # Points before sell strike reaches ATM (Nifty)

# ---------------------------------------------------------------------------
# Exit — option SL
# Fires when sell leg LTP > OPTION_SL_MULTIPLIER * sell_entry on either side.
# Both sides exit simultaneously on trigger.
# ---------------------------------------------------------------------------
ENABLE_OPTION_SL        = False
OPTION_SL_MULTIPLIER    = 2.0

# ---------------------------------------------------------------------------
# Exit — spread SL (combined both sides, hard floor)
# Fires when combined unrealised P&L drops below -SPREAD_SL_POINTS.
# Set SPREAD_SL_POINTS = None to disable.
# ---------------------------------------------------------------------------
ENABLE_SPREAD_SL        = False
SPREAD_SL_POINTS        = 100           # Exit when combined P&L <= -X points

# ---------------------------------------------------------------------------
# Exit — trailing stop
# Activates once unrealised P&L >= TRAIL_ACTIVATION_POINTS.
# Fires when P&L drops more than TRAIL_POINTS from its peak.
# Set ENABLE_TRAIL_STOP = False to disable entirely.
# ---------------------------------------------------------------------------
ENABLE_TRAIL_STOP       = False
TRAIL_ACTIVATION_POINTS = 20            # Trail arms once peak P&L reaches this
TRAIL_POINTS            = 10            # Exit if P&L falls this far from peak

# ---------------------------------------------------------------------------
# Pre-expiry exit (mandatory — always active, not toggleable)
# Exit all legs at ELM_EXIT_TIME on the last trading day before the sell expiry.
# Day is computed via last_trading_day_before() — holiday-adjusted.
# ---------------------------------------------------------------------------
ELM_EXIT_TIME           = '10:25'      # HH:MM — exit time on the day before sell expiry

# ---------------------------------------------------------------------------
# Adjustment — winning side roll
# When conditions are met mid-trade, roll the winning side's sell leg to a
# closer strike to collect additional premium. Buy legs are never touched.
# Losing side is never touched. Maximum one adjustment per trade.
# ---------------------------------------------------------------------------
ENABLE_ADJUSTMENT               = True
ADJUST_BUY_LEG                  = False  # if True, roll the buy leg to match the new sell strike
                                          # same strike as new sell, same buy expiry as original
ADJUSTMENT_TRIGGER_OFFSET       = -300    # pts from sold strike at which trigger fires
                                        # positive = still OTM, 0 = ATM, negative = ITM
                                        # Trigger A: fires when spot >= ce_sell_strike - offset
                                        # Trigger B: fires when spot <= pe_sell_strike + offset
ADJUSTMENT_NEW_STRIKE_DISTANCE  = 400   # new sell strike this many pts from current spot
ADJUSTMENT_EXCLUDED_DAYS        = (6, 7)  # trade days on which adjustment cannot fire
                                                     # day 0 = entry day; allows days 3, 4, 5

# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
SLIPPAGE_POINTS         = 1.0           # Per leg; applied on all exits except pre-expiry
LOT_SIZE                = 75            # Nifty lot size (update if SEBI changes this)
RISK_FREE_RATE          = 5.0           # Annualised risk-free rate (%) for mibian BS

# ---------------------------------------------------------------------------
# Backtest scope
# ---------------------------------------------------------------------------
BACKTEST_START_DATE     = '2020-01-01'
BACKTEST_END_DATE       = '2026-04-20'          # None = full available data