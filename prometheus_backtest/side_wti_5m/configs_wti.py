"""
Prometheus — WTI 5-minute side project (2026-09-08): tests the already-
decided Phase 3 mult-2.0/2.5 combos, unchanged, against Kaggle WTI 5-min
data as a loose approximation cross-check. NOT a rigorous validation like
phase3_crudeoil/ (same commodity, same exchange mechanics) — WTI is NYMEX,
a genuinely different market, with a different session structure and no
volume field in the source data. See side_wti_5m/README.md for the full
list of caveats. Kept out of the numbered phase*/ folders deliberately —
this is a side project, not part of the calibration decision chain.
"""

import os

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
PROMETHEUS_DIR = os.path.dirname(BASE_DIR)
REPO_ROOT      = os.path.dirname(PROMETHEUS_DIR)

WTI_DATA_FILE = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'wti',
                              'WTI-Crude-Oil-5-Minute-OHLC-Candles.csv')

DATA_DIR       = os.path.join(BASE_DIR, 'data')
DATA_SWEEP_DIR = os.path.join(BASE_DIR, 'data_sweep')

# ---------------------------------------------------------------------------
# No real position/margin concept for a hypothetical WTI trade — point-based
# P&L only, no currency conversion (LOT_SIZE=1 makes *_pnl_rs columns reduce
# to *_pnl_points; kept as "_rs" column names only for schema parity with
# the phase3 pipeline this mirrors, values are USD points, not Rs).
# ---------------------------------------------------------------------------
LOT_SIZE = 1
LOTS     = 1

# ---------------------------------------------------------------------------
# Signal — fixed at the live production values. No sweep/recalibration here
# (explicit scope decision): this tests whether the already-decided combo
# holds up directionally on a different market, not a fresh optimization.
# ---------------------------------------------------------------------------
ST_PERIOD     = 10
ST_MULTIPLIER = 2.0

# The two bespoke-calibrated exit combos from Phase 3 (mult, sl_pct, t1_pct,
# t2_pct) — hardcoded tuples, matching how phase3/bespoke_2lot_p3.py itself
# stores them (not configs_p3 attributes there either).
BESPOKE_COMBOS = [
    (2.0, 2.2, 2.0, 5.0),
    (2.5, 1.0, 1.25, 4.0),
]

# ---------------------------------------------------------------------------
# No MIN_ENTRY_TIME / session-boundary gate — WTI trades ~23h/day on NYMEX/
# Globex with no MCX-style defined session to anchor an entry-time buffer
# against. The whole series is treated as one continuous stream.
# ---------------------------------------------------------------------------

SLIPPAGE_ENABLED = False
