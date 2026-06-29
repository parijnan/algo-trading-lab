"""
Entry point for the Leto integrated backtest.

Usage:
    python leto_backtest/run.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
import pandas as pd
from configs import (
    ARTEMIS_NIFTY_PATH, ARTEMIS_SENSEX_PATH,
    ARTEMIS_CONTRACTS_NIFTY_PATH, ARTEMIS_CONTRACTS_SENSEX_PATH,
    ATHENA_PATH, IRIS_PATH, VIX_PATH, OUTPUT_PATH,
)
from loader import load_artemis, load_athena, load_iris, load_artemis_entry_dates
from router import prepare_vix_df
from simulator import run
from analysis import run_analysis

logging.basicConfig(level=logging.INFO, format='%(levelname)s  %(message)s')
logger = logging.getLogger(__name__)


def main():
    logger.info('Loading data...')
    artemis_df = load_artemis(ARTEMIS_NIFTY_PATH, ARTEMIS_SENSEX_PATH)
    athena_df  = load_athena(ATHENA_PATH)
    iris_df    = load_iris(IRIS_PATH)
    vix_df     = prepare_vix_df(VIX_PATH)

    artemis_nifty_dates, artemis_sensex_dates = load_artemis_entry_dates(
        ARTEMIS_CONTRACTS_NIFTY_PATH, ARTEMIS_CONTRACTS_SENSEX_PATH
    )
    athena_entry_dates = set(athena_df['entry_date'])

    logger.info(
        'Loaded: artemis=%d  athena=%d  iris=%d  vix_days=%d',
        len(artemis_df), len(athena_df), len(iris_df),
        len(vix_df['_date'].unique()),
    )
    logger.info(
        'Entry day sets: artemis_nifty=%d  artemis_sensex=%d  athena=%d',
        len(artemis_nifty_dates), len(artemis_sensex_dates), len(athena_entry_dates),
    )

    logger.info('Running simulation...')
    log_df = run(
        vix_df, artemis_df, athena_df, iris_df,
        artemis_nifty_dates, artemis_sensex_dates, athena_entry_dates,
    )

    log_df.to_csv(OUTPUT_PATH, index=False)
    logger.info('Wrote %d rows to %s', len(log_df), OUTPUT_PATH)

    run_analysis(OUTPUT_PATH)


if __name__ == '__main__':
    main()
