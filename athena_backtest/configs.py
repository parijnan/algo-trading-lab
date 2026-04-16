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
ENTRY_TIME              = '15:20'       # Monday entry time (HH:MM)
DELTA_TARGET            = 0.20          # Sell leg target delta (abs value)
STRIKE_STEP             = 100           # Nifty strike rounding interval — liquidity constraint
BUY_LEG_MIN_DTE         = 14            # Roll buy leg to next month if DTE below this at entry

# ---------------------------------------------------------------------------
# Exit — profit target
# ---------------------------------------------------------------------------
ENABLE_PROFIT_TARGET    = False
PROFIT_TARGET_PCT       = 0.50          # % of max theoretical profit at entry (combined both sides)

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
# A double calendar is a net debit strategy — you pay more for the far leg
# than you receive for the near leg. Spread SL fires when combined P&L loss
# exceeds SPREAD_SL_PCT of the total net debit paid at entry.
# ---------------------------------------------------------------------------
ENABLE_SPREAD_SL        = False
SPREAD_SL_PCT           = 0.75          # % of total net debit paid at entry

# ---------------------------------------------------------------------------
# Pre-expiry exit (mandatory — always active, not toggleable)
# Exit all legs at 15:15 on Monday before sell leg's Tuesday expiry.
# elm_time derived from contract list — holiday-adjusted.
# ---------------------------------------------------------------------------
ELM_SECONDS_BEFORE_EXPIRY = 87300       # 24h 15min in seconds → gives 15:15 day before expiry

# ---------------------------------------------------------------------------
# Adjustment (re-enter one-sided calendar on breached side after SL exit)
# Breached side: spot above entry_spot → CE breached; spot below → PE breached.
# Adjustment is evaluated independently — its own P&L vs its own max theoretical profit.
# ---------------------------------------------------------------------------
ENABLE_ADJUSTMENT           = False
ADJUSTMENT_CUTOFF_DAY       = 2         # Wednesday (0=Mon, 1=Tue, 2=Wed)
ADJUSTMENT_CUTOFF_TIME      = '15:00'   # No adjustment after this time on cutoff day
MAX_ADJUSTMENTS_PER_SIDE    = 1         # Maximum one adjustment per trade week

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
BACKTEST_END_DATE       = None          # None = full available data