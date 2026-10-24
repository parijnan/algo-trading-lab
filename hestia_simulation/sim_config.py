"""
Hestia compounding simulation: the one place for the capital rule, margin formulas, charges and paths (the hestia-simulation skill and every script here read from this file).

The rule is the owner's dynamic-sizing rule (plans/hestia-dynamic-sizing.md): at every entry an engine's units are the account value, divided by the number of live engines, over the
engine's own margin per unit at the entry price, rounded DOWN and limited by the unit cap; below one unit the entry is skipped. The account value is the starting capital plus the net
profit of every trade that had already closed.
"""

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'hestia_backtest'))
import hb_config  # noqa: E402

OUT_DIR = os.path.join(REPO, 'hestia_simulation', 'outputs')              # gitignored, regenerable
ARTIFACT_URL_FILE = os.path.join(REPO, 'hestia_simulation', 'artifact_url.txt')
INTEGRATED_TRADES = os.path.join(hb_config.OUT_DIR, 'integrated_trades.csv')   # written by the hestia-backtest skill
COMPARISON_CSV = os.path.join(hb_config.OUT_DIR, 'comparison.csv')            # live versus backtest, the source of the slippage
JANUS_TRADES = hb_config.JANUS['trades_csv']                                # local run of janus_backtest/machine_run.py --source previous_session
TRACKER_TEMPLATE = os.path.join(REPO, 'hestia_performance', 'tracker_template.html')   # the Hestia Dashboard page: its CSS is the look of this page

START_DATE = '2023-04-03'      # all four engines assumed live from this day (Typhon's first backtest trade)
CAPITAL = 400000.0
DIVISOR = 4                    # live engines sharing the account
UNIT_CAP = 100

# Janus replaces Prometheus in the crude oil slot; one unit is 3 lots. Margin per unit = price x 10 / 3 x JANUS_MARGIN_MULT:
#   6 scales Prometheus's margin per lot (x4 for two lots) to three lots; 4 keeps the margin of a two-lot Prometheus unit.
JANUS_LOTS = 3
SCENARIOS = {'p': {'swap': False, 'janus_mult': 4.0}, 'j6': {'swap': True, 'janus_mult': 6.0}, 'j4': {'swap': True, 'janus_mult': 4.0}}

# Margin per unit (the engines' own formulas, engine_configs.py + levels.py) and the order value of one unit (price x contract size x lots per unit)
MARGIN_PER_UNIT = {
    'Prometheus': lambda p, jm=4.0: p * 10 / 3.0 * 4.0,
    'Selene': lambda p, jm=4.0: p * 1 / 8.0 * 4.0,
    'Helios': lambda p, jm=4.0: (p * 1 / 1.0 * (4 * 1380 / 14893)) * 20,
    'Typhon': lambda p, jm=4.0: (p * 250 / 1.0 * (4 * 14000 / (299.7 * 250))) * 2,
    'Janus': lambda p, jm=4.0: p * 10 / 3.0 * jm,
}
ORDER_VALUE = {
    'Prometheus': lambda p: p * 10 * 2,
    'Selene': lambda p: p * 1.0,
    'Helios': lambda p: p * 1 * 20,
    'Typhon': lambda p: p * 250 * 2,
    'Janus': lambda p: p * 10 * JANUS_LOTS,
}

# Charges per order, non-agricultural commodity futures, as brokers publish them (check against a contract note)
BROKERAGE = 20.0               # Angel One, per executed order
EXCHANGE_FEE = 0.000021        # MCX transaction charge, 0.0021% of the order value
SEBI_FEE = 0.000001            # Rs 10 per crore
STAMP_DUTY_BUY = 0.00002       # 0.002%, buy orders only
CTT_SELL = 0.0001              # commodity transaction tax 0.01%, sell orders only
GST = 0.18                     # on brokerage + exchange fee + SEBI fee
