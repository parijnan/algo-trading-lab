"""
Weekend-flatten study (2026-10-12, user: "how our engines would fare if we flattened completely before the weekends"). Research only: nothing here is read by an engine.
Each engine is studied at its LIVE / decided config through its own parity simulator (the same ones the trailing-profit and early-MFE studies use). Parameters live here only.
"""

import os
import sys
from datetime import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'outputs')
sys.path.insert(0, os.path.join(REPO_ROOT, 'research', 'trailing_profit'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'research', 'early_mfe'))
sys.path.insert(0, REPO_ROOT)

SPLIT_DATE = '2026-01-01'                  # the before / after split every engine's calibration uses

# Flatten clock, minutes before the session close (the close is read off the data: the last 1-minute bar of the week's last session + 1 minute).
# 'user' = the times the user named: 23:15 on a 23:30 close, 23:45 on a 23:55 close. 'engine' = Hestia's own rollover time (hestia_core/roll_policy.py:
# 15 minutes before the close, so 23:15 and 23:40). Any other close (an early-closing or evening-only session) uses DEFAULT_OFFSET_MIN.
OFFSET_VARIANTS = {
    'user': {time(23, 30): 15, time(23, 55): 10},
    'engine': {time(23, 30): 15, time(23, 55): 15},
}
DEFAULT_OFFSET_MIN = 15
MAIN_VARIANT = 'user'

SLIPPAGE_PCT = [0.0, 0.02, 0.05]           # extra cost on the flatten fill only, percent of price (late Friday is thin)
SUSPECT_LAST_WEEKDAY = 2                   # a week whose last session is Mon..Wed (weekday <= 2) is reported as a possible data hole, not trusted silently
REENTRY_DELAY_MIN = 15                     # the engines' min_entry_buffer_min: no entry in the first 15 minutes of a session
STOP_SLIP_PCT = 0.05                       # a baseline stop filled this far (percent of entry) past its level counts as a gap fill

# Prometheus (live ST 2.5, SL 1.0 / T1 1.25 / T2 4.0): saved per-trade logs of the Fyers-history track
PROMETHEUS_TRACK = ('phase3_fyers', 'mult_2.5')
