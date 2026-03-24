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
ST_75MIN_MULTIPLIER     = 3.0       # Band multiplier

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
# All three are checked — first to trigger exits the trade
# ---------------------------------------------------------------------------
# 1. Index-based stop: exit when spot is within INDEX_SL_OFFSET points of
#    the sell strike (i.e. approaching ATM, still OTM). Once delta crosses
#    ~0.50 gamma becomes brutal — we exit before that.
#    For bearish (sold CE): SL when spot >= sell_strike - INDEX_SL_OFFSET
#    For bullish (sold PE): SL when spot <= sell_strike + INDEX_SL_OFFSET
INDEX_SL_OFFSET         = 50        # points before sell strike reaches ATM

# No exit at 09:15 — defer to 09:16 and re-check to avoid wild price discovery
NO_EXIT_BEFORE          = '09:16'

# 2. Option premium multiplier stop
OPTION_SL_MULTIPLIER    = 2.0       # Exit if sold option LTP > entry * this multiplier

# 3. Spread loss cap (for credit spread: % of max possible loss on the spread)
#    For debit spread: % of premium paid
SPREAD_LOSS_CAP         = 0.75      # Exit if spread has lost 75% of max loss

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