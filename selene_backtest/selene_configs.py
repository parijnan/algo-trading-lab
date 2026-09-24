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

# ---------------------------------------------------------------------------
# Phase 3 -- exit calibration (plan §5, exit_calib_selene.py). Bespoke 2-lot
# scale-out on top of the raw signal: lot 1 books at TARGET1, lot 2 at TARGET2,
# a shared stop-loss, trend-flip as the fallback exit; positional, no EOD
# square-off. Staged one-variable-at-a-time (SL -> T1 -> T2, each stage picking
# the best Calmar with the others pinned), same as Prometheus's exit_calib_p3.py.
# ---------------------------------------------------------------------------
# Shortlist carried out of the Phase 2 sweep (plan §11): 2.5 and 3.0 lead ex-2026,
# 2.0 kept as the full-window co-leader/reference (user, 2026-09-24: "all 3").
CALIBRATION_MULTIPLIERS = [2.0, 2.5, 3.0]

# Grids start wider than Prometheus's on purpose: its T1 grid landed on its own
# edge on the first pass and had to be widened after the fact (prometheus_backtest/
# README.md Phase 3 caveat #1). Percent of entry price.
SL_GRID = [0.6, 1.0, 1.4, 1.8, 2.2, 2.6, 3.0, 3.5, 4.0]
T1_GRID = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
T2_GRID = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0]

# Values the not-yet-calibrated legs are pinned at in the first stages. T2's
# starting value must exceed the largest T1 in the grid (T1 stage pins T2).
T1_STARTING_DEFAULT = 1.0
T2_STARTING_DEFAULT = 4.0

# Same production guard Prometheus replicates: no SL/target check on a session's
# own first 1-min bar.
NO_EXIT_BEFORE_BUFFER_MIN = 1

# Winners are also reported on each side of this date, since the Phase 2 ranking
# was 2026-led (plan §11) and a combo that only works in 2026 is not a finding.
WALKFORWARD_SPLIT_DATE = '2026-01-01'

# ---------------------------------------------------------------------------
# Phase 3b -- exit STRUCTURE comparison (exit_structures_selene.py). Added after
# the staged calibration above hit its own grid edges (T1 3.0%, T2 8.0%) and
# showed targets adding nothing (plan §12): tests "SL only, trend-flip is the
# only profit exit" against the 2-lot structure over wider target grids.
# ---------------------------------------------------------------------------
# A percentage this large can never be reached, i.e. "leg switched off".
DISABLED_PCT = 1000.0
# Stage A: stop-loss only (both targets disabled); None-equivalent = DISABLED_PCT row.
SL_ONLY_GRID = [0.6, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 8.0]
# Stage B: full T1 x T2 grid at the SL fixed by stage A (T1 < T2 only), and a
# same-target single-exit variant is covered by T1 == T2.
T1_WIDE_GRID = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0]
T2_WIDE_GRID = [2.0, 3.0, 4.0, 6.0, 8.0, 10.0, 12.0, 16.0]

# ---------------------------------------------------------------------------
# Production-parity backtest (parity_backtest_selene.py, plan §13). Mirrors how
# prometheus_production/ actually handles contracts (plans/prometheus-phase3-
# production.md §3-§9, §18), applied to SILVERMIC's decided config below.
# ---------------------------------------------------------------------------
DECIDED_MULTIPLIER = 2.5     # plan §12
DECIDED_SL_PCT     = 3.0     # wide protective stop, % of entry price; no targets

# prometheus_configs.SEED_DAYS: each session's ST is seeded from this many
# calendar days of the CONTRACT'S OWN 1-minute history, then extended bar by bar.
ST_SEED_DAYS = 18

# Rollover fallback (position still open late on the eve of a roll): production runs it at
# ROLLOVER_TIME = CLOSING_TIME - 15 min (23:15 when the evening session closes 23:30, 23:40 when
# it closes 23:55). Here: (last 1-min bar of the session) - ROLLOVER_BUFFER_MIN.
ROLLOVER_BUFFER_MIN = 14
# historical_basis_price refuses a lookup further than this from the original entry time.
BASIS_MAX_GAP_MIN = 5

# Fyers-complete segment the parity run covers: from DATA_START to the last day the April-2026
# and June-2026 contracts both have Fyers data. Fyers has nothing 2026-04-01..06-29 and no
# unexpired-contract history for Nov-2026, so per-contract dual tracking is impossible after this.
PARITY_END = '2026-03-31'

# Extension to the end of the data (user, 2026-09-24): AngelOne fills where Fyers has nothing.
# AngelOne's per-contract files hold the file's OWN contract only from the day the pipeline began
# tracking it (2026-09-02 for Nov-2026/Feb-2027); earlier rows are the then-front-month contract's
# real prices under a not-yet-front token (data_downloader_mcx.py's header), so they are relabelled
# to whichever contract was front month that day, and only where Fyers has no data for that contract.
ANGELONE_OWN_FROM = '2026-09-02'
PARITY_END_EXTENDED = '2026-09-23'
