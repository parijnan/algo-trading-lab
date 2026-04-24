"""
configs.py — Artemis Backtest Configuration

All parameters live here. To switch instruments, change INSTRUMENT.
All optimisable SL parameters are clearly marked.
Paths are absolute, derived from INSTRUMENT.
"""

import os

# ---------------------------------------------------------------------------
# Instrument selection
# ---------------------------------------------------------------------------
# 'nifty'  — use for Dec 2024 to Aug 2025 period
# 'sensex' — use for Sep 2025 to Mar 2026 period
INSTRUMENT              = 'sensex'

# ---------------------------------------------------------------------------
# Backtest scope
# ---------------------------------------------------------------------------
# Set to None to use all available contracts for the selected instrument.
# Dates are inclusive. Format: 'YYYY-MM-DD'.
BACKTEST_START_DATE     = None
BACKTEST_END_DATE       = None

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT               = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINE_DATA           = os.path.join(REPO_ROOT, 'data_pipeline', 'data')
PIPELINE_CFG            = os.path.join(REPO_ROOT, 'data_pipeline', 'config')

# Index data (same path structure for both instruments)
SENSEX_INDEX_FILE       = os.path.join(PIPELINE_DATA, 'indices', 'sensex.csv')
NIFTY_INDEX_FILE        = os.path.join(PIPELINE_DATA, 'indices', 'nifty.csv')
VIX_INDEX_FILE          = os.path.join(PIPELINE_DATA, 'indices', 'india_vix.csv')

# Options data directories (one subdirectory per expiry date YYYY-MM-DD)
SENSEX_OPTIONS_PATH     = os.path.join(PIPELINE_DATA, 'sensex')
NIFTY_OPTIONS_PATH      = os.path.join(PIPELINE_DATA, 'nifty', 'options')

# Holidays file (used by generate_contracts.py and backtest.py)
HOLIDAYS_FILE           = os.path.join(PIPELINE_CFG, 'holidays.csv')

# Output and generated files — all under artemis_backtest/data/
BACKTEST_DIR            = os.path.dirname(os.path.abspath(__file__))
CONTRACTS_FILE          = os.path.join(BACKTEST_DIR, 'data', 'contracts.csv')
TRADE_LOGS_DIR          = os.path.join(BACKTEST_DIR, 'data', 'trade_logs')
TRADE_SUMMARY_FILE      = os.path.join(BACKTEST_DIR, 'data', 'trade_summary.csv')

# ---------------------------------------------------------------------------
# Instrument-specific parameters
# ---------------------------------------------------------------------------
# Resolved at runtime based on INSTRUMENT. Do not edit the resolver block —
# edit the per-instrument dicts above it.

_NIFTY_PARAMS = {
    'lot_size':             65,
    'strike_interval':      100,
    'expected_premium':     30,
    'hedge_points':         300,
    'index_sl_offset':      50,       # OPTIMISABLE — base value, overridden by VIX band
    'adjustment_distance':  200,
    'minimum_gap':          350,
    'minimum_gap_iterator': 100,
}

_SENSEX_PARAMS = {
    'lot_size':             20,
    'strike_interval':      100,
    'expected_premium':     120,
    'hedge_points':         1000,
    'index_sl_offset':      200,      # OPTIMISABLE — base value, overridden by VIX band
    'adjustment_distance':  600,
    'minimum_gap':          1000,
    'minimum_gap_iterator': 400,
}

_PARAMS = _NIFTY_PARAMS if INSTRUMENT == 'nifty' else _SENSEX_PARAMS

LOT_SIZE                = _PARAMS['lot_size']
STRIKE_INTERVAL         = _PARAMS['strike_interval']
EXPECTED_PREMIUM        = _PARAMS['expected_premium']
HEDGE_POINTS            = _PARAMS['hedge_points']
INDEX_SL_OFFSET         = _PARAMS['index_sl_offset']
ADJUSTMENT_DISTANCE     = _PARAMS['adjustment_distance']
MINIMUM_GAP             = _PARAMS['minimum_gap']
MINIMUM_GAP_ITERATOR    = _PARAMS['minimum_gap_iterator']

# ---------------------------------------------------------------------------
# VIX regime gate
# ---------------------------------------------------------------------------
VIX_THRESHOLD           = 16.0

# ---------------------------------------------------------------------------
# VIX band thresholds — used for VIX-conditional SL parameter selection
# ---------------------------------------------------------------------------
VIX_BAND_LT12           = 12.0    # VIX < this value  -> 'vix_lt12'
VIX_BAND_12_14          = 14.0    # 12 <= VIX < this  -> 'vix_12_14'
VIX_BAND_14_16          = 16.0    # 14 <= VIX < this  -> 'vix_14_16'
                                  # VIX >= 16         -> 'vix_gte16'

# ---------------------------------------------------------------------------
# Index stop loss — OPTIMISABLE per VIX band
# ---------------------------------------------------------------------------
# INDEX_SL_OFFSET: base/default offset, used to initialise INDEX_SL_OFFSETS.
#   Keep in sync with the dict values when updating the default.
# INDEX_SL_OFFSETS: per-VIX-band offset dict. The backtest uses this dict,
#   not the scalar. Edit individual band values to tune.
#   PE index SL: spot < pe_sell_strike - INDEX_SL_OFFSETS[band]
#   CE index SL: spot > ce_sell_strike + INDEX_SL_OFFSETS[band]
#
# All bands initialised to current single value (200 for Sensex, 50 for Nifty).
# To disable the index SL entirely: set ENABLE_INDEX_SL = False.
INDEX_SL_OFFSETS = {
    'vix_lt12':  INDEX_SL_OFFSET,
    'vix_12_14': INDEX_SL_OFFSET,
    'vix_14_16': INDEX_SL_OFFSET,
    'vix_gte16': INDEX_SL_OFFSET,
}

# ---------------------------------------------------------------------------
# Option SL DTE multipliers — OPTIMISABLE per VIX band
# ---------------------------------------------------------------------------
# SL_4_DTE ... SL_0_DTE: default multipliers, used to initialise SL_DTE_MULTIPLIERS.
#   Keep in sync with the dict values when updating defaults.
# SL_DTE_MULTIPLIERS: per-VIX-band, per-DTE multiplier dict. The backtest uses
#   this dict. Edit individual band/DTE values to tune.
#   option_sl fires when: sell_ltp >= sell_entry x SL_DTE_MULTIPLIERS[band][dte]
#   DTE clamped to [0, 4]. DTE 4 = entry day (Monday). DTE 0 = expiry day.
SL_4_DTE                = 2.66      # DTE >= 4  — OPTIMISABLE per VIX band
SL_3_DTE                = 2.33      # DTE == 3  — OPTIMISABLE per VIX band
SL_2_DTE                = 2.00      # DTE == 2  — OPTIMISABLE per VIX band
SL_1_DTE                = 1.66      # DTE == 1  — OPTIMISABLE per VIX band
SL_0_DTE                = 1.33      # DTE == 0  — OPTIMISABLE per VIX band

SL_DTE_MULTIPLIERS = {
    'vix_lt12': {
        4: SL_4_DTE,
        3: SL_3_DTE,
        2: SL_2_DTE,
        1: SL_1_DTE,
        0: SL_0_DTE,
    },
    'vix_12_14': {
        4: SL_4_DTE,
        3: SL_3_DTE,
        2: SL_2_DTE,
        1: SL_1_DTE,
        0: SL_0_DTE,
    },
    'vix_14_16': {
        4: SL_4_DTE,
        3: SL_3_DTE,
        2: SL_2_DTE,
        1: SL_1_DTE,
        0: SL_0_DTE,
    },
    'vix_gte16': {
        4: SL_4_DTE,
        3: SL_3_DTE,
        2: SL_2_DTE,
        1: SL_1_DTE,
        0: SL_0_DTE,
    },
}

# ---------------------------------------------------------------------------
# SL enable flags
# ---------------------------------------------------------------------------
ENABLE_INDEX_SL         = True
ENABLE_OPTION_SL        = True

# ---------------------------------------------------------------------------
# Other optimisable parameters — resolved above from instrument params
# ---------------------------------------------------------------------------
# EXPECTED_PREMIUM       — target sell option LTP at entry scan
# HEDGE_POINTS           — spread width (sell to buy distance)
# ADJUSTMENT_DISTANCE    — how far to roll sell strike on adjustment
# MINIMUM_GAP            — minimum distance from spot to sell strike
# MINIMUM_GAP_ITERATOR   — fallback gap when spot has moved past minimum_gap

# ---------------------------------------------------------------------------
# Execution model
# ---------------------------------------------------------------------------
EXPIRY_FALLBACK_PRICE   = 0.05

# ---------------------------------------------------------------------------
# Lot count
# ---------------------------------------------------------------------------
LOT_COUNT               = 2

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
ENABLE_TRADE_LOGS       = True