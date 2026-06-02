"""
Run all signal detectors against the full Nifty 1-min dataset and compute
MFE/MAE/close excursions at each horizon. Output: one CSV per signal in data/.

Usage (from repo root):
    python iris_backtest/research/run_all.py [--signal ST_FAST]

--signal: run only the named signal (optional; omit to run all)
"""
import sys
import argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import load_nifty_1min, compute_excursions
from configs import OUTPUT_DIR
from signals import ALL_SIGNALS


def run_signal(sig_mod, df_1min):
    name       = sig_mod.SIGNAL_NAME
    bar_period = sig_mod.BAR_PERIOD_MINUTES
    print(f'[{name}] detecting...', end=' ', flush=True)
    signals = sig_mod.detect(df_1min)
    print(f'{len(signals)} fires', end='  |  ', flush=True)

    if signals.empty:
        print('no output written')
        return

    print('computing excursions...', end=' ', flush=True)
    excursions = compute_excursions(df_1min, signals, bar_period)
    out_path   = OUTPUT_DIR / f'{name}_excursions.csv'
    excursions.to_csv(out_path, index=False)
    print(f'saved → {out_path.name}  ({len(excursions)} rows)')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--signal', type=str, default=None,
                        help='Run only this signal (e.g. ST_FAST). Omit for all.')
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)
    df_1min = load_nifty_1min()
    print(f'Loaded {len(df_1min):,} 1-min bars  '
          f'({df_1min.index[0].date()} → {df_1min.index[-1].date()})\n')

    targets = ALL_SIGNALS
    if args.signal:
        targets = [m for m in ALL_SIGNALS if m.SIGNAL_NAME == args.signal.upper()]
        if not targets:
            print(f'Unknown signal: {args.signal}')
            print(f'Available: {[m.SIGNAL_NAME for m in ALL_SIGNALS]}')
            sys.exit(1)

    for sig_mod in targets:
        run_signal(sig_mod, df_1min)


if __name__ == '__main__':
    main()
