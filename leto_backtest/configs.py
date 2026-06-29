import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ERA_SPLIT_DATE    = '2025-09-01'   # Artemis switches Nifty → Sensex; Nifty expiry moves to Tuesday
ROUTING_VIX_LOW   = 16.0           # below → Artemis; at or above → Athena
ROUTING_VIX_HIGH  = 25.0           # above → Iris
BACKTEST_START    = '2020-01-01'
VIX_SNAP_HOUR     = 10
VIX_SNAP_MINUTE   = 30
VIX_SNAP_TOL_MIN  = 5              # fallback window (minutes) if 10:30 candle missing

# Data sources
ARTEMIS_NIFTY_PATH   = os.path.join(BASE, 'artemis_backtest', 'data', 'trade_summary_nifty_rerun.csv')
ARTEMIS_SENSEX_PATH  = os.path.join(BASE, 'artemis_backtest', 'data', 'trade_summary_sensex_rerun.csv')
ARTEMIS_CONTRACTS_NIFTY_PATH   = os.path.join(BASE, 'artemis_backtest', 'data', 'contracts.csv')
ARTEMIS_CONTRACTS_SENSEX_PATH  = os.path.join(BASE, 'artemis_backtest', 'data', 'contracts_sensex.csv')
ATHENA_PATH          = os.path.join(BASE, 'athena_backtest', 'data', 'trade_summary_vix_all.csv')
IRIS_PATH            = os.path.join(BASE, 'iris_backtest', 'data', 'iris_backtest_summary.csv')
VIX_PATH             = os.path.join(BASE, 'data_pipeline', 'data', 'indices', 'india_vix.csv')

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'leto_trade_log.csv')
