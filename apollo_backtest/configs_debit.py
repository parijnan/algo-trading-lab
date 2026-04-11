"""
configs_debit.py — Apollo Debit Spread Backtest Configuration
All parameters in one place. Tweak here, nothing else needs to change.

Signal and Supertrend parameters are identical to the credit spread baseline.
Debit spread specific parameters are in the lower sections.
"""

import os

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINE_DATA   = os.path.join(REPO_ROOT, "data_pipeline", "data")

NIFTY_INDEX_FILE    = os.path.join(PIPELINE_DATA, "indices", "nifty.csv")
VIX_INDEX_FILE      = os.path.join(PIPELINE_DATA, "indices", "india_vix.csv")
NIFTY_OPTIONS_PATH  = os.path.join(PIPELINE_DATA, "nifty", "options")
CONTRACT_LIST_FILE  = os.path.join(REPO_ROOT, "data_pipeline", "config", "options_list_nf.csv")

PRECOMPUTED_DIR     = os.path.join(os.path.dirname(__file__), "data")
NIFTY_15MIN_FILE    = os.path.join(PRECOMPUTED_DIR, "nifty_15min.csv")
NIFTY_75MIN_FILE    = os.path.join(PRECOMPUTED_DIR, "nifty_75min.csv")
VIX_DAILY_FILE      = os.path.join(PRECOMPUTED_DIR, "vix_daily.csv")

TRADE_LOGS_DIR      = os.path.join(os.path.dirname(__file__), "data", "trade_logs_debit")
TRADE_SUMMARY_FILE  = os.path.join(os.path.dirname(__file__), "data", "trade_summary_debit.csv")

# ---------------------------------------------------------------------------
# VIX Regime Filter
# ---------------------------------------------------------------------------
VIX_THRESHOLD           = 16.0      # Deploy strategy only when VIX > this value

# ---------------------------------------------------------------------------
# Supertrend Parameters — confirmed optimum from credit spread phase
# ---------------------------------------------------------------------------
ST_75MIN_PERIOD         = 10
ST_75MIN_MULTIPLIER     = 3.0       # Confirmed optimum

ST_15MIN_PERIOD         = 10
ST_15MIN_MULTIPLIER     = 3.0       # Confirmed optimum

TF_HIGH                 = 75        # 375 / 5 = exactly 5 candles/day
TF_LOW                  = 15

# ---------------------------------------------------------------------------
# Spread Type
# ---------------------------------------------------------------------------
SPREAD_TYPE             = 'debit'

# ---------------------------------------------------------------------------
# Debit Spread — Strike Selection
# ---------------------------------------------------------------------------
STRIKE_STEP             = 50        # Nifty strike interval

# Buy leg offset from ATM in index points.
# 0 = ATM (starting assumption), negative = ITM, positive = OTM.
# Bullish (buying PE): buy_strike = ATM + BUY_LEG_OFFSET
# Bearish (buying CE): buy_strike = ATM - BUY_LEG_OFFSET
# where ATM = round(spot / STRIKE_STEP) * STRIKE_STEP
BUY_LEG_OFFSET          = -50        # starting assumption: ATM

# Spread width — distance from buy leg to sell leg in index points.
# Sell leg is placed further OTM from the buy leg.
# max_profit = HEDGE_POINTS - net_debit
HEDGE_POINTS            = 300      # starting assumption: 100pts (test 50/150/200)

# Expiry roll threshold
MIN_DTE                 = 2        # If DTE < this, roll to next expiry

# ---------------------------------------------------------------------------
# Debit Spread — Exit Mechanisms
# All exits fire on 1-min candle closes, checked continuously.
# First to trigger wins. trend_flip and expiry are always active.
# ---------------------------------------------------------------------------

# No exit at 09:15 candle — defer to 09:16 and re-check (15-min fallback only)
NO_EXIT_BEFORE          = '09:16'

# ------ Exit Toggles -------------------------------------------------------
# Set to False to disable that exit for a run (all off = D-R01 calibration)
ENABLE_HARD_STOP        = True
ENABLE_PROFIT_TARGET    = True
ENABLE_DAY0_SPREAD_SL   = False
ENABLE_TIME_GATE        = True
ENABLE_TRAILING_PROFIT  = False

# ------ 1. Profit Target ---------------------------------------------------
# VIX-sensitive profit target — uses entry_vix to select threshold.
# entry_vix < PROFIT_TARGET_VIX_LOW:                    PCT_LOW_VIX
# PROFIT_TARGET_VIX_LOW <= entry_vix < VIX_HIGH:        PCT_MID_VIX
# entry_vix >= PROFIT_TARGET_VIX_HIGH:                  PCT_HIGH_VIX
#
# To use a uniform threshold: set all three PCT values to the same number.
# Calibrated from D-R01f winner depth analysis:
#   VIX < 20:  50% — median winner peak 62-64%, well served by 50%
#   VIX 20-30: 40% — shallow winners in 20-23 band, only 17-50% reach 50%
#   VIX >= 30: 60% — deep winners, mean peak 149%, room to run past 50%
PROFIT_TARGET_VIX_LOW       = 20.0
PROFIT_TARGET_VIX_HIGH      = 30.0
PROFIT_TARGET_PCT_LOW_VIX   = 0.50    # VIX < 20
PROFIT_TARGET_PCT_MID_VIX   = 0.50    # VIX 20–30
PROFIT_TARGET_PCT_HIGH_VIX  = 0.50    # VIX >= 30

# ------ 2. Day 0 Spread SL ------------------------------------------------
# Active ONLY on the entry day (days_in_trade == 0).
# Automatically inactive from Day 1 onwards — no additional toggle needed.
# Exit if: unrealised_pl < -net_debit * DAY0_SPREAD_SL_PCT
#
# Calibrated from D-R08 Day 0 analysis:
#   Day 0 winners median dip: -6.3% of net debit
#   Day 0 losers  median dip: -34.5% of net debit
#   At -20%: catches 30/36 losers (83%), stops 1/8 winners (12%)
DAY0_SPREAD_SL_PCT      = 0.20        # exit if loss > 20% of net debit on Day 0

# ------ 3. Time Gate -------------------------------------------------------
# Exit dead trades that have shown no life within N calendar days.
# Gate activation: first trading day on or after entry_date + TIME_GATE_DAYS.
# Weekends and holidays are skipped — gate_date is always a trading day.
# Both conditions must be true simultaneously to fire:
#   - current date >= gate_date AND current time >= TIME_GATE_CHECK_TIME
#   - max unrealised P&L so far < gate_min_profit_pct * max_profit
# Trades that have ever touched the threshold survive the gate.
#
# VIX-sensitive threshold — uses entry_vix to select gate threshold.
# entry_vix < TIME_GATE_VIX_THRESHOLD:  TIME_GATE_MIN_PROFIT_PCT_LOW_VIX
# entry_vix >= TIME_GATE_VIX_THRESHOLD: TIME_GATE_MIN_PROFIT_PCT_HIGH_VIX
#
# To use a uniform threshold: set both PCT values to the same number.
# Calibrated from D-R01f analysis:
#   VIX < 23:  25% — near-perfect loser/winner separation in 20-23 band
#   VIX >= 23: 33% — current confirmed optimum
TIME_GATE_DAYS                      = 1
TIME_GATE_CHECK_TIME                = '09:30'
TIME_GATE_VIX_THRESHOLD             = 23.0
TIME_GATE_MIN_PROFIT_PCT_LOW_VIX    = 0.33    # VIX < 23
TIME_GATE_MIN_PROFIT_PCT_HIGH_VIX   = 0.33    # VIX >= 23

# ------ 3. Trailing Profit Lock --------------------------------------------
# Ratchet on unrealised P&L as % of max_profit. Activates at Stage 1 trigger,
# then only ever moves up — never loosens. Persistent state tracked per trade.
#
# trigger: unrealised_pl < trailing_profit_floor (once any stage active)
#
# VIX gate: trailing is only active for trades where entry_vix >= TRAIL_VIX_THRESHOLD.
# Set TRAIL_VIX_THRESHOLD = 0.0 to enable for all trades.
# Set TRAIL_VIX_THRESHOLD = 999.0 to disable for all trades.
# ENABLE_TRAILING_PROFIT = False disables trailing entirely regardless of VIX.
#
# Stage 1: activates when unrealised_pl >= max_profit * TRAIL_TRIGGER_1
#          floor set to max_profit * TRAIL_FLOOR_1
# Stage 2: upgrades when unrealised_pl >= max_profit * TRAIL_TRIGGER_2
#          floor moves to max_profit * TRAIL_FLOOR_2
# Stage 3: upgrades when unrealised_pl >= max_profit * TRAIL_TRIGGER_3
#          floor moves to max_profit * TRAIL_FLOOR_3
# Calibrated from D-R07c: tight settings for high-VIX regimes.
TRAIL_VIX_THRESHOLD     = 20.0    # trailing active only when entry_vix >= this value

TRAIL_TRIGGER_1         = 0.15    # activate at 20% of max profit
TRAIL_FLOOR_1           = 0.05    # lock in 10% of max profit

TRAIL_TRIGGER_2         = 0.25    # upgrade at 35% of max profit
TRAIL_FLOOR_2           = 0.15    # lock in 20% of max profit

TRAIL_TRIGGER_3         = 0.40    # upgrade at 45% of max profit
TRAIL_FLOOR_3           = 0.25    # lock in 30% of max profit

# ------ Hard Stop Loss -----------------------------------------------------
# Exits the full position when unrealised P&L drops to or below this level.
# Fires on every 1-min candle close. Executes at next 1-min candle open.
# Primary purpose: risk management and trading psychology.
# Set HARD_STOP_POINTS to a large number (e.g. 9999) to effectively disable
# without toggling ENABLE_HARD_STOP.
#
# Calibrated from D-R03fg analysis:
#   Worst loser in dataset: -83.95 pts
#   Losers breaching -65: 10/104 (10%), avg saving +7.1 pts each
#   Winners breaching -65: 1/103 (1%), cost acceptable
#   Net P&L impact: approximately flat (-Rs 184)
HARD_STOP_POINTS        = 67.5        # exit when unrealised_pl <= -67.5 pts

# ---------------------------------------------------------------------------
# Additional Lots & ELM (Extra Loss Margin)
# ---------------------------------------------------------------------------
# Set ENABLE_ADDITIONAL_LOTS = False for single-lot baseline runs.
# When False, has_additional is never set — base lot only, no ELM exit needed.
# When True, 1 additional lot per 2 base lots; P&L = base_pl + add_pl * 0.5
ENABLE_ADDITIONAL_LOTS    = False      # False = single lot baseline
ADDITIONAL_LOT_MULTIPLIER = 0.5
LOT_CAPITAL               = 100000
ELM_SECONDS_BEFORE_EXPIRY = 87300     # 24h 15min — full position exits at 15:15 day before expiry

# ---------------------------------------------------------------------------
# Execution Assumptions
# ---------------------------------------------------------------------------
SLIPPAGE_POINTS         = 1.0      # Per leg; not applied on expiry exits

# ---------------------------------------------------------------------------
# Backtest Scope
# ---------------------------------------------------------------------------
BACKTEST_START_DATE     = '2020-01-01'
BACKTEST_END_DATE       = None

LOT_SIZE                = 75       # Nifty lot size
# ---------------------------------------------------------------------------
# Entry Filters
# Pre-entry conditions checked on every valid Supertrend flip signal.
# If any filter rejects, the entry is skipped entirely.
# In-trade management is completely unaffected — these only block new entries.
# A trade already open on a filtered day continues to be managed normally.
# ---------------------------------------------------------------------------

# Filter 1: Excluded days of week.
# 0=Monday, 1=Tuesday, 2=Wednesday, 3=Thursday, 4=Friday
# Evaluated on signal candle day (ts.dayofweek), not entry execution day.
# A Monday signal that enters at Tuesday open is treated as a Monday signal.
# Set to [] to disable.
# Tuesday (1) = Nifty weekly expiry day. Expiry-day dynamics break trend-following.
#   39 trades, 38.5% WR, -Rs 18,709 — only day with negative P&L.
EXCLUDE_TRADE_DAYS      = [1]          # e.g. [1] to exclude Tuesday

# Direction-specific day exclusion — checked IN ADDITION TO EXCLUDE_TRADE_DAYS.
# EXCLUDE_TRADE_DAYS blocks both directions; these block one direction only.
# Day encoding: 0=Monday, 1=Tuesday, 2=Wednesday, 3=Thursday, 4=Friday
# Evaluated on exec_ts.dayofweek (entry execution candle day).
EXCLUDE_BEARISH_DAYS    = []          # e.g. [0] excludes bearish entries on Monday
EXCLUDE_BULLISH_DAYS    = []          # e.g. [0] excludes bullish entries on Monday

# Filter 2: Excluded signal candle close times.
# Apollo signals fire on 15-min candle CLOSE; entry executes at NEXT candle OPEN.
# This list contains the SIGNAL CANDLE CLOSE times to block (not the entry times).
#   Signal '09:45' close → entry at 10:00 open  (market re-settling, 40% WR)
#   Signal '10:00' close → entry at 10:15 open  (dead zone, 0% WR historically)
# Format: list of 'HH:MM' strings. Set to [] to disable.
EXCLUDE_SIGNAL_CANDLES  = []          # e.g. ['09:45', '10:00']

# ---------------------------------------------------------------------------
# Direction-Specific Parameter Overrides
# When set (not None), these override the corresponding base parameter for
# that direction only. When None, the base parameter applies to both.
# Resolution happens once at entry time — not re-evaluated per candle.
# Validation: all None must reproduce identical results to base config.
# ---------------------------------------------------------------------------

# Profit Target — direction-specific PCT override.
# Overrides PROFIT_TARGET_PCT_LOW/MID/HIGH_VIX for that direction.
# None = use the VIX-band base PT for both directions.
PROFIT_TARGET_PCT_BULL      = 0.35   # e.g. 0.30
PROFIT_TARGET_PCT_BEAR      = 0.60   # e.g. 0.50

# Time Gate — direction-specific threshold override.
# Overrides TIME_GATE_MIN_PROFIT_PCT_LOW/HIGH_VIX for that direction.
# None = use the VIX-band base gate PCT for both directions.
TIME_GATE_MIN_PROFIT_PCT_BULL = 0.25  # e.g. 0.20
TIME_GATE_MIN_PROFIT_PCT_BEAR = 0.35  # e.g. 0.33

# Time Gate — direction-specific days override.
# Overrides TIME_GATE_DAYS for that direction only.
# None = use TIME_GATE_DAYS for both directions.
TIME_GATE_DAYS_BULL         = 1   # e.g. 2
TIME_GATE_DAYS_BEAR         = 1   # e.g. 1

# Hard Stop — direction-specific points override.
# Overrides HARD_STOP_POINTS for that direction only.
# None = use HARD_STOP_POINTS for both directions.
HARD_STOP_POINTS_BULL       = 40   # e.g. 50.0
HARD_STOP_POINTS_BEAR       = 67.5   # e.g. 67.5

# Bullish VIX exclusion — skip bullish entry signals when entry_vix exceeds
# this level. Checked at entry time using the same VIX value as PT/gate lookups.
# None = trade all VIX bands for bullish signals.
EXCLUDE_BULLISH_VIX_ABOVE   = None   # e.g. 20.0