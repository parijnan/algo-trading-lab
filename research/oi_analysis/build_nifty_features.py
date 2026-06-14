"""
build_nifty_features.py — Batch OI feature builder for all Nifty weekly expiries.

Runs oi_engine.build_oi_profile() over every expiry directory and writes a
consolidated CSV to research/oi_analysis/data/nifty_oi_features.csv.

Usage:
    python research/oi_analysis/build_nifty_features.py
    python research/oi_analysis/build_nifty_features.py --from 2024-01-01 --to 2026-06-09
    python research/oi_analysis/build_nifty_features.py --workers 4   # parallel

Output columns:
  ts, expiry, spot, atm_strike,
  ce_wall_strike, ce_wall_oi, ce_wall_dist_pts, ce_wall_dist_pct,
  pe_wall_strike, pe_wall_oi, pe_wall_dist_pts, pe_wall_dist_pct,
  max_pain_strike, max_pain_dist_pts, pcr_near, pcr_broad, total_oi
"""

import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)

from research.oi_analysis.oi_engine import build_oi_profile

NIFTY_OPTIONS_PATH = os.path.join(_REPO_ROOT, 'data_pipeline', 'data', 'nifty', 'options')
NIFTY_INDEX_FILE   = os.path.join(_REPO_ROOT, 'data_pipeline', 'data', 'indices', 'nifty.csv')
OUTPUT_DIR         = os.path.join(os.path.dirname(__file__), 'data')
OUTPUT_FILE        = os.path.join(OUTPUT_DIR, 'nifty_oi_features.csv')

STRIKE_STEP  = 50
RESAMPLE     = '5min'
WALL_MIN_PCT = 0.5
WALL_MAX_PCT = 10.0
NEAR_STRIKES = 5


def _load_index():
    df = pd.read_csv(NIFTY_INDEX_FILE, parse_dates=['time_stamp'])
    df = df.rename(columns={'time_stamp': 'ts'}).set_index('ts')
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


_worker_index = None   # cached per worker process after _worker_init


def _worker_init():
    global _worker_index
    _worker_index = _load_index()


def _process_one(expiry, resample):
    """Worker: build features for one expiry. Returns (expiry, df, error)."""
    try:
        idx = _worker_index if _worker_index is not None else _load_index()
        feat = build_oi_profile(
            expiry_date=expiry,
            options_dir=os.path.join(NIFTY_OPTIONS_PATH, expiry),
            index_df=idx,
            strike_step=STRIKE_STEP,
            resample=resample,
            wall_min_pct=WALL_MIN_PCT,
            wall_max_pct=WALL_MAX_PCT,
            near_strikes=NEAR_STRIKES,
        )
        return expiry, feat.reset_index() if not feat.empty else None, None
    except Exception as e:
        return expiry, None, str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from',     dest='date_from', default=None)
    ap.add_argument('--to',       dest='date_to',   default=None)
    ap.add_argument('--resample', default=RESAMPLE)
    ap.add_argument('--workers',  type=int, default=1, help='Parallel workers (default: 1)')
    args = ap.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    expiry_dirs = sorted([
        d for d in os.listdir(NIFTY_OPTIONS_PATH)
        if os.path.isdir(os.path.join(NIFTY_OPTIONS_PATH, d))
    ])
    if args.date_from:
        expiry_dirs = [d for d in expiry_dirs if d >= args.date_from]
    if args.date_to:
        expiry_dirs = [d for d in expiry_dirs if d <= args.date_to]

    n = len(expiry_dirs)
    print(f'Processing {n} expiries ({expiry_dirs[0]} → {expiry_dirs[-1]})')
    print(f'Resample: {args.resample}  Workers: {args.workers}')
    print(f'Output: {OUTPUT_FILE}')

    all_frames = []
    t0 = time.time()
    done = 0

    if args.workers > 1:
        with ProcessPoolExecutor(max_workers=args.workers, initializer=_worker_init) as ex:
            futs = {ex.submit(_process_one, exp, args.resample): exp for exp in expiry_dirs}
            for fut in as_completed(futs):
                exp, df, err = fut.result()
                done += 1
                if err:
                    print(f'  [{done:3d}/{n}] {exp}  ERROR: {err}')
                elif df is not None:
                    rows = len(df)
                    elapsed = time.time() - t0
                    print(f'  [{done:3d}/{n}] {exp}  {rows:4d} rows  (total {elapsed:.0f}s)')
                    all_frames.append(df)
    else:
        # Sequential — load index once
        print('Loading Nifty index...', end=' ', flush=True)
        index_df = _load_index()
        print(f'{len(index_df):,} rows')
        for i, expiry in enumerate(expiry_dirs):
            t_exp = time.time()
            try:
                feat = build_oi_profile(
                    expiry_date=expiry,
                    options_dir=os.path.join(NIFTY_OPTIONS_PATH, expiry),
                    index_df=index_df,
                    strike_step=STRIKE_STEP,
                    resample=args.resample,
                    wall_min_pct=WALL_MIN_PCT,
                    wall_max_pct=WALL_MAX_PCT,
                    near_strikes=NEAR_STRIKES,
                )
                rows = len(feat)
                elapsed = time.time() - t_exp
                print(f'  [{i+1:3d}/{n}] {expiry}  {rows:4d} rows  ({elapsed:.1f}s)')
                if not feat.empty:
                    all_frames.append(feat.reset_index())
            except Exception as e:
                print(f'  [{i+1:3d}/{n}] {expiry}  ERROR: {e}')

    if not all_frames:
        print('No data produced.')
        return

    master = pd.concat(all_frames, ignore_index=True).sort_values(['expiry', 'ts'])
    master.to_csv(OUTPUT_FILE, index=False)
    total = time.time() - t0
    print(f'\nDone: {len(master):,} rows → {OUTPUT_FILE}  ({total:.0f}s total)')


if __name__ == '__main__':
    main()
