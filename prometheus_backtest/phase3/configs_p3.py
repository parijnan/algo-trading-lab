"""
Prometheus - Phase 3: raw signal-quality sweep over Supertrend (period,
multiplier), decoupled from exit-parameter calibration.

Motivation (user, 2026-09-01): ST_PERIOD=10/ST_MULTIPLIER=3.0 was never
actually calibrated for Prometheus -- configs_p2.py's own docstring says
"same day-1 starting values as Iris/Prometheus v1, not yet calibrated for
this design specifically", and sweep_p2.py computes the ST series ONCE,
before its sweep loop, so every calibration pass to date (SL_PCT,
TARGET1_PCT, TARGET2_MODE) held the entry signal itself fixed and never
questioned it. Crude's cleaner trending character (vs. Nifty/Sensex, which
10,3 actually WAS tuned for, via Iris) is a real, testable reason to
suspect a different multiplier suits it better.

Design (user-specified, 2026-09-01):
  1. No profit target, no stop loss, no EOD square-off -- the ONLY exit is
     the opposite ST_15 flip. Purely positional: a position can be held
     overnight, across multiple days, even across a contract roll.
  2. 1 lot per trade (no scale-out).
  3. Every trade gets a minute-by-minute log (entry to exit) tracking
     running MAE/MFE and unrealised P&L, for later analysis -- NOT to
     pick a winner by total P&L alone (there's no target/SL to optimise
     against yet; that's a later phase once a signal set is chosen here).
  4. Sweep grid over ST_MULTIPLIER (period held at the existing 10) --
     the user's specific hypothesis is 10,4 vs 10,3, tested with a wider
     grid around it for resolution.
  5. Rollover boundary artefacts across the sweep period are an accepted,
     known limitation (same ST-splicing issue already documented and
     accepted for Phase 1/2 -- see plans/prometheus-phase2-production.md
     §1) -- explicitly NOT worked around here, per direct instruction.
"""

import os

import pandas as pd

SYMBOL = 'CRUDEOILM'

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
PROMETHEUS_DIR = os.path.dirname(BASE_DIR)
REPO_ROOT      = os.path.dirname(PROMETHEUS_DIR)

MCX_DATA_DIR           = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'mcx')
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
LOTS     = 1   # single position, no scale-out (user-specified)

# ---------------------------------------------------------------------------
# Session / entry window. Only MIN_ENTRY_TIME survives from Phase 2 -- there
# is no EOD square-off to derive a "runway before close" cutoff from, and a
# positional strategy has no reason to avoid entries late in the session
# (there's no longer anything to hold "until" within the day).
# ---------------------------------------------------------------------------
MIN_ENTRY_TIME = '09:15'   # skip first 15 min — thin opening liquidity

# ---------------------------------------------------------------------------
# Signal — the thing actually under test this phase.
# ---------------------------------------------------------------------------
ST_PERIOD = 10   # held fixed; not swept this round (grid is multiplier-only,
                 # matching the user's specific 10,3-vs-10,4 hypothesis)
ST_MULTIPLIER_GRID = [2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5]

# ---------------------------------------------------------------------------
# Costs — same convention as every other phase: deliberately absent.
# ---------------------------------------------------------------------------
SLIPPAGE_ENABLED = False
