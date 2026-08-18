"""
Entry point for the Kronos backtest.

Usage:
    python kronos_backtest/run.py --phase 0

Phases are defined in plans/kronos-monthly-premium.md §6. Phase 0 (validation)
and Phase 1 (naive baseline, the kill gate) are implemented.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description='Kronos backtest')
    parser.add_argument('--phase', default='0',
                        help="Backtest phase to run: 0 (validation), 1 (baseline), or 'signal' (regime signal annotation)")
    parser.add_argument('--refresh', action='store_true',
                        help='Re-scan the Phase 0 feasibility cache (several minutes)')
    parser.add_argument('--label', default=None,
                        help='Run label for tracked phases (1, sizing sweeps). '
                             'Required for any run whose output should be numbered and kept.')
    args = parser.parse_args()

    if args.phase == '0':
        import phase0
        logger.info('Kronos — Phase 0 validation')
        logger.info('=' * 72)
        return 1 if phase0.run(refresh=args.refresh) else 0

    if args.phase == 'signal':
        import regime_signal
        logger.info('Kronos — regime signal annotation (Decision E, plan \u00a710)')
        logger.info('=' * 72)
        regime_signal.run(refresh=args.refresh)
        return 0

    if args.phase == '1':
        import loader, engine, analysis, run_tracking
        if not args.label:
            parser.error("--label is required for --phase 1 (e.g. --label baseline_35dte)")
        run = run_tracking.RunContext.start(args.label, phase='phase1_static_condor')
        logger.info(f'Kronos — Phase 1 baseline  [run {run.slug}]')
        logger.info('=' * 72)
        holidays = loader.load_holidays()
        nifty_1m, vix_1m = loader.load_index_data()
        universe, _ = loader.load_monthly_universe(holidays)
        logger.info('')
        trades, skips, legs = engine.run_backtest(universe, holidays, nifty_1m, vix_1m, run=run)
        engine.save_trades(trades, skips, run=run)
        result = analysis.summarise(trades, skips, len(universe), run=run)
        logger.info('')
        logger.info(f'Run output: {run.dir}')
        return 0 if result.get('verdict') == 'PASS' else 1

    logger.error(f"Phase {args.phase} is not implemented yet. "
                 f"See plans/kronos-monthly-premium.md §6.")
    return 2


if __name__ == '__main__':
    sys.exit(main())
