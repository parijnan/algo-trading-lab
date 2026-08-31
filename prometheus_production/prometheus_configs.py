"""
Prometheus production configuration.
All tuneable parameters live here — nothing hardcoded in prometheus.py /
prometheus_functions.py.

Calibrated 2026-08-27 against prometheus_backtest/phase2/configs_p2.py
(219 trades, 55.7% WR, Rs.44,693 total P&L, Calmar 2.99/5.20 annualized —
see prometheus_backtest/README.md and plans/prometheus-phase2-production.md).
This file is a deliberate DUPLICATE of configs_p2.py's calibrated values,
not an import — matches the repo's existing iris_configs.py/iris_backtest
convention exactly (CLAUDE.md "Module naming", plan §0): production must
never silently follow future backtest experimentation.

DRY_RUN = True  → log intended orders, place nothing; use feed LTP as fill price.
          False → live orders. See the DRY_RUN assignment below for the
          actual decision record of when/why this was flipped.
"""
from pathlib import Path

REPO_ROOT       = Path(__file__).parent.parent
PROMETHEUS_ROOT = Path(__file__).parent
DATA_DIR        = PROMETHEUS_ROOT / 'data'
LOG_DIR         = PROMETHEUS_ROOT / 'logs'
TRADE_LOGS_DIR  = DATA_DIR / 'trade_logs'

FLAG_PATH    = DATA_DIR / 'prometheus_active.flag'
PID_FILE     = DATA_DIR / 'prometheus.pid'
STATE_FILE   = DATA_DIR / 'prometheus_state.csv'
CREDS_FILE   = REPO_ROOT / 'data' / 'user_credentials.csv'   # shared root file (Leto/Apollo/Artemis
                                                              # convention — user_name/qr_code columns,
                                                              # already present on both local and Delos,
                                                              # not a per-strategy copy like Iris's own
TRADES_FILE  = DATA_DIR / 'prometheus_trades.csv'
COUNTER_FILE = DATA_DIR / 'trade_counter.txt'
SERIES_15M_FILE = DATA_DIR / 'prometheus_15m_series.csv'

# Prometheus's own circuit breaker — deliberately separate from
# data/SLACK_COMMAND.flag (§5): an operator managing NSE/BSE shouldn't
# accidentally also kill the MCX side, and vice versa.
COMMAND_FLAG_PATH = DATA_DIR / 'prometheus_command.flag'

# ── Shared MCX data pipeline paths ───────────────────────────────────────────
MCX_DATA_DIR            = REPO_ROOT / 'data_pipeline' / 'data' / 'mcx'
INSTRUMENT_MASTER_FILE  = REPO_ROOT / 'data_pipeline' / 'data' / 'mcx_instrument_master.csv'
MCX_HOLIDAYS_FILE       = REPO_ROOT / 'data_pipeline' / 'data' / 'mcx_holidays.csv'

# ── Instrument (Slack-switchable — §5/§6) ────────────────────────────────────
SYMBOL          = 'CRUDEOILM'
MARGIN_PER_UNIT = 100000   # Rs — coupled to SYMBOL; overridden together via
                           # btn_prometheus_instrument (instrument_override.json)

try:
    import json as _json
    _o = _json.loads((DATA_DIR / 'instrument_override.json').read_text())
    SYMBOL          = str(_o['symbol'])
    MARGIN_PER_UNIT = float(_o['margin_per_unit'])
except Exception:
    pass


def _lookup_lot_size(symbol: str) -> int:
    import pandas as pd
    df = pd.read_csv(INSTRUMENT_MASTER_FILE)
    rows = df[df['name'] == symbol]
    if rows.empty:
        raise ValueError(f"No instrument master rows found for '{symbol}' in {INSTRUMENT_MASTER_FILE}")
    return int(rows.iloc[0]['lotsize'])


LOT_SIZE     = _lookup_lot_size(SYMBOL)
LOTS_PER_LEG = 1     # 1 unit = 2 lots (1 lot each leg) — see prometheus_state.py / §6

FO_EXCHANGE  = 'MCX'
MCX_FO_WS_EXCHANGE_TYPE = 5   # websocket_feed.py exchange_type int for MCX F&O
                              # (matches mcx_live_downloader.py's MCX_FO=5, live-verified)

# ── Kill switch ───────────────────────────────────────────────────────────────
DRY_RUN = False    # live as of 2026-08-31, STATIC_UNITS=1 (2 lots), specifically to verify the
                    # order-update WebSocket for MCX (Rollout step 2). Rollout step 3 (formal
                    # backtest/live parity diff) was NOT done -- decision made on the strength of
                    # ~2.5hrs of clean DRY_RUN seed/signal/state behavior against real ticks
                    # instead. Revert to True to go back to paper mode.

# ── Session ───────────────────────────────────────────────────────────────────
SESSION_START_TIME = '09:00'   # cron starts ahead of this; poller/seed both key off it
MIN_ENTRY_TIME      = '09:15'  # skip first 15 min — thin opening liquidity

# CLOSING_TIME is DST-dependent and must be toggled by hand around the US
# DST changes (~2nd Sun of March -> 23:30, ~1st Sun of Nov -> 23:55) — see
# plan §3. Current value is correct for 2026-08-30 (DST in force).
CLOSING_TIME = '23:30'

MAX_ENTRY_BEFORE_CLOSE_MIN     = 60   # no new entry within this many min of CLOSING_TIME
EOD_SQUAREOFF_BEFORE_CLOSE_MIN = 15   # flat with this many min left, always (clock-driven, not grid-snapped)

# Poller/WS session-end buffer — tracks CLOSING_TIME plus the same generous
# buffer mcx_live_downloader.py uses (SESSION_END_TIME=23:55 vs its own
# MARKET_CLOSE=23:30, a 25-min buffer).
SESSION_END_BUFFER_MIN = 25


def _nearest_15min_boundary(hhmm: str) -> str:
    from datetime import datetime as _dt
    t = _dt.strptime(hhmm, '%H:%M')
    total_min = t.hour * 60 + t.minute
    rounded = round(total_min / 15) * 15
    h, m = divmod(rounded % (24 * 60), 60)
    return f'{h:02d}:{m:02d}'


def _minus_minutes(hhmm: str, minutes: int) -> str:
    from datetime import datetime as _dt, timedelta as _td
    t = _dt.strptime(hhmm, '%H:%M') - _td(minutes=minutes)
    return t.strftime('%H:%M')


# Entries only ever happen on 15-min bar opens, so the raw offset from
# CLOSING_TIME must snap to the grid rather than being used as a continuous
# cutoff (plan §3): CLOSING_TIME=23:30 -> LAST_ENTRY_TIME=22:30 (60min runway);
# CLOSING_TIME=23:55 -> LAST_ENTRY_TIME=23:00 (55min runway, both verified in plan).
LAST_ENTRY_TIME = _nearest_15min_boundary(_minus_minutes(CLOSING_TIME, MAX_ENTRY_BEFORE_CLOSE_MIN))

# EOD_SQUAREOFF is LTP-driven, checked every loop tick (not bar-gated) — a
# plain clock trigger, no grid-snapping needed.
EOD_SQUAREOFF_TIME = _minus_minutes(CLOSING_TIME, EOD_SQUAREOFF_BEFORE_CLOSE_MIN)

SESSION_END_TIME = _minus_minutes(CLOSING_TIME, -SESSION_END_BUFFER_MIN)   # CLOSING_TIME + buffer

# ── Signal: ST_15, single timeframe (no regime gate — Phase 2 design) ───────
ST_PERIOD     = 10
ST_MULTIPLIER = 3.0

# Calendar days of 1-min history tail-read for seeding (15-20 recommended,
# plan §1 — lands ~975-1,300 fifteen-min bars, same generosity band as
# Iris's SEED_DAYS=13 relative to its own ST_PERIOD).
SEED_DAYS = 18

# ── Scale-out (calibrated 2026-08-27, configs_p2.py) — 'pct' hardcoded per
# Rollout step 5: "backtest keeps both modes for comparison, production
# hardcodes 'pct'". ─────────────────────────────────────────────────────────
TARGET1_PCT      = 1.0
TARGET2_MODE     = 'flat_pct'
TARGET2_FLAT_PCT = 2.3
SL_PCT           = 1.8

# ── Contract rollover — capital efficiency over parity (plan §1/§6) ─────────
# Roll to the next contract out once fewer than this many TRADING days
# (mcx_holidays.csv, not calendar days) remain until the current front-month
# contract's expiry — avoids MCX's tender-margin window on energy contracts.
TENDER_ROLL_TRADING_DAYS = 5

# ── Sizing — "units," not "lots" (§6): 1 unit = 2 lots (1 lot each leg). ────
DYNAMIC_SIZING  = False
STATIC_UNITS    = 1
# Artemis's formula, not Apollo/Athena's dual-constraint one — Prometheus is
# the only strategy on the MCX side of the account, so availablecash already
# reflects whatever's genuinely free (no other own position to guard against).

try:
    import json as _json2
    _s = _json2.loads((DATA_DIR / 'sizing_override.json').read_text())
    DYNAMIC_SIZING = bool(_s['lot_calc'])     # same JSON key names as the other 3
    STATIC_UNITS   = int(_s['lot_count'])     # strategies' sizing_override.json (§5)
except Exception:
    pass

# ── Resilient polling (§3) — ported from mcx_live_downloader.py, itself
# ported from Iris's 2026-08-10 hardening. ──────────────────────────────────
POLL_INTERVAL_SEC        = 60
INNER_RETRY_ATTEMPTS     = 3
INNER_RETRY_INTERVAL_SEC = 1
CANDLE_CLOSE_BUFFER_SEC  = 0    # fire exactly at the boundary, no artificial margin

CANDLE_POLL_LIMIT = 3      # getCandleData calls/sec — broker-wide client-side cap
LTP_POLL_LIMIT    = 10     # REST ltpData calls/sec when WS feed is disconnected

# ── Order execution ──────────────────────────────────────────────────────────
ORDER_TIMEOUT_SEC = 30     # seconds to wait for order fill (WS fast path + REST fallback)

# ── Slack ─────────────────────────────────────────────────────────────────────
TRADE_UPDATE_SEC = 20      # matches Artemis's/Athena's convention, not Iris's 10s (§5)

# ── 15-min debug series retention (§4) — non-authoritative, purely for human
# visibility; never read back as an input to live ST computation. ───────────
SERIES_15M_RETENTION_DAYS = 20
