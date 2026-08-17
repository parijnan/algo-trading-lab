"""
Entry point for the Kronos backtest.

Usage:
    python kronos_backtest/run.py --phase 0

Phases are defined in plans/kronos-monthly-premium.md §6. Only Phase 0
(validation) is implemented; the engine comes after its results are reviewed.
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
                        help='Backtest phase to run (only 0 is implemented)')
    parser.add_argument('--refresh', action='store_true',
                        help='Re-scan the Phase 0 feasibility cache (several minutes)')
    args = parser.parse_args()

    if args.phase == '0':
        import phase0
        logger.info('Kronos — Phase 0 validation')
        logger.info('=' * 72)
        return 1 if phase0.run(refresh=args.refresh) else 0

    logger.error(f"Phase {args.phase} is not implemented yet. "
                 f"See plans/kronos-monthly-premium.md §6.")
    return 2


if __name__ == '__main__':
    sys.exit(main())
