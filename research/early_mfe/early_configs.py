"""
Early-MFE study, all four live engines (2026-10-09). Research only: nothing here is read by an engine. Each engine is studied at its LIVE config
(Prometheus ST 2.5 with SL 1.0 / T1 1.25 / T2 4.0, Selene 2.5 / 3.0%, Helios 3.5 / 1.6%, Typhon 3.0 / 0.8% / 15%). Parameters live here only (repo convention).
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'outputs')
sys.path.insert(0, os.path.join(REPO_ROOT, 'research', 'trailing_profit'))
sys.path.insert(0, REPO_ROOT)

CHECKPOINTS = [15, 30, 60, 120, 240, 480]      # trading minutes since entry (bars, so an overnight hold does not inflate them)
RULE_CHECKPOINTS = [60, 120, 240]              # where the first-look exit rules act
MFE_R_FRACTIONS = [0.1, 0.25, 0.5]             # "peak gain below q x R", R = the engine's own stop distance, so engines compare on one scale
SPLIT_DATE = '2026-01-01'                      # before / after, the split every engine's calibration already uses
MIN_COHORT = 30                                # a tercile or cell below this many trades is printed but not read

# name: (backtest folder, multiplier folder, R = that track's stop % of entry). The Fyers 2026 stretch and the Angel One 2026 track cover the SAME months
# (two data sources of one contract, not independent samples); CRUDEOIL is a different contract. mult 2.0 (SL 2.2) is the reference for 'did the multiplier change matter'.
PROMETHEUS_TRACKS = {
    'Prometheus (CRUDEOILM, Fyers history)': ('phase3_fyers', 'mult_2.5', 1.0),
    'Prometheus (CRUDEOILM, Angel One 2026)': ('phase3', 'mult_2.5', 1.0),
    'Prometheus (CRUDEOIL cross-check)': ('phase3_crudeoil', 'mult_2.5', 1.0),
    'Prometheus (CRUDEOILM, Fyers history, mult 2.0 reference)': ('phase3_fyers', 'mult_2.0', 2.2),
    'Prometheus (CRUDEOILM, Angel One 2026, mult 2.0 reference)': ('phase3', 'mult_2.0', 2.2),
}
INNER_SPLIT_DATE = '2026-06-01'                # a split INSIDE 2026, because the 2026-only tracks have no pre-2026 half
COSTS = [0.0, 0.02, 0.05]                      # extra cost per triggered early exit, percent of price (a market order at a non-boundary minute)
