"""
leto_config.py — Leto Session Manager Configuration

Read by leto.py at startup (static constants) and at each routing decision
(ROUTING_MODE / MANUAL_STRATEGY, via importlib.reload).
Routing override is persisted in data/routing_state.json (gitignored) by
slack_listener.py — leto_config.py itself is never modified at runtime.

Named leto_config (not configs_live) to avoid colliding with the
configs_live.py files inside each strategy directory — Python caches
modules by name, so a shared name causes cross-imports via sys.modules.
"""

import json
from datetime import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Market hours
# ---------------------------------------------------------------------------
MARKET_OPEN     = time(9, 15)
MARKET_CLOSE    = time(15, 40)   # CAS (3 Aug 2026): derivatives trade till 15:40

# ---------------------------------------------------------------------------
# VIX routing thresholds
# ---------------------------------------------------------------------------
VIX_ARTEMIS_MAX = 16.0
VIX_ATHENA_MAX  = 25.0

# ---------------------------------------------------------------------------
# Angel One tokens
# ---------------------------------------------------------------------------
NIFTY_INDEX_TOKEN = "99926000"
VIX_TOKEN         = "99926017"

# ---------------------------------------------------------------------------
# Angel One scrip master
# ---------------------------------------------------------------------------
SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# ---------------------------------------------------------------------------
# Slack channels — shared by Leto and standalone strategies (Iris)
# ---------------------------------------------------------------------------
SLACK_CHANNEL          = "#tradebot-updates"   # legacy alias — Leto uses this
SLACK_TRADEBOT_CHANNEL = "#tradebot-updates"   # session lifecycle: login, logout, WS status
SLACK_TRADE_ALERTS     = "#trade-alerts"        # entries, exits, SL hits
SLACK_TRADE_UPDATES    = "#trade-updates"       # periodic in-trade P&L updates
SLACK_ERRORS_CHANNEL   = "#error-alerts"        # exceptions, feed failures

# ---------------------------------------------------------------------------
# Routing override — written by slack_listener.py to data/routing_state.json
# Loaded fresh on every importlib.reload() so leto.py picks up changes live.
# ---------------------------------------------------------------------------
_ROUTING_STATE_FILE = Path(__file__).parent / 'data' / 'routing_state.json'

def _load_routing_state():
    try:
        s = json.loads(_ROUTING_STATE_FILE.read_text())
        return s.get('routing_mode', 'auto'), s.get('manual_strategy', 'artemis')
    except (FileNotFoundError, json.JSONDecodeError):
        return 'auto', 'artemis'

ROUTING_MODE, MANUAL_STRATEGY = _load_routing_state()
