"""
Prometheus - Phase 3, Fyers-sourced independent validation track.

Parallel to ../phase3/ (CRUDEOILM, Angel-One-sourced) and
../phase3_crudeoil/ (CRUDEOIL, Angel-One-sourced) — this one is
CRUDEOILM sourced from Fyers's own historical data
(data_pipeline/data/mcx_fyers/) instead of Angel One's
(data_pipeline/data/mcx/), per the user's own 2026-09-15 instruction:
mirror production's rollover logic and the already-decided mult 2.0 +
bespoke exit parameters exactly, kept as a fully separate result set
until the user has validated the findings against the existing
Angel-One-sourced Phase 3 numbers — nothing here feeds into
prometheus_backtest/README.md's published tables.

Same ST_PERIOD/ST_MULTIPLIER/entry-buffer/exit-buffer values as
../phase3/configs_p3.py, for direct comparability — only the data
source differs (see data_loader_fyers.py's own docstring for the
Angel-One gap-fill window, 2026-03-13 through 2026-06-29, where Fyers
itself has a confirmed real data void).
"""

import os

import pandas as pd

SYMBOL = 'CRUDEOILM'   # scoped to the mini contract only for now, per the
                       # user's own instruction ("Build it for the mini
                       # contract for now") -- CRUDEOIL cross-validation,
                       # if wanted later, is a separate follow-up track.

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
PROMETHEUS_DIR = os.path.dirname(BASE_DIR)
REPO_ROOT      = os.path.dirname(PROMETHEUS_DIR)

INSTRUMENT_MASTER_FILE = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'mcx_instrument_master.csv')

DATA_DIR       = os.path.join(BASE_DIR, 'data')
DATA_SWEEP_DIR = os.path.join(BASE_DIR, 'data_sweep')


def _lookup_lot_size(symbol: str) -> int:
    df = pd.read_csv(INSTRUMENT_MASTER_FILE)
    rows = df[df['name'] == symbol]
    if rows.empty:
        raise ValueError(f"No instrument master rows found for '{symbol}' in {INSTRUMENT_MASTER_FILE}")
    return int(rows.iloc[0]['lotsize'])


LOT_SIZE = _lookup_lot_size(SYMBOL)
LOTS     = 1   # single position, no scale-out at the raw-signal stage
               # (2-lot scale-out is applied afterward by bespoke_2lot_p3.py,
               # same convention as ../phase3/)

# ---------------------------------------------------------------------------
# Session / entry window -- identical to ../phase3/configs_p3.py.
# ---------------------------------------------------------------------------
MIN_ENTRY_BUFFER_MIN = 15
NO_EXIT_BEFORE_BUFFER_MIN = 1

# ---------------------------------------------------------------------------
# Signal -- the already-decided production value, not swept here.
# ---------------------------------------------------------------------------
ST_PERIOD = 10
ST_MULTIPLIER_GRID = [2.0]

# ---------------------------------------------------------------------------
# Costs -- same convention as every other phase: deliberately absent.
# ---------------------------------------------------------------------------
SLIPPAGE_ENABLED = False
