"""
configs.py — Athena Backtest Configuration
All parameters in one place. Tweak here, nothing else needs to change.

Strategy: Double calendar spread with safety wings (Calendar Condor).
Sell 0.30 delta CE and PE on the near-term weekly expiry.
Buy same strikes on the monthly expiry.
Buy 0.05 delta wings on monthly expiry for gap protection.
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

PRECOMPUTED_DIR     = os.path.join(REPO_ROOT, "apollo_backtest", "data")
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
    (18.0, 0.30),   # VIX up to 18:   0.30 delta
    (20.0, 0.30),   # VIX 18–20:      0.30 delta
    (22.0, 0.30),   # VIX 20–22:      0.30 delta
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
# Asymmetric Delta (Path A)
# Skews entry deltas based on 75m Supertrend regime.
# ---------------------------------------------------------------------------
ENABLE_ASYMMETRIC_DELTA         = False
DELTA_TESTED_SIDE               = 0.25    # Delta for the side the market is moving toward
DELTA_SAFE_SIDE                 = 0.30    # Delta for the side the market is moving away from

# ---------------------------------------------------------------------------
# Safety Wings (Path B)
# Buys far-OTM wings at entry to cap maximum loss (Calendar Condor).
# ---------------------------------------------------------------------------
ENABLE_SAFETY_WINGS             = True
SAFETY_WING_DELTA               = 0.05    # Target delta for the safety wings
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Adjustment — winning side roll (DISABLED for final verification)
# ---------------------------------------------------------------------------
ENABLE_ADJUSTMENT               = False
ADJUST_BUY_LEG                  = True
ADJUSTMENT_TRIGGER_OFFSET       = -150
ADJUSTMENT_WING_THRESHOLD       = 15.0
ADJUSTMENT_MIN_CREDIT_GAIN      = 15.0
ADJUSTMENT_NEW_STRIKE_DISTANCE  = 400
ADJUSTMENT_EXCLUDED_DAYS        = (0,)

# ---------------------------------------------------------------------------
# Emergency Hedge (Phase 2 Smart Parachute)
# ---------------------------------------------------------------------------
ENABLE_EMERGENCY_HEDGE          = True
EMERGENCY_HEDGE_DELTA           = 0.35    # Monthly CE bought on upside stress
EMERGENCY_TRIGGER_OFFSET        = -150    # pts past CE strike to BUY hedge
EMERGENCY_EXIT_OFFSET           = 0       # pts from CE strike to SELL hedge (on reversal)
EMERGENCY_MAX_ATTEMPTS          = 1       # Limit whipsaw cost

ENABLE_PE_EMERGENCY_HEDGE       = True
PE_EMERGENCY_HEDGE_DELTA        = 0.35    # Monthly PE bought on downside stress
PE_EMERGENCY_TRIGGER_OFFSET     = -150    # pts past PE strike to BUY hedge (spot <= pe_sell - 150)
PE_EMERGENCY_EXIT_OFFSET        = 0       # pts from PE strike to SELL hedge (on reversal)
PE_EMERGENCY_MAX_ATTEMPTS       = 1       # Limit whipsaw cost
# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
SLIPPAGE_POINTS         = 1.0           # Per leg; applied on all exits except pre-expiry
LOT_SIZE                = 65            # Nifty lot size (update if SEBI changes this)
RISK_FREE_RATE          = 5.0           # Annualised risk-free rate (%) for mibian BS

# ---------------------------------------------------------------------------
# Backtest scope
# ---------------------------------------------------------------------------
BACKTEST_START_DATE     = '2020-01-01'
BACKTEST_END_DATE       = None          # None = full available data