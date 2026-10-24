"""
Hestia integrated backtest: the one place for engine registry, paths and tolerances (the hestia-backtest skill and every script here read from this file).
Rupees per point are for ONE live unit: lots per unit times rupees per point per lot (Prometheus 2 x 10, Selene 1 x 1, Helios 20 x 1, Typhon 2 x 250).
"""

import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO, 'hestia_backtest', 'outputs')
MCX_DIR = os.path.join(REPO, 'data_pipeline', 'data', 'mcx')
FYERS_DIR = os.path.join(REPO, 'data_pipeline', 'data', 'mcx_fyers')
PERF_CONFIG = os.path.join(REPO, 'hestia_performance', 'tracker_config.json')   # live start trade ids, lot values
ARTIFACT_URL_FILE = os.path.join(REPO, 'hestia_backtest', 'artifact_url.txt')

# trades_csv: where each engine's backtest trade list lives; kind says how build_ledger reads it
ENGINES = {
    'Prometheus': {'key': 'prometheus', 'instrument': 'CRUDEOILM', 'rs_per_point_unit': 20, 'rs_per_summed_point': 10,   # its recorded points (live and backtest) are summed over the two lots, so Rs = points x 10
                   'backtest_csv': os.path.join(REPO, 'prometheus_backtest', 'phase3_fyers', 'data_sweep', 'parity', 'parity_trades.csv'), 'kind': 'prometheus'},
    'Selene': {'key': 'selene', 'instrument': 'SILVERMIC', 'rs_per_point_unit': 1,
               'backtest_csv': os.path.join(REPO, 'selene_backtest', 'data_sweep', 'parity_trades.csv'), 'kind': 'parity'},
    'Helios': {'key': 'helios', 'instrument': 'GOLDPETAL', 'rs_per_point_unit': 20,
               'backtest_csv': os.path.join(REPO, 'helios_backtest', 'data_sweep', 'parity_trades.csv'), 'kind': 'parity'},
    'Typhon': {'key': 'typhon', 'instrument': 'NATGASMINI', 'rs_per_point_unit': 500,
               'backtest_csv': os.path.join(REPO, 'typhon_backtest', 'data_sweep', 'parity_decided_trades.csv'), 'kind': 'parity'},
}

# Janus swap for Prometheus's crude oil slot on the integrated page: its state-machine run (janus_backtest/machine_run.py, source previous_session, defaults), one unit = 3 lots, Rs 10 a point a lot
JANUS = {'name': 'Janus', 'lots_per_unit': 3, 'rs_per_point_lot': 10,
         'trades_csv': os.path.join(REPO, 'janus_backtest', 'outputs', 'machine', 'CRUDEOILM_previous_session_rebuy-off_counter-off_tsl-off', 'trades.csv')}

COMPLETE_SESSION_BARS = 400          # a date counts as a complete session in a contract file with at least this many 1-minute bars
MATCH_TOLERANCE_MIN = 20             # a live and a backtest trade pair up if same direction and entry times within this many minutes (the signal bar is 15 minutes)
PRICE_TOLERANCE_PCT = 0.05           # entry prices within this percent of each other count as the same fill
