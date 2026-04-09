"""
configs_debit_phase2.py — Apollo Phase 2 Triple-Timeframe Backtest Configuration
All parameters in one place. Tweak here, nothing else needs to change.

Phase 2 adds a 5-min Supertrend as the entry/exit trigger.
75-min ST defines the regime. 15-min ST must be aligned (no flip required).
5-min ST must flip into alignment with both higher timeframes to trigger entry.
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

PRECOMPUTED_DIR     = os.path.join(os.path.dirname(__file__), "data")
NIFTY_5MIN_FILE     = os.path.join(PRECOMPUTED_DIR, "nifty_5min_phase2.csv")
NIFTY_15MIN_FILE    = os.path.join(PRECOMPUTED_DIR, "nifty_15min_phase2.csv")
NIFTY_75MIN_FILE    = os.path.join(PRECOMPUTED_DIR, "nifty_75min_phase2.csv")
VIX_DAILY_FILE      = os.path.join(PRECOMPUTED_DIR, "vix_daily_phase2.csv")
TRADE_LOGS_DIR      = os.path.join(PRECOMPUTED_DIR, "trade_logs_phase2")
TRADE_SUMMARY_FILE  = os.path.join(PRECOMPUTED_DIR, "trade_summary_phase2.csv")

# ---------------------------------------------------------------------------
# VIX Regime Filter
# ---------------------------------------------------------------------------
VIX_THRESHOLD               = 16.0

# ---------------------------------------------------------------------------
# Supertrend Parameters — THREE timeframes
# ---------------------------------------------------------------------------
# Regime timeframe — defines the trend direction
ST_75MIN_PERIOD             = 10
ST_75MIN_MULTIPLIER         = 3.0
TF_HIGH                     = 75

# Context timeframe — must be aligned with 75-min (no flip required for entry)
ST_15MIN_PERIOD             = 10
ST_15MIN_MULTIPLIER         = 3.0
TF_MID                      = 15

# Entry/exit trigger timeframe — flip into alignment triggers entry and exit
ST_5MIN_PERIOD              = 10           # optimisable
ST_5MIN_MULTIPLIER          = 3.0          # optimisable
TF_LOW                      = 5

# ---------------------------------------------------------------------------
# Options Parameters
# ---------------------------------------------------------------------------
SPREAD_TYPE                 = 'debit'
BUY_LEG_OFFSET              = -50
HEDGE_POINTS                = 300
STRIKE_STEP                 = 50
MIN_DTE                     = 2

# ---------------------------------------------------------------------------
# Entry Filters
# ---------------------------------------------------------------------------
# Filter 1: Excluded days of week. 0=Mon … 4=Fri
# Tuesday (1) = Nifty weekly expiry. Confirmed harmful in Phase 1.
EXCLUDE_TRADE_DAYS          = [0]

# Filter 2: Excluded signal candle close times ('HH:MM').
# Phase 1 values (09:45, 10:00) were 15-min candle times — do not carry over.
# Leave empty for D5-R01. Re-derive from 5-min signal distribution.
EXCLUDE_SIGNAL_CANDLES      = []

# ---------------------------------------------------------------------------
# Exit Mechanisms
# All exits fire on 1-min candle closes, checked continuously.
# First to trigger wins. trend_flip_5 and expiry are always active.
# ---------------------------------------------------------------------------

# Gap open guard — defers hard stop and profit target from firing on 09:15
# 1-min candle close; re-checks at 09:16. Trend flip cannot fire before
# 09:20 (first 5-min candle close) so no separate guard is needed for it.
NO_EXIT_BEFORE              = '09:16'

# ------ Exit Toggles -------------------------------------------------------
# Set to False to disable that exit for a run (all off = D5-R01 calibration)
ENABLE_HARD_STOP            = False
ENABLE_PROFIT_TARGET        = False
ENABLE_DAY0_SPREAD_SL       = False
ENABLE_TIME_GATE            = False
ENABLE_TIME_GATE_HOURS      = False
ENABLE_TRAILING_PROFIT      = False

# ------ Hard Stop Loss -----------------------------------------------------
# Fires when unrealised P&L <= -HARD_STOP_POINTS on any 1-min candle close.
# Executes at open of next 1-min candle. Phase 1 confirmed optimum: 67.5 pts.
HARD_STOP_POINTS            = 67.5         # optimisable

# ------ Profit Target ------------------------------------------------------
# VIX-sensitive. Uses entry_vix to select threshold band.
# To use uniform threshold: set all three PCT values to the same number.
PROFIT_TARGET_VIX_LOW       = 20.0
PROFIT_TARGET_VIX_HIGH      = 30.0
PROFIT_TARGET_PCT_LOW_VIX   = 0.50         # VIX < 20
PROFIT_TARGET_PCT_MID_VIX   = 0.50         # VIX 20-30 (optimisable)
PROFIT_TARGET_PCT_HIGH_VIX  = 0.50         # VIX >= 30 (optimisable)

# ------ Day 0 Spread SL ----------------------------------------------------
# Active ONLY on entry day (days_in_trade == 0).
# Exit if unrealised_pl < -net_debit * DAY0_SPREAD_SL_PCT.
DAY0_SPREAD_SL_PCT          = 0.20         # optimisable

# ------ Time Gate ----------------------------------------------------------
# Exits dead trades at TIME_GATE_CHECK_TIME on gate day if max unrealised P&L
# has never reached TIME_GATE_MIN_PROFIT_PCT * max_profit.
# VIX-sensitive threshold — set both PCT values equal for uniform behaviour.
TIME_GATE_DAYS              = 1            # optimisable
TIME_GATE_CHECK_TIME        = '09:30'      # optimisable
TIME_GATE_VIX_THRESHOLD     = 23.0
TIME_GATE_MIN_PROFIT_PCT_LOW_VIX  = 0.33  # VIX < 23  (optimisable)
TIME_GATE_MIN_PROFIT_PCT_HIGH_VIX = 0.33  # VIX >= 23 (optimisable)

# ------ Hours-After-Entry Time Gate ----------------------------------------
# Fires N hours after the specific entry timestamp if max unrealised P&L has
# not reached the threshold. Works for entries at any time of day — unlike
# the calendar-day gate which fires at a fixed clock time and silently misses
# trades entered after TIME_GATE_CHECK_TIME when TIME_GATE_DAYS=0.
# Both gates can run simultaneously — first to trigger wins.
# Exit reason: 'time_gate_hours'
ENABLE_TIME_GATE_HOURS          = True        # set True to activate
TIME_GATE_HOURS                 = 3.0          # hours after entry — optimisable
TIME_GATE_HOURS_MIN_PROFIT_PCT  = 0.10         # % of max_profit required — optimisable

# ------ Trailing Profit Lock -----------------------------------------------
# Ratchet on unrealised P&L as % of max_profit.
# VIX-gated: only active when entry_vix >= TRAIL_VIX_THRESHOLD.
# ENABLE_TRAILING_PROFIT = False disables regardless of VIX.
TRAIL_VIX_THRESHOLD         = 20.0
TRAIL_TRIGGER_1             = 0.20
TRAIL_FLOOR_1               = 0.10
TRAIL_TRIGGER_2             = 0.35
TRAIL_FLOOR_2               = 0.20
TRAIL_TRIGGER_3             = 0.45
TRAIL_FLOOR_3               = 0.30

# ---------------------------------------------------------------------------
# Additional Lots & ELM (Extra Loss Margin)
# ---------------------------------------------------------------------------
ENABLE_ADDITIONAL_LOTS      = False
ADDITIONAL_LOT_MULTIPLIER   = 0.5          # inactive when ENABLE_ADDITIONAL_LOTS = False
ELM_SECONDS_BEFORE_EXPIRY   = 87300        # 15:15 day before expiry

# ---------------------------------------------------------------------------
# Execution Assumptions
# ---------------------------------------------------------------------------
SLIPPAGE_POINTS             = 1.0          # per leg; not applied on expiry exits
LOT_SIZE                    = 75
BACKTEST_START_DATE         = '2020-01-01'
BACKTEST_END_DATE           = None
LOT_CAPITAL                 = 100000