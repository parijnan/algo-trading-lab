"""
backtest_vix15.py — Athena with VIX_FILTER_LOW = 15.0

Tests Option C from plans/vix-grey-zone-routing.md:
  Lower Athena's VIX floor from 16 to 15, adding the 15–16 grey zone trades.

Output: athena_backtest/data_vix15/trade_summary_vix15.csv
        athena_backtest/data_vix15/trade_logs/  (suppressed — saves time)

Usage (from repo root):
    python athena_backtest/backtest_vix15.py
"""

import os
import sys
import logging
import pandas as pd

_DIR      = os.path.dirname(os.path.abspath(__file__))
_OUT_DIR  = os.path.join(_DIR, 'data_vix15')
_SUMMARY  = os.path.join(_OUT_DIR, 'trade_summary_vix15.csv')
_BASELINE = os.path.join(_DIR, 'data', 'trade_summary.csv')

sys.path.insert(0, _DIR)

# -- Patch configs BEFORE importing backtest --
import configs as _cfg
_cfg.VIX_FILTER_LOW      = 15.0
_cfg.TRADE_LOGS_DIR      = os.path.join(_OUT_DIR, 'trade_logs')
_cfg.TRADE_SUMMARY_FILE  = _SUMMARY

os.makedirs(_OUT_DIR, exist_ok=True)
os.makedirs(_cfg.TRADE_LOGS_DIR, exist_ok=True)

# Now import backtest — it will bind the patched values
from backtest import (
    load_index_data, load_contracts,
    run_backtest, save_trade_summary,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

if __name__ == '__main__':
    print("=== Athena VIX-15 Backtest ===")
    print(f"VIX range: 15.0 – 25.0  (baseline was 16.0 – 25.0)")
    print(f"Output: {_SUMMARY}")
    print()

    nifty_1m, vix_1m = load_index_data()

    holidays_path = os.path.join(
        _DIR, '..', 'data_pipeline', 'config', 'holidays.csv')
    holidays_df = pd.read_csv(holidays_path, parse_dates=['date'])
    holidays_df['date'] = pd.to_datetime(holidays_df['date']).dt.date

    contracts_df = load_contracts(holidays_df)

    all_trades = run_backtest(nifty_1m, vix_1m, contracts_df, holidays_df)
    save_trade_summary(all_trades)

    # -- Comparison vs baseline --
    if not all_trades:
        print("No trades generated.")
        sys.exit(0)

    df = pd.DataFrame(all_trades)
    total   = len(df)
    winners = df[df['total_pl_points'] > 0]
    losers  = df[df['total_pl_points'] <= 0]
    wr      = len(winners) / total * 100
    avg_win = winners['total_pl_points'].mean() if len(winners) else 0
    avg_los = losers['total_pl_points'].mean()  if len(losers)  else 0
    rr      = abs(avg_win / avg_los) if avg_los else 0
    total_rs = df['total_pl_rupees'].sum()

    print()
    print("=" * 56)
    print("COMPARISON")
    print("=" * 56)
    print(f"{'':20}  {'Baseline (VIX≥16)':>18}  {'VIX≥15':>10}")
    print(f"{'─'*56}")

    if os.path.exists(_BASELINE):
        base = pd.read_csv(_BASELINE)
        bn   = len(base)
        bw   = len(base[base['total_pl_points'] > 0])
        bwr  = bw / bn * 100
        baw  = base[base['total_pl_points'] > 0]['total_pl_points'].mean()
        bal  = base[base['total_pl_points'] <= 0]['total_pl_points'].mean()
        brr  = abs(baw / bal) if bal else 0
        brs  = base['total_pl_rupees'].sum()

        print(f"{'Trades':20}  {bn:>18}  {total:>10}")
        print(f"{'Winners':20}  {bw:>17}  {len(winners):>10}")
        print(f"{'Win Rate':20}  {bwr:>17.1f}%  {wr:>9.1f}%")
        print(f"{'R:R':20}  {brr:>18.2f}  {rr:>10.2f}")
        print(f"{'P&L (₹)':20}  {brs:>+18,.0f}  {total_rs:>+10,.0f}")
        print(f"{'Delta vs baseline':20}  {'—':>18}  {total_rs-brs:>+10,.0f}")
    else:
        print(f"Trades:   {total}")
        print(f"Win Rate: {wr:.1f}%")
        print(f"R:R:      {rr:.2f}")
        print(f"P&L:      ₹{total_rs:+,.0f}")

    # Show the new trades (15–16 VIX)
    grey_new = df[(df['entry_vix'] >= 15.0) & (df['entry_vix'] < 16.0)]
    if not grey_new.empty:
        gw  = (grey_new['total_pl_points'] > 0).mean() * 100
        grs = grey_new['total_pl_rupees'].sum()
        print()
        print(f"New grey-zone trades (VIX 15–16): {len(grey_new)}")
        print(f"  Win rate : {gw:.1f}%")
        print(f"  P&L      : ₹{grs:+,.0f}")
