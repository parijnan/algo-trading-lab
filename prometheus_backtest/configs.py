"""
Prometheus — CRUDEOILM/CRUDEOIL intraday trend-following backtest.
Sole source of truth for parameters; no magic numbers elsewhere.
"""

import os
import pandas as pd

# ---------------------------------------------------------------------------
# Symbol switch — flip to 'CRUDEOIL' to validate the same strategy on the
# full-size contract once it's been calibrated on CRUDEOILM. Drives both the
# data directory and the lot_size lookup below.
# ---------------------------------------------------------------------------
SYMBOL = 'CRUDEOILM'   # 'CRUDEOILM' or 'CRUDEOIL'

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)

MCX_DATA_DIR           = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'mcx')
INSTRUMENT_MASTER_FILE = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'mcx_instrument_master.csv')

DATA_DIR       = os.path.join(BASE_DIR, 'data')
TRADE_LOG_FILE = os.path.join(DATA_DIR, 'prometheus_trade_log.csv')
TRADE_LOGS_DIR = os.path.join(DATA_DIR, 'trade_logs')   # per-trade 1-min path CSVs


# ---------------------------------------------------------------------------
# Instrument — lot size looked up from the live instrument master rather
# than hardcoded, since it differs between CRUDEOILM (10) and CRUDEOIL (100).
# ---------------------------------------------------------------------------
def _lookup_lot_size(symbol: str) -> int:
    df = pd.read_csv(INSTRUMENT_MASTER_FILE)
    rows = df[df['name'] == symbol]
    if rows.empty:
        raise ValueError(f"No instrument master rows found for '{symbol}' in {INSTRUMENT_MASTER_FILE}")
    return int(rows.iloc[0]['lotsize'])


LOT_SIZE = _lookup_lot_size(SYMBOL)
LOTS     = 1   # static, single-lot v1 — position sizing is not a calibration target yet

# ---------------------------------------------------------------------------
# Session / entry window
# ---------------------------------------------------------------------------
MIN_ENTRY_TIME = '09:15'       # skip the first 15 min — thin opening liquidity
MAX_ENTRY_TIME = '22:45'       # no new entries this close to session end
EOD_SQUAREOFF_TIME = '23:15'   # hard exit, a few minutes before the widest observed
                                # session close (23:54 in winter — MCX non-agri DST
                                # shift). TUNABLE — not yet calibrated.
SKIP_ENTRY_WINDOWS = []        # e.g. [('13:00', '14:00')] — no known crude-specific
                                # dead zone identified yet; add if one emerges.

# ---------------------------------------------------------------------------
# Signal — Iris's live values (iris_production/iris_configs.py), used as
# day-1 starting points. NOT assumed correct for crude's own volatility
# profile — first calibration target once the harness is validated.
# ---------------------------------------------------------------------------
ENTRY_TF_MIN   = 5
REGIME_TF_MIN  = 15
ST_PERIOD      = 10
ST_MULTIPLIER  = 3.0

# ---------------------------------------------------------------------------
# Exit checks — pluggable, ordered, individually toggled (see backtest.py's
# _EXIT_CHECK_FUNCS). v1 only holds a position until the signal is
# invalidated (opposing Supertrend flip) or the session forces a square-off.
# stop_loss / profit_target / max_hold are implemented in the same pluggable
# shape but excluded here — enable them one at a time in later calibration
# experiments (repo convention: one variable changed per experiment).
# ---------------------------------------------------------------------------
EXIT_CHECKS_ENABLED = ['trend_flip', 'eod_squareoff']

STOP_LOSS_POINTS     = None   # e.g. 20  — not yet enabled
PROFIT_TARGET_POINTS = None   # e.g. 40  — not yet enabled
MAX_HOLD_MIN          = None  # e.g. 120 — not yet enabled

# ---------------------------------------------------------------------------
# Costs — deliberately absent in v1 (raw price P&L only), so a signal edge
# isn't conflated with cost assumptions. When enabled, mirror
# apollo_backtest/backtest_debit.py's apply_slippage() pattern.
# ---------------------------------------------------------------------------
SLIPPAGE_ENABLED = False
