"""
Helios - GOLDTEN Supertrend strategy discovery, cross-validation instrument
(plans/helios-goldpetal-st-strategy.md §0). Mirrors helios_backtest/helios_configs.py
(GOLDPETAL, the primary candidate) -- same shape Prometheus used for its own
CRUDEOILM-primary / CRUDEOIL-cross-validation split (prometheus_backtest/phase3/
vs phase3_crudeoil/), one directory per instrument rather than a runtime symbol
switch, so nothing here risks silently drifting the primary candidate's config.

Raised by the user 2026-09-29 mid-Phase-2, before the GOLDPETAL sweep had run:
GOLDPETAL's 1-gram lot looked too small. Comparison done before building this
(plan, "Instrument choice" discussion): GOLDTEN and GOLDPETAL are comparably
liquid in real GRAM terms (the lot-count-only liquidity screen numbers make
GOLDTEN look ~9x thinner, but that's a unit-size artifact -- GOLDTEN's lot is
10x bigger); GOLDTEN's bigger lot directly addresses the flat-per-order-cost
concern flagged for GOLDPETAL (plan §1.3); GOLDTEN's usable Fyers history is
much shorter (~17 months vs GOLDPETAL's ~5 years). User's decision: run both,
compare on the actual sweep numbers rather than deciding from the tradeoff alone.

Module-named helios_configs_goldten.py, not configs.py, for the same
sys.modules reason as every other configs module in this repo (CLAUDE.md
"module naming" rule) -- this directory also imports
prometheus_backtest/data_loader_p3.py, whose chain does a bare `import configs`.
"""

import os

import pandas as pd

SYMBOL = 'GOLDTEN'

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)

PROMETHEUS_DIR         = os.path.join(REPO_ROOT, 'prometheus_backtest')
INSTRUMENT_MASTER_FILE = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'mcx_instrument_master.csv')

DATA_SWEEP_DIR = os.path.join(BASE_DIR, 'data_sweep')

HOLIDAYS_FILE = os.path.join(REPO_ROOT, 'data', 'mcx_holidays_2022_2026.csv')


def _lookup_lot_size(symbol: str) -> int:
    df = pd.read_csv(INSTRUMENT_MASTER_FILE)
    rows = df[df['name'] == symbol]
    if rows.empty:
        raise ValueError(f"No instrument master rows found for '{symbol}' in {INSTRUMENT_MASTER_FILE}")
    return int(rows.iloc[0]['lotsize'])


LOT_SIZE = _lookup_lot_size(SYMBOL)   # 10 (grams) -- confirmed against the instrument master 2026-09-29;
                                       # 10x GOLDPETAL's 1-gram lot, same tick_size=100 (real tick Rs 1).
                                       # PHYSICAL size only -- do NOT use this for Rs P&L or margin notional
                                       # (see RS_PER_POINT_PER_LOT below); it is the real gram figure used
                                       # for the liquidity/gram comparison against GOLDPETAL (plan §4).
LOTS     = 1                          # single position, no scale-out

# GOLDTEN's quoted LTP is already the FULL 10-gram lot's value (~Rs 150,000-151,000 in the current
# data), not a per-gram price the way GOLDPETAL's is -- confirmed 2026-09-29 by comparing the two
# instruments' close prices at the same minute (GOLDTEN / GOLDPETAL ~= 10.0, e.g. 150,599 / 15,116).
# This is exactly the "hidden unit-conversion factor" trap Selene's plan §1.6 flagged for GOLD/
# GOLDGUINEA/ALUMINIUM -- SILVERMIC and GOLDPETAL both happened to have price-quote-unit == lot
# unit (1 kg and 1 gram respectively), but GOLDTEN does not. One index point/tick move is Rs 1 on
# the WHOLE lot already, so the correct Rs-per-point-per-lot factor is 1, not LOT_SIZE=10 -- a bug
# in the first cut of backtest_helios_goldten.py (used LOT_SIZE directly, inflating every Rs figure
# 10x) was caught before Phase 3 and fixed here + in that file.
RS_PER_POINT_PER_LOT = 1

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
# Fyers coverage for GOLDTEN is much shorter than GOLDPETAL's: 16 monthly contract files,
# 2025-04-30 through 2026-08-31 (with the same known systemic Fyers void as every other MCX
# instrument, roughly 2026-03 to 2026-06). Earliest file (2025-04) has real, non-thin volume
# from day one (median ~2,824 lots/day = ~28,240 grams/day, checked 2026-09-29) -- no early
# illiquid stretch to exclude, same finding as GOLDPETAL's own earliest month.
DATA_START = '2025-04-01'

# Tender period: NOT independently confirmed for GOLDTEN. Carried over from GOLDPETAL's
# user-confirmed 5 working days (2026-09-29) on the assumption that MCX's gold mini/micro
# variants share the same tender-period rule -- this is an assumption, not a confirmation,
# and should be checked the same way GOLDPETAL's was before any production config.
TENDER_ROLL_TRADING_DAYS = 5

# ---------------------------------------------------------------------------
# Capital per unit (NOT used by the sweep). Margin-per-unit ratio carried over from
# GOLDPETAL's single-point derivation (2026-09-29: ~9.27% of notional) as a WORKING
# ASSUMPTION -- margin is typically a percentage of contract value (SPAN+exposure) and
# should be similar across gold variants of the same underlying, but this has NOT been
# independently confirmed for GOLDTEN with its own observed margin figure. Re-derive
# before trusting for real sizing work, same caveat as GOLDPETAL's own §1.4/§2.
# IMPORTANT: when this is actually used (Phase 4/5), the notional is
# `ltp * RS_PER_POINT_PER_LOT`, NOT `ltp * LOT_SIZE` -- see RS_PER_POINT_PER_LOT's own
# comment above. GOLDTEN's LTP already prices the whole lot.
# ---------------------------------------------------------------------------
MARGIN_CONTRACT_VALUE_DIVISOR = 1
MARGIN_SIZING_MULTIPLIER      = 1380 / 14893   # carried from GOLDPETAL, unconfirmed for GOLDTEN

# ---------------------------------------------------------------------------
# Session / entry guards -- same as every other engine's Phase 3 / production values.
# ---------------------------------------------------------------------------
MIN_ENTRY_BUFFER_MIN = 15

# ---------------------------------------------------------------------------
# Signal -- the thing under test. Same grid as GOLDPETAL's, for a like-for-like comparison.
# ---------------------------------------------------------------------------
ST_PERIOD = 10
ST_MULTIPLIER_GRID = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0]

SAVE_TRADE_LOGS = False
SLIPPAGE_ENABLED = False   # cost-free sweep, same decision as GOLDPETAL's (plan §10)
