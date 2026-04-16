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
ENTRY_TIME              = '15:20'       # Entry time on the day before the prior expiry (see backtest.py) (HH:MM)
DELTA_TARGET            = 0.25          # Sell leg target delta (abs value)
STRIKE_STEP             = 100           # Nifty strike rounding interval — liquidity constraint
BUY_LEG_MIN_DTE         = 16            # Roll buy leg to next month if DTE below this at entry

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
# Consistent with SPREAD_SL_PCT which also uses net debit as denominator.
# ---------------------------------------------------------------------------
ENABLE_PROFIT_TARGET            = False
PROFIT_TARGET_PCT_NET_DEBIT     = 0.20      # Exit when combined P&L >= 20% of total net debit paid

# ---------------------------------------------------------------------------
# Exit — index SL
# CE side: exit when spot >= ce_sell_strike - INDEX_SL_OFFSET  (approaching from below)
# PE side: exit when spot <= pe_sell_strike + INDEX_SL_OFFSET  (approaching from above)
# Both sides exit simultaneously on trigger.
# ---------------------------------------------------------------------------
ENABLE_INDEX_SL         = True
INDEX_SL_OFFSET         = 100            # Points before sell strike reaches ATM (Nifty)

# ---------------------------------------------------------------------------
# Exit — option SL
# Fires when sell leg LTP > OPTION_SL_MULTIPLIER * sell_entry on either side.
# Both sides exit simultaneously on trigger.
# ---------------------------------------------------------------------------
ENABLE_OPTION_SL        = False
OPTION_SL_MULTIPLIER    = 2.0

# ---------------------------------------------------------------------------
# Exit — spread SL (combined both sides, hard floor)
# A double calendar is a net debit strategy — you pay more for the far leg
# than you receive for the near leg. Spread SL fires when combined P&L loss
# exceeds SPREAD_SL_PCT of the total net debit paid at entry.
# ---------------------------------------------------------------------------
ENABLE_SPREAD_SL        = False
SPREAD_SL_PCT           = 0.75          # % of total net debit paid at entry

# ---------------------------------------------------------------------------
# Pre-expiry exit (mandatory — always active, not toggleable)
# Exit all legs at 15:15 on the last trading day before the sell expiry.
# Computed in load_contracts() via last_trading_day_before() — handles
# any number of consecutive holidays or weekend bridges correctly.
# No configurable parameter needed.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Adjustment (re-enter fresh double calendar at current spot after any SL exit)
# Fires on any SL exit (index_sl, option_sl, spread_sl) if days remaining
# at SL trigger >= ADJUSTMENT_MIN_DAYS_REMAINING.
# Re-entry uses same sell and buy expiry as original trade.
# Both CE and PE sides re-entered at current spot and target delta.
# Maximum one adjustment per trade — hardcoded, no config parameter.
# Evaluated independently: own P&L vs own net debit (not original trade's).
# ---------------------------------------------------------------------------
ENABLE_ADJUSTMENT               = True
ADJUSTMENT_MIN_DAYS_REMAINING   = 3     # minimum calendar days from SL trigger to elm_time
                                        # for re-entry to be attempted

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
BACKTEST_END_DATE       = '2026-04-15'          # None = full available data