"""
Liquidity check: volume and OI across ITM depths for Nifty options.

Samples ST_FAST signal dates and compares daily volume, OI, and active bars
at ATM, ITM-50, ITM-100, ITM-150, ITM-200 for both CE and PE.

Usage (from repo root):
    python iris_backtest/research/options_liquidity.py
"""
import sys
import random
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from datetime import date
from configs import OUTPUT_DIR, OPTIONS_PATH, STRIKE_STEP

DEPTHS       = [0, 1, 2, 3, 4]          # steps ITM
DEPTH_LABELS = ['ATM', 'ITM_50', 'ITM_100', 'ITM_150', 'ITM_200']
SAMPLE_SIZE  = 60                         # signal rows to sample
RANDOM_SEED  = 7


def _load_expiry_dates():
    return sorted(date.fromisoformat(d.name)
                  for d in OPTIONS_PATH.glob('20??-??-??'))


def _nearest_expiry(signal_date, expiry_dates, min_dte=2):
    for exp in expiry_dates:
        if (exp - signal_date).days >= min_dte:
            return exp
    return None


def _day_stats(expiry: date, strike: int, right: str,
               signal_date: date) -> dict:
    path = OPTIONS_PATH / expiry.isoformat() / f'{strike}{right}.csv'
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=['datetime'])
    df = df.set_index('datetime').sort_index()
    # Restrict to the signal date only
    day_df = df[df.index.date == signal_date]
    if day_df.empty:
        return None
    return {
        'volume':      day_df['volume'].sum(),
        'avg_oi':      day_df['open_interest'].mean(),
        'active_bars': int((day_df['volume'] > 0).sum()),
        'total_bars':  len(day_df),
    }


def main():
    exc_path = OUTPUT_DIR / 'options_sim_results.csv'
    if not exc_path.exists():
        print('Run run_options_sim.py first.')
        sys.exit(1)

    sim = pd.read_csv(exc_path, parse_dates=['signal_ts'])
    expiry_dates = _load_expiry_dates()

    random.seed(RANDOM_SEED)
    sample = sim.sample(min(SAMPLE_SIZE, len(sim)), random_state=RANDOM_SEED)

    # Accumulate stats per depth per right
    stats = {lbl: {'volume': [], 'avg_oi': [], 'active_bars': [], 'found': 0}
             for lbl in DEPTH_LABELS}

    for _, row in sample.iterrows():
        signal_date = row['signal_ts'].date()
        spot        = row['spot']
        direction   = row['direction']
        expiry      = _nearest_expiry(signal_date, expiry_dates)
        if expiry is None or pd.isna(spot):
            continue

        atm   = int(round(spot / STRIKE_STEP) * STRIKE_STEP)
        right = 'ce' if direction == 'bullish' else 'pe'
        sign  = -1 if right == 'ce' else +1

        for depth, label in zip(DEPTHS, DEPTH_LABELS):
            strike = atm + sign * depth * STRIKE_STEP
            s = _day_stats(expiry, strike, right, signal_date)
            if s:
                stats[label]['volume'].append(s['volume'])
                stats[label]['avg_oi'].append(s['avg_oi'])
                stats[label]['active_bars'].append(s['active_bars'])
                stats[label]['found'] += 1

    # Print results
    print(f'Nifty options liquidity by ITM depth  '
          f'(sample: {SAMPLE_SIZE} ST_FAST signal dates, CE+PE combined)\n')
    print(f'{"Depth":<10}  {"Files":>6}  {"AvgVol/day":>12}  '
          f'{"MedVol/day":>12}  {"AvgOI":>10}  {"ActiveBars":>12}  '
          f'{"VsATM%":>8}')
    print('─' * 80)

    atm_vol = None
    for label in DEPTH_LABELS:
        s    = stats[label]
        n    = s['found']
        if n == 0:
            print(f'{label:<10}  {"no data":>6}')
            continue
        avol = np.mean(s['volume'])
        mvol = np.median(s['volume'])
        aoi  = np.mean(s['avg_oi'])
        abars = np.mean(s['active_bars'])
        if atm_vol is None:
            atm_vol = avol
        pct = (avol / atm_vol * 100) if atm_vol else 0
        print(f'{label:<10}  {n:>6}  {avol:>12,.0f}  {mvol:>12,.0f}  '
              f'{aoi:>10,.0f}  {abars:>12.1f}  {pct:>7.1f}%')


if __name__ == '__main__':
    main()
