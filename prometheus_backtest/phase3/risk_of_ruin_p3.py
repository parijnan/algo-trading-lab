"""
Prometheus - Phase 3, mult 2.0: Risk-of-Ruin Monte Carlo at 50-unit sizing
(README's "Risk of Ruin at 50-unit sizing" section, originally run 2026-09-08,
first checked in as a proper script 2026-09-09 -- previously ad-hoc/ephemeral,
rebuilt from the README's own method description each time it needed a
refresh, e.g. after TARGET1_PCT changed 2.0% -> 2.2%).

Method: bootstrap-resample the 381 real backtested trades from
bespoke_trade_summary.csv with replacement (each trade's total_pnl_rs is one
atomic outcome -- lot1+lot2 combined, appropriate for synthesizing new
orderings, not reconstructing the original timeline). Scale to 50-unit
sizing (linear x50 on each trade's 1-unit P&L). Capital base Rs 50,00,000
(50 units x MARGIN_PER_UNIT's Rs 1,00,000 -- the fully-buffered allocation
per unit, not the raw Rs 50,000 margin -- see prometheus_backtest/README.md's
"Why 40% drawdown is the ruin threshold" for the margin math). 20,000
simulated paths, each ~2 years long at the backtest's own observed trade
pace. Ruin = max drawdown > 40% at any point AND equity has not recovered
back to its pre-drawdown peak by the end of the 2-year horizon.

Usage: python risk_of_ruin_p3.py
"""

import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
MULT_2_0_DIR = os.path.join(HERE, 'data_sweep', 'mult_2.0')

UNITS = 50
CAPITAL_BASE = 50 * 100_000  # 50 units * MARGIN_PER_UNIT
N_PATHS = 20_000
RUIN_DD_PCT = -0.40
SEED = 20260909


def main():
    df = pd.read_csv(os.path.join(MULT_2_0_DIR, 'bespoke_trade_summary.csv'), parse_dates=['entry_ts'])
    pnl_1unit = df['total_pnl_rs'].to_numpy()
    n_trades = len(pnl_1unit)

    days_span = (df['entry_ts'].max() - df['entry_ts'].min()).days
    trades_per_year = n_trades / days_span * 365.25
    trades_per_path = int(round(2 * trades_per_year))
    print(f'n_trades={n_trades}  days_span={days_span}  trades_per_year={trades_per_year:.1f}  '
          f'trades_per_path={trades_per_path}')

    rng = np.random.default_rng(SEED)

    max_dd_pcts = np.empty(N_PATHS)
    breach_flags = np.zeros(N_PATHS, dtype=bool)
    recovered_flags = np.zeros(N_PATHS, dtype=bool)
    recovery_trades = np.full(N_PATHS, -1, dtype=int)
    ever_negative = np.zeros(N_PATHS, dtype=bool)
    terminal_equity = np.empty(N_PATHS)

    month_trades = trades_per_year / 12
    horizons = {'1mo': month_trades, '3mo': 3 * month_trades, '6mo': 6 * month_trades, '1yr': trades_per_year}

    for p in range(N_PATHS):
        sample = rng.choice(pnl_1unit, size=trades_per_path, replace=True) * UNITS
        equity = CAPITAL_BASE + np.cumsum(sample)
        peak = np.maximum.accumulate(np.concatenate(([CAPITAL_BASE], equity)))[1:]
        dd_pct = (equity - peak) / peak

        min_dd = dd_pct.min()
        max_dd_pcts[p] = min_dd
        terminal_equity[p] = equity[-1]
        ever_negative[p] = (equity.min() < 0)

        if min_dd <= RUIN_DD_PCT:
            breach_flags[p] = True
            trough_idx = int(np.argmin(dd_pct))
            pre_dd_peak = peak[trough_idx]
            recovery_idx = None
            for j in range(trough_idx, len(equity)):
                if equity[j] >= pre_dd_peak:
                    recovery_idx = j
                    break
            if recovery_idx is not None:
                recovered_flags[p] = True
                recovery_trades[p] = recovery_idx - trough_idx

    ruin_flags = breach_flags & ~recovered_flags
    p_ruin = ruin_flags.mean() * 100
    p_breach = breach_flags.mean() * 100
    n_breach = int(breach_flags.sum())

    breach_recovered = recovered_flags[breach_flags]
    breach_recovery_trades = recovery_trades[breach_flags]
    pct_within = {}
    for label, h in horizons.items():
        pct_within[label] = ((breach_recovered & (breach_recovery_trades <= h)).sum() / n_breach * 100
                              if n_breach else float('nan'))

    print()
    print(f'P(ruin) = {p_ruin:.2f}%  ({int(ruin_flags.sum())} of {N_PATHS} paths)')
    print(f'P(max drawdown > 40% at any point): {p_breach:.2f}% ({n_breach}/{N_PATHS} paths)')
    print(f'Of those {n_breach}, recovered within horizon:')
    for label in ['1mo', '3mo', '6mo', '1yr']:
        print(f'  within {label}: {pct_within[label]:.1f}%')
    print(f'  recovered at all (by path end): {breach_recovered.sum()}/{n_breach} = '
          f'{breach_recovered.mean()*100 if n_breach else float("nan"):.1f}%')
    print(f'P(equity ever negative): {ever_negative.mean()*100:.2f}%')
    print(f'Max drawdown distribution: p50={np.percentile(-max_dd_pcts,50)*100:.1f}%  '
          f'p90={np.percentile(-max_dd_pcts,90)*100:.1f}%  p95={np.percentile(-max_dd_pcts,95)*100:.1f}%  '
          f'p99={np.percentile(-max_dd_pcts,99)*100:.1f}%')
    print(f'Median terminal equity: Rs {np.median(terminal_equity):,.0f}  (from Rs {CAPITAL_BASE:,} base)')

    wins = pnl_1unit[pnl_1unit > 0]
    losses = pnl_1unit[pnl_1unit <= 0]
    print(f'\n1-unit stats used: win rate {len(wins)/n_trades*100:.1f}%  avg win Rs{wins.mean():,.0f}  '
          f'avg loss Rs{losses.mean():,.0f}')


if __name__ == '__main__':
    main()
