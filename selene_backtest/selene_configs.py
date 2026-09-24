"""
Selene - SILVERMIC Supertrend strategy discovery: Phase 2 raw signal sweep
configuration (plans/selene-silvermic-st-strategy.md). Source of truth for
every parameter in this directory -- no magic numbers in the scripts.

Module-named selene_configs.py, not configs.py, on purpose: this directory
imports prometheus_backtest/data_loader_p3.py, whose own chain does
`import configs` expecting prometheus_backtest/configs.py (CLAUDE.md
"module naming" rule -- sys.modules caches by bare name).

Design mirrors prometheus_backtest/phase3/configs_p3.py (raw signal-following
state machine: no SL, no target, no EOD square-off, single position, the ONLY
exit is the opposite ST_15 flip, fills at the open of the bar after the flip
bar, every trade tracked for MFE/MAE) -- the multiplier grid is the one
variable under test, ST_PERIOD held at 10.

Returns as a percentage are deliberately NOT computed in this phase (user,
2026-09-24: "we'll calculate returns %age only after fine-tuning the actual
strategy"). The capital-per-unit convention that will drive them later is
recorded below (MARGIN_*) so it lives in one place.
"""

import os

import pandas as pd

SYMBOL = 'SILVERMIC'

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)

PROMETHEUS_DIR         = os.path.join(REPO_ROOT, 'prometheus_backtest')
INSTRUMENT_MASTER_FILE = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'mcx_instrument_master.csv')

DATA_SWEEP_DIR = os.path.join(BASE_DIR, 'data_sweep')

# MCX holiday calendar for the early-roll trading-day count (user-supplied 2026-09-24,
# columns: Year, Date, Day, Holiday, Morning Session, Evening Session). The file name says
# 2022-2026; 2021's weekday holidays were appended the same day from truedata.in's 2021 MCX
# list and cross-checked against the 1-min data. Production's own
# data_pipeline/data/mcx_holidays.csv only lists 2026, so the loader unions both.
HOLIDAYS_FILE = os.path.join(REPO_ROOT, 'data', 'mcx_holidays_2022_2026.csv')


def _lookup_lot_size(symbol: str) -> int:
    df = pd.read_csv(INSTRUMENT_MASTER_FILE)
    rows = df[df['name'] == symbol]
    if rows.empty:
        raise ValueError(f"No instrument master rows found for '{symbol}' in {INSTRUMENT_MASTER_FILE}")
    return int(rows.iloc[0]['lotsize'])


LOT_SIZE = _lookup_lot_size(SYMBOL)   # 1 (kg) -- price is quoted per kg, so 1 index point = Rs 1 per lot
LOTS     = 1                          # single position, no scale-out

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
# Earliest date loaded. Fyers has SILVERMIC from 2021-01-31 (June-2021
# contract's own window) -- see selene_data_loader.py for the source-blend
# rules and the known-thin far-month early stretch.
DATA_START = '2021-04-01'

# Early-roll threshold in trading days before a contract's final expiry.
# User-confirmed 2026-09-24: the tender margin applies from 5 working days
# before final expiry; also matches the observed liquidity crossover
# (plan §1.3/§1.4). The rollover functions actually applied live in
# prometheus_backtest/data_loader_p3.py (which owns the same-named constant,
# TENDER_ROLL_TRADING_DAYS = 5) -- selene_data_loader.py asserts the two agree.
TENDER_ROLL_TRADING_DAYS = 5

# ---------------------------------------------------------------------------
# Capital per unit (NOT used by the sweep -- recorded for the later
# return-%age phase, plan §1.5/§1.7): required capital for one unit =
# entry_price * LOT_SIZE / MARGIN_CONTRACT_VALUE_DIVISOR * MARGIN_SIZING_MULTIPLIER.
# (Crude's is /3 * 4.) User-confirmed 2026-09-24.
# ---------------------------------------------------------------------------
MARGIN_CONTRACT_VALUE_DIVISOR = 8
MARGIN_SIZING_MULTIPLIER      = 4

# ---------------------------------------------------------------------------
# Session / entry guards -- same as Prometheus's Phase 3 / production values.
# ---------------------------------------------------------------------------
MIN_ENTRY_BUFFER_MIN = 15   # minutes since the session's own first bar before a fresh entry may fill

# ---------------------------------------------------------------------------
# Signal -- the thing under test
# ---------------------------------------------------------------------------
ST_PERIOD = 10   # held fixed; grid is multiplier-only, same as Prometheus Phase 3
ST_MULTIPLIER_GRID = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0]   # user, 2026-09-24: 1.0-6.0

# Per-trade minute-by-minute CSVs (running MAE/MFE/unrealised) are ~1.3M
# rows per multiplier over the full history -- written only on request. The
# sweep's MFE/MAE summary columns are computed either way.
SAVE_TRADE_LOGS = False

SLIPPAGE_ENABLED = False   # costs deliberately absent, as in every other phase
