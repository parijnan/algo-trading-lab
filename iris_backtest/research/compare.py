"""
Load excursion CSVs from data/ and print a side-by-side comparison table.
Also saves data/signal_comparison.csv.

Usage (from repo root):
    python iris_backtest/research/compare.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from configs import OUTPUT_DIR, HORIZONS
from signals import ALL_SIGNALS


def signal_stats(df: pd.DataFrame, name: str, total_years: float) -> dict:
    n   = len(df)
    row = {'signal': name, 'fires': n, 'fires_per_year': round(n / total_years, 1)}

    for N in HORIZONS:
        c   = df[f'close_{N}'].dropna()
        mfe = df[f'mfe_{N}'].dropna()
        mae = df[f'mae_{N}'].dropna()

        if c.empty:
            for k in (f'wr_{N}', f'avg_mfe_{N}', f'avg_mae_{N}', f'rr_{N}', f'med_close_{N}'):
                row[k] = np.nan
            continue

        avg_mfe = mfe.mean()
        avg_mae = mae.mean()
        row[f'wr_{N}']        = round((c > 0).mean() * 100, 1)
        row[f'avg_mfe_{N}']   = round(avg_mfe, 2)
        row[f'avg_mae_{N}']   = round(avg_mae, 2)
        row[f'rr_{N}']        = round(avg_mfe / avg_mae, 2) if avg_mae > 0 else np.nan
        row[f'med_close_{N}'] = round(c.median(), 2)

    return row


def main():
    from utils import load_nifty_1min
    df_1min     = load_nifty_1min()
    total_years = (df_1min.index[-1] - df_1min.index[0]).days / 365.25

    rows = []
    for sig_mod in ALL_SIGNALS:
        name     = sig_mod.SIGNAL_NAME
        exc_path = OUTPUT_DIR / f'{name}_excursions.csv'
        if not exc_path.exists():
            print(f'  [{name}] no file — run run_all.py first')
            continue
        df = pd.read_csv(exc_path)
        rows.append(signal_stats(df, name, total_years))

    if not rows:
        print('No results. Run:  python iris_backtest/research/run_all.py')
        return

    summary = pd.DataFrame(rows).set_index('signal')
    summary.to_csv(OUTPUT_DIR / 'signal_comparison.csv')

    date_range = f'{df_1min.index[0].date()} → {df_1min.index[-1].date()}'
    print(f'\nNifty 1-min  |  {date_range}  |  {total_years:.1f} years\n')

    # ── Header ──────────────────────────────────────────────────────────────
    h1 = f'{"Signal":<14}  {"Fires":>6}  {"/ yr":>6}'
    h2 = f'{"":14}  {"":6}  {"":6}'
    for N in HORIZONS:
        lbl = f'{N}m'
        h1 += f'  {lbl:>27}'
        h2 += f'  {"WR%":>5} {"MFE":>6} {"MAE":>6} {"RR":>5} {"med":>5}'
    print(h1)
    print(h2)
    print('─' * len(h1))

    for sig, r in summary.iterrows():
        line = f'{sig:<14}  {int(r["fires"]):>6}  {r["fires_per_year"]:>6.1f}'
        for N in HORIZONS:
            wr  = r.get(f'wr_{N}',        np.nan)
            mfe = r.get(f'avg_mfe_{N}',   np.nan)
            mae = r.get(f'avg_mae_{N}',   np.nan)
            rr  = r.get(f'rr_{N}',        np.nan)
            med = r.get(f'med_close_{N}', np.nan)
            line += f'  {wr:>5.1f} {mfe:>6.1f} {mae:>6.1f} {rr:>5.2f} {med:>5.1f}'
        print(line)

    print(f'\nSaved → {OUTPUT_DIR / "signal_comparison.csv"}')


if __name__ == '__main__':
    main()
