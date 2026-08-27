"""
Prometheus - Phase 2: CRUDEOILM ST_15 scale-out strategy.
Sole source of truth for parameters; no magic numbers elsewhere.

Design (user-specified, 2026-08-27; calibrated 2026-08-27 after a points vs
pct threshold sweep, cross-validated on both CRUDEOILM and CRUDEOIL):
  1. Supertrend on the 15-min timeframe (ST_15) — single timeframe, no
     regime-alignment gate like Prometheus v1.
  2. Daily pivot/R1-3/S1-3 levels (previous day's H/L/C) — still computed,
     but no longer target2's default (see 6).
  3. Entry long/short on ST_15 flip.
  4. Entry with 2 lots (2 independently-managed 1-lot legs).
  5. Lot 1 books at TARGET1_PCT% of entry. Under calibration as of
     2026-08-27: swept 1.0%-1.75%, cross-validated on both symbols; 1.75%
     posts the best headline P&L but ~70-75% of its improvement over 1.0%
     concentrates in the high-price tercile on BOTH CRUDEOILM and CRUDEOIL
     (replicated, not a fluke) — treat as a real regime-dependency risk,
     not yet a settled default. 1.0% is the steadier candidate.
  6. Lot 2 books at a flat TARGET2_FLAT_PCT% of entry (2.3%) — NOT the
     nearest pivot/R/S level. This overrides rule 6 as originally
     specified: a flat-pct target cross-validated as beating the pivot
     mechanism on both CRUDEOILM (Rs.35,731 vs Rs.23,133) and CRUDEOIL
     (Rs.426,839 vs Rs.341,641) once thresholds are expressed
     proportionally. Pivot logic (_find_pivot_target) is kept in
     backtest_p2.py and selectable via TARGET2_MODE='pivot' if this needs
     revisiting.
  7. SL = SL_PCT% of entry (1.8%, cross-validated: Calmar 1.07->2.36 on
     CRUDEOILM, 1.70->4.00 on CRUDEOIL), checked ahead of trend_flip.
     trend_flip remains the fallback/secondary SL and still fires the next
     entry in the opposite direction on the same bar it closes an open
     trade (rule 7 as originally specified) — that dual-duty behaviour is
     unchanged; only the primary stop level is new.
  8. Any open lot(s) forced flat once fewer than
     EOD_SQUAREOFF_BEFORE_CLOSE_MIN minutes remain in that trading day.
  9. Points-based SL/target1/target2 are still available
     (THRESHOLD_MODE='points', TARGET2_MODE='flat'/'pivot') for comparison
     — see backtest_p2.py's fill-price and gate logic, unchanged either way.
"""

import os

import pandas as pd

SYMBOL = 'CRUDEOILM'

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
PROMETHEUS_DIR = os.path.dirname(BASE_DIR)
REPO_ROOT      = os.path.dirname(PROMETHEUS_DIR)

MCX_DATA_DIR           = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'mcx')
INSTRUMENT_MASTER_FILE = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'mcx_instrument_master.csv')

DATA_DIR           = os.path.join(BASE_DIR, 'data')
TRADE_SUMMARY_FILE = os.path.join(DATA_DIR, 'trade_summary_p2.csv')
TRADE_LOGS_DIR     = os.path.join(DATA_DIR, 'trade_logs')


def _lookup_lot_size(symbol: str) -> int:
    df = pd.read_csv(INSTRUMENT_MASTER_FILE)
    rows = df[df['name'] == symbol]
    if rows.empty:
        raise ValueError(f"No instrument master rows found for '{symbol}' in {INSTRUMENT_MASTER_FILE}")
    return int(rows.iloc[0]['lotsize'])


LOT_SIZE     = _lookup_lot_size(SYMBOL)
LOTS_PER_LEG = 1   # "2 lots treated as 1 unit" on entry = two independent 1-lot legs

# ---------------------------------------------------------------------------
# Session / entry window. Both cutoffs are measured relative to each
# trading day's own actual last bar (handles half/early-close days
# uniformly, no separate clock fallback needed) — a BACKTEST-ONLY
# convenience, since the session's actual end isn't knowable in advance in
# a live run; a live port needs a clock + holiday-calendar cutoff instead.
# ---------------------------------------------------------------------------
MIN_ENTRY_TIME               = '09:15'   # skip first 15 min — thin opening liquidity
MAX_ENTRY_BEFORE_CLOSE_MIN   = 60        # rule: no new entry within 60 min of close
EOD_SQUAREOFF_BEFORE_CLOSE_MIN = 15      # rule 8: flat with 15 min left, always

# ---------------------------------------------------------------------------
# Signal — same day-1 starting values as Iris/Prometheus v1, not yet
# calibrated for this design specifically.
# ---------------------------------------------------------------------------
ST_PERIOD     = 10
ST_MULTIPLIER = 3.0

# ---------------------------------------------------------------------------
# Threshold unit — 'points' (default) or 'pct'. Governs how lot1_target and
# stop_loss are computed (see backtest_p2._resolve_thresholds), and doubles
# as the pivot-qualifying gate for target2 ("beyond LOT1_TARGET_POINTS" in
# points mode, "beyond TARGET1_PCT of entry" in pct mode) — there is one
# resolved price distance per entry, not two parallel systems, so this mode
# switch is the only place the unit choice lives. Entry price ranged
# Rs.5,617-10,683 across the backtest series (CV 16.6%): a flat point value
# is roughly a 2x swing in relative size depending on where price sits, so
# 'pct' exists to check whether that matters. Not yet decided which is
# better default — see the pct sweep this feeds.
# ---------------------------------------------------------------------------
THRESHOLD_MODE = 'pct'

# ---------------------------------------------------------------------------
# Scale-out
# ---------------------------------------------------------------------------
LOT1_TARGET_POINTS = 100   # used when THRESHOLD_MODE == 'points' (legacy/comparison only)
TARGET1_PCT = 1.0          # under active calibration — sweeping 1.0%-1.75%, see sweep_p2.py
PIVOT_LEVELS = ['pp', 'r1', 'r2', 'r3', 's1', 's2', 's3']

# Lot 2 target: 'pivot' (rule 6 as originally specified), 'flat' (fixed
# points, no level lookup), or 'flat_pct' (fixed % of entry, no level
# lookup — cross-validated default as of 2026-08-27, see module docstring).
TARGET2_MODE = 'flat_pct'
TARGET2_FLAT_POINTS = 180
TARGET2_FLAT_PCT = 2.3

# ---------------------------------------------------------------------------
# Stop loss — a single shared level protecting whichever lot(s) are still
# open (not per-lot), checked ahead of trend_flip. None = disabled, falls
# back to trend_flip as the only SL. 1.8% cross-validated 2026-08-27 (see
# module docstring) — a real, non-concentrated improvement on both symbols.
# ---------------------------------------------------------------------------
STOP_LOSS_POINTS = None    # used when THRESHOLD_MODE == 'points' (legacy/comparison only)
SL_PCT = 1.8

# ---------------------------------------------------------------------------
# Costs — deliberately absent in v1 of this phase, same convention as
# Prometheus v1.
# ---------------------------------------------------------------------------
SLIPPAGE_ENABLED = False
