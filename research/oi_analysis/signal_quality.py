"""
signal_quality.py — OI feature signal quality map for Nifty options.

For every (OI feature, forward horizon) pair, computes:
  - IC: Spearman rank correlation with p-value and effective N
  - Quintile lift: mean forward return per feature quintile (5 buckets)

Also runs a barrier analysis: conditional on spot being within X% of the CE/PE
OI wall, what fraction of bars see a wall breakthrough vs bounce within 2 hours?

Forward horizons:
  intraday:  15min, 30min, 1hr, 2hr, 4hr, EOD (to 15:30 same day)
  multiday:  1D, 3D, 5D (≈ 1 week), to_expiry (settlement)

OI features tested:
  pcr_near, pcr_broad                — directional, put/call balance
  ce_wall_dist_pct, pe_wall_dist_pct — proximity to OI resistance/support
  ce_wall_oi, pe_wall_oi             — wall strength (absolute)
  max_pain_dist_pts                  — max pain offset from spot
  total_oi                           — overall market participation
  wall_asym                          — ce_wall_dist_pct − pe_wall_dist_pct
  wall_oi_ratio                      — pe_wall_oi / ce_wall_oi

All features are rolling z-score normalised (trailing 252 bars ≈ 21 trading
days) before IC computation so cross-year OI scale differences don't pollute
threshold comparisons.

Prerequisites:
  Features CSV must exist: research/oi_analysis/data/nifty_oi_features.csv
  Pass --build to generate it first (~20 min, or faster with --workers N).

Usage:
  python research/oi_analysis/signal_quality.py
  python research/oi_analysis/signal_quality.py --build --workers 4
  python research/oi_analysis/signal_quality.py --from 2022-01-01 --to 2024-12-31
  python research/oi_analysis/signal_quality.py --features-csv PATH
"""

import argparse, os, sys, subprocess, warnings
import numpy as np
import pandas as pd
from scipy import stats

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)

_HERE           = os.path.dirname(__file__)
FEATURES_CSV    = os.path.join(_HERE, 'data', 'nifty_oi_features.csv')
INDEX_FILE      = os.path.join(_REPO_ROOT, 'data_pipeline', 'data', 'indices', 'nifty.csv')
OUTPUT_DIR      = os.path.join(_HERE, 'data')

INTRADAY_MIN    = [15, 30, 60, 120, 240]    # forward horizon in minutes
MULTIDAY_D      = [1, 3, 5]                 # forward horizon in trading days
NORM_WINDOW     = 252                        # rolling z-score window (≈21 trading days at 5min)
N_QUINTILES     = 5
BARRIER_BARS    = 24                         # 2 hours at 5-min for wall breakthrough

BASE_FEATURES = [
    'pcr_near', 'pcr_broad',
    'ce_wall_dist_pct', 'pe_wall_dist_pct',
    'ce_wall_oi', 'pe_wall_oi',
    'max_pain_dist_pts', 'total_oi',
]
ALL_FEATURES = BASE_FEATURES + ['wall_asym', 'wall_oi_ratio']

FWD_COLS = (
    [f'fwd_{h}m'  for h in INTRADAY_MIN]
    + ['fwd_eod']
    + [f'fwd_{d}d' for d in MULTIDAY_D]
    + ['fwd_expiry']
)
HORIZON_LABELS = (
    [f'{h}min'  for h in INTRADAY_MIN]
    + ['EOD']
    + [f'{d}D'   for d in MULTIDAY_D]
    + ['to_exp']
)


# ─── data loading ─────────────────────────────────────────────────────────────

def _load_features(path, date_from, date_to):
    df = pd.read_csv(path, parse_dates=['ts', 'expiry'])
    if date_from:
        df = df[df['ts'] >= pd.Timestamp(date_from)]
    if date_to:
        df = df[df['ts'] <= pd.Timestamp(date_to)]
    return df


def _dedup_nearest_expiry(df):
    """For each 5-min bar, keep only the nearest unexpired expiry."""
    df = df.copy()
    df['days_to_expiry'] = (df['expiry'] - df['ts'].dt.normalize()).dt.days
    df = df[df['days_to_expiry'] >= 0]
    df = (df.sort_values(['ts', 'days_to_expiry'])
            .drop_duplicates('ts', keep='first')
            .sort_values('ts')
            .reset_index(drop=True))
    return df


def _add_derived(df):
    df = df.copy()
    df['wall_asym'] = df['ce_wall_dist_pct'] - df['pe_wall_dist_pct']
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        df['wall_oi_ratio'] = df['pe_wall_oi'] / df['ce_wall_oi'].replace(0, np.nan)
    return df


def _load_index_5min(path):
    raw = pd.read_csv(path, parse_dates=['time_stamp'])
    raw = raw.rename(columns={'time_stamp': 'ts'}).set_index('ts')
    if raw.index.tz is not None:
        raw.index = raw.index.tz_localize(None)
    idx = raw['close'].resample('5min', label='right', closed='right').last()
    idx = idx.between_time('09:15', '15:30').dropna()
    return idx


# ─── forward return computation ───────────────────────────────────────────────

def _build_forward_returns(idx_5min):
    """Build a DataFrame of forward returns at every horizon, keyed by ts."""
    fwd = pd.DataFrame(index=idx_5min.index)

    # Intraday
    for h in INTRADAY_MIN:
        n = h // 5
        fwd[f'fwd_{h}m'] = idx_5min.shift(-n) / idx_5min - 1

    # EOD: to 15:30 same day
    daily_eod   = idx_5min.groupby(idx_5min.index.date).last()
    eod_by_date = dict(zip(daily_eod.index, daily_eod.values))
    fwd['fwd_eod'] = pd.Series(
        [eod_by_date.get(ts.date(), np.nan) / c - 1
         for ts, c in zip(idx_5min.index, idx_5min.values)],
        index=idx_5min.index,
    )

    # Multi-day: next N trading day close
    trading_days = sorted(daily_eod.index)
    td_pos       = {d: i for i, d in enumerate(trading_days)}
    for n_days in MULTIDAY_D:
        future_by_date = {
            d: daily_eod.iloc[i + n_days]
            for d, i in td_pos.items()
            if (i + n_days) < len(trading_days)
        }
        fwd[f'fwd_{n_days}d'] = pd.Series(
            [future_by_date.get(ts.date(), np.nan) / c - 1
             for ts, c in zip(idx_5min.index, idx_5min.values)],
            index=idx_5min.index,
        )

    fwd.index.name = 'ts'
    return fwd


def _add_expiry_returns(df, idx_5min):
    """Add fwd_expiry: return from bar T to expiry settlement (last 5-min bar on expiry day)."""
    expiry_close = {}
    for exp in df['expiry'].dt.normalize().unique():
        day_bars = idx_5min[idx_5min.index.date == exp.date()]
        if not day_bars.empty:
            expiry_close[exp.date()] = day_bars.iloc[-1]

    df = df.copy()
    df['_exp_close'] = df['expiry'].dt.normalize().dt.date.map(expiry_close)
    df['fwd_expiry'] = df['_exp_close'] / df['spot'] - 1
    df.drop(columns=['_exp_close'], inplace=True)
    return df


# ─── feature normalisation ────────────────────────────────────────────────────

def _normalize_features(df, features, window=NORM_WINDOW):
    """Rolling z-score for each feature (sorted by ts, in-place on copy)."""
    df = df.copy().sort_values('ts').reset_index(drop=True)
    for f in features:
        if f not in df.columns:
            continue
        col  = df[f]
        mu   = col.rolling(window, min_periods=20).mean()
        sig  = col.rolling(window, min_periods=20).std().replace(0, np.nan)
        df[f + '_z'] = (col - mu) / sig
    return df


# ─── IC & quintile computation ────────────────────────────────────────────────

def _ic_table(df, features, fwd_cols):
    rows = []
    for feat in features:
        col = feat + '_z' if (feat + '_z') in df.columns else feat
        row = {'feature': feat}
        for fwd in fwd_cols:
            sub = df[[col, fwd]].dropna()
            n   = len(sub)
            if n < 50:
                row[fwd]      = np.nan
                row[fwd+'_p'] = np.nan
                row[fwd+'_n'] = n
            else:
                ic, pval      = stats.spearmanr(sub[col], sub[fwd])
                row[fwd]      = round(ic, 4)
                row[fwd+'_p'] = round(pval, 6)
                row[fwd+'_n'] = n
        rows.append(row)
    return pd.DataFrame(rows)


def _quintile_lift(df, features, fwd_cols, n=N_QUINTILES):
    rows = []
    for feat in features:
        col = feat + '_z' if (feat + '_z') in df.columns else feat
        for fwd in fwd_cols:
            sub = df[[col, fwd]].dropna()
            if len(sub) < n * 20:
                continue
            try:
                labels, bins = pd.qcut(
                    sub[col], q=n, retbins=True, labels=False, duplicates='drop'
                )
            except ValueError:
                continue
            sub = sub.copy()
            sub['q'] = labels.values
            for q in range(n):
                g = sub[sub['q'] == q]
                if g.empty:
                    continue
                rows.append({
                    'feature':          feat,
                    'horizon':          fwd,
                    'quintile':         q + 1,
                    'n':                len(g),
                    'mean_fwd_ret_pct': round(g[fwd].mean() * 100, 4),
                    'std_fwd_ret_pct':  round(g[fwd].std() * 100, 4),
                    'bin_lo':           round(bins[q],   4),
                    'bin_hi':           round(bins[q+1], 4),
                })
    return pd.DataFrame(rows)


# ─── barrier analysis ─────────────────────────────────────────────────────────

def _day_future_extremes(idx_5min, n_bars):
    """
    For each bar t, compute the max and min over the next n_bars bars within
    the same calendar day. Returns (future_high_series, future_low_series).
    """
    fh_parts = []
    fl_parts = []
    for _, day_bars in idx_5min.groupby(idx_5min.index.date):
        s   = pd.Series(day_bars.values, index=day_bars.index, dtype=float)
        rev = s.iloc[::-1]
        fh  = rev.rolling(n_bars, min_periods=1).max().iloc[::-1].shift(-1)
        fl  = rev.rolling(n_bars, min_periods=1).min().iloc[::-1].shift(-1)
        fh_parts.append(fh)
        fl_parts.append(fl)
    return pd.concat(fh_parts), pd.concat(fl_parts)


def _barrier_analysis(df, idx_5min):
    future_high, future_low = _day_future_extremes(idx_5min, BARRIER_BARS)

    feat = df.copy()
    feat = feat.merge(
        pd.DataFrame({
            'ts':          future_high.index,
            'future_high': future_high.values,
            'future_low':  future_low.values,
        }),
        on='ts', how='left',
    )

    rows    = []
    buckets = [(0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 5.0)]

    for wall, dist_col, strike_col, fcol, op in [
        ('CE', 'ce_wall_dist_pct', 'ce_wall_strike', 'future_high', '>'),
        ('PE', 'pe_wall_dist_pct', 'pe_wall_strike', 'future_low',  '<'),
    ]:
        for lo, hi in buckets:
            mask = (feat[dist_col] >= lo) & (feat[dist_col] < hi) & feat[strike_col].notna()
            sub  = feat[mask].dropna(subset=[fcol, strike_col])
            if len(sub) < 10:
                continue
            rate = (
                (sub[fcol] > sub[strike_col]).mean() if op == '>'
                else (sub[fcol] < sub[strike_col]).mean()
            )
            rows.append({
                'wall':              wall,
                'dist_pct_range':    f'{lo:.1f}–{hi:.1f}%',
                'n_bars':            len(sub),
                'breakthrough_rate': round(rate, 3),
            })
    return pd.DataFrame(rows)


# ─── printing ─────────────────────────────────────────────────────────────────

def _star(pval):
    if pval is None or np.isnan(pval): return ''
    if pval < 0.001: return '***'
    if pval < 0.01:  return '**'
    if pval < 0.05:  return '*'
    return ''


def _print_ic(ic_df, fwd_cols, labels):
    col_w  = 9
    feat_w = 20
    print()
    print('OI SIGNAL QUALITY — Spearman IC × Forward Horizon')
    print('(* p<0.05  ** p<0.01  *** p<0.001  |  features z-scored)')
    print()
    header = f'{"Feature":<{feat_w}}' + ''.join(f'{lb:>{col_w}}' for lb in labels)
    print(header)
    print('─' * len(header))
    for _, row in ic_df.iterrows():
        line = f'{row["feature"]:<{feat_w}}'
        for fwd in fwd_cols:
            ic   = row.get(fwd, np.nan)
            pval = row.get(fwd + '_p', np.nan)
            if pd.isna(ic):
                line += f'{"–":>{col_w}}'
            else:
                cell = f'{ic:+.3f}{_star(pval)}'
                line += f'{cell:>{col_w}}'
        print(line)
    print()


def _print_barrier(barrier_df):
    print()
    print('BARRIER ANALYSIS — OI Wall Breakthrough Rate (within 2 hr, same day)')
    print('─' * 60)
    for wall in ['CE', 'PE']:
        sub = barrier_df[barrier_df['wall'] == wall]
        if sub.empty:
            continue
        action = 'spot > CE wall strike' if wall == 'CE' else 'spot < PE wall strike'
        print(f'\n{wall} wall  [{action}]')
        print(f'  {"Spot distance from wall":28s}  {"N":>6}  {"Breakthru%":>10}')
        for _, r in sub.iterrows():
            print(f'  {r["dist_pct_range"]:28s}  {r["n_bars"]:>6,}  '
                  f'{r["breakthrough_rate"]:>10.1%}')
    print()


def _print_top_quintile_spread(ql_df, top_n=15):
    print(f'\nTOP {top_n} SIGNALS BY Q5–Q1 SPREAD IN MEAN FORWARD RETURN:')
    print('─' * 72)
    rows = []
    for (feat, hor), grp in ql_df.groupby(['feature', 'horizon']):
        q1 = grp[grp['quintile'] == 1]['mean_fwd_ret_pct'].values
        q5 = grp[grp['quintile'] == 5]['mean_fwd_ret_pct'].values
        if len(q1) and len(q5):
            rows.append({
                'feature': feat, 'horizon': hor,
                'Q1': q1[0], 'Q5': q5[0],
                'spread': abs(q5[0] - q1[0]),
            })
    top = pd.DataFrame(rows).sort_values('spread', ascending=False).head(top_n)
    for _, r in top.iterrows():
        print(f'  {r["feature"]:22s}  {r["horizon"]:12s}  '
              f'Q1={r["Q1"]:+6.3f}%  Q5={r["Q5"]:+6.3f}%  '
              f'|spread|={r["spread"]:.3f}%')
    print()


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--features-csv', default=FEATURES_CSV)
    ap.add_argument('--build',   action='store_true',
                    help='Run build_nifty_features.py first if CSV missing')
    ap.add_argument('--workers', type=int, default=1)
    ap.add_argument('--from',    dest='date_from', default=None)
    ap.add_argument('--to',      dest='date_to',   default=None)
    args = ap.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ── build feature CSV if needed ──────────────────────────────────────────
    if not os.path.exists(args.features_csv):
        if not args.build:
            print(f'Feature CSV not found: {args.features_csv}')
            print('Run build_nifty_features.py first, or pass --build.')
            sys.exit(1)
        print('Building OI features for all expiries (this takes ~20 min)...')
        builder = os.path.join(_HERE, 'build_nifty_features.py')
        subprocess.run(
            [sys.executable, builder, '--workers', str(args.workers)],
            check=True,
        )

    # ── load & prepare features ──────────────────────────────────────────────
    print(f'Loading OI features from {args.features_csv}...', end=' ', flush=True)
    df = _load_features(args.features_csv, args.date_from, args.date_to)
    print(f'{len(df):,} rows, {df["expiry"].nunique()} expiries')

    print('Dedup to nearest expiry...', end=' ', flush=True)
    df = _dedup_nearest_expiry(df)
    print(f'{len(df):,} unique bars')

    df = _add_derived(df)

    # ── load index, build forward returns ────────────────────────────────────
    print('Loading Nifty 1-min index → 5-min...', end=' ', flush=True)
    idx_5min = _load_index_5min(INDEX_FILE)
    print(f'{len(idx_5min):,} bars')

    print('Computing forward returns...', end=' ', flush=True)
    fwd_df = _build_forward_returns(idx_5min)
    print('done')

    df = df.merge(
        fwd_df.reset_index(),
        on='ts', how='left',
    )
    df = _add_expiry_returns(df, idx_5min)

    # ── normalise features ───────────────────────────────────────────────────
    print('Normalising features (rolling z-score)...', end=' ', flush=True)
    df = _normalize_features(df, [f for f in ALL_FEATURES if f in df.columns])
    print('done')

    present_fwd = [c for c in FWD_COLS if c in df.columns]
    present_feat = [f for f in ALL_FEATURES if f in df.columns]

    # ── IC ───────────────────────────────────────────────────────────────────
    print('Computing Spearman IC...', end=' ', flush=True)
    ic_df = _ic_table(df, present_feat, present_fwd)
    print('done')

    # ── quintile lift ────────────────────────────────────────────────────────
    print('Computing quintile lift...', end=' ', flush=True)
    ql_df = _quintile_lift(df, present_feat, present_fwd)
    print('done')

    # ── barrier analysis ─────────────────────────────────────────────────────
    print('Running barrier analysis...', end=' ', flush=True)
    barrier_df = _barrier_analysis(df, idx_5min)
    print('done')

    # ── print ────────────────────────────────────────────────────────────────
    present_labels = [
        HORIZON_LABELS[FWD_COLS.index(c)]
        for c in present_fwd
    ]
    _print_ic(ic_df, present_fwd, present_labels)
    _print_barrier(barrier_df)
    if not ql_df.empty:
        _print_top_quintile_spread(ql_df)

    # ── save ─────────────────────────────────────────────────────────────────
    ic_path      = os.path.join(OUTPUT_DIR, 'signal_quality_ic.csv')
    ql_path      = os.path.join(OUTPUT_DIR, 'signal_quality_quintiles.csv')
    barrier_path = os.path.join(OUTPUT_DIR, 'signal_quality_barrier.csv')

    ic_df.to_csv(ic_path, index=False)
    ql_df.to_csv(ql_path, index=False)
    barrier_df.to_csv(barrier_path, index=False)

    print(f'\nSaved:')
    print(f'  IC table  → {ic_path}')
    print(f'  Quintiles → {ql_path}')
    print(f'  Barrier   → {barrier_path}')


if __name__ == '__main__':
    main()
