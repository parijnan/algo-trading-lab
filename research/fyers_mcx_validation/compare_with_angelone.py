"""
Plan §1.1 steps 3-5 / §1.2: bar-by-bar comparison of Fyers-sourced expired-
contract data (fetch_fyers_data.py's output) against our existing Angel One
data for the same real contract.

Run from repo root:
  python research/fyers_mcx_validation/compare_with_angelone.py \\
      --angel-file data_pipeline/data/mcx/CRUDEOILM/2026-08-19_futures.csv \\
      --fyers-file research/fyers_mcx_validation/data/fyers_MCX_CRUDEOILM26AUGFUT_1min.csv
"""
import argparse
import pandas as pd
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def load(path, label):
    df = pd.read_csv(path, parse_dates=['time_stamp'])
    df['time_stamp'] = df['time_stamp'].dt.tz_localize(None) if df['time_stamp'].dt.tz is None \
        else df['time_stamp'].dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    df = df.sort_values('time_stamp').drop_duplicates(subset='time_stamp').set_index('time_stamp')
    print(f'{label}: {len(df)} rows, {df.index.min()} -> {df.index.max()}')
    return df


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--angel-file', required=True)
    p.add_argument('--fyers-file', required=True)
    args = p.parse_args()

    angel = load(REPO_ROOT / args.angel_file, 'Angel One')
    fyers = load(REPO_ROOT / args.fyers_file, 'Fyers')

    print()
    print('=' * 70)
    print('ROW COUNT / RANGE COMPARISON')
    print('=' * 70)
    print(f'Angel One rows: {len(angel)}   Fyers rows: {len(fyers)}   diff: {len(fyers) - len(angel)}')
    only_angel = angel.index.difference(fyers.index)
    only_fyers = fyers.index.difference(angel.index)
    print(f'Timestamps only in Angel One: {len(only_angel)}')
    print(f'Timestamps only in Fyers:     {len(only_fyers)}')
    if len(only_fyers) > 0:
        print(f'  Fyers-only range: {only_fyers.min()} -> {only_fyers.max()}')
        print(f'  Fyers-only sample (first 5): {list(only_fyers[:5])}')
        print(f'  Fyers-only sample (last 5):  {list(only_fyers[-5:])}')
    if len(only_angel) > 0:
        print(f'  Angel-only range: {only_angel.min()} -> {only_angel.max()}')
        print(f'  Angel-only sample (first 5): {list(only_angel[:5])}')

    print()
    print('=' * 70)
    print('OHLC COMPARISON ON OVERLAPPING TIMESTAMPS')
    print('=' * 70)
    common = angel.index.intersection(fyers.index)
    print(f'Common timestamps: {len(common)}')
    a = angel.loc[common]
    f = fyers.loc[common]

    for col in ['open', 'high', 'low', 'close']:
        diff = (a[col] - f[col]).abs()
        n_mismatch = (diff > 1e-6).sum()
        print(f'  {col:6s}: max abs diff = {diff.max():.4f}, mismatched rows = {n_mismatch} '
              f'({n_mismatch/len(common)*100:.2f}%)')
        if n_mismatch > 0:
            worst = diff.nlargest(5)
            print(f'    worst 5 mismatches:')
            for ts, d in worst.items():
                print(f'      {ts}: angel={a.loc[ts, col]}  fyers={f.loc[ts, col]}  diff={d:.4f}')

    print()
    print('=' * 70)
    print('VOLUME COMPARISON')
    print('=' * 70)
    vol_diff = (a['volume'] - f['volume']).abs()
    print(f'  volume: max abs diff = {vol_diff.max()}, mean abs diff = {vol_diff.mean():.2f}')
    print(f'  Angel One total volume (common range): {a["volume"].sum()}')
    print(f'  Fyers total volume (common range):     {f["volume"].sum()}')
    print(f'  Angel One sample volumes: {a["volume"].head(5).tolist()}')
    print(f'  Fyers sample volumes:     {f["volume"].head(5).tolist()}')

    print()
    print('=' * 70)
    print('OPENING-BAR ARTIFACT CHECK (09:00 bar, first day)')
    print('=' * 70)
    first_day = angel.index.min().date()
    open_ts = pd.Timestamp(f'{first_day} 09:00:00')
    if open_ts in angel.index and open_ts in fyers.index:
        a_row = angel.loc[open_ts]
        f_row = fyers.loc[open_ts]
        print(f'  Angel One 09:00 bar: O={a_row.open} H={a_row.high} L={a_row.low} C={a_row.close} '
              f'(TR={a_row.high - a_row.low:.2f})')
        print(f'  Fyers     09:00 bar: O={f_row.open} H={f_row.high} L={f_row.low} C={f_row.close} '
              f'(TR={f_row.high - f_row.low:.2f})')
    else:
        print(f'  09:00 bar not present in both sources for {first_day} -- skipping this check.')


if __name__ == '__main__':
    main()
