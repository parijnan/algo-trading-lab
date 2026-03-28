"""
configs.py — Apollo Backtest Configuration
All parameters in one place. Tweak here, nothing else needs to change.
"""

import os

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# Repo root — derived from this file's location (apollo_backtest/configs.py)
# so paths resolve correctly regardless of working directory.
REPO_ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Data pipeline data directory — all raw market data lives here
PIPELINE_DATA   = os.path.join(REPO_ROOT, "data_pipeline", "data")

# Index data
NIFTY_INDEX_FILE    = os.path.join(PIPELINE_DATA, "indices", "nifty.csv")
VIX_INDEX_FILE      = os.path.join(PIPELINE_DATA, "indices", "india_vix.csv")

# Nifty options — one folder per expiry date (YYYY-MM-DD)
NIFTY_OPTIONS_PATH  = os.path.join(PIPELINE_DATA, "nifty", "options")

# Contract list — lives in data_pipeline/config/ alongside the downloader scripts
CONTRACT_LIST_FILE  = os.path.join(REPO_ROOT, "data_pipeline", "config", "options_list_nf.csv")

# Precomputed intermediate files (written by precompute.py, read by backtest.py)
PRECOMPUTED_DIR     = os.path.join(os.path.dirname(__file__), "data")
NIFTY_15MIN_FILE    = os.path.join(PRECOMPUTED_DIR, "nifty_15min.csv")
NIFTY_75MIN_FILE    = os.path.join(PRECOMPUTED_DIR, "nifty_75min.csv")
VIX_DAILY_FILE      = os.path.join(PRECOMPUTED_DIR, "vix_daily.csv")

# Trade output
TRADE_LOGS_DIR      = os.path.join(os.path.dirname(__file__), "data", "trade_logs")
TRADE_SUMMARY_FILE  = os.path.join(os.path.dirname(__file__), "data", "trade_summary.csv")

# ---------------------------------------------------------------------------
# VIX Regime Filter
# ---------------------------------------------------------------------------
VIX_THRESHOLD           = 16.0      # Deploy strategy only when VIX > this value
VIX_CAUTION_LOW         = 14.0      # Below this: iron condor territory
VIX_CAUTION_HIGH        = 16.0      # Above this: Apollo strategy territory

# ---------------------------------------------------------------------------
# Supertrend Parameters
# ---------------------------------------------------------------------------
# Higher timeframe — defines trend regime
ST_75MIN_PERIOD         = 10        # ATR period
ST_75MIN_MULTIPLIER     = 3.0       # Band multiplier — higher = fewer but cleaner regime flips

# Lower timeframe — entry/exit trigger
ST_15MIN_PERIOD         = 10        # ATR period
ST_15MIN_MULTIPLIER     = 3.0       # Band multiplier

# Timeframes (in minutes) — derived from market session of 375 minutes
TF_HIGH                 = 75        # 375 / 5 = 75 min, gives exactly 5 candles/day
TF_LOW                  = 15        # Entry/exit trigger timeframe

# ---------------------------------------------------------------------------
# Options Parameters
# ---------------------------------------------------------------------------
SPREAD_TYPE             = 'credit'  # 'credit' or 'debit'
TARGET_DELTA            = 0.20      # Sell option with delta <= this value
HEDGE_POINTS            = 300       # Distance of bought option from sold option (points)
STRIKE_STEP             = 50        # Nifty strike interval

# Expiry roll threshold
MIN_DTE                 = 2         # If DTE < this, roll to next expiry

# ---------------------------------------------------------------------------
# Stop Loss Parameters
# Five SL mechanisms run concurrently — first to trigger exits the trade.
# Each of the four non-trend-flip SLs can be individually toggled on/off.
# ---------------------------------------------------------------------------

# No exit at 09:15 candle — defer to 09:16 and re-check (15-min fallback only)
NO_EXIT_BEFORE          = '09:16'

# ------ SL Toggles --------------------------------------------------------
# Set to False to disable that SL entirely for a run (useful for optimisation)
ENABLE_INDEX_SL         = False
ENABLE_OPTION_SL        = False
ENABLE_SPREAD_SL        = False
ENABLE_TRAILING_SL      = True

# ------ 1. Index SL -------------------------------------------------------
# Exit when spot is within INDEX_SL_OFFSET points of the sell strike (OTM side).
# Bearish (sold CE): fires when spot >= sell_strike - INDEX_SL_OFFSET
# Bullish (sold PE): fires when spot <= sell_strike + INDEX_SL_OFFSET
INDEX_SL_OFFSET         = 50        # points from sell strike to trigger exit

# ------ 2. Option SL ------------------------------------------------------
# Based solely on the sold option's LTP vs its entry price.
# Multiplier steps down each calendar day in trade, floors at OPTION_SL_FLOOR_MULT.
# Exit if: sell_ltp >= sell_entry * multiplier_for_day
# Day 0: 2.00x, Day 1: 1.66x, Day 2: 1.33x, Day 3: 1.00x, Day 4: 0.66x,
# Day 5+: 0.33x (floor)
OPTION_SL_MULTIPLIERS   = [2.00, 1.66, 1.33, 1.00, 0.66]  # index = days_in_trade
OPTION_SL_FLOOR_MULT    = 0.33      # multiplier used from Day 5 onwards

# ------ 3. Spread SL ------------------------------------------------------
# Based on unrealised P&L of the entire spread vs net credit received.
# Steps down each calendar day in trade, floors at SPREAD_SL_FLOOR_PCT (0 = breakeven).
# Exit if: unrealised_pl_pts <= -net_credit * pct_for_day
# Day 0: 50%, Day 1: 40%, Day 2: 30%, Day 3: 20%, Day 4: 10%, Day 5+: 0%
SPREAD_SL_PCTS          = [0.50, 0.40, 0.30, 0.20, 0.10]  # index = days_in_trade
SPREAD_SL_FLOOR_PCT     = 0.00      # floor pct used from Day 5 onwards

# ------ 4. Trailing SL ----------------------------------------------------
# Ratchet SL on unrealised P&L — activates once profit threshold is reached,
# then can only move up (never loosens). Checked against unrealised_pl_pts.
# Persistent state 'trailing_sl_floor' is tracked in trade state.
#
# Stage 1: activates when unrealised_pl_pts >= net_credit * TRAILING_SL_TRIGGER_1
#          floor set to net_credit * TRAILING_SL_FLOOR_1 (0 = breakeven)
# Stage 2: upgrades when unrealised_pl_pts >= net_credit * TRAILING_SL_TRIGGER_2
#          floor moves to net_credit * TRAILING_SL_FLOOR_2
# Stage 3: upgrades when unrealised_pl_pts >= net_credit * TRAILING_SL_TRIGGER_3
#          floor moves to net_credit * TRAILING_SL_FLOOR_3
TRAILING_SL_TRIGGER_1   = 0.3333   # activate at 33.33% of net credit
TRAILING_SL_FLOOR_1     = 0.00     # lock in breakeven (0% of net credit)
TRAILING_SL_TRIGGER_2   = 0.6666   # upgrade at 66.66% of net credit
TRAILING_SL_FLOOR_2     = 0.3333   # lock in 33.33% of net credit
TRAILING_SL_TRIGGER_3   = 0.80     # upgrade at 80% of net credit
TRAILING_SL_FLOOR_3     = 0.40     # lock in 40% of net credit

# ---------------------------------------------------------------------------
# Additional Lots & ELM (Extra Loss Margin)
# ---------------------------------------------------------------------------
# For every 2 base lots, 1 additional lot is traded (same strikes, same spread).
# This utilises idle margin and is normalised to per-₹1,04,000 unit for reporting.
# additional_lots = base_lots // 2  →  multiplier = 0.5 always
ADDITIONAL_LOT_MULTIPLIER = 0.5

# Capital per reporting unit — used for lot sizing context
LOT_CAPITAL             = 104000    # Rs per unit of base position

# ELM: exit additional lots at 15:15 the day before expiry (Monday for Nifty Tuesday expiry)
# elm_time = expiry - 87300 seconds (24h 15min)
# Holiday adjustment: if the day before expiry is a holiday, move elm_time back one day
ELM_SECONDS_BEFORE_EXPIRY = 87300  # 24h 15min in seconds

# ---------------------------------------------------------------------------
# Execution Assumptions
# ---------------------------------------------------------------------------
# Use 'open' of the next candle after signal for entry/exit pricing
# This simulates realistic execution — signal fires on candle close,
# order executes at next candle open
ENTRY_PRICE_COL         = 'open'
EXIT_PRICE_COL          = 'open'

# Slippage model: add this many points to entry cost, subtract from exit proceeds
# Applied per leg
SLIPPAGE_POINTS         = 1.0

# ---------------------------------------------------------------------------
# Backtest Scope
# ---------------------------------------------------------------------------
# Date range for backtest — set to None to use full available data
BACKTEST_START_DATE     = '2020-01-01'
BACKTEST_END_DATE       = None      # None = use all available data

# Lot size
LOT_SIZE                = 75        # Nifty lot size (update if SEBI changes this)

# ---------------------------------------------------------------------------
# Options Pricing
# ---------------------------------------------------------------------------
RISK_FREE_RATE          = 5.0       # Annualised risk-free rate (%) — RBI repo rate approx