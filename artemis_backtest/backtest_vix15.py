"""
backtest_vix15.py — Artemis with VIX_THRESHOLD = 15.0

Tests Option C from plans/vix-grey-zone-routing.md:
  Lower Artemis's VIX ceiling from 16 to 15, removing the 15–16 grey zone trades.

Runs both instruments sequentially:
  Nifty  : 2019-12-01 → 2025-08-25   (matches baseline)
  Sensex : 2025-09-01 → present       (matches baseline)

Output:
  artemis_backtest/data_vix15/trade_summary_nifty_vix15.csv
  artemis_backtest/data_vix15/trade_summary_sensex_vix15.csv

Usage (from repo root):
    python artemis_backtest/backtest_vix15.py
"""

import os
import sys
import importlib
import logging
import pandas as pd

_DIR      = os.path.dirname(os.path.abspath(__file__))
_OUT_DIR  = os.path.join(_DIR, 'data_vix15')
os.makedirs(_OUT_DIR, exist_ok=True)

sys.path.insert(0, _DIR)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

_BASELINES = {
    'nifty':  os.path.join(_DIR, 'data', 'trade_summary_nifty_rerun.csv'),
    'sensex': os.path.join(_DIR, 'data', 'trade_summary_sensex_rerun.csv'),
}

_RUNS = [
    {
        'instrument':  'nifty',
        'start':       '2019-12-01',
        'end':         '2025-08-25',
        'output':      os.path.join(_OUT_DIR, 'trade_summary_nifty_vix15.csv'),
        'logs_dir':    os.path.join(_OUT_DIR, 'trade_logs_nifty'),
    },
    {
        'instrument':  'sensex',
        'start':       '2025-09-01',
        'end':         None,
        'output':      os.path.join(_OUT_DIR, 'trade_summary_sensex_vix15.csv'),
        'logs_dir':    os.path.join(_OUT_DIR, 'trade_logs_sensex'),
    },
]


def run_one(run: dict):
    instrument = run['instrument']
    print(f"\n--- Artemis {instrument.upper()} (VIX < 15.0) ---")
    print(f"    Period : {run['start']} → {run['end'] or 'present'}")
    print(f"    Output : {run['output']}")

    # Patch configs before (re)importing backtest
    import configs as cfg
    cfg.INSTRUMENT          = instrument
    cfg.VIX_THRESHOLD       = 15.0
    cfg.BACKTEST_START_DATE = run['start']
    cfg.BACKTEST_END_DATE   = run['end']
    cfg.TRADE_SUMMARY_FILE  = run['output']
    cfg.TRADE_LOGS_DIR      = run['logs_dir']
    os.makedirs(run['logs_dir'], exist_ok=True)

    # Reload backtest so module-level assignments (INDEX_FILE, OPTIONS_PATH, etc.)
    # are recomputed from the freshly-patched configs
    if 'backtest' in sys.modules:
        importlib.reload(sys.modules['backtest'])
    import backtest as bt

    bt.run_backtest()

    # -- Comparison vs baseline --
    if not os.path.exists(run['output']):
        print("  No output file generated.")
        return

    df = pd.read_csv(run['output'])
    df = df[df['entry_time'].notna()]
    total    = len(df)
    if total == 0:
        print("  No trades in output.")
        return

    winners  = df[df['total_pl_rupees'] > 0]
    total_rs = df['total_pl_rupees'].sum()
    wr       = len(winners) / total * 100

    print()
    print(f"  {'':22}  {'Baseline (VIX<16)':>18}  {'VIX<15':>8}")
    print(f"  {'─'*52}")
    base_path = _BASELINES.get(instrument)
    if base_path and os.path.exists(base_path):
        base     = pd.read_csv(base_path)
        base     = base[base['entry_time'].notna()]
        bn       = len(base)
        bw       = len(base[base['total_pl_rupees'] > 0])
        bwr      = bw / bn * 100 if bn else 0
        brs      = base['total_pl_rupees'].sum()
        print(f"  {'Trades':22}  {bn:>18}  {total:>8}")
        print(f"  {'Win Rate':22}  {bwr:>17.1f}%  {wr:>7.1f}%")
        print(f"  {'P&L (₹)':22}  {brs:>+18,.0f}  {total_rs:>+8,.0f}")
        print(f"  {'Delta':22}  {'—':>18}  {total_rs-brs:>+8,.0f}")
    else:
        print(f"  Trades: {total}  WR: {wr:.1f}%  P&L: ₹{total_rs:+,.0f}")

    # Show removed grey-zone trades (were VIX 15–16 in old baseline)
    grey_removed = df[(df['entry_vix'] >= 15.0) & (df['entry_vix'] < 16.0)]
    if not grey_removed.empty:
        print(f"\n  NOTE: {len(grey_removed)} grey-zone (VIX 15–16) trades still appear "
              f"— VIX may have been read slightly above 15.")


if __name__ == '__main__':
    print("=== Artemis VIX-15 Backtest ===")
    print(f"VIX ceiling: < 15.0  (baseline was < 16.0)")

    results = {}
    for run in _RUNS:
        run_one(run)
        if os.path.exists(run['output']):
            df = pd.read_csv(run['output'])
            df = df[df['entry_time'].notna()]
            results[run['instrument']] = df['total_pl_rupees'].sum() if len(df) else 0

    # Combined summary
    if results:
        print()
        print("=" * 56)
        print("COMBINED ARTEMIS SUMMARY (VIX < 15)")
        print("=" * 56)
        combined_new = sum(results.values())
        combined_base = sum(
            pd.read_csv(p)[pd.read_csv(p)['entry_time'].notna()]['total_pl_rupees'].sum()
            for p in _BASELINES.values() if os.path.exists(p)
        )
        print(f"  Baseline combined  : ₹{combined_base:+,.0f}")
        print(f"  VIX-15 combined    : ₹{combined_new:+,.0f}")
        print(f"  Delta              : ₹{combined_new - combined_base:+,.0f}")
