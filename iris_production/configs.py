"""
Iris production configuration.
All tuneable parameters live here — nothing hardcoded in iris.py.

PAPER_MODE = True  → log intended orders, place nothing.
             False → live orders. Only flip after paper parity is confirmed.
"""
from pathlib import Path

REPO_ROOT    = Path(__file__).parent.parent
IRIS_ROOT    = Path(__file__).parent
DATA_DIR     = IRIS_ROOT / 'data'
LOG_DIR      = IRIS_ROOT / 'logs'
FLAG_PATH    = DATA_DIR / 'iris_active.flag'
STATE_FILE   = DATA_DIR / 'iris_state.csv'
CREDS_FILE   = DATA_DIR / 'user_credentials.csv'
HOLIDAYS_FILE = DATA_DIR / 'holidays.csv'

# ── Kill switch ───────────────────────────────────────────────────────────────
PAPER_MODE = True   # MUST be manually set to False for live trading

# ── Signal entry filter ───────────────────────────────────────────────────────
# Skip signals whose entry time falls in these windows (end exclusive).
# 10:45–11:15 is the only net-negative time window from the full backtest.
SKIP_ENTRY_WINDOWS = [('10:45', '11:15')]

# ── Instrument ────────────────────────────────────────────────────────────────
LOT_SIZE          = 65
LOT_COUNT         = 1               # position size in lots (start small)
STRIKE_STEP       = 50              # Nifty strike grid
ITM_DEPTH_STEPS   = 3               # 3 × 50 = 150 pts ITM
MIN_DTE           = 2               # skip expiry if ELM date is today or earlier
QTY_FREEZE        = 1800            # Angel One Nifty freeze limit (units)
FO_EXCHANGE       = 'NFO'
INDEX_EXCHANGE    = 'NSE'

# ── Nifty live feed ───────────────────────────────────────────────────────────
NIFTY_TOKEN       = '99926000'      # Nifty 50 index token (NSE)
VIX_TOKEN         = '99926017'      # India VIX token (NSE)

# ── Signal: ST_FAST (5m entry + 15m regime) ──────────────────────────────────
ENTRY_TF_MIN      = 5               # entry timeframe in minutes
REGIME_TF_MIN     = 15              # regime timeframe in minutes
ST_PERIOD         = 10
ST_MULTIPLIER     = 3.0
SEED_CANDLES      = 150             # historical candles to seed the ST
# NOTE: live signal uses getCandleData("FIVE_MINUTE") — verify this interval
# is served by Angel One before first paper session.

# ── Exit parameters (paper calibration knobs — not derived from backtest data)
# Set conservatively for first paper sessions; tighten after observing live P&L.
PROFIT_TARGET_PCT = 0.25            # exit when option LTP ≥ entry × 1.25
STOP_LOSS_PCT     = 0.20            # exit when option LTP ≤ entry × 0.80
EXIT_BY_TIME      = '15:00'         # force-exit at this time regardless of P&L

# ── Session ───────────────────────────────────────────────────────────────────
MARKET_OPEN       = '09:15'
MARKET_CLOSE      = '15:30'
TRADE_UPDATE_SEC  = 30              # Slack update cadence when in-trade (seconds)

# ── Slack ─────────────────────────────────────────────────────────────────────
# Channels imported from leto_config at runtime to avoid hardcoding tokens here.
