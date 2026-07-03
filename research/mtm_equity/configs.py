"""
configs.py — paths, lot sizes, and stress-window date ranges for the
MTM equity-curve diagnostic (Step 0 of the Poseidon plan).

All paths are relative to REPO_ROOT so the module works from any cwd.
"""

import os

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

# ---------------------------------------------------------------------------
# Trade roster — the routed portfolio from leto_backtest
# ---------------------------------------------------------------------------
LETO_TRADE_LOG = os.path.join(REPO_ROOT, 'leto_backtest', 'data', 'leto_trade_log.csv')

# ---------------------------------------------------------------------------
# Per-strategy trade-log directories
# ---------------------------------------------------------------------------
ATHENA_LOGS_DIR   = os.path.join(REPO_ROOT, 'athena_backtest', 'data', 'trade_logs')
ARTEMIS_NIFTY_DIR = os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_logs_nifty')
ARTEMIS_SENEX_DIR = os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_logs_sensex')
IRIS_LOGS_DIR     = os.path.join(REPO_ROOT, 'iris_backtest', 'data', 'trade_logs')

# ---------------------------------------------------------------------------
# Lot sizes — from artemis_backtest/configs.py and athena conventions
# Nifty options: 65 (pre-2024 lot-size change; backtest uses 65 throughout)
# Sensex options: 20
# ---------------------------------------------------------------------------
LOT_SIZES = {
    'artemis_nifty':  65,
    'artemis_sensex': 20,
    'athena':         65,
    'iris':           65,
}

# ---------------------------------------------------------------------------
# Auto-calibration fallback threshold
# If |mtm_at_exit_bar| < this, or factor <= 0 (opposite signs), fall back to
# LOT_SIZE conversion + synthetic exit point.
# ---------------------------------------------------------------------------
EXIT_BAR_EPSILON = 0.01

# ---------------------------------------------------------------------------
# Stress windows — the proactive/crisis periods to replay
# Format: (label, start, end)
# ---------------------------------------------------------------------------
STRESS_WINDOWS = [
    ('2020 COVID proactive (Feb 24 – Mar 12)', '2020-02-24', '2020-03-12'),
    ('2020 COVID acute (Mar 13 – Apr 30)',    '2020-03-13', '2020-04-30'),
    ('2022 rate-hike spike (Jan – Jun)',      '2022-01-01', '2022-06-30'),
    ('2024 election vol (May – Jun)',         '2024-05-01', '2024-06-30'),
]

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'data')
PORTFOLIO_MTM_PARQUET = os.path.join(OUTPUT_DIR, 'portfolio_mtm_equity.parquet')
PER_TRADE_MTM_PARQUET = os.path.join(OUTPUT_DIR, 'per_trade_mtm.parquet')
SUMMARY_CSV           = os.path.join(OUTPUT_DIR, 'mtm_vs_realized_summary.csv')
