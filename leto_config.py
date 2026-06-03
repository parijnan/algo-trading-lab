"""
leto_config.py — Leto Session Manager Configuration

Read by leto.py at startup (static constants) and at each routing decision
(ROUTING_MODE / MANUAL_STRATEGY, via importlib.reload).
Edited by slack_listener.py for routing override changes.

Named leto_config (not configs_live) to avoid colliding with the
configs_live.py files inside each strategy directory — Python caches
modules by name, so a shared name causes cross-imports via sys.modules.
"""

from datetime import time

# ---------------------------------------------------------------------------
# Market hours
# ---------------------------------------------------------------------------
MARKET_OPEN     = time(9, 15)
MARKET_CLOSE    = time(15, 30)

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
# Routing override — edited by slack_listener.py
# ---------------------------------------------------------------------------
ROUTING_MODE    = 'auto'      # 'auto' | 'manual'
MANUAL_STRATEGY = 'artemis'   # 'artemis' | 'athena'
