"""
run_sweep.py — test whether lowering ROUTING_VIX_HIGH (Iris's activation
floor) closes the 2020 COVID proactive-window MTM gap more cheaply than
building Poseidon.

Reuses leto_backtest's own loader/router/simulator unmodified; only
router.ROUTING_VIX_HIGH is monkeypatched per sweep value. One variable
changed per run.

Usage:
    python research/iris_threshold/run_sweep.py
"""

import os
import sys
import logging
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_configs import LETO_BACKTEST_DIR, OUTPUT_DIR, THRESHOLDS, COVID_WINDOW_START, COVID_WINDOW_END

sys.path.insert(0, LETO_BACKTEST_DIR)
import configs as leto_configs
from loader import load_artemis, load_athena, load_iris, load_artemis_entry_dates
import router
from router import prepare_vix_df
import simulator

logging.basicConfig(level=logging.INFO, format='%(levelname)s  %(message)s')
logger = logging.getLogger(__name__)


def realized_stats(log_df: pd.DataFrame) -> dict:
    trades = log_df[log_df['routing_outcome'] == 'entered'].copy()
    if trades.empty:
        return {'n_trades': 0, 'total_pl': 0.0, 'max_dd': 0.0, 'calmar': float('nan')}
    pl = trades['pl_rs'].astype(float)
    cumpl = pl.cumsum()
    max_dd = (cumpl - cumpl.cummax()).min()
    total_pl = pl.sum()
    calmar = total_pl / abs(max_dd) if max_dd != 0 else float('nan')
    return {
        'n_trades': len(trades),
        'total_pl': total_pl,
        'max_dd': max_dd,
        'calmar': calmar,
        'by_strategy': trades.groupby('strategy')['pl_rs'].sum().to_dict(),
    }


def window_stats(log_df: pd.DataFrame, start: str, end: str) -> dict:
    trades = log_df[log_df['routing_outcome'] == 'entered'].copy()
    trades['entry_date'] = pd.to_datetime(trades['entry_date']).dt.date
    w = trades[(trades['entry_date'] >= pd.Timestamp(start).date()) &
               (trades['entry_date'] <= pd.Timestamp(end).date())]
    return {
        'n_trades': len(w),
        'total_pl': w['pl_rs'].astype(float).sum() if len(w) else 0.0,
        'trades': w[['entry_date', 'strategy', 'pl_rs']].to_dict('records'),
    }


def main():
    logger.info('Loading data (once, shared across all sweep runs)...')
    artemis_df = load_artemis(leto_configs.ARTEMIS_NIFTY_PATH, leto_configs.ARTEMIS_SENSEX_PATH)
    athena_df  = load_athena(leto_configs.ATHENA_PATH)
    iris_df    = load_iris(leto_configs.IRIS_PATH)
    vix_df     = prepare_vix_df(leto_configs.VIX_PATH)
    artemis_nifty_dates, artemis_sensex_dates = load_artemis_entry_dates(
        leto_configs.ARTEMIS_CONTRACTS_NIFTY_PATH, leto_configs.ARTEMIS_CONTRACTS_SENSEX_PATH
    )
    athena_entry_dates = set(athena_df['entry_date'])

    baseline_threshold = router.ROUTING_VIX_HIGH
    results = []

    for threshold in THRESHOLDS:
        router.ROUTING_VIX_HIGH = threshold
        logger.info('--- ROUTING_VIX_HIGH = %.1f ---', threshold)

        log_df = simulator.run(
            vix_df, artemis_df, athena_df, iris_df,
            artemis_nifty_dates, artemis_sensex_dates, athena_entry_dates,
        )

        overall = realized_stats(log_df)
        covid_window = window_stats(log_df, COVID_WINDOW_START, COVID_WINDOW_END)

        logger.info(
            '  Overall: n=%d  total_pl=Rs%.0f  max_dd=Rs%.0f  calmar=%.2f  by_strategy=%s',
            overall['n_trades'], overall['total_pl'], overall['max_dd'], overall['calmar'],
            overall.get('by_strategy'),
        )
        logger.info(
            '  COVID proactive window (%s to %s): n=%d  total_pl=Rs%.0f',
            COVID_WINDOW_START, COVID_WINDOW_END, covid_window['n_trades'], covid_window['total_pl'],
        )
        for t in covid_window['trades']:
            logger.info('    %s  %-8s  Rs%.0f', t['entry_date'], t['strategy'], t['pl_rs'])

        results.append({
            'routing_vix_high': threshold,
            'n_trades': overall['n_trades'],
            'total_pl_rs': overall['total_pl'],
            'max_dd_rs': overall['max_dd'],
            'calmar': overall['calmar'],
            'covid_window_n_trades': covid_window['n_trades'],
            'covid_window_pl_rs': covid_window['total_pl'],
        })

    router.ROUTING_VIX_HIGH = baseline_threshold  # restore

    summary = pd.DataFrame(results)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, 'threshold_sweep_summary.csv')
    summary.to_csv(out_path, index=False)

    logger.info('')
    logger.info('=' * 70)
    logger.info('SWEEP SUMMARY (baseline = ROUTING_VIX_HIGH=25.0)')
    logger.info('=' * 70)
    logger.info('\n%s', summary.to_string(index=False))
    logger.info('Saved to %s', out_path)


if __name__ == '__main__':
    main()
