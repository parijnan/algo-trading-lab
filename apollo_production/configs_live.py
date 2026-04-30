"""
configs_live.py — Apollo Production Configuration
All parameters in one place. Tweak here, nothing else needs to change.

Frozen production config: D-R-D06g
Strategy: ITM debit spread, direction-split PT/gate/hard stop
Entry filters: no Tuesday trades, direction-specific day and candle exclusions

Credentials are loaded once at module level from data/user_credentials.csv.
All other modules import from here — credentials are never loaded twice.
"""

import os
import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DATA_DIR        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LOGS_DIR        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

STATE_FILE      = os.path.join(DATA_DIR, "apollo_state.csv")
TRADES_FILE     = os.path.join(DATA_DIR, "apollo_trades.csv")
ST_CACHE_FILE         = os.path.join(DATA_DIR, "supertrend_cache.csv")

# ---------------------------------------------------------------------------
# Credentials — loaded once at module level
# ---------------------------------------------------------------------------
CREDENTIALS_FILE = os.path.join(DATA_DIR, "user_credentials.csv")

_creds      = pd.read_csv(CREDENTIALS_FILE).iloc[0]
api_key     = _creds['api_key']
user_name   = _creds['user_name']
password    = str(_creds['password'])
qr_code     = _creds['qr_code']
slack_token = _creds['slack_token']
bot_token   = _creds['bot_token']
bot_id      = str(_creds['bot_id'])
channel_id  = _creds['channel_id']

# ---------------------------------------------------------------------------
# Instrument tokens — fixed, never change
# ---------------------------------------------------------------------------
NIFTY_INDEX_TOKEN   = "99926000"    # Nifty 50 index — NSE CM
VIX_TOKEN           = "99926017"    # India VIX      — NSE CM

# ---------------------------------------------------------------------------
# Market session
# ---------------------------------------------------------------------------
MARKET_OPEN         = "09:15"
MARKET_CLOSE        = "15:30"

# ---------------------------------------------------------------------------
# VIX regime filter
# ---------------------------------------------------------------------------
VIX_THRESHOLD       = 16.0          # Deploy only when today's opening VIX > this

# ---------------------------------------------------------------------------
# Supertrend parameters
# Must match D-R-P2c exactly — do not change without re-running backtest
# ---------------------------------------------------------------------------
ST_75MIN_PERIOD     = 10
ST_75MIN_MULTIPLIER = 3.0

ST_15MIN_PERIOD     = 10
ST_15MIN_MULTIPLIER = 3.0

TF_HIGH             = 75            # Higher timeframe in minutes
TF_LOW              = 15            # Lower timeframe in minutes

# Number of historical 15-min candles to fetch at session start for ST seeding.
# 200 candles ~= 10 trading days. Adjustable without code changes.
ST_HISTORY_CANDLES  = 600

# ---------------------------------------------------------------------------
# Options / spread structure
# ---------------------------------------------------------------------------
SPREAD_TYPE         = 'debit'
BUY_LEG_OFFSET      = -50           # ITM: -50 from ATM. Negative = ITM for both CE and PE.
HEDGE_POINTS        = 300           # OTM sell leg distance from buy leg
STRIKE_STEP         = 50            # Nifty strike interval
MIN_DTE             = 2             # Roll to next expiry if DTE < this
LOT_SIZE            = 65            # Nifty lot size — update if SEBI changes this

# Lot sizing
LOT_CALC            = False
LOT_COUNT           = 1             # lots per signal when LOT_CALC = False
LOT_CAPITAL         = 100000        # capital per lot for auto-calculation (Rs)
CASH_PER_LOT_REQUIRED = 15000       # upfront pure cash required per lot

# ---------------------------------------------------------------------------
# Entry filters (D-R-D06g)
# Applied to signal candle timestamp. In-trade management unaffected.
# ---------------------------------------------------------------------------
# Days of week to exclude entries for BOTH directions: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri
# Tuesday = Nifty expiry day. Expiry-day gamma and pinning break ST signal.
EXCLUDE_TRADE_DAYS      = [1]

# Direction-specific day exclusions (in addition to EXCLUDE_TRADE_DAYS)
EXCLUDE_BEARISH_DAYS    = [0]       # Monday bearish excluded
EXCLUDE_BULLISH_DAYS    = []

# Signal candle close times to exclude (entry would execute 15 min later).
EXCLUDE_SIGNAL_CANDLES  = ['10:00', '10:15', '14:15', '14:30']

# ---------------------------------------------------------------------------
# Exit mechanisms — D-R-D06g
# ---------------------------------------------------------------------------

# Hard stop: exit when unrealised P&L <= -hard_stop_pts (resolved at entry)
ENABLE_HARD_STOP            = True
HARD_STOP_POINTS_BULL       = 40.0
HARD_STOP_POINTS_BEAR       = 67.5

# Profit target: exit when unrealised P&L >= max_profit * pt_pct (resolved at entry)
ENABLE_PROFIT_TARGET        = True
PROFIT_TARGET_PCT_BULL      = 0.35
PROFIT_TARGET_PCT_BEAR      = 0.60

# Time gate: exit at TIME_GATE_CHECK_TIME on gate day if max unrealised P&L
# since entry < max_profit * gate_min_profit_pct (resolved at entry)
ENABLE_TIME_GATE            = True
TIME_GATE_DAYS_BULL         = 1
TIME_GATE_DAYS_BEAR         = 1
TIME_GATE_CHECK_TIME        = '09:30'
TIME_GATE_MIN_PROFIT_PCT_BULL = 0.25
TIME_GATE_MIN_PROFIT_PCT_BEAR = 0.35

# Trend flip: exit when 15-min ST flips against position direction
# Always active — not toggled by a flag

# Pre-expiry exit: exit full position at 15:15 the day before expiry
ELM_SECONDS_BEFORE_EXPIRY = 87300  # 24h 15min in seconds -> 15:15 day before expiry

# Disabled mechanisms — confirmed net negative in backtest, do not enable
ENABLE_DAY0_SPREAD_SL   = False
ENABLE_TRAILING_PROFIT  = False

# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
# No exit on 09:15 candle close — defer SL check to 09:16 (15-min fallback)
NO_EXIT_BEFORE          = '09:16'

QTY_FREEZE              = 1800      # Angel One qty freeze for Nifty

# Slippage: used for P&L tracking only — live execution uses market orders
SLIPPAGE_POINTS         = 1.0

# Order management
ORDER_TIMEOUT_SEC       = 10        # Seconds to wait for order fill confirmation
CANDLE_FETCH_RETRIES        = 5     # Extra retries if candle data unavailable
CANDLE_FETCH_RETRY_INTERVAL = 10    # Seconds between retries

# Dry run mode — no real orders placed. Fill prices sourced from live LTP.
# Set to False only when ready to go live on delos.
DRY_RUN                 = False

# Trade update and log interval in seconds.
# Drives both the #trade-updates Slack message and the trade log append.
# Every TRADE_UPDATE_INTERVAL seconds: one log row + one Slack update.
TRADE_UPDATE_INTERVAL   = 20

# ---------------------------------------------------------------------------
# Angel One API — exchange segment strings
# ---------------------------------------------------------------------------
EXCHANGE_NSE            = "NSE"     # Index candle data
EXCHANGE_NFO            = "NFO"     # Options order placement
FO_EXCHANGE_SEGMENT     = "NFO"     # F&O segment for order params

# ---------------------------------------------------------------------------
# Slack channels
# ---------------------------------------------------------------------------
SLACK_TRADEBOT_CHANNEL  = "#tradebot-updates"   # Login, logout, archival
SLACK_TRADE_ALERTS      = "#trade-alerts"        # Orders, entries, exits, SL triggers
SLACK_TRADE_UPDATES     = "#trade-updates"       # Periodic open trade status (muted)
SLACK_ERRORS_CHANNEL    = "#error-alerts"        # Errors and exceptions

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Set to "DEBUG" during testing, "INFO" for production.
# DEBUG: all variable values, candle closes, filter decisions, LTP polls
# INFO:  startup, entries, exits, errors only
LOG_LEVEL               = "DEBUG"