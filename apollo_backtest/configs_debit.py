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
# 0 = ATM (starting assumption), negative = OTM, positive = ITM.
# Bullish (buying PE): buy_strike = ATM + BUY_LEG_OFFSET
# Bearish (buying CE): buy_strike = ATM - BUY_LEG_OFFSET
# where ATM = round(spot / STRIKE_STEP) * STRIKE_STEP
BUY_LEG_OFFSET          = 0        # starting assumption: ATM

# Spread width — distance from buy leg to sell leg in index points.
# Sell leg is placed further OTM from the buy leg.
# max_profit = HEDGE_POINTS - net_debit
HEDGE_POINTS            = 100      # starting assumption: 100pts (test 50/150/200)

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
ENABLE_PROFIT_TARGET    = False
ENABLE_TIME_GATE        = False
ENABLE_TRAILING_PROFIT  = False

# ------ 1. Profit Target ---------------------------------------------------
# Exit when unrealised P&L reaches a % of max spread profit.
# max_profit = HEDGE_POINTS - net_debit
# trigger: unrealised_pl >= max_profit * PROFIT_TARGET_PCT
# Calibrated from D-R01: only 8.6% of winners reach 60%; 60% reach 20%.
PROFIT_TARGET_PCT       = 0.20     # exit at 20% of max spread profit

# ------ 2. Time Gate -------------------------------------------------------
# Exit dead trades that have shown no life within N calendar days.
# Gate activation: first trading day on or after entry_date + TIME_GATE_DAYS.
# Weekends and holidays are skipped — gate_date is always a trading day.
# Both conditions must be true simultaneously to fire:
#   - current date >= gate_date AND current time >= TIME_GATE_CHECK_TIME
#   - max unrealised P&L so far < TIME_GATE_MIN_PROFIT_PCT * max_profit
# Trades that have ever touched TIME_GATE_MIN_PROFIT_PCT survive the gate.
TIME_GATE_DAYS          = 1        # calendar days before gate activates
TIME_GATE_MIN_PROFIT_PCT = 0.20   # must have reached 20% of max profit to survive
TIME_GATE_CHECK_TIME    = '09:30'  # gate evaluates from this time on gate day
                                    # 09:30 = after first 15-min candle has closed
                                    # avoids noisy gap-open 09:15 candle

# ------ 3. Trailing Profit Lock --------------------------------------------
# Ratchet on unrealised P&L as % of max_profit. Activates at Stage 1 trigger,
# then only ever moves up — never loosens. Persistent state tracked per trade.
#
# trigger: unrealised_pl < trailing_profit_floor (once any stage active)
#
# Stage 1: activates when unrealised_pl >= max_profit * TRAIL_TRIGGER_1
#          floor set to max_profit * TRAIL_FLOOR_1
# Stage 2: upgrades when unrealised_pl >= max_profit * TRAIL_TRIGGER_2
#          floor moves to max_profit * TRAIL_FLOOR_2
# Stage 3: upgrades when unrealised_pl >= max_profit * TRAIL_TRIGGER_3
#          floor moves to max_profit * TRAIL_FLOOR_3
# Calibrated from D-R01: only 25% of winners reach 40% of max profit.
TRAIL_TRIGGER_1         = 0.20    # activate at 20% of max profit
TRAIL_FLOOR_1           = 0.10    # lock in 10% of max profit

TRAIL_TRIGGER_2         = 0.35    # upgrade at 35% of max profit
TRAIL_FLOOR_2           = 0.20    # lock in 20% of max profit

TRAIL_TRIGGER_3         = 0.50    # upgrade at 50% of max profit
TRAIL_FLOOR_3           = 0.30    # lock in 30% of max profit

# ---------------------------------------------------------------------------
# Additional Lots & ELM (Extra Loss Margin)
# ---------------------------------------------------------------------------
ADDITIONAL_LOT_MULTIPLIER = 0.5
LOT_CAPITAL             = 104000
ELM_SECONDS_BEFORE_EXPIRY = 87300  # 24h 15min in seconds

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