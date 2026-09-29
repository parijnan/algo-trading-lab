"""
Helios - Gold Petal Supertrend strategy discovery: Phase 2 raw signal sweep
configuration (plans/helios-goldpetal-st-strategy.md). Source of truth for
every parameter in this directory -- no magic numbers in the scripts.

Module-named helios_configs.py, not configs.py, on purpose: this directory
imports prometheus_backtest/data_loader_p3.py, whose own chain does
`import configs` expecting prometheus_backtest/configs.py (CLAUDE.md
"module naming" rule -- sys.modules caches by bare name). Same reason
selene_backtest/selene_configs.py is named the way it is.

Design mirrors selene_backtest/selene_configs.py's Phase 2 (raw
signal-following state machine: no SL, no target, no EOD square-off,
single position, the ONLY exit is the opposite ST_15 flip, fills at the
open of the bar after the flip bar, every trade tracked for MFE/MAE) --
the multiplier grid is the one variable under test, ST_PERIOD held at 10
by the same convention (re-derive from scratch for Gold Petal's own
character in Phase 3, do not assume this transfers).

Returns as a percentage are deliberately not computed in this phase, same
reasoning and phasing as Selene's (plan §1.7/§4): margin/return-% work
comes after a signal+exit config is decided, not before.
"""

import os

import pandas as pd

SYMBOL = 'GOLDPETAL'

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)

PROMETHEUS_DIR         = os.path.join(REPO_ROOT, 'prometheus_backtest')
INSTRUMENT_MASTER_FILE = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'mcx_instrument_master.csv')

DATA_SWEEP_DIR = os.path.join(BASE_DIR, 'data_sweep')

# Same holiday calendar Selene uses for the early-roll trading-day count (2021-2026,
# unioned in the loader with production's own data_pipeline/data/mcx_holidays.csv).
HOLIDAYS_FILE = os.path.join(REPO_ROOT, 'data', 'mcx_holidays_2022_2026.csv')


def _lookup_lot_size(symbol: str) -> int:
    df = pd.read_csv(INSTRUMENT_MASTER_FILE)
    rows = df[df['name'] == symbol]
    if rows.empty:
        raise ValueError(f"No instrument master rows found for '{symbol}' in {INSTRUMENT_MASTER_FILE}")
    return int(rows.iloc[0]['lotsize'])


LOT_SIZE = _lookup_lot_size(SYMBOL)   # 1 (gram) -- price is quoted per gram, so 1 index point = Rs 1 per lot
                                       # confirmed independently 2026-09-29 against three public sources
                                       # (plan §1.1/§2, "Web research done"), matches the cached instrument master
LOTS     = 1                          # single position, no scale-out

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
# Earliest usable date in the local Fyers staging tree (data_pipeline/data/mcx_fyers/GOLDPETAL/,
# 2021-11 through 2026-08 contract files, ~57 months). The earliest contract file's own data starts
# 2021-10-01 with real, non-thin daily volume from day one (median ~15-17k lots/day in Oct-Dec 2021,
# checked 2026-09-29) -- unlike Selene's SILVERMIC, no early illiquid stretch was found to exclude.
DATA_START = '2021-10-01'

# Early-roll threshold in trading days before a contract's final expiry. User-confirmed 2026-09-29:
# Gold Petal's tender margin period is 5 working days before expiry -- same as CRUDEOILM and SILVERMIC
# (plan §2, "Both resolved by the user"). The rollover functions actually applied live in
# prometheus_backtest/data_loader_p3.py (TENDER_ROLL_TRADING_DAYS = 5) -- helios_data_loader.py
# asserts the two agree, same pattern as Selene's loader.
TENDER_ROLL_TRADING_DAYS = 5

# ---------------------------------------------------------------------------
# Capital per unit (NOT used by the sweep -- recorded for the later return-%age phase, plan §1.4/§2).
# required capital for one unit = entry_price * LOT_SIZE / MARGIN_CONTRACT_VALUE_DIVISOR * MARGIN_SIZING_MULTIPLIER.
# Derived 2026-09-29 from a single observed point (user: LTP 14,893, margin ~1,380 -> ~9.27% of notional).
# No clean small-integer divisor/multiplier split exists the way Prometheus's /3*4 or Selene's /8*4 did,
# so it is recorded directly as a ratio. Single-point derivation -- re-check against a second observed
# margin figure before trusting it for real sizing work (same caveat as Selene's own §1.5).
# ---------------------------------------------------------------------------
MARGIN_CONTRACT_VALUE_DIVISOR = 1
MARGIN_SIZING_MULTIPLIER      = 1380 / 14893   # ~0.0927, i.e. ~9.27% of notional (entry_price * LOT_SIZE)

# ---------------------------------------------------------------------------
# Session / entry guards -- same as Prometheus's/Selene's Phase 3 / production values.
# ---------------------------------------------------------------------------
MIN_ENTRY_BUFFER_MIN = 15   # minutes since the session's own first bar before a fresh entry may fill

# ---------------------------------------------------------------------------
# Signal -- the thing under test
# ---------------------------------------------------------------------------
ST_PERIOD = 10   # held fixed; grid is multiplier-only, same convention as Prometheus/Selene Phase 2
ST_MULTIPLIER_GRID = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0]

# Per-trade minute-by-minute CSVs (running MAE/MFE/unrealised) are large over an 11-value grid --
# written only on request. The sweep's MFE/MAE summary columns are computed either way.
SAVE_TRADE_LOGS = False

SLIPPAGE_ENABLED = False   # costs deliberately absent in Phase 2 (user decision, 2026-09-29: cost-free
                            # sweep like Selene's, despite the 1-gram-lot cost concern flagged in plan §1.3 --
                            # costs come in starting Phase 3/4, not folded into the raw sweep)

# ---------------------------------------------------------------------------
# Phase 3 -- exit calibration (plan §4e, exit_calib_helios.py). Unlike Prometheus's fixed
# 2-lot scale-out or Selene's fixed 1-lot (no scale-out), GOLDPETAL's 1-gram lot makes a much
# bigger unit practical: 20 lots per unit (user, 2026-09-29), split into N EQUAL tranches, each
# with its own staged, grid-searched profit target (same flat-%-of-entry-price convention as
# Prometheus/Selene). Targets are NOT assumed to help -- the user's explicit instruction is to
# compare against a genuine SL-only/trend-flip-only candidate (Selene's own decided design) on
# equal footing, backed by data, not to presuppose scale-out wins.
# ---------------------------------------------------------------------------
UNIT_LOTS = 20                       # 1 unit = 20 lots = 20 grams (user, 2026-09-29)
TRANCHE_COUNTS = [1, 2, 3, 4]        # candidate N values, each with N staged targets, equal-weight tranches
SL_ONLY_CANDIDATE = True             # also run the N=1-tranche, NO-target (trend-flip-only) candidate

# Shortlist carried out of the Phase 2 sweep (plan §4): P&L plateaus 3.5-5.5 on both the
# full-window and same-window-normalized views, peaking at 4.0 (full-window) / 5.0-5.5 (normalized).
CALIBRATION_MULTIPLIERS = [3.0, 3.5, 4.0, 4.5, 5.0, 5.5]

# A percentage this large can never be reached, i.e. "target disabled" -- same convention as
# Selene's DISABLED_PCT (exit_structures_selene.py), used for the SL-only candidate and for
# not-yet-calibrated later-stage targets while an earlier stage is pinned.
DISABLED_PCT = 1000.0

# Starting grids -- percent of entry price. Widened relative to Selene's own starting grids
# since GOLDPETAL's per-trade MFE (plan §4d) reaches into the low single digits more often at
# these multipliers; re-narrow/widen based on where the staged winners actually land (same
# discipline as Prometheus's own T1-grid-widening precedent).
SL_GRID = [0.5, 0.8, 1.2, 1.6, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0]
# Widened 2026-09-29 before the full run: a smoke test at mult 4.0 showed the SL-only Calmar%
# still rising at the original grid's 4.0% edge (16.14, vs a local dip to ~15.1-15.4 at 3.0-3.5%);
# extending found the true plateau at 5.0% (17.72, identical through 6.0-10.0% -- no trades bind
# that wide, so it converges to the raw/no-stop signal). 6.0% is kept as one point past the
# plateau's start to confirm it, not because a wider stop is expected to win.
# Widened again 2026-09-29: the first full run crashed at N=4's 4th stage -- T3 had already
# landed on the grid's own 8.0% ceiling, leaving nothing above it for T4 to pick from (each
# stage requires target > the previous stage's winner). Extended well past the MFE p99 range
# found in §4d (up to ~15% at some multipliers) so a 4-tranche ladder always has headroom.
TARGET_GRID = [0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0]

# Same production guard Prometheus/Selene replicate: no SL/target check on a session's own
# first 1-min bar.
NO_EXIT_BEFORE_BUFFER_MIN = 1

# Walk-forward split for the pre/post consistency check (plan §4g). Same purpose as Selene's
# WALKFORWARD_SPLIT_DATE: catch a calibration that only "works" in one regime.
WALKFORWARD_SPLIT_DATE = '2025-01-01'

# ---------------------------------------------------------------------------
# Decided config (plan §4g, 2026-09-29): walk-forward validated, not the naive full-window pick.
# ST_MULTIPLIER 3.5, SL 1.6% of entry price, no target -- trend-flip is the only exit, same
# design shape as Selene's. 1 unit = 20 lots, single tranche (no scale-out; N=3/N=4 never won
# in Phase 3, plan §4f).
# ---------------------------------------------------------------------------
DECIDED_MULTIPLIER = 3.5
DECIDED_SL_PCT = 1.6

# ---------------------------------------------------------------------------
# Production-parity backtest (parity_backtest_helios.py, plan §4h). Mirrors how
# prometheus_production/ and Selene's own event-driven simulator handle contracts: per-contract
# ST (no splice), production's early-roll rule, and the roll machinery (coincident-flip re-entry,
# close-and-switch, rollover-time veto with historical-basis stop recalibration).
# ---------------------------------------------------------------------------
# Each session's ST is seeded from this many calendar days of the CONTRACT'S OWN 1-minute
# history, then extended bar by bar -- same value Prometheus/Selene both use.
ST_SEED_DAYS = 18

# Rollover fallback (position still open late on the eve of a roll): (last 1-min bar of the
# session) - ROLLOVER_BUFFER_MIN, same convention/value as Selene's.
ROLLOVER_BUFFER_MIN = 14
# historical_basis_price refuses a lookup further than this from the original entry time.
BASIS_MAX_GAP_MIN = 5

# Fyers-complete segment: from DATA_START to the last day before the same systemic Fyers void
# Selene hit for SILVERMIC also affects GOLDPETAL (checked 2026-09-29): the April-2026 and
# May-2026 contract files both effectively end 2026-03-31 (May's file has almost no real data --
# 1,260 rows, all pre-void), and the next real contract data (July-2026 file) starts 2026-06-30.
# So the void is 2026-04-01 -> 2026-06-29, identical in shape to Selene's own finding.
PARITY_END = '2026-03-31'

# Extension to the end of the data: AngelOne fills where Fyers has nothing. AngelOne's
# per-contract files hold their OWN contract only from this date (a pipeline-wide behavior, not
# instrument-specific -- same date Selene's SILVERMIC loader uses); earlier rows are the
# then-front-month contract's real prices under a not-yet-front token, relabelled accordingly.
ANGELONE_OWN_FROM = '2026-09-02'
PARITY_END_EXTENDED = '2026-09-25'

# Winners are also reported on each side of this date -- Phase 2's own sweep was not checked
# for a regime break (plan §4, "Not yet done"), so this is a first look at it via the exit
# calibration's own pre/post split, same convention as Selene's WALKFORWARD_SPLIT_DATE.
WALKFORWARD_SPLIT_DATE = '2025-01-01'
