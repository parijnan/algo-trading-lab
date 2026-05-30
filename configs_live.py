"""
configs_live.py — Leto Session Manager Configuration

Read by leto.py at startup (static constants) and at each routing decision
(ROUTING_MODE / MANUAL_STRATEGY, via importlib.reload).
Edited by slack_listener.py for routing override changes.
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
# Slack channel for Leto-level messages
# ---------------------------------------------------------------------------
SLACK_CHANNEL = "#tradebot-updates"

# ---------------------------------------------------------------------------
# Routing override — edited by slack_listener.py
# ---------------------------------------------------------------------------
ROUTING_MODE    = 'auto'      # 'auto' | 'manual'
MANUAL_STRATEGY = 'artemis'   # 'artemis' | 'athena'
