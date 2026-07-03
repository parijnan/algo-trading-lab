import os

LETO_BACKTEST_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'leto_backtest',
)

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

# Thresholds to test against the production baseline (25.0).
# Single variable changed per run: ROUTING_VIX_HIGH only.
THRESHOLDS = [25.0, 22.0, 20.0, 18.0]

COVID_WINDOW_START = '2020-02-24'
COVID_WINDOW_END   = '2020-03-12'
