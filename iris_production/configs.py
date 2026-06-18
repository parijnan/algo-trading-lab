"""
Iris production configuration.
All tuneable parameters live here — nothing hardcoded in iris.py.

DRY_RUN = True  → log intended orders, place nothing; use feed LTP as entry price.
          False → live orders. Only flip after paper parity is confirmed.
"""
from pathlib import Path

REPO_ROOT    = Path(__file__).parent.parent
IRIS_ROOT    = Path(__file__).parent
DATA_DIR     = IRIS_ROOT / 'data'
LOG_DIR      = IRIS_ROOT / 'logs'
FLAG_PATH    = DATA_DIR / 'iris_active.flag'
PID_FILE     = DATA_DIR / 'iris.pid'
STATE_FILE   = DATA_DIR / 'iris_state.csv'
CREDS_FILE   = DATA_DIR / 'user_credentials.csv'
HOLIDAYS_FILE = DATA_DIR / 'holidays.csv'

# ── Kill switch ───────────────────────────────────────────────────────────────
DRY_RUN = False     # paper parity confirmed; set True to revert to paper mode

# ── Signal entry time constraints ────────────────────────────────────────────
# 332 of 360 trades in the 09:15 window fire at exactly 09:20 (first 5-min bar).
# Opening option prices can be stale/wide at 09:20 in live trading.
# Set to '09:25' if paper trading reveals consistent opening noise at 09:20.
# In paper mode this is safe to leave at '09:15' — LTP reads real-time price.
MIN_ENTRY_TIME = '09:15'            # no entry before this time (HH:MM)
MAX_ENTRY_TIME = '15:00'            # no entry after this time — last valid entry at 15:00 open

# ── Signal entry window filter ────────────────────────────────────────────────
# Skip signals whose entry time falls in these windows (end exclusive).
# 10:45–11:30: post-opening-move dead zone (three consecutive losing 15-min
# windows; 11:30 recovers sharply at WR 72.7%).
SKIP_ENTRY_WINDOWS = [('10:45', '11:30')]

# ── Instrument ────────────────────────────────────────────────────────────────
LOT_SIZE              = 65
LOT_COUNT             = 40          # position size in lots when LOT_CALC = False
LOT_CALC              = False       # True = auto-calculate from available cash; False = use LOT_COUNT
CASH_PER_LOT_REQUIRED = 25000       # cash reserved per lot for dynamic sizing (ITM-150 premium + buffer)
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
SEED_DAYS         = 13              # calendar days of 5-min history to fetch at seed
                                    # 13d × 75 bars/day = ~975 bars (<1000 API limit)
                                    # single FIVE_MINUTE call; resampled to 15m for regime ST

# ── Exit parameters — calibrated from full backtest (stop=25%, target=10%, hold=30m)
# Backtest: 1,172 trades, WR 59.5%, avg ₹220/lot, median ₹380/lot over 7.3 years.
PROFIT_TARGET_PCT = 0.10            # exit when option LTP ≥ entry × 1.10
STOP_LOSS_PCT     = 0.25            # exit when option LTP ≤ entry × 0.75
MAX_HOLD_MIN      = 30              # per-trade time limit (minutes); hard exit if no other trigger
EXIT_BY_TIME      = '15:15'         # daily hard cutoff regardless of P&L

# ── Session ───────────────────────────────────────────────────────────────────
MARKET_OPEN       = '09:15'
MARKET_CLOSE      = '15:30'
TRADE_UPDATE_SEC  = 10              # Slack update cadence when in-trade (seconds)
ORDER_TIMEOUT_SEC = 30              # seconds to wait for order fill (WS fast path + REST fallback)

# ── Slack ─────────────────────────────────────────────────────────────────────
# Channels imported from leto_config at runtime to avoid hardcoding tokens here.

# ── Sizing override ───────────────────────────────────────────────────────────
# Written by slack_listener.py Manage Sizing modal to data/sizing_override.json.
# Overrides LOT_CALC and LOT_COUNT at import time. Delete the file to revert.
try:
    import json as _json, pathlib as _pl
    _s = _json.loads((_pl.Path(__file__).parent / 'data' / 'sizing_override.json').read_text())
    LOT_CALC  = bool(_s['lot_calc'])
    LOT_COUNT = int(_s['lot_count'])
except Exception:
    pass
