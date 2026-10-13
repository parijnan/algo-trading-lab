"""
Session analysis (2026-10-13, user: "how trades fare in the mornings vs evenings ... entries, exits, profitability, volatility, range ... insights only, no strategy changes").
Research only: nothing here is read by Hestia or any engine. All parameters live here.

MCX trades continuously from 09:00 to 23:30 (23:55 in the US-DST-off months); the exchange's morning and evening sessions are not separated by a break, so the split is a clock one.
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'outputs')
for sub in ('selene_backtest', 'helios_backtest', 'typhon_backtest', os.path.join('prometheus_backtest', 'phase3_fyers'), ''):
    sys.path.insert(0, os.path.join(REPO_ROOT, sub))

LEDGER_CSV = os.path.join(REPO_ROOT, 'hestia_backtest', 'outputs', 'integrated_trades.csv')       # closed trades of the four engines at their live configs (parity backtests; Prometheus Fyers track)
PROMETHEUS_BESPOKE_CSV = os.path.join(REPO_ROOT, 'prometheus_backtest', 'phase3_fyers', 'data_sweep', 'mult_2.5', 'bespoke_trade_summary.csv')   # two lots, each with its own exit time and price

# clock buckets (hour of day, start inclusive, end exclusive). 'Day' is the exchange's morning session and the afternoon; 'Evening' is 17:00 to the close.
SESSIONS = {'Morning': (9, 13), 'Afternoon': (13, 17), 'Evening': (17, 24)}
TWO_WAY = {'Day': ('Morning', 'Afternoon'), 'Evening': ('Evening',)}
OVERNIGHT = 'Overnight gap'                 # the move between one day's last bar and the next day's first bar (and over weekends)
GAP_MIN_MINUTES = 60                        # a jump of at least this many minutes between consecutive bars is a gap, not a minute bar

SPLIT_DATE = '2026-01-01'                   # the before / after split the engine calibrations use
ENGINES = {                                 # name: (instrument, rupees per point for ONE live unit)
    'Prometheus': ('CRUDEOILM', 20), 'Selene': ('SILVERMIC', 1), 'Helios': ('GOLDPETAL', 20), 'Typhon': ('NATGASMINI', 500),
}
MIN_TRADES_REPORT = 30                      # a bucket with fewer trades is printed but flagged as thin
