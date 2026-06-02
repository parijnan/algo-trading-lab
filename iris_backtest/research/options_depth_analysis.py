"""
Entry premium distribution and average Black-Scholes delta by ITM depth.

Uses ATM entry price to back-estimate IV per trade (Brenner-Subrahmanyam
approximation), then applies standard BS delta formula for each depth.

Usage (from repo root):
    python iris_backtest/research/options_depth_analysis.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from scipy.stats import norm
from configs import OUTPUT_DIR, STRIKE_STEP

RISK_FREE   = 0.065          # Indian repo rate
DEPTH_LABELS = ['ATM', 'ITM_50', 'ITM_100', 'ITM_150', 'ITM_200']
DEPTHS       = [0, 1, 2, 3, 4]

PERCENTILES  = [5, 25, 50, 75, 95]


def _estimate_iv(C: float, S: float, T: float) -> float:
    """
    Brenner-Subrahmanyam ATM approximation: C ≈ S × σ × √(T / 2π)
    Returns annualised IV. Clipped to [5%, 80%].
    """
    if T <= 0 or S <= 0 or C <= 0:
        return 0.15
    return float(np.clip(C * np.sqrt(2 * np.pi / T) / S, 0.05, 0.80))


def _bs_delta(S: float, K: float, T: float, r: float,
              sigma: float, right: str) -> float:
    if T <= 0 or sigma <= 0:
        return np.nan
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    return float(norm.cdf(d1) if right == 'ce' else norm.cdf(d1) - 1)


def main():
    sim_path = OUTPUT_DIR / 'options_sim_results.csv'
    if not sim_path.exists():
        print('Run run_options_sim.py first.')
        sys.exit(1)

    df = pd.read_csv(sim_path, parse_dates=['signal_ts'])
    df['expiry_date'] = pd.to_datetime(df['expiry'])

    # DTE in years
    df['T'] = (df['expiry_date'] - df['signal_ts']).dt.days / 252

    # Estimate IV from ATM entry price for each row
    atm_col   = 'ATM_entry'
    df['iv']  = df.apply(
        lambda r: _estimate_iv(r[atm_col], r['spot'], r['T'])
        if pd.notna(r[atm_col]) and pd.notna(r['spot']) else 0.15, axis=1)

    # ── Entry premium distribution ─────────────────────────────────────────
    print('Entry premium distribution (option points)\n')
    print(f'{"Depth":<10}  {"N":>5}  {"Mean":>7}',
          ''.join(f'  {"P"+str(p):>6}' for p in PERCENTILES))
    print('─' * 70)

    for label, depth in zip(DEPTH_LABELS, DEPTHS):
        col = f'{label}_entry'
        if col not in df.columns:
            continue
        s = df[col].dropna()
        pcts = [s.quantile(p / 100) for p in PERCENTILES]
        print(f'{label:<10}  {len(s):>5}  {s.mean():>7.1f}',
              ''.join(f'  {p:>6.1f}' for p in pcts))

    # ── Average delta by depth ─────────────────────────────────────────────
    print('\n\nBlack-Scholes delta by ITM depth\n')
    print(f'{"Depth":<10}  {"N":>5}  {"Mean Δ":>8}  {"Med Δ":>8}  '
          f'{"P5 Δ":>8}  {"P95 Δ":>8}  {"Avg IV":>8}')
    print('─' * 70)

    for label, depth in zip(DEPTH_LABELS, DEPTHS):
        entry_col = f'{label}_entry'
        if entry_col not in df.columns:
            continue

        valid = df[df[entry_col].notna() & df['spot'].notna() & df['T'].notna()].copy()

        deltas = []
        for _, row in valid.iterrows():
            S     = row['spot']
            right = 'ce' if row['direction'] == 'bullish' else 'pe'
            atm   = int(round(S / STRIKE_STEP) * STRIKE_STEP)
            sign  = -1 if right == 'ce' else +1
            K     = atm + sign * depth * STRIKE_STEP
            d     = _bs_delta(S, K, row['T'], RISK_FREE, row['iv'], right)
            deltas.append(abs(d))       # absolute delta regardless of direction

        if not deltas:
            continue
        arr = np.array(deltas)
        arr = arr[~np.isnan(arr)]
        avg_iv = valid['iv'].mean()

        print(f'{label:<10}  {len(arr):>5}  {arr.mean():>8.3f}  '
              f'{np.median(arr):>8.3f}  '
              f'{np.percentile(arr, 5):>8.3f}  '
              f'{np.percentile(arr, 95):>8.3f}  '
              f'{avg_iv:>7.1%}')


if __name__ == '__main__':
    main()
