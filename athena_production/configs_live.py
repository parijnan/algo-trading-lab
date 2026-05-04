"""
configs_live.py — Athena Production Configuration
All parameters in one place. Tweak here, nothing else needs to change.

Strategy: Nifty Double Calendar Condor (Theta Positive, Market Neutral)
Finalized configuration from backtest: Static 0.30 Delta with 0.05 Wings.

Credentials are loaded once at module level from the shared data/user_credentials.csv.
"""

import os
import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATHENA_DIR      = os.path.join(REPO_ROOT, "athena_production")
DATA_DIR        = os.path.join(ATHENA_DIR, "data")
LOGS_DIR        = os.path.join(ATHENA_DIR, "logs")
TRADE_LOGS_DIR  = os.path.join(DATA_DIR, "trade_logs")

STATE_FILE      = os.path.join(DATA_DIR, "athena_state.csv")
TRADES_FILE     = os.path.join(DATA_DIR, "athena_trades.csv")

# ---------------------------------------------------------------------------
# Credentials — loaded once from shared data directory
# ---------------------------------------------------------------------------
CREDENTIALS_FILE = os.path.join(REPO_ROOT, "data", "user_credentials.csv")

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
# Instrument tokens — fixed
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
VIX_FILTER_LOW      = 16.0
VIX_FILTER_HIGH     = 25.0

# ---------------------------------------------------------------------------
# Options / spread structure
# ---------------------------------------------------------------------------
TARGET_DELTA_SOLD   = 0.30
SAFETY_WING_DELTA   = 0.05
ENABLE_SAFETY_WINGS = True          # If True, buys PE wing at entry (Phase 2 is PE-only)

STRIKE_STEP         = 100           # Nifty strike interval for Calendar
BUY_LEG_MIN_DTE     = 16            # Roll buy leg to next month if DTE < this
LOT_SIZE            = 65            # Nifty lot size (update if SEBI changes this)
RISK_FREE_RATE      = 5.0           # Annualized risk-free rate in %

# ---------------------------------------------------------------------------
# Emergency Hedge (Phase 2 Smart Parachute)
# ---------------------------------------------------------------------------
ENABLE_EMERGENCY_HEDGE   = True
EMERGENCY_HEDGE_DELTA    = 0.35     # Monthly CE bought on upside stress
EMERGENCY_TRIGGER_OFFSET = 150      # pts past CE Sell Strike to trigger BUY
EMERGENCY_EXIT_OFFSET    = 0        # pts from CE Sell Strike to trigger SELL (reversal)
EMERGENCY_MAX_ATTEMPTS   = 1        # Max parachutes per trade

# Lot sizing
LOT_CALC            = True          # Dynamic lot count based on capital
LOT_COUNT           = 1             # Trading 1 lot
LOT_CAPITAL         = 120000        # Capital per lot buffer (Rs)
CASH_PER_LOT_REQUIRED = 50000       # Upfront pure cash required per lot

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
ENTRY_TIME          = "10:30"       # 10:30 AM Entry
ELM_EXIT_TIME       = "10:25"       # 10:25 AM on day before sell expiry

# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
QTY_FREEZE          = 1800          # Angel One qty freeze for Nifty
ORDER_TIMEOUT_SEC   = 10            # Seconds to wait for order fill
ORDER_LIMIT         = 10            # Orders per second limit

# Polling interval in seconds (REST polling instead of WebSockets)
# Every 20 seconds: fetch LTPs, update log, send Slack (if interval reached).
TRADE_UPDATE_INTERVAL = 20

# Dry run mode — set to False for live trading on Monday
DRY_RUN             = False

# Force entry on any day — set to False for standard Monday entry
FORCE_ENTRY         = True

# ---------------------------------------------------------------------------
# Angel One API — exchange segment strings
# ---------------------------------------------------------------------------
EXCHANGE_NSE        = "NSE"
EXCHANGE_NFO        = "NFO"
FO_EXCHANGE_SEGMENT = "NFO"

# ---------------------------------------------------------------------------
# Slack channels
# ---------------------------------------------------------------------------
SLACK_TRADE_ALERTS   = "#trade-alerts"
SLACK_TRADE_UPDATES  = "#trade-updates"
SLACK_ERRORS_CHANNEL = "#error-alerts"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL            = "DEBUG"
