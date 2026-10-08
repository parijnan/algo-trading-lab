"""
Parameters of the provisional-margin measurement (plans/hestia-provisional-all-engines.md, section 1: pre-registered before any run). Research only:
nothing here is read by an engine. The supertrend settings are taken from each engine's own config so the measurement cannot drift from what runs live.
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, 'prometheus_backtest'))
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'outputs')       # gitignored

from helios_engine.engine_configs import DEFAULT as HELIOS     # noqa: E402
from prometheus_engine.engine_configs import DEFAULT as PROMETHEUS     # noqa: E402
from selene_engine.engine_configs import DEFAULT as SELENE     # noqa: E402
from typhon_engine.engine_configs import DEFAULT as TYPHON     # noqa: E402

# symbol -> (engine, supertrend period, multiplier); CRUDEOILM is the reference (its margin is the 0.15% placeholder)
INSTRUMENTS = {
    'SILVERMIC': ('selene', SELENE.st_period, SELENE.st_multiplier),
    'GOLDPETAL': ('helios', HELIOS.st_period, HELIOS.st_multiplier),
    'NATGASMINI': ('typhon', TYPHON.st_period, TYPHON.st_multiplier),
    'CRUDEOILM': ('prometheus', PROMETHEUS.st_period, PROMETHEUS.st_multiplier),
}
# first date of usable Fyers history per instrument (the engines' own loaders, and janus_backtest, use the same)
DATA_START = {'CRUDEOILM': '2023-04-01', 'SILVERMIC': '2021-04-01', 'GOLDPETAL': '2021-10-01', 'NATGASMINI': '2023-04-01'}
HOLIDAYS_FILE = os.path.join(REPO_ROOT, 'data', 'mcx_holidays_2022_2026.csv')

BAR_MINUTES = 15
# the pre-registered split: the margin is fitted on bars before this date and verified on bars from it
FIT_END = '2026-01-01'
# the bound U counts a move from the final minute's close to the next minute's open only when that minute opens within this many minutes of the boundary
GAP_WINDOW_MIN = 3
# the margin grid (percent of price); m* is the smallest grid value at or above the fit-period maximum of r
GRID_STEP_PCT = 0.01
GRID_MAX_PCT = 2.00
# percentile points reported next to the rule (not used to choose)
REPORT_PERCENTILES = [99.0, 99.9, 99.99]
TOP_BARS = 10
