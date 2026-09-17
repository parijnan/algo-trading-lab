"""
prometheus_backtest/phase3_fyers/options_iv/configs_iv.py

Parameters for the historical ATM-straddle IV research track (2026-09-17).
See resample_utils.py and build_atm_schedule.py for the design rationale.
"""
import os

INSTRUMENT = 'CRUDEOILM'
GRANULARITY_MINUTES = 15   # matches ST_15, the strategy's own signal timeframe

# Risk-free rate for the Black-76-via-Merton mibian trick (see project
# research this session) -- reuses the exact convention already established
# elsewhere in this repo (athena_backtest/configs.py, kronos_backtest/
# greeks.py, research/greek_analysis/greek_engine.py all use this same
# fixed 5.0% rather than a live rate series), for consistency.
RISK_FREE_RATE_PCT = 5.0

# Confirmed 2026-09-17: as days_to_expiry -> 0, Black-76 IV blows up even for
# a tiny, genuine amount of extrinsic value (well-known instability of IV
# near expiry, not a computation bug) -- observed directly, every boundary
# with atm_iv > 250% had days_to_expiry < 0.2 (a few hours). Excluding the
# final trading day of each option's own life removes this cleanly (max_iv
# drops from 498.9% to 248.7%, only ~4% of boundaries dropped) without
# losing anything analytically relevant -- Prometheus itself never holds a
# position this close to an option's own expiry anyway (rollover happens 5
# trading days before the FUTURES expiry, which is itself only 2-4 days
# after the paired options expiry -- user's own confirmation, 2026-09-17).
MIN_DAYS_TO_EXPIRY = 1.0

# Historical window -- matches the bespoke mult-2.0 trade set this IV series
# is meant to correlate against (prometheus_backtest/phase3_fyers/
# data_sweep/mult_2.0/bespoke_trade_summary.csv, 2023-03-03 -> 2026-08-19).
HISTORY_START = '2023-01-01'   # a little before the trade data's own start, margin for the
                               # first contract's own pre-launch expiry-calendar lookup

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
EXPIRY_CALENDAR_FILE = os.path.join(DATA_DIR, 'expiry_calendar.csv')
STRIKE_LADDERS_DIR = os.path.join(DATA_DIR, 'strike_ladders')       # one JSON per options expiry cycle
ATM_SCHEDULE_FILE = os.path.join(DATA_DIR, 'atm_schedule.csv')      # boundary -> (options_expiry, strike)
OPTIONS_STAGING_DIR = os.path.join(DATA_DIR, 'options_1min')        # <expiry>/<strike><CE|PE>.csv
IV_SERIES_FILE = os.path.join(DATA_DIR, 'atm_iv_15min.csv')         # final output
