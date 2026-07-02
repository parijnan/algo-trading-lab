"""
Post-simulation analysis: P&L aggregation, drawdown, Calmar, year-by-year breakdown.
"""

import pandas as pd
import numpy as np


def run_analysis(log_path: str) -> None:
    df = pd.read_csv(log_path)
    trades = df[df['routing_outcome'] == 'entered'].copy()
    trades['entry_ts'] = pd.to_datetime(trades['entry_ts'])
    trades['exit_ts']  = pd.to_datetime(trades['exit_ts'])
    trades = trades.sort_values('entry_ts').reset_index(drop=True)
    trades['year']     = trades['entry_ts'].dt.year

    print('=' * 60)
    print('LETO INTEGRATED BACKTEST — RESULTS')
    print('=' * 60)

    _print_overall(trades)
    _print_by_strategy(trades)
    _print_by_year(trades)
    _print_routing_outcomes(df)
    _validate(trades, df)


def _print_overall(trades: pd.DataFrame) -> None:
    n = len(trades)
    if n == 0:
        print('No trades found.')
        return

    pl = trades['pl_rs'].astype(float)
    wins = (pl > 0).sum()
    win_rate = wins / n * 100
    expectancy = pl.mean()
    total_pl = pl.sum()

    # Running drawdown
    cumpl = pl.cumsum()
    running_max = cumpl.cummax()
    drawdown = cumpl - running_max
    max_dd = drawdown.min()
    calmar = total_pl / abs(max_dd) if max_dd != 0 else float('nan')

    print(f'\nTotal trades  : {n}')
    print(f'Win rate      : {win_rate:.1f}%  ({wins}W / {n-wins}L)')
    print(f'Expectancy    : ₹{expectancy:,.0f} per trade')
    print(f'Total P&L     : ₹{total_pl:,.0f}')
    print(f'Max drawdown  : ₹{max_dd:,.0f}')
    print(f'Calmar        : {calmar:.2f}')


def _print_by_strategy(trades: pd.DataFrame) -> None:
    print('\n── By strategy ─────────────────────────────────────────')
    for strat, grp in trades.groupby('strategy'):
        pl = grp['pl_rs'].astype(float)
        n  = len(grp)
        wins = (pl > 0).sum()
        print(f"  {strat:<14} trades={n:>4}  win%={wins/n*100:>5.1f}"
              f"  total=₹{pl.sum():>10,.0f}  avg=₹{pl.mean():>8,.0f}")


def _print_by_year(trades: pd.DataFrame) -> None:
    print('\n── Year-by-year ─────────────────────────────────────────')
    for year, grp in trades.groupby('year'):
        pl = grp['pl_rs'].astype(float)
        n  = len(grp)
        wins = (pl > 0).sum()
        print(f"  {year}  trades={n:>3}  win%={wins/n*100:>5.1f}"
              f"  total=₹{pl.sum():>10,.0f}")


def _print_routing_outcomes(df: pd.DataFrame) -> None:
    print('\n── Routing outcome breakdown ────────────────────────────')
    counts = df['routing_outcome'].value_counts()
    for outcome, n in counts.items():
        print(f"  {outcome:<30} {n:>5}")


def _validate(trades: pd.DataFrame, df: pd.DataFrame) -> None:
    print('\n── Validation checks ────────────────────────────────────')

    # 1. No overlapping trades
    if len(trades) > 1:
        t = trades.sort_values('entry_ts').reset_index(drop=True)
        t['prev_exit'] = pd.to_datetime(t['exit_ts'].shift(1))
        t['entry_dt']  = pd.to_datetime(t['entry_ts'])
        overlaps = t[t['entry_dt'] < t['prev_exit']]
        if overlaps.empty:
            print('  [PASS] No overlapping trades')
        else:
            print(f'  [FAIL] {len(overlaps)} overlapping trade pairs:')
            print(overlaps[['entry_ts', 'exit_ts', 'strategy']].to_string(index=False))

    # 2. Era boundaries
    era_split = pd.Timestamp('2025-09-01')
    artemis = trades[trades['strategy'] == 'artemis']
    nifty_after  = artemis[(artemis['instrument'] == 'nifty')  & (pd.to_datetime(artemis['entry_ts']) >= era_split)]
    sensex_before = artemis[(artemis['instrument'] == 'sensex') & (pd.to_datetime(artemis['entry_ts']) < era_split)]
    if nifty_after.empty and sensex_before.empty:
        print('  [PASS] Era boundaries correct')
    else:
        if not nifty_after.empty:
            print(f'  [FAIL] {len(nifty_after)} Artemis Nifty trades on/after ERA_SPLIT_DATE')
        if not sensex_before.empty:
            print(f'  [FAIL] {len(sensex_before)} Artemis Sensex trades before ERA_SPLIT_DATE')

    # 3. VIX consistency
    def _check_vix_range(strat, lo, hi, inclusive_lo, inclusive_hi):
        s = trades[trades['strategy'] == strat].dropna(subset=['vix_at_entry'])
        if s.empty:
            return
        v = s['vix_at_entry'].astype(float)
        bad_lo = (v < lo) if inclusive_lo else (v <= lo)
        bad_hi = (v > hi) if inclusive_hi else (v >= hi)
        bad = s[bad_lo | bad_hi]
        if bad.empty:
            print(f'  [PASS] {strat} VIX range consistent')
        else:
            print(f'  [FAIL] {strat} has {len(bad)} trades outside expected VIX range')

    _check_vix_range('artemis', 0, 16.0, False, False)   # vix < 16
    _check_vix_range('athena',  16.0, 25.0, True, True)  # 16 <= vix <= 25
    _check_vix_range('iris',    25.0, 999, False, False)  # vix > 25

    # 4. Strategy trade counts vs reference
    counts = trades['strategy'].value_counts()
    print(f'\n  Trade mix: {dict(counts)}')
    total_pl = trades['pl_rs'].astype(float).sum()
    ref_pl = 320000
    if abs(total_pl - ref_pl) / ref_pl < 0.30:
        print(f'  [PASS] Total P&L ₹{total_pl:,.0f} within 30% of reference ₹{ref_pl:,}')
    else:
        print(f'  [WARN] Total P&L ₹{total_pl:,.0f} deviates >30% from reference ₹{ref_pl:,}')
