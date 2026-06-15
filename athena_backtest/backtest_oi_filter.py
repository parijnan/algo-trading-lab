"""
backtest_oi_filter.py — Athena with PCR-based OI entry filter.

Skips entries where pcr_near (near-expiry put-call ratio) at entry time is below
OI_FILTER_PCR_MIN. Threshold = 0.71 = 20th-percentile pcr_near across 124 Athena
VIX-16-25 trades, derived from validate_athena_entry.py §6.

Full-sample result: baseline n=124 mean=+22.3 → filtered n=101 mean=+25.4 (+3.1 pts).
See comparison output for period breakdown and honest verdict.

Usage (from repo root):
    python athena_backtest/backtest_oi_filter.py

Requires: research/oi_analysis/data/nifty_oi_features.csv
    Build if missing: python research/oi_analysis/build_nifty_features.py --workers 4
"""

import os
import sys
import logging
import pandas as pd

_DIR      = os.path.dirname(os.path.abspath(__file__))
_OUT_DIR  = os.path.join(_DIR, 'data_oi_filter')
_SUMMARY  = os.path.join(_OUT_DIR, 'trade_summary_oi_filter.csv')
_BASELINE = os.path.join(_DIR, 'data', 'trade_summary.csv')

sys.path.insert(0, _DIR)

# -- Patch configs BEFORE importing backtest --
import configs as _cfg
_cfg.ENABLE_OI_FILTER    = True
_cfg.OI_FILTER_PCR_MIN   = 0.71
_cfg.TRADE_LOGS_DIR      = os.path.join(_OUT_DIR, 'trade_logs')
_cfg.TRADE_SUMMARY_FILE  = _SUMMARY

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
    sep = '─' * 100

    print()
    print('═' * 100)
    print(f'COMPARISON: Baseline vs OI-filtered  (pcr_near ≥ {_cfg.OI_FILTER_PCR_MIN})')
    print('═' * 100)

    if not os.path.exists(_BASELINE):
        print('[WARN] No baseline trade_summary.csv — comparison unavailable.')
        return
    if not os.path.exists(_SUMMARY):
        print('[WARN] Filtered summary not found.')
        return

    b = pd.read_csv(_BASELINE, parse_dates=['entry_time'])
    f = pd.read_csv(_SUMMARY,  parse_dates=['entry_time'])
    b['year'] = b['entry_time'].dt.year
    f['year'] = f['entry_time'].dt.year

    # Ground-truth removed set: trades in baseline but absent in filtered run
    b_dates  = set(b['entry_time'].dt.date)
    f_dates  = set(f['entry_time'].dt.date)
    removed  = b[b['entry_time'].dt.date.isin(b_dates - f_dates)].copy()

    def row_str(label, br, fr):
        return (f'  {label:24s}  {br["n"]:>5d}   {br["total"]:>+8.1f}   {br["mean"]:>+7.1f}   '
                f'{br["wr"]:>6.1f}%   {br["rr"]:>5.2f}   '
                f'{fr["n"]:>5d}   {fr["total"]:>+8.1f}   {fr["mean"]:>+7.1f}   '
                f'{fr["wr"]:>6.1f}%   {fr["rr"]:>5.2f}')

    sep = '─' * 100
    hdr = (f'  {"Period":24s}  {"N":>5s}   {"TotalPL":>8s}   {"MeanPL":>7s}   '
           f'{"WinRate":>7s}   {"R:R":>5s}   {"N":>5s}   {"TotalPL":>8s}   {"MeanPL":>7s}   '
           f'{"WinRate":>7s}   {"R:R":>5s}')
    shdr = (f'  {"":24s}  {"── baseline ──":>35s}      {"── filtered ──":>33s}')

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

    # ---- Removed trades ----
    removed = removed.sort_values('total_pl_points')
    removed['period'] = removed['year'].apply(
        lambda y: f'recent({SPLIT_YEAR}+)' if y >= SPLIT_YEAR else f'early(<{SPLIT_YEAR})')
    n_rem = len(removed)

    print()
    print(f'Removed trades: {n_rem}  ({100*n_rem/len(b):.0f}% of baseline universe)')
    print()
    cols = ['entry_time', 'period', 'total_pl_points', 'ce_pl_points', 'pe_pl_points']
    print(removed[cols].to_string(index=False))
    print()

    for yr_early, label in [(True, f'early (<{SPLIT_YEAR})'), (False, f'recent ({SPLIT_YEAR}+)')]:
        sub = removed[removed['year'] < SPLIT_YEAR] if yr_early \
              else removed[removed['year'] >= SPLIT_YEAR]
        if not sub.empty:
            nw = (sub['total_pl_points'] > 0).sum()
            nl = (sub['total_pl_points'] <= 0).sum()
            print(f'  {label:20s}: n={len(sub):2d}  mean={sub["total_pl_points"].mean():+.1f}  '
                  f'sum={sub["total_pl_points"].sum():+.1f}  ({nw}W/{nl}L)')

    # ---- Verdict ----
    bs = _summary_row(b)
    fs = _summary_row(f)
    b_rec = b[b['year'] >= SPLIT_YEAR]
    f_rec = f[f['year'] >= SPLIT_YEAR]
    rec_b = _summary_row(b_rec)
    rec_f = _summary_row(f_rec)
    n_rem_recent = (removed['year'] >= SPLIT_YEAR).sum()
    rec_removed  = removed[removed['year'] >= SPLIT_YEAR]

    print()
    print('VERDICT')
    print(sep)
    d_full = fs['mean'] - bs['mean']
    d_rec  = rec_f['mean'] - rec_b['mean']
    print(f'  Trades removed     : {n_rem} total ({(removed["year"] < SPLIT_YEAR).sum()} early, '
          f'{n_rem_recent} recent)')
    print(f'  Full-sample Δmean  : {bs["mean"]:+.1f} → {fs["mean"]:+.1f}  (Δ={d_full:+.1f} pts)')
    print(f'  Recent-period Δmean: {rec_b["mean"]:+.1f} → {rec_f["mean"]:+.1f}  '
          f'(Δ={d_rec:+.1f} pts, n_kept={rec_f["n"]}, n_removed={n_rem_recent})')
    print()

    if d_full > 0 and d_rec < 0:
        print('  [CAUTION] Filter lifts full-sample but HURTS recent period.')
        print('  Signal is in-sample (2020-22 driven). Not recommended for live deployment.')
    elif d_full > 0 and d_rec >= 0:
        has_big_winner_removed = (not rec_removed.empty and
                                  rec_removed['total_pl_points'].max() > 50)
        if n_rem_recent >= 3 and has_big_winner_removed:
            print('  [WEAK-PASS] Both periods improve but recent-period removes a large winner.')
            print(f'  Δ={d_rec:+.1f} pts in recent period is fragile (n={rec_f["n"]}).')
            print('  pcr_near IC in recent period = 0.143 (weak). Defer deployment.')
        else:
            print('  [PASS] Filter improves both full-sample and recent-period mean P&L.')
            print('  Review n before deploying.')
    else:
        print('  [FAIL] Filter does not improve full-sample mean P&L. No deployment case.')


if __name__ == '__main__':
    print("=== Athena OI Entry Filter Backtest ===")
    print(f"Filter: pcr_near < {_cfg.OI_FILTER_PCR_MIN} → skip entry")
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
