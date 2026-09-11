"""
Prometheus - Phase 3 CRUDEOIL validation (2026-09-07): identical pipeline to
prometheus_backtest/phase3/, re-pointed at the full-size CRUDEOIL contract
instead of CRUDEOILM, to check the already-decided, live-production exit
combo (ST_MULTIPLIER=2.0, SL=2.2%, T1=2.0%, T2=5.0% -- see
bespoke_2lot_p3.py) holds up on the contract Prometheus doesn't actually
trade. Not a re-calibration -- SYMBOL is the only change from phase3/'s own
configs_p3.py, matching the original plan's own "CRUDEOIL validation"
requirement (flip SYMBOL, re-run unchanged, compare side by side). Stored
in a separate sibling folder rather than overwriting phase3/'s own
CRUDEOILM outputs, per explicit instruction. Original phase3/ docstring
below, unmodified, for the shared pipeline's own design rationale.

---

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

SYMBOL = 'CRUDEOIL'

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
# Session / entry window. There is no EOD square-off to derive a "runway
# before close" cutoff from, and a positional strategy has no reason to
# avoid entries late in the session (there's no longer anything to hold
# "until" within the day).
# ---------------------------------------------------------------------------
# Minutes since the session's own first bar (dynamic anchor, not a
# hardcoded clock time -- see backtest_p3.py's _first_bar_by_day) before a
# fresh entry is allowed to fill; skips thin opening liquidity/first-minute
# price discovery. Matches prometheus_production/prometheus_configs.py's
# MIN_ENTRY_BUFFER_MIN. Replaced the old hardcoded MIN_ENTRY_TIME='09:15'
# clock-time check 2026-09-11 -- it coincidentally worked on a normal
# 09:00 session but did nothing on an evening-only special session (real
# open 17:00, already past 09:15 on the clock), same bug class production
# itself fixed 2026-09-04 (commit a483c7d).
MIN_ENTRY_BUFFER_MIN = 15

# Minutes since the session's own first bar before a managed SL/target
# check is allowed to act on a bar's high/low -- the exit-side analogue of
# the entry gate above. Matches prometheus_production/prometheus_configs.py's
# NO_EXIT_BEFORE_BUFFER_MIN. Used by exit_calib_p3.py/bespoke_2lot_p3.py's
# bar-walking simulators, added 2026-09-11 after a confirmed case (trade
# 389, CRUDEOILM) where the backtest credited a target1 fill off a single
# wild opening-bar print that production's own NO_EXIT_BEFORE_BUFFER_MIN
# guard would never have acted on.
NO_EXIT_BEFORE_BUFFER_MIN = 1

# ---------------------------------------------------------------------------
# Signal — the thing actually under test this phase.
# ---------------------------------------------------------------------------
ST_PERIOD = 10   # held fixed; not swept this round (grid is multiplier-only,
                 # matching the user's specific 10,3-vs-10,4 hypothesis)
ST_MULTIPLIER_GRID = [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5]
# 2.0 added 2026-09-01: raw Calmar was still climbing steadily from 5.5 down
# to 2.5, the classic edge-of-grid overfitting signature -- extending one
# step further was needed to check whether that climb continues (it didn't;
# see prometheus_backtest/README.md's Phase 3 section).

# ---------------------------------------------------------------------------
# Costs — same convention as every other phase: deliberately absent.
# ---------------------------------------------------------------------------
SLIPPAGE_ENABLED = False
