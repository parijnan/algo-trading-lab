"""
configs_p4.py — Phase 4 Unified Nifty Configuration

Specific to the Nifty Tuesday cycle research.
Includes toggles for Cross-Pollinated Hedging (Smart Parachutes).
"""

import os

# Strategy Identity
INSTRUMENT = 'nifty'

# Paths
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINE_DATA = os.path.join(REPO_ROOT, 'data_pipeline', 'data')
NIFTY_INDEX_FILE = os.path.join(PIPELINE_DATA, 'indices', 'nifty.csv')
VIX_INDEX_FILE = os.path.join(PIPELINE_DATA, 'indices', 'india_vix.csv')
NIFTY_OPTIONS_PATH = os.path.join(PIPELINE_DATA, 'nifty', 'options')
HOLIDAYS_FILE = os.path.join(REPO_ROOT, 'data_pipeline', 'config', 'holidays.csv')

# Output
PHASE4_DIR = os.path.dirname(os.path.abspath(__file__))
CONTRACTS_FILE = os.path.join(PHASE4_DIR, 'data', 'contracts_p4.csv')
TRADE_LOGS_DIR = os.path.join(PHASE4_DIR, 'data', 'trade_logs')
TRADE_SUMMARY_FILE = os.path.join(PHASE4_DIR, 'data', 'trade_summary_p4.csv')

# Nifty Parameters
LOT_SIZE = 75
STRIKE_INTERVAL = 100
EXPECTED_PREMIUM = 35 # Adjusted for 4-day theta capture
HEDGE_POINTS = 300
ADJUSTMENT_DISTANCE = 200
MINIMUM_GAP = 300
MINIMUM_GAP_ITERATOR = 100

# VIX Gates
VIX_THRESHOLD = 16.0 # Artemis only runs if VIX < 16

# Phase 4 Specific: Cross-Pollinated Hedging
ENABLE_WEEKEND_PARACHUTE = True # Borrowed from Athena
PARACHUTE_DISTANCE_PERCENT = 2.0 # 2% move triggers exit or hedge

# Stop Losses (Base Multipliers)
SL_DTE_MULTIPLIERS = {
    'vix_lt16': {
        4: 2.5, # Entry (Thu)
        3: 2.2, # Fri
        2: 1.8, # Mon (Weekend passed)
        1: 1.5, # Tue (Expiry Day)
        0: 1.2
    }
}

# Logging
ENABLE_TRADE_LOGS = True
