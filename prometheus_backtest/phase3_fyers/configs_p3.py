"""
Prometheus - Phase 3, Fyers-sourced independent validation track.

Parallel to ../phase3/ (CRUDEOILM, Angel-One-sourced) and
../phase3_crudeoil/ (CRUDEOIL, Angel-One-sourced) — this one is
CRUDEOILM sourced from Fyers's own historical data
(data_pipeline/data/mcx_fyers/) instead of Angel One's
(data_pipeline/data/mcx/), per the user's own 2026-09-15 instruction:
mirror production's rollover logic and the already-decided mult 2.0 +
bespoke exit parameters exactly, kept as a fully separate result set
until the user has validated the findings against the existing
Angel-One-sourced Phase 3 numbers — nothing here feeds into
prometheus_backtest/README.md's published tables.

Same ST_PERIOD/ST_MULTIPLIER/entry-buffer/exit-buffer values as
../phase3/configs_p3.py, for direct comparability — only the data
source differs (see data_loader_fyers.py's own docstring for the
Angel-One gap-fill window, 2026-03-13 through 2026-06-29, where Fyers
itself has a confirmed real data void).
"""

import os

import pandas as pd

SYMBOL = 'CRUDEOILM'   # scoped to the mini contract only for now, per the
                       # user's own instruction ("Build it for the mini
                       # contract for now") -- CRUDEOIL cross-validation,
                       # if wanted later, is a separate follow-up track.

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
PROMETHEUS_DIR = os.path.dirname(BASE_DIR)
REPO_ROOT      = os.path.dirname(PROMETHEUS_DIR)

INSTRUMENT_MASTER_FILE = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'mcx_instrument_master.csv')

DATA_DIR       = os.path.join(BASE_DIR, 'data')
DATA_SWEEP_DIR = os.path.join(BASE_DIR, 'data_sweep')


def _lookup_lot_size(symbol: str) -> int:
    df = pd.read_csv(INSTRUMENT_MASTER_FILE)
    rows = df[df['name'] == symbol]
    if rows.empty:
        raise ValueError(f"No instrument master rows found for '{symbol}' in {INSTRUMENT_MASTER_FILE}")
    return int(rows.iloc[0]['lotsize'])


LOT_SIZE = _lookup_lot_size(SYMBOL)
LOTS     = 1   # single position, no scale-out at the raw-signal stage
               # (2-lot scale-out is applied afterward by bespoke_2lot_p3.py,
               # same convention as ../phase3/)

# ---------------------------------------------------------------------------
# Session / entry window -- identical to ../phase3/configs_p3.py.
# ---------------------------------------------------------------------------
MIN_ENTRY_BUFFER_MIN = 15
NO_EXIT_BEFORE_BUFFER_MIN = 1

# ---------------------------------------------------------------------------
# Signal -- the already-decided production value, not swept here.
# ---------------------------------------------------------------------------
ST_PERIOD = 10
ST_MULTIPLIER_GRID = [2.0, 2.5]   # 2.5 added 2026-10-08: Prometheus live multiplier since 2026-10-05, for the combined equity curve

# ---------------------------------------------------------------------------
# Costs -- same convention as every other phase: deliberately absent.
# ---------------------------------------------------------------------------
SLIPPAGE_ENABLED = False

# ---------------------------------------------------------------------------
# Production-parity backtest (parity_backtest_prometheus.py, 2026-10-16). One real contract at a time, per-contract Supertrend,
# the real roll machinery (hestia_core/roll_policy.py) and the live two-lot exits. Every value below is the LIVE one in
# prometheus_engine/engine_configs.py (tests/test_prometheus_parity_backtest.py pins them to it).
# ---------------------------------------------------------------------------
PARITY_DATA_START = '2023-03-01'    # the first Fyers CRUDEOILM history (the older splice track starts 2023-03-03)
PARITY_ST_MULTIPLIER = 2.5
PARITY_SL_PCT = 1.0
PARITY_T1_PCT = 1.25
PARITY_T2_PCT = 4.0
LOTS_PER_LEG = 1                    # lots in each of the two legs of one unit (lot 1 books at T1, lot 2 at T2)
ST_SEED_DAYS = 18                   # calendar days of 1-minute history behind each session's Supertrend seed (hestia_config seed_days)
# Production rolls at the session close minus 15 minutes (23:15 on a 23:30 close, 23:40 on 23:55). The simulator measures from the LAST
# one-minute bar of the session (23:29 / 23:54), hence 14.
ROLLOVER_BUFFER_MIN = 14
BASIS_MAX_GAP_MIN = 5               # the recalibration basis must come from a minute this close to the old entry
ANGELONE_OWN_FROM = '2026-09-02'    # live-tree files hold a contract's OWN prices only from this date (earlier rows are a relabelled front month)
HOLIDAYS_FILE = os.path.join(REPO_ROOT, 'data', 'mcx_holidays_2022_2026.csv')
PARITY_OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data_sweep', 'parity')
