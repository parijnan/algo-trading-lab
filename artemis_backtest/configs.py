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
    'lot_size':             75,
    'strike_interval':      100,
    'expected_premium':     30,       # target sell option LTP at entry
    'hedge_points':         300,      # distance from sell to buy strike
    'pe_index_sl_offset':   50,       # OPTIMISABLE
    'ce_index_sl_offset':   50,       # OPTIMISABLE
    'adjustment_distance':  200,      # points to move sell strike on adjustment
    'minimum_gap':          350,      # minimum gap between spot and sell strike
    'minimum_gap_iterator': 100,      # fallback gap when resetting sell strike
}

_SENSEX_PARAMS = {
    'lot_size':             20,
    'strike_interval':      100,
    'expected_premium':     120,
    'hedge_points':         1000,
    'pe_index_sl_offset':   200,   # OPTIMISABLE
    'ce_index_sl_offset':   200,   # OPTIMISABLE —- analysis thread will tune
    'adjustment_distance':  600,
    'minimum_gap':          1000,
    'minimum_gap_iterator': 400,
}

_PARAMS = _NIFTY_PARAMS if INSTRUMENT == 'nifty' else _SENSEX_PARAMS

LOT_SIZE                = _PARAMS['lot_size']
STRIKE_INTERVAL         = _PARAMS['strike_interval']
EXPECTED_PREMIUM        = _PARAMS['expected_premium']
HEDGE_POINTS            = _PARAMS['hedge_points']
PE_INDEX_SL_OFFSET      = _PARAMS['pe_index_sl_offset']   # OPTIMISABLE
CE_INDEX_SL_OFFSET      = _PARAMS['ce_index_sl_offset']   # OPTIMISABLE
ADJUSTMENT_DISTANCE     = _PARAMS['adjustment_distance']
MINIMUM_GAP             = _PARAMS['minimum_gap']
MINIMUM_GAP_ITERATOR    = _PARAMS['minimum_gap_iterator']

# ---------------------------------------------------------------------------
# VIX regime gate
# ---------------------------------------------------------------------------
# Artemis runs when VIX < this value. Weeks where vix_open >= threshold
# are skipped and logged as 'skipped_vix'.
VIX_THRESHOLD           = 16.0

# ---------------------------------------------------------------------------
# Option stop loss multipliers — OPTIMISABLE
# ---------------------------------------------------------------------------
# option_sl fires when: sell_ltp >= sell_entry_price × multiplier
# DTE = np.busday_count(candle_date, expiry_date), clamped to [0, 4]
#
# PE and CE multipliers are independent sets. Both are currently initialised
# to the same values (2.66 / 2.33 / 2.00 / 1.66 / 1.33) to replicate the
# original shared-multiplier behaviour until tuned by the analysis thread.

# PE sell option SL multipliers — OPTIMISABLE
PE_SL_4_DTE             = 2.66      # DTE >= 4
PE_SL_3_DTE             = 2.33      # DTE == 3
PE_SL_2_DTE             = 2.00      # DTE == 2
PE_SL_1_DTE             = 1.66      # DTE == 1
PE_SL_0_DTE             = 1.33      # DTE == 0

# CE sell option SL multipliers — OPTIMISABLE
CE_SL_4_DTE             = 2.66      # DTE >= 4
CE_SL_3_DTE             = 2.33      # DTE == 3
CE_SL_2_DTE             = 2.00      # DTE == 2
CE_SL_1_DTE             = 1.66      # DTE == 1
CE_SL_0_DTE             = 1.33      # DTE == 0

# ---------------------------------------------------------------------------
# SL enable flags
# ---------------------------------------------------------------------------
# Set either to False to disable that SL mechanism entirely.
# Useful for baseline analysis — observing how option prices and spot
# behave across the week without any intervention.
ENABLE_INDEX_SL         = True
ENABLE_OPTION_SL        = True

# ---------------------------------------------------------------------------
# Index stop loss — OPTIMISABLE
# ---------------------------------------------------------------------------
# PE_INDEX_SL_OFFSET: distance in points inside the PE sell strike at which
#   the PE index SL fires.
#   Condition: spot < pe_sell_strike - PE_INDEX_SL_OFFSET
#
# CE_INDEX_SL_OFFSET: distance in points inside the CE sell strike at which
#   the CE index SL fires.
#   Condition: spot > ce_sell_strike + CE_INDEX_SL_OFFSET
#
# Both are independently optimisable per instrument.
# Currently set equal (200/200 for Sensex, 50/50 for Nifty) to replicate
# the original single-offset behaviour until tuned by the analysis thread.

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
# Entry:   open of the candle AFTER the 10:30 signal candle (i.e. 10:31 open)
# SL exit: open of the candle AFTER SL is detected on close
# ELM exit: open of 15:16 candle (elm_time is 15:15 close)
# Expiry:  close of last available candle at or before 15:30; 0.05 if missing

EXPIRY_FALLBACK_PRICE   = 0.05      # price assumed for missing expiry candles

# ---------------------------------------------------------------------------
# Lot count
# ---------------------------------------------------------------------------
# Minimum 2 — ensures additional_lots = lots // 2 = 1, which activates the
# additional lots logic on adjustments before cutoff_time.
# In the live code this is derived from available margin (lot_calc). Here it
# is fixed. P&L is normalised to per-base-lot in the summary so results are
# comparable across runs regardless of this value.
LOT_COUNT               = 2

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Set to True to write per-trade 1-min logs to TRADE_LOGS_DIR.
# Disable for faster optimisation runs.
ENABLE_TRADE_LOGS       = True