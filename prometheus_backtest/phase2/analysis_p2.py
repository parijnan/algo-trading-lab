"""
Prometheus - Phase 2 analysis: consolidated trade summary stats, adapted
from prometheus_backtest/analysis.py for the two-lot wide schema.
"""

import pandas as pd


def run_analysis(summary_path: str) -> None:
    df = pd.read_csv(summary_path)
    df['entry_ts'] = pd.to_datetime(df['entry_ts'])
    for col in ('lot1_exit_ts', 'lot2_exit_ts'):
        df[col] = pd.to_datetime(df[col])
    df['total_pnl_rs'] = df[['lot1_pnl_rs', 'lot2_pnl_rs']].sum(axis=1, min_count=1)
    df['exit_ts'] = df[['lot1_exit_ts', 'lot2_exit_ts']].max(axis=1)
    df['hold_min'] = (df['exit_ts'] - df['entry_ts']).dt.total_seconds() / 60
    df['month'] = df['entry_ts'].dt.to_period('M')

    print('=' * 60)
    print('PROMETHEUS PHASE 2 — RESULTS')
    print('=' * 60)

    _print_overall(df)
    _print_lot1_hit_rate(df)
    _print_by_month(df)
    _print_by_exit_reason(df, 'lot1_exit_reason', 'Lot 1')
    _print_by_exit_reason(df, 'lot2_exit_reason', 'Lot 2')
    _validate(df)


def _print_overall(df: pd.DataFrame) -> None:
    n = len(df)
    if n == 0:
        print('\nNo trades found.')
        return

    pl = df['total_pnl_rs'].astype(float)
    wins = (pl > 0).sum()
    cumpl = pl.cumsum()
    max_dd = (cumpl - cumpl.cummax()).min()
    total_pl = pl.sum()

    print(f'\nTotal trades  : {n}')
    print(f'Win rate      : {wins / n * 100:.1f}%  ({wins}W / {n - wins}L)')
    print(f'Expectancy    : ₹{pl.mean():,.0f} per trade')
    print(f'Total P&L     : ₹{total_pl:,.0f}')
    print(f'Max drawdown  : ₹{max_dd:,.0f}')
    print(f'Calmar        : {(total_pl / abs(max_dd)) if max_dd != 0 else float("nan"):.2f}')
    print(f'Avg hold      : {df["hold_min"].mean():.0f} min')


def _print_lot1_hit_rate(df: pd.DataFrame) -> None:
    n = len(df)
    if n == 0:
        return
    lot1_hit = (df['lot1_exit_reason'] == 'target_100').sum()
    lot2_pivot_hit = (df['lot2_exit_reason'] == 'target_pivot').sum()
    lot2_flat_hit = (df['lot2_exit_reason'].isin(['target_flat', 'target_flat_pct'])).sum()
    lot2_fallback = (df['lot2_exit_reason'] == 'target_100_no_pivot').sum()
    print('\n── Scale-out mechanics ─────────────────────────────')
    print(f'  Lot 1 hit target       : {lot1_hit}/{n}  ({lot1_hit/n*100:.1f}%)')
    if lot2_pivot_hit:
        print(f'  Lot 2 hit pivot target : {lot2_pivot_hit}/{n}  ({lot2_pivot_hit/n*100:.1f}%)')
    if lot2_flat_hit:
        print(f'  Lot 2 hit flat target  : {lot2_flat_hit}/{n}  ({lot2_flat_hit/n*100:.1f}%)')
    if lot2_fallback:
        print(f'  Lot 2 no-pivot fallback: {lot2_fallback}/{n}  ({lot2_fallback/n*100:.1f}%)')


def _print_by_month(df: pd.DataFrame) -> None:
    if df.empty:
        return
    print('\n── Month-by-month ──────────────────────────────────')
    for month, grp in df.groupby('month'):
        pl = grp['total_pnl_rs'].astype(float)
        n = len(grp)
        wins = (pl > 0).sum()
        print(f"  {month}  trades={n:>3}  win%={wins / n * 100:>5.1f}"
              f"  total=₹{pl.sum():>10,.0f}")


def _print_by_exit_reason(df: pd.DataFrame, col: str, label: str) -> None:
    if df.empty or col not in df:
        return
    pnl_col = 'lot1_pnl_rs' if col == 'lot1_exit_reason' else 'lot2_pnl_rs'
    print(f'\n── {label} exit reason ──────────────────────────────')
    for reason, grp in df.groupby(col):
        pl = grp[pnl_col].astype(float)
        n = len(grp)
        wins = (pl > 0).sum()
        print(f"  {reason:<24} trades={n:>4}  win%={wins / n * 100:>5.1f}"
              f"  total=₹{pl.sum():>10,.0f}  avg=₹{pl.mean():>8,.0f}")


def _validate(df: pd.DataFrame) -> None:
    print('\n── Validation checks ──────────────────────────────────')
    if df.empty:
        print('  No trades to validate.')
        return

    t = df.sort_values('entry_ts').reset_index(drop=True)
    t['prev_exit'] = t['exit_ts'].shift(1)
    overlaps = t[t['entry_ts'] < t['prev_exit']]
    if overlaps.empty:
        print('  [PASS] No overlapping trades')
    else:
        print(f'  [FAIL] {len(overlaps)} overlapping trade pairs')

    crosses_day = t[t['entry_ts'].dt.date != t['exit_ts'].dt.date]
    if crosses_day.empty:
        print('  [PASS] No trade carries across a calendar day')
    else:
        print(f'  [FAIL] {len(crosses_day)} trade(s) span more than one day')

    lot2_before_lot1 = t[t['lot2_exit_ts'] < t['lot1_exit_ts']]
    if lot2_before_lot1.empty:
        print('  [PASS] Lot 2 never exits before Lot 1')
    else:
        print(f'  [FAIL] {len(lot2_before_lot1)} trade(s) where lot 2 exited before lot 1')

    print(f'\n  Lot 1 exit reason mix: {dict(df["lot1_exit_reason"].value_counts())}')
    print(f'  Lot 2 exit reason mix: {dict(df["lot2_exit_reason"].value_counts())}')
