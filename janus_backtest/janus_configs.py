"""
Camarilla pivot intraday research (plans/janus-camarilla-research.md). Every parameter lives here: no magic numbers in the other
scripts. Research only: nothing in this directory touches live code, Hestia, Delos or the engines' configs.
"""

import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMETHEUS_DIR = os.path.join(REPO_ROOT, 'prometheus_backtest')            # data_loader_p3's rollover machinery is reused, not copied
FYERS_DATA_DIR = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'mcx_fyers')
HOLIDAYS_FILE = os.path.join(REPO_ROOT, 'data', 'mcx_holidays_2022_2026.csv')
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'outputs')

# The four instruments the existing engines trade (liquidity already confirmed); any of them could later be hosted by Hestia.
SYMBOLS = ['CRUDEOILM', 'SILVERMIC', 'GOLDPETAL', 'NATGASMINI']

# First date of usable Fyers history per instrument (the earliest contract file's start; same values the engines' own loaders use).
DATA_START = {'CRUDEOILM': '2023-04-01', 'SILVERMIC': '2021-04-01', 'GOLDPETAL': '2021-10-01', 'NATGASMINI': '2023-04-01'}

# Systemic Fyers data void shared by all instruments (found by the Selene/Helios/Typhon work): the April and May 2026 contract files
# effectively end 2026-03-31 and the next real data starts 2026-06-30. Sessions inside the void are excluded, never filled.
FYERS_VOID = ('2026-04-01', '2026-06-29')
# Angel One per-contract files hold their OWN contract only from this date (earlier rows are a relabelled front month); Phase 0 uses
# Fyers only, so the study ends where Fyers history ends and the Angel One tail is a later extension.
DATA_END = None            # None: whatever the Fyers files hold

# Camarilla levels: previous session high H, low L, close C, range R = H - L.
#   R1..R4 = C + R * FACTOR * (1/12, 1/6, 1/4, 1/2);  S1..S4 mirror them below C.
#   R5 = (H / L) * C;  S5 = C - (R5 - C).
CAMARILLA_FACTOR = 1.1
LEVEL_DIVISORS = {1: 12.0, 2: 6.0, 3: 4.0, 4: 2.0}

# The previous session counts as "the previous trading day" only if it is no more than this many calendar days back (weekends and one
# holiday pass; a longer hole in the contract's own data means the levels would be stale, so that session is skipped).
MAX_PREV_SESSION_GAP_DAYS = 5

# A session needs at least this many 1-minute bars (after dropping placeholders) to be used, previous session included.
MIN_BARS_PER_SESSION = 120

# Open-location buckets in units of R from the previous close are defined by the levels themselves (see janus_events.open_zone).
# Touch events within this many minutes of the session's first bar are reported separately as "at open" (a gap beyond a level is not a touch).
AT_OPEN_MINUTES = 1

# Levels whose first touch is studied (level numbers; both sides, R and S). R5/S5 have no further outward level, so no first-passage pair.
TOUCH_LEVELS = [3, 4, 5]
# First-passage pairs (outward level, inward level) measured after the first touch of the keyed level, in the frame where the touched level
# is an upper one (lower-side touches are mirrored). 'C' is the previous close. The outward level acts as the stop of a fade or the
# continuation target of a breakout, the inward one as the fade target.
FIRST_PASSAGE = {3: [(4, 2), (4, 'C')], 4: [(5, 3), (5, 2)], 5: []}
# Open-location zones, from the levels themselves (see janus_events.open_zone).
