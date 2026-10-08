"""
Trailing-profit overlay study for Selene, Helios and Typhon (2026-10-08, user: "nothing to lose"). Research only: nothing here is read by an engine.
Each instrument is tested at its DECIDED config (multiplier, stop, target) from its own backtest configs; a trailing rule is layered on top of the decided stop.
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'outputs')
for d in ('selene_backtest', 'helios_backtest', 'typhon_backtest', 'prometheus_backtest'):
    sys.path.insert(0, os.path.join(REPO_ROOT, d))

SPLIT_DATE = '2026-01-01'          # the same before/after split every engine's exit calibration uses

# percent of price; trail = distance below the running peak (long frame), activation = gain at which a rule switches on
TRAIL_GRID = [0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0]                 # a trail that is always on from entry
ACT_GRID = [0.5, 1.0, 2.0, 3.0, 5.0]                                    # activation levels (also the breakeven triggers)
ACT_TRAIL_GRID = [0.5, 1.0, 1.5, 2.0, 3.0]                              # the trail distance after activation
