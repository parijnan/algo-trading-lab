"""
backtest_greek_exit.py — Athena with delta-based emergency hedge trigger.

Replaces the fixed-offset trigger (spot >= ce_sell_strike + 150) with a
CE sell leg delta threshold (delta >= 0.45).

Diagnostic finding (run.py, 2026-06-23):
  The offset trigger fires when CE sell delta is already 0.77 median (deep ITM,
  DTE 1-3 days). Threshold 0.45 fires much earlier — this tests early-warning
  insurance, not vol-awareness (VIX effect p=0.67, not significant).

Vol-aware thesis verdict: DOES NOT HOLD — trigger delta is ~constant across
VIX (mean 0.80 at low VIX vs 0.79 at high VIX). The early-warning thesis
(fire when CE approaches ITM, not when already deep ITM) is the surviving
hypothesis being tested here.

Gate: BOTH full-sample AND recent-period (2023+) must improve vs baseline.
If either degrades, close Branch 6.

Usage (from repo root):
    python athena_backtest/backtest_greek_exit.py
"""

import os
import sys
import logging
import pandas as pd

_DIR      = os.path.dirname(os.path.abspath(__file__))
_OUT_DIR  = os.path.join(_DIR, 'data_greek_exit')
_SUMMARY  = os.path.join(_OUT_DIR, 'trade_summary_greek_exit.csv')
_BASELINE = os.path.join(_DIR, 'data', 'trade_summary.csv')

sys.path.insert(0, _DIR)

# -- Patch configs BEFORE importing backtest --
import configs as _cfg
_cfg.EMERGENCY_TRIGGER_MODE    = 'delta'
_cfg.EMERGENCY_DELTA_THRESHOLD = 0.45
_cfg.TRADE_LOGS_DIR            = os.path.join(_OUT_DIR, 'trade_logs')
_cfg.TRADE_SUMMARY_FILE        = _SUMMARY

os.makedirs(_OUT_DIR, exist_ok=True)
os.makedirs(_cfg.TRADE_LOGS_DIR, exist_ok=True)

from backtest import (
    load_index_data, load_contracts,
    run_backtest, save_trade_summary,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

SPLIT_YEAR = 2023


def _summary_row(df):
    n = len(df)
    if n == 0:
        return dict(n=0, wins=0, wr=0.0, mean=0.0, total=0.0, avg_win=0.0, avg_los=0.0, rr=0.0)
    wins = int((df['total_pl_points'] > 0).sum())
    wr   = wins / n * 100
    mean = df['total_pl_points'].mean()
    tot  = df['total_pl_points'].sum()
    aw   = df.loc[df['total_pl_points'] > 0,  'total_pl_points'].mean() if wins > 0 else 0.0
    al   = df.loc[df['total_pl_points'] <= 0, 'total_pl_points'].mean() if (n - wins) > 0 else 0.0
    rr   = abs(aw / al) if al else 0.0
    return dict(n=n, wins=wins, wr=wr, mean=mean, total=tot, avg_win=aw, avg_los=al, rr=rr)


def print_comparison():
    sep = '─' * 104

    print()
    print('═' * 104)
    print(f'COMPARISON: Baseline (offset trigger) vs Delta-mode trigger '
          f'(delta ≥ {_cfg.EMERGENCY_DELTA_THRESHOLD})')
    print('═' * 104)

    if not os.path.exists(_BASELINE):
        print('[WARN] No baseline trade_summary.csv — comparison unavailable.')
        return
    if not os.path.exists(_SUMMARY):
        print('[WARN] Delta-mode summary not found.')
        return

    b = pd.read_csv(_BASELINE, parse_dates=['entry_time'])
    f = pd.read_csv(_SUMMARY,  parse_dates=['entry_time'])
    b['year'] = b['entry_time'].dt.year
    f['year'] = f['entry_time'].dt.year

    def row_str(label, br, fr):
        return (f'  {label:24s}  {br["n"]:>5d}   {br["total"]:>+8.1f}   {br["mean"]:>+7.1f}   '
                f'{br["wr"]:>6.1f}%   {br["rr"]:>5.2f}   '
                f'{fr["n"]:>5d}   {fr["total"]:>+8.1f}   {fr["mean"]:>+7.1f}   '
                f'{fr["wr"]:>6.1f}%   {fr["rr"]:>5.2f}')

    hdr = (f'  {"Period":24s}  {"N":>5s}   {"TotalPL":>8s}   {"MeanPL":>7s}   '
           f'{"WinRate":>7s}   {"R:R":>5s}   {"N":>5s}   {"TotalPL":>8s}   {"MeanPL":>7s}   '
           f'{"WinRate":>7s}   {"R:R":>5s}')
    shdr = (f'  {"":24s}  {"── baseline (offset) ──":>35s}      {"── delta mode ──":>33s}')

    print(hdr)
    print(shdr)
    print(sep)
    print(row_str('Full sample', _summary_row(b), _summary_row(f)))
    print()

    for label, yr_early in [
        (f'early (2020–{SPLIT_YEAR-1})', True),
        (f'recent ({SPLIT_YEAR}+)',       False),
    ]:
        bdf = b[b['year'] < SPLIT_YEAR] if yr_early else b[b['year'] >= SPLIT_YEAR]
        fdf = f[f['year'] < SPLIT_YEAR] if yr_early else f[f['year'] >= SPLIT_YEAR]
        print(row_str(label, _summary_row(bdf), _summary_row(fdf)))

    print(sep)

    # ---- Per-trade delta diff ----
    # Find trades where P&L changed between modes
    b_idx = b.set_index(b['entry_time'].dt.date)['total_pl_points']
    f_idx = f.set_index(f['entry_time'].dt.date)['total_pl_points']
    diff  = f_idx - b_idx
    changed = diff[diff.abs() > 0.01].sort_values()

    print()
    print(f'Trades where P&L changed: {len(changed)} / {len(b)}')
    if not changed.empty:
        b_sub = b.set_index(b['entry_time'].dt.date)
        print()
        print(f'  {"date":<12}  {"baseline":>9}  {"delta_mode":>10}  {"change":>8}  {"vix":>5}  '
              f'{"max_spot_vs_ce":>14}')
        for date, delta_pl in changed.items():
            brow = b_sub.loc[date] if date in b_sub.index else None
            if brow is None:
                continue
            base_pl  = float(brow['total_pl_points'])
            dlt_pl   = base_pl + delta_pl
            vix_val  = float(brow['entry_vix'])
            msvce    = float(brow['max_spot']) - float(brow['ce_sell_strike'])
            print(f'  {str(date):<12}  {base_pl:>+9.2f}  {dlt_pl:>+10.2f}  '
                  f'{delta_pl:>+8.2f}  {vix_val:>5.1f}  {msvce:>+14.0f}')

    # ---- Hedge activation counts ----
    n_base_hedged  = int((b['emer_strike'].notna()).sum())
    n_delta_hedged = int((f['emer_strike'].notna()).sum())
    print()
    print(f'Hedge activations: baseline={n_base_hedged}  delta_mode={n_delta_hedged}')
    print(f'  (Note: emer_entry=0 in summary for closed hedges — '
          f'emer_strike tracks activation)')

    # ---- Verdict ----
    bs  = _summary_row(b)
    fs  = _summary_row(f)
    b_r = _summary_row(b[b['year'] >= SPLIT_YEAR])
    f_r = _summary_row(f[f['year'] >= SPLIT_YEAR])

    print()
    print('VERDICT')
    print(sep)
    d_full = fs['mean'] - bs['mean']
    d_rec  = f_r['mean'] - b_r['mean']
    print(f'  Full-sample Δmean  : {bs["mean"]:+.1f} → {fs["mean"]:+.1f}  (Δ={d_full:+.1f} pts)')
    print(f'  Recent-period Δmean: {b_r["mean"]:+.1f} → {f_r["mean"]:+.1f}  '
          f'(Δ={d_rec:+.1f} pts, n={f_r["n"]})')
    print()

    if d_full > 0 and d_rec > 0:
        print('  [PASS] Delta trigger improves BOTH full-sample and recent period.')
        print('  Branch 6 remains open. Review trade-level changes before deploying.')
    elif d_full > 0 and d_rec <= 0:
        print('  [CAUTION] Delta trigger lifts full-sample but HURTS recent period.')
        print('  Signal is early-period driven. Branch 6 CLOSED — close condition triggered.')
    elif d_full <= 0 and d_rec > 0:
        print('  [CAUTION] Delta trigger improves recent period but not full-sample.')
        print('  Not sufficient. Branch 6 CLOSED — close condition triggered.')
    else:
        print('  [FAIL] Delta trigger does not improve either period.')
        print('  Branch 6 CLOSED — close condition triggered.')


if __name__ == '__main__':
    print("=== Athena Delta-Mode Emergency Trigger Backtest ===")
    print(f"Trigger: CE sell delta >= {_cfg.EMERGENCY_DELTA_THRESHOLD} (vs offset mode: spot >= ce_sell + 150)")
    print(f"Output: {_SUMMARY}")
    print()

    nifty_1m, vix_1m = load_index_data()

    holidays_path = os.path.join(_DIR, '..', 'data_pipeline', 'config', 'holidays.csv')
    holidays_df = pd.read_csv(holidays_path, parse_dates=['date'])
    holidays_df['date'] = pd.to_datetime(holidays_df['date']).dt.date

    contracts_df = load_contracts(holidays_df)

    all_trades = run_backtest(nifty_1m, vix_1m, contracts_df, holidays_df)
    save_trade_summary(all_trades)

    print_comparison()
