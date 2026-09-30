"""
Typhon - NATGASMINI Supertrend strategy discovery: Phase 2 raw signal sweep
configuration (plans/typhon-natgasmini-st-strategy.md). Source of truth for
every parameter in this directory -- no magic numbers in the scripts.

Module-named typhon_configs.py, not configs.py, same reason
selene_backtest/selene_configs.py and helios_backtest/helios_configs.py are
named the way they are (CLAUDE.md "module naming" rule -- this directory
imports prometheus_backtest/data_loader_p3.py, whose own chain does
`import configs` expecting prometheus_backtest/configs.py).

Design mirrors helios_backtest/helios_configs.py's Phase 2 (raw
signal-following state machine: no SL, no target, no EOD square-off, single
position, the ONLY exit is the opposite ST_15 flip, fills at the open of the
bar after the flip bar, every trade tracked for MFE/MAE) with ONE structural
difference, found before any sweep code was written (2026-09-30): NATGASMINI's
monthly roll gap is large (median ~5.6%, mean ~7.5% absolute, up to ~24%,
measured at the actual early-roll switch date -- see
plans/typhon-natgasmini-st-strategy.md Step 1) -- an order of magnitude bigger
than CRUDEOILM's. Prometheus/Selene/Helios's Phase 2 all run a naive spliced
series (real per-contract prices concatenated at the roll date) because their
own roll gaps are small enough to be noise; doing the same here would credit
or debit large fake P&L to any trade spanning a roll and would corrupt both
the multiplier ranking and the regime read. typhon_data_loader.py's
back_adjust() additively back-adjusts the historical segments so the joined
series has no roll-day jump -- this exactly reproduces real
close-old-at-roll-price/open-new-at-roll-price P&L (verified algebraically,
plan Step 1), NOT just an approximation. ST_SEED_DAYS/ROLLOVER_BUFFER_MIN
etc. (the real, non-adjusted parity-backtest path, ported once Phase 2/3 are
decided) are unaffected -- back-adjustment is confined to Phase 2/3 discovery.
"""

import os

import pandas as pd

SYMBOL = 'NATGASMINI'

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)

PROMETHEUS_DIR         = os.path.join(REPO_ROOT, 'prometheus_backtest')
INSTRUMENT_MASTER_FILE = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'mcx_instrument_master.csv')

DATA_SWEEP_DIR = os.path.join(BASE_DIR, 'data_sweep')

# Same holiday calendar Selene/Helios use for the early-roll trading-day count (2021-2026,
# unioned in the loader with production's own data_pipeline/data/mcx_holidays.csv).
HOLIDAYS_FILE = os.path.join(REPO_ROOT, 'data', 'mcx_holidays_2022_2026.csv')


def _lookup_lot_size(symbol: str) -> int:
    df = pd.read_csv(INSTRUMENT_MASTER_FILE)
    rows = df[df['name'] == symbol]
    if rows.empty:
        raise ValueError(f"No instrument master rows found for '{symbol}' in {INSTRUMENT_MASTER_FILE}")
    return int(rows.iloc[0]['lotsize'])


LOT_SIZE = _lookup_lot_size(SYMBOL)   # 250 mmBtu -- price is quoted per mmBtu, so 1 index POINT = Rs 250/lot,
                                       # 1 TICK (0.1, confirmed against the instrument master's tick_size=10
                                       # paise) = Rs 25/lot. Do not carry over Helios's "Rs 1 per lot" framing --
                                       # GOLDPETAL's LOT_SIZE happens to be 1, NATGASMINI's is not.
LOTS     = 1                          # single position, no scale-out (Phase 2 raw sweep)

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
# Fyers has NATGASMINI from 2023-03-14 (the 2023-04-25 contract's own window), but the first
# ~2.5 weeks are thin (median 1-min volume 4-6, vs 12+ from 2024 onward as the contract matured --
# plan Step 1). Skipped as an early thin/partial stretch, same convention as Selene's own 3-month
# skip for SILVERMIC -- open to override, not a hard constraint (CRUDEOILM trades live today with
# an all-history median volume of 11, thinner than NATGASMINI's 2024+ levels).
DATA_START = '2023-04-01'

# Early-roll threshold in trading days before a contract's final expiry. User-confirmed
# 2026-09-30: NATGASMINI's tender margin period is 5 working days before expiry -- same value as
# CRUDEOILM/SILVERMIC/GOLDPETAL. The rollover functions actually applied live in
# prometheus_backtest/data_loader_p3.py (TENDER_ROLL_TRADING_DAYS = 5) -- typhon_data_loader.py
# asserts the two agree, same pattern as Selene's/Helios's loaders.
TENDER_ROLL_TRADING_DAYS = 5

# ---------------------------------------------------------------------------
# Capital per unit (NOT used by the sweep -- recorded for the later return-%age phase).
# required capital for one lot = entry_price * LOT_SIZE * MARGIN_SIZING_MULTIPLIER.
# Derived 2026-09-30 from a single observed point (user: margin ~Rs 14,000/lot "right now";
# front contract (27OCT26) last close in the data is 299.7 on 2026-09-28, notional
# 299.7 * 250 = Rs 74,925/lot -> ratio ~18.7% of notional. Single-point derivation, and NatGas's
# own margin steps with SPAN/volatility far more than Gold/Silver/Crude's do (the very next
# contract out, 24NOV26, was already trading ~12% higher same day) -- re-check against a second
# observed margin figure before trusting this for real sizing work (same caveat as Selene's/
# Helios's own single-point derivations).
# ---------------------------------------------------------------------------
MARGIN_CONTRACT_VALUE_DIVISOR = 1
MARGIN_SIZING_MULTIPLIER      = 14000 / (299.7 * 250)   # ~0.1869, i.e. ~18.7% of notional

# ---------------------------------------------------------------------------
# Session / entry guards -- same as Prometheus's/Selene's/Helios's Phase 3 / production values.
# ---------------------------------------------------------------------------
MIN_ENTRY_BUFFER_MIN = 15   # minutes since the session's own first bar before a fresh entry may fill

# ---------------------------------------------------------------------------
# Signal -- the thing under test
# ---------------------------------------------------------------------------
ST_PERIOD = 10   # held fixed; grid is multiplier-only, same convention as Prometheus/Selene/Helios Phase 2
ST_MULTIPLIER_GRID = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0]

# Per-trade minute-by-minute CSVs (running MAE/MFE/unrealised) are large over an 11-value grid --
# written only on request. The sweep's MFE/MAE summary columns are computed either way.
SAVE_TRADE_LOGS = False

SLIPPAGE_ENABLED = False   # costs deliberately absent in Phase 2, same convention as Selene/Helios --
                            # costs come in starting Phase 3/4, not folded into the raw sweep

# Regimes (plan Step 2): NatGas's own known regime drivers are seasonal (winter demand-driven
# spikes, e.g. the genuine ~600 print on 2026-01-27, a real cold-snap move not a data error --
# plan Step 1) and realized-volatility clustering, NOT a single hand-picked split date the way
# Prometheus/Selene/Helios's WALKFORWARD_SPLIT_DATE checks were. Both cuts are computed in the
# regime-analysis script, not assumed in advance.
WINTER_MONTHS = {11, 12, 1, 2}   # Nov-Feb, Northern Hemisphere heating-demand season
REALIZED_VOL_LOOKBACK_DAYS = 20  # trailing daily-return stdev window for the vol-bucket cut
REALIZED_VOL_BUCKETS = 3         # tercile split (low/mid/high realized vol)

# A walk-forward split is still computed as a secondary, non-seasonal check (same purpose as
# Selene's/Helios's WALKFORWARD_SPLIT_DATE), not as the primary regime read here.
WALKFORWARD_SPLIT_DATE = '2025-01-01'

# ---------------------------------------------------------------------------
# Phase 3 -- exit calibration (plan Step 3, exit_calib_typhon.py). User decision, 2026-09-30:
# carry the full sweep plateau (not a single pre-committed multiplier) into calibration, same
# posture as Helios's own broad shortlist; and run TWO candidates side by side rather than
# picking a tranche design upfront -- NATGASMINI's own lot economics (~Rs 75k notional/lot
# against ~Rs 14k margin) don't suggest a "1 unit = N lots" choice the way GOLDPETAL's tiny
# 1-gram lot did for Helios's 20-lot unit.
#
# Unlike Helios's single shared UNIT_LOTS split N ways, the two Typhon candidates have their OWN
# unit sizes (this is the real difference the user's two options described, not just a tranche
# count): "1 lot = 1 unit, no scale-out" (Selene's own decided shape) vs "2 lots = 1 unit,
# Prometheus-style scale-out" -- exit_calib_typhon.py's run_variant()/calibrate_*() take an
# explicit unit_lots argument rather than reading a single global constant.
# ---------------------------------------------------------------------------
UNIT_LOTS_SL_ONLY = 1     # "1 lot = 1 unit, no scale-out" candidate -- Selene's own shape
UNIT_LOTS_SCALEOUT = 2    # "2 lots = 1 unit, Prometheus-style scale-out" candidate
SL_ONLY_CANDIDATE = True  # always run the SL-only/trend-flip-only candidate, not just N-tranche
TRANCHE_COUNTS = [2]      # only the 2-lot Prometheus-style shape -- not Helios's broader [1,2,3,4]
                           # sweep, since the user asked to compare exactly these two shapes,
                           # not search an open tranche count

# Shortlist carried out of the Phase 2 sweep + regime read (plan Step 2, user decision
# 2026-09-30): the full plateau, not a single pre-committed multiplier.
CALIBRATION_MULTIPLIERS = [3.0, 3.5, 4.0, 5.5]

# A percentage this large can never be reached, i.e. "target disabled" -- same convention as
# Selene's/Helios's DISABLED_PCT, used for the SL-only candidate and for not-yet-calibrated
# later-stage targets while an earlier stage is pinned.
DISABLED_PCT = 1000.0

# Starting grids -- percent of (back-adjusted) entry price. Derived from the Phase 2 sweep's own
# avg/max MAE-MFE points (plan Step 2 summary), converted at a rough reference price of ~300 (the
# back-adjusted series' current level): mult 3.0 averaged ~1.1%/2.1% MAE/MFE, mult 5.5 ~1.7%/3.3%
# -- but NatGas's own tail is real and wide (mult 5.5's max_mfe_points was 84.4, i.e. ~28% of
# price, matching the genuine winter-squeeze character found in Step 1), so both grids are set
# well past the typical range rather than tight around it. Re-narrow/widen based on where the
# staged winners actually land, same discipline as Helios's own T1-grid-widening precedent --
# expect this starting point to need adjustment, not to be final.
SL_GRID = [0.5, 0.8, 1.2, 1.6, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0]
TARGET_GRID = [0.5, 0.8, 1.2, 1.6, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 20.0, 25.0]

# Same production guard Prometheus/Selene/Helios replicate: no SL/target check on a session's own
# first 1-min bar.
NO_EXIT_BEFORE_BUFFER_MIN = 1

# ---------------------------------------------------------------------------
# SUPERSEDED (kept as a record, not read anywhere): the 2026-09-30 back-adjusted Step 3 result
# (mult 4.0, SL 0.5%, targets [0.5%, 20%], 2-lot scale-out) was found the SAME DAY to be
# confounded -- every percentage in Step 2/3 was computed against the back-adjusted price, not
# the real one. Additive back-adjustment preserves point distances, not percentages -- for an
# old trade the adjusted price sits far above the real price (the earliest offset is +338.4 on a
# real price of ~195, so "0.5%" tested there was actually ~1.35% of the real price), while a
# current trade's offset is ~0 so "0.5%" is genuinely 0.5%. This confounded the walk-forward
# comparison that moved the pick away from mult 3.0/3.5. Rebuilt Step 4 (the production-parity
# backtest, real per-contract prices, real roll execution -- typhon_backtest/
# parity_backtest_typhon.py, exit_calib_parity_typhon.py) as the corrected source of truth
# instead of patching the back-adjusted scripts, per the user's own suggestion.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# DECIDED config (plan Step 4, 2026-09-30): mult 3.0, SL 0.8%, target 15%, single lot -- the
# REAL-price, real-roll-execution parity calibration's own winner, confirmed independently four
# ways (raw-signal full-window Calmar, raw-signal walk-forward, calibrated full-window Calmar,
# calibrated walk-forward all agree on mult 3.0). Full stats: profit factor 1.31 full-window
# (1.57 pre-2025 / 1.15 post-2025, the best post-2025 profit factor of every multiplier tested),
# Calmar% 14.07, max drawdown only 10.7% of total return. The target is a near-uncapped safety
# cap in practice (hit on only 0.5% of trades) -- trend-flip does almost all the real work, same
# character as Helios's/Selene's own decided designs, but the calibration found this ONE target
# genuinely helps here (unlike Selene's/Helios's own SL-only decisions) -- not assumed, tested:
# scale-out (2 lots, 2 targets) beat SL-only at every multiplier in the (superseded) back-adjusted
# comparison, and a single lot with one target beat pure SL-only in this real-price one too.
# typhon_backtest/data_sweep/parity_decided_trades.csv/parity_decided_legs.csv are the full trade
# log (1,392 closed trades, 2023-04-03 to 2026-09-29).
# ---------------------------------------------------------------------------
DECIDED_MULTIPLIER = 3.0
DECIDED_SL_PCT = 0.8
DECIDED_TARGET_PCT = 15.0
DECIDED_UNIT_LOTS = 1

# ---------------------------------------------------------------------------
# Production-parity backtest (parity_backtest_typhon.py, plan Step 4). Mirrors how
# prometheus_production/, Selene's, and Helios's own event-driven parity simulators handle
# contracts: per-contract ST (no splice, no back-adjustment -- the whole point of this phase is
# to sidestep the percentage bug back-adjustment caused, plan Step 3's correction section),
# production's early-roll rule, and the real roll machinery (coincident-flip re-entry,
# close-and-switch, rollover-time veto with historical-basis stop recalibration).
# ---------------------------------------------------------------------------
# Each session's ST is seeded from this many calendar days of the CONTRACT'S OWN 1-minute
# history, then extended bar by bar -- same value Prometheus/Selene/Helios all use.
ST_SEED_DAYS = 18

# Rollover fallback (position still open late on the eve of a roll): (last 1-min bar of the
# session) - ROLLOVER_BUFFER_MIN, same convention/value as Selene's/Helios's.
ROLLOVER_BUFFER_MIN = 14
# historical_basis_price refuses a lookup further than this from the original entry time.
BASIS_MAX_GAP_MIN = 5

# Fyers-complete segment: from DATA_START to the last day before the same systemic Fyers void
# Selene/Helios both hit also affects NATGASMINI (checked 2026-09-30): the April-2026 and
# May-2026 contract files both effectively end 2026-03-31, and the next real contract data
# (July-2026 file) starts 2026-06-30 -- identical window, 2026-04-01 -> 2026-06-29.
PARITY_END = '2026-03-31'

# Extension to the end of the data: AngelOne fills where Fyers has nothing. AngelOne's
# per-contract files hold their OWN contract only from this date (a pipeline-wide behavior, not
# instrument-specific -- same date Selene's/Helios's own loaders use); earlier rows are the
# then-front-month contract's real prices under a not-yet-front token, relabelled accordingly.
ANGELONE_OWN_FROM = '2026-09-02'
PARITY_END_EXTENDED = '2026-09-29'   # last date with real data in data_pipeline/data/mcx/NATGASMINI/

# Winners are also reported on each side of this date -- same convention as Step 2/3's own
# WALKFORWARD_SPLIT_DATE, now computed on real prices instead of the back-adjusted series.
PARITY_WALKFORWARD_SPLIT_DATE = WALKFORWARD_SPLIT_DATE
