"""
Quick slippage analysis on near-ATM Nifty option files.

For each sampled file: compute (open[t+1] - close[t]) for all consecutive
1-min bars. This is the gap a market-order entry pays between the signal
bar's close and the next bar's open — the practical "slippage" in the sim.

Usage (from repo root):
    python iris_backtest/research/options_slippage.py
"""
import sys
import random
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from configs import OPTIONS_PATH

SAMPLE_SIZE = 25   # number of option files to sample
RANDOM_SEED = 42


def load_option(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=['datetime'])
    df = df.set_index('datetime').sort_index()
    df = df[(df['open'] > 0) & (df['close'] > 0)]
    return df


def analyse_file(path: Path) -> dict | None:
    df = load_option(path)
    if len(df) < 10:
        return None

    gaps = df['open'].shift(-1) - df['close']
    gaps = gaps.dropna()
    if gaps.empty:
        return None

    mid = df['close'].median()
    return {
        'file':        path.parent.name + '/' + path.name,
        'bars':        len(df),
        'mid_premium': round(mid, 2),
        'mean_gap':    round(gaps.mean(), 3),
        'median_gap':  round(gaps.median(), 3),
        'std_gap':     round(gaps.std(), 3),
        'p5_gap':      round(gaps.quantile(0.05), 3),
        'p95_gap':     round(gaps.quantile(0.95), 3),
        'pct_zero':    round((gaps == 0).mean() * 100, 1),
        'pct_within1': round((gaps.abs() <= 1.0).mean() * 100, 1),
    }


def main():
    expiry_dirs = sorted(OPTIONS_PATH.glob('20*'))
    # Focus on recent 3 years for relevance
    recent = [d for d in expiry_dirs if d.name >= '2022-01-01']

    random.seed(RANDOM_SEED)
    sampled_dirs = random.sample(recent, min(SAMPLE_SIZE, len(recent)))

    files = []
    for d in sorted(sampled_dirs):
        option_files = sorted(d.glob('*.csv'))
        if not option_files:
            continue
        # Pick the file whose premium (median close) sits in 30–400 pts
        # — i.e., near-ATM and liquid, not deep OTM/ITM
        for f in option_files[len(option_files)//3 : 2*len(option_files)//3]:
            try:
                df_sample = pd.read_csv(f, usecols=['close'])
                med = df_sample['close'].median()
                rows = len(df_sample)
                if 30 <= med <= 400 and rows >= 300:
                    files.append(f)
                    break
            except Exception:
                continue

    print(f'Analysing {len(files)} near-ATM option files (random sample, 2022–2026)\n')

    results = []
    for f in files:
        r = analyse_file(f)
        if r:
            results.append(r)

    if not results:
        print('No data.')
        return

    df = pd.DataFrame(results)

    print(f'{"File":<40} {"Bars":>5} {"Mid":>7} {"Mean":>7} {"Med":>7} '
          f'{"Std":>6} {"P5":>7} {"P95":>7} {"=0%":>5} {"≤1pt%":>6}')
    print('─' * 105)
    for _, r in df.iterrows():
        print(f'{r["file"]:<40} {r["bars"]:>5} {r["mid_premium"]:>7.1f} '
              f'{r["mean_gap"]:>7.3f} {r["median_gap"]:>7.3f} {r["std_gap"]:>6.3f} '
              f'{r["p5_gap"]:>7.3f} {r["p95_gap"]:>7.3f} '
              f'{r["pct_zero"]:>5.1f} {r["pct_within1"]:>6.1f}')

    print('─' * 105)
    print(f'\nAggregated across all {len(df)} files:')
    print(f'  Mean gap (close→next open):  {df["mean_gap"].mean():+.3f} pts')
    print(f'  Median gap:                  {df["median_gap"].median():+.3f} pts')
    print(f'  Avg std of gap:              {df["std_gap"].mean():.3f} pts')
    print(f'  % bars where gap = 0:        {df["pct_zero"].mean():.1f}%')
    print(f'  % bars where |gap| ≤ 1 pt:  {df["pct_within1"].mean():.1f}%')
    print(f'\nConclusion: entering at next-bar open introduces ~{abs(df["mean_gap"].mean()):.2f} pts '
          f'avg gap vs prior close.')


if __name__ == '__main__':
    main()
