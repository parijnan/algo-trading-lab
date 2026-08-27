"""
Post-simulation analysis: P&L aggregation, drawdown, Calmar, month-by-month
and exit-reason breakdowns, plus sanity/validation checks. Pattern mirrors
leto_backtest/analysis.py.
"""

import pandas as pd


def run_analysis(log_path: str) -> None:
    df = pd.read_csv(log_path)
    trades = df[df['exit_ts'].notna()].copy()
    trades['entry_ts'] = pd.to_datetime(trades['entry_ts'])
    trades['exit_ts']  = pd.to_datetime(trades['exit_ts'])
    trades = trades.sort_values('entry_ts').reset_index(drop=True)
    trades['month'] = trades['entry_ts'].dt.to_period('M')

    print('=' * 60)
    print('PROMETHEUS BACKTEST — RESULTS')
    print('=' * 60)

    open_trades = len(df) - len(trades)
    if open_trades:
        print(f'\n[NOTE] {open_trades} trade(s) never closed (still open at data end) — excluded below.')

    _print_overall(trades)
    _print_by_month(trades)
    _print_by_exit_reason(trades)
    _validate(trades)


def _print_overall(trades: pd.DataFrame) -> None:
    n = len(trades)
    if n == 0:
        print('\nNo closed trades found.')
        return

    pl = trades['pnl_rs'].astype(float)
    wins = (pl > 0).sum()
    win_rate = wins / n * 100
    expectancy = pl.mean()
    total_pl = pl.sum()

    cumpl = pl.cumsum()
    running_max = cumpl.cummax()
    drawdown = cumpl - running_max
    max_dd = drawdown.min()
    calmar = total_pl / abs(max_dd) if max_dd != 0 else float('nan')

    print(f'\nTotal trades  : {n}')
    print(f'Win rate      : {win_rate:.1f}%  ({wins}W / {n - wins}L)')
    print(f'Expectancy    : ₹{expectancy:,.0f} per trade')
    print(f'Total P&L     : ₹{total_pl:,.0f}')
    print(f'Max drawdown  : ₹{max_dd:,.0f}')
    print(f'Calmar        : {calmar:.2f}')
    print(f'Avg hold      : {trades["hold_min"].mean():.0f} min')


def _print_by_month(trades: pd.DataFrame) -> None:
    if trades.empty:
        return
    print('\n── Month-by-month ──────────────────────────────────')
    for month, grp in trades.groupby('month'):
        pl = grp['pnl_rs'].astype(float)
        n  = len(grp)
        wins = (pl > 0).sum()
        print(f"  {month}  trades={n:>3}  win%={wins / n * 100:>5.1f}"
              f"  total=₹{pl.sum():>10,.0f}")


def _print_by_exit_reason(trades: pd.DataFrame) -> None:
    if trades.empty:
        return
    print('\n── By exit reason ──────────────────────────────────')
    for reason, grp in trades.groupby('exit_reason'):
        pl = grp['pnl_rs'].astype(float)
        n  = len(grp)
        wins = (pl > 0).sum()
        print(f"  {reason:<24} trades={n:>4}  win%={wins / n * 100:>5.1f}"
              f"  total=₹{pl.sum():>10,.0f}  avg=₹{pl.mean():>8,.0f}")


def _validate(trades: pd.DataFrame) -> None:
    print('\n── Validation checks ──────────────────────────────────')
    if trades.empty:
        print('  No closed trades to validate.')
        return

    # 1. No overlapping trades — the state machine should make this
    #    structurally impossible, but confirm empirically.
    t = trades.sort_values('entry_ts').reset_index(drop=True)
    t['prev_exit'] = t['exit_ts'].shift(1)
    overlaps = t[t['entry_ts'] < t['prev_exit']]
    if overlaps.empty:
        print('  [PASS] No overlapping trades')
    else:
        print(f'  [FAIL] {len(overlaps)} overlapping trade pairs')

    # 2. No trade crosses a calendar day (pure intraday requirement)
    crosses_day = t[t['entry_ts'].dt.date != t['exit_ts'].dt.date]
    if crosses_day.empty:
        print('  [PASS] No trade carries across a calendar day')
    else:
        print(f'  [FAIL] {len(crosses_day)} trade(s) span more than one day')

    # 3. Entry/exit timestamps fall within a plausible session window
    out_of_session = t[(t['entry_ts'].dt.time < pd.Timestamp('09:00').time())
                        | (t['exit_ts'].dt.time > pd.Timestamp('23:55').time())]
    if out_of_session.empty:
        print('  [PASS] All entries/exits within session hours')
    else:
        print(f'  [FAIL] {len(out_of_session)} trade(s) outside session hours')

    print(f'\n  Exit reason mix: {dict(trades["exit_reason"].value_counts())}')
