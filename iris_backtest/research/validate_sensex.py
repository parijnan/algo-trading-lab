"""
Validate TRIPLE_CONFIRM on Sensex 1-min data.

Prints all signals found in the Sensex dataset with entry price and
excursion metrics (MFE/MAE/close at 5, 15, 30 min). Highlights specific
dates of interest for Artemis session review.

Usage (from repo root):
    python iris_backtest/research/validate_sensex.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from utils import load_sensex_1min, compute_excursions
from signals.triple_confirm import detect, BAR_PERIOD_MINUTES

HIGHLIGHT_DATES = {'2026-06-01', '2026-06-02'}
HORIZONS        = [5, 15, 30]


def main():
    df = load_sensex_1min()
    span_years = (df.index[-1] - df.index[0]).days / 365.25
    print(f'Sensex 1-min: {df.index[0].date()} → {df.index[-1].date()}  '
          f'({span_years:.1f} years,  {len(df):,} bars)\n')

    print('Running TRIPLE_CONFIRM...', end=' ', flush=True)
    signals = detect(df)
    print(f'{len(signals)} fires  ({len(signals)/span_years:.1f}/year)\n')

    if signals.empty:
        print('No signals found.')
        return

    exc = compute_excursions(df, signals, BAR_PERIOD_MINUTES, horizons=HORIZONS)
    merged = signals.merge(exc[['signal_ts', 'entry_ts', 'entry_price',
                                 'mfe_5', 'mae_5', 'close_5',
                                 'mfe_15', 'mae_15', 'close_15',
                                 'mfe_30', 'mae_30', 'close_30']],
                           left_on='timestamp', right_on='signal_ts', how='left')

    hdr = (f"{'Date':<12} {'Signal':>6} {'Entry':>7} {'Dir':<8}"
           f"  {'MFE5':>6} {'C5':>6}  {'MFE15':>6} {'C15':>6}  {'MFE30':>6} {'C30':>6}")
    sep = '─' * len(hdr)

    print(hdr)
    print(sep)

    for _, row in merged.iterrows():
        ts       = row['timestamp']
        date_str = str(ts.date())
        flag     = ' ◄◄' if date_str in HIGHLIGHT_DATES else ''
        mark     = '★' if date_str in HIGHLIGHT_DATES else ' '

        def fmt(v):
            return f'{v:+6.1f}' if pd.notna(v) else '   n/a'

        print(f"{mark} {date_str:<10} {ts.strftime('%H:%M'):>6}  "
              f"{row['entry_price']:>7.0f} {row['direction']:<8}"
              f"  {fmt(row['mfe_5'])} {fmt(row['close_5'])}"
              f"  {fmt(row['mfe_15'])} {fmt(row['close_15'])}"
              f"  {fmt(row['mfe_30'])} {fmt(row['close_30'])}"
              f"{flag}")

    print(sep)

    # Summary stats
    valid = merged.dropna(subset=['close_15'])
    if not valid.empty:
        wr15  = (valid['close_15'] > 0).mean() * 100
        rr15  = valid['mfe_15'].mean() / valid['mae_15'].mean()
        med15 = valid['close_15'].median()
        print(f'\nSensex summary ({len(valid)} trades with full 15-min data):')
        print(f'  WR@15m: {wr15:.1f}%   RR@15m: {rr15:.2f}   Median close@15m: {med15:+.1f} pts')

    # Save excursions
    out = Path(__file__).parent.parent / 'data' / 'TRIPLE_CONFIRM_sensex_excursions.csv'
    exc.to_csv(out, index=False)
    print(f'\nSaved → {out.name}')


if __name__ == '__main__':
    main()
