"""
validate_artemis_sensex.py — OI signals at Artemis Sensex entry (Sep 2025 – Mar 2026).

Builds OI profiles on-the-fly for each of the 27 Sensex traded weeks,
then runs the same correlation analysis as validate_artemis_entry.py.

Key differences from the Nifty version:
  - Sensex options at data_pipeline/data/sensex/<expiry>/ (no /options/ subdir)
  - Sensex index uses 'time_stamp' column with IST timezone (+05:30)
  - strike_step=100 (Sensex strikes in 100-pt increments vs Nifty's 50)
  - OI column: 'oi' (same as Nifty resampled files; engine handles both)
  - Only 27 trades — results are directional indicators, NOT statistically significant

Statistical caveat: n=27 requires |r| > 0.39 for p<0.05 (two-tailed).
No finding here should be acted on without also passing the Nifty time-split test.
The purpose is to confirm whether the Nifty directional pattern holds in sign for Sensex.

Usage:
    python research/oi_analysis/validate_artemis_sensex.py
"""

import os, sys, glob, time
import numpy as np
import pandas as pd
from scipy import stats

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)

from research.oi_analysis.oi_engine import build_oi_profile

TRADE_SUMMARY    = os.path.join(_REPO_ROOT, 'artemis_backtest', 'data', 'trade_summary_sensex_rerun.csv')
SENSEX_INDEX     = os.path.join(_REPO_ROOT, 'data_pipeline', 'data', 'indices', 'sensex.csv')
SENSEX_OPT_BASE  = os.path.join(_REPO_ROOT, 'data_pipeline', 'data', 'sensex')
OUTPUT_DIR       = os.path.join(os.path.dirname(__file__), 'data')

STRIKE_STEP  = 100   # Sensex strikes in 100-pt increments
RESAMPLE     = '5min'

RATIO_FEATURES = [
    'pcr_near', 'pcr_broad',
    'ce_wall_dist_pct', 'pe_wall_dist_pct',
    'wall_asym',
    'wall_oi_ratio',
    'max_pain_dist_pts',
]
ABS_FEATURES = ['total_oi', 'ce_wall_oi', 'pe_wall_oi']
ALL_FEATURES  = RATIO_FEATURES + ABS_FEATURES
TARGET_COLS   = ['total_pl_points', 'ce_pl_points', 'pe_pl_points']


def load_sensex_index() -> pd.DataFrame:
    df = pd.read_csv(SENSEX_INDEX, parse_dates=['time_stamp'])
    df = df.rename(columns={'time_stamp': 'ts'}).set_index('ts')
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


def load_trades() -> pd.DataFrame:
    df = pd.read_csv(TRADE_SUMMARY)
    df['entry_time'] = pd.to_datetime(df['entry_time'], errors='coerce')
    df['expiry']     = pd.to_datetime(df['expiry'],     errors='coerce')
    df = df[df['week_outcome'] == 'traded'].dropna(subset=['entry_time']).copy()
    df['sell_expiry'] = df['expiry'].dt.date.astype(str)
    return df.reset_index(drop=True)


def closest_before(profile: pd.DataFrame, ts: pd.Timestamp):
    """Return last OI feature row at or before ts."""
    profile.index = pd.to_datetime(profile.index)
    before = profile[profile.index <= ts]
    return before.iloc[-1] if not before.empty else None


def print_sep(char='─', width=90):
    print(char * width)


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print('Loading Sensex index...', end=' ', flush=True)
    index_df = load_sensex_index()
    print(f'{len(index_df):,} rows  ({index_df.index.min().date()} → {index_df.index.max().date()})')

    print('Loading Artemis Sensex trades...', end=' ', flush=True)
    trades = load_trades()
    print(f'{len(trades)} traded weeks  '
          f'({trades["entry_time"].min().date()} → {trades["entry_time"].max().date()})')
    print(f'  P&L: mean={trades["total_pl_points"].mean():.1f} pts, '
          f'median={trades["total_pl_points"].median():.1f} pts, '
          f'winners={(trades["total_pl_points"]>0).mean()*100:.0f}%')

    # ── build OI features on-the-fly for each trade ──────────────────────
    print()
    print('Building OI profiles on-the-fly:')
    print_sep()
    rows = []
    for _, trade in trades.iterrows():
        expiry    = trade['sell_expiry']
        entry_ts  = trade['entry_time']
        opt_dir   = os.path.join(SENSEX_OPT_BASE, expiry)

        t0 = time.time()
        profile = build_oi_profile(
            expiry_date=expiry,
            options_dir=opt_dir,
            index_df=index_df,
            strike_step=STRIKE_STEP,
            resample=RESAMPLE,
        )
        elapsed = time.time() - t0

        feat = {f: np.nan for f in ALL_FEATURES}
        lag  = np.nan

        if not profile.empty:
            row_at_entry = closest_before(profile, entry_ts)
            if row_at_entry is not None:
                for f in ['pcr_near', 'pcr_broad', 'ce_wall_dist_pct', 'pe_wall_dist_pct',
                          'max_pain_dist_pts', 'total_oi', 'ce_wall_oi', 'pe_wall_oi']:
                    feat[f] = float(row_at_entry.get(f, np.nan))
                feat['wall_asym']    = feat['ce_wall_dist_pct'] - feat['pe_wall_dist_pct']
                feat['wall_oi_ratio'] = (feat['pe_wall_oi'] / feat['ce_wall_oi']
                                         if feat['ce_wall_oi'] > 0 else np.nan)
                lag = round((entry_ts - profile.index[profile.index <= entry_ts][-1]).total_seconds() / 60, 1)

        print(f'  {expiry}  entry={entry_ts.strftime("%H:%M")}  '
              f'pcr_near={feat["pcr_near"]:.3f}  '
              f'pcr_broad={feat["pcr_broad"]:.3f}  '
              f'total_pl={trade["total_pl_points"]:+.1f}  [{elapsed:.1f}s]')

        row_out = {'entry_time': entry_ts, 'sell_expiry': expiry,
                   'entry_spot': trade['entry_spot'], 'entry_vix': trade['entry_vix'],
                   'ce_sell_strike': trade['ce_sell_strike'],
                   'pe_sell_strike': trade['pe_sell_strike'],
                   'ce_pl_points': trade['ce_pl_points'],
                   'pe_pl_points': trade['pe_pl_points'],
                   'total_pl_points': trade['total_pl_points'],
                   '_lag_min': lag}
        row_out.update(feat)
        rows.append(row_out)

    print_sep()
    merged = pd.DataFrame(rows)
    n_matched = merged['pcr_near'].notna().sum()
    print(f'OI features matched: {n_matched}/{len(merged)}')

    merged.to_csv(os.path.join(OUTPUT_DIR, 'artemis_sensex_entry_oi_joined.csv'), index=False)

    # ═══════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print(f'STATISTICAL CAVEAT: n={len(merged)} trades. Need |r| > 0.39 for p<0.05.')
    print('Results show DIRECTIONAL CONSISTENCY with Nifty, not standalone significance.')
    print_sep('═')

    # ═══════════════════════════════════════════════════════════════════════
    # 1. Correlations
    # ═══════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 1: Spearman correlations — Sensex (n=27)')
    print('           [Nifty reference in brackets]')
    print_sep('═')

    # Nifty reference values (from validate_artemis_entry.py run)
    nifty_ref = {
        'pcr_near':         {'total_pl_points': -0.001, 'ce_pl_points': -0.066, 'pe_pl_points': 0.075},
        'pcr_broad':        {'total_pl_points': -0.003, 'ce_pl_points': -0.137, 'pe_pl_points': 0.153},
        'ce_wall_dist_pct': {'total_pl_points':  0.072, 'ce_pl_points':  0.111, 'pe_pl_points':-0.118},
        'pe_wall_dist_pct': {'total_pl_points':  0.026, 'ce_pl_points':  0.047, 'pe_pl_points':-0.045},
        'wall_asym':        {'total_pl_points':  0.030, 'ce_pl_points':  0.083, 'pe_pl_points':-0.058},
        'wall_oi_ratio':    {'total_pl_points': -0.122, 'ce_pl_points': -0.132, 'pe_pl_points': 0.011},
        'max_pain_dist_pts':{'total_pl_points':  0.065, 'ce_pl_points':  0.087, 'pe_pl_points':-0.070},
    }

    print(f'  {"Feature":20s}  {"":5s}  {"total_pl":>8s}  {"ce_pl":>8s}  {"pe_pl":>8s}')
    print_sep()
    for feat in RATIO_FEATURES:
        valid = merged.dropna(subset=[feat] + TARGET_COLS)
        if len(valid) < 5:
            print(f'  {feat:20s}  Sx:    {"(no data)":>8s}')
            continue
        r_tot, _ = stats.spearmanr(valid[feat], valid['total_pl_points'])
        r_ce,  _ = stats.spearmanr(valid[feat], valid['ce_pl_points'])
        r_pe,  _ = stats.spearmanr(valid[feat], valid['pe_pl_points'])
        ref = nifty_ref.get(feat, {})
        print(f'  {feat:20s}  Sx:   {r_tot:+8.3f}  {r_ce:+8.3f}  {r_pe:+8.3f}')
        print(f'  {"":20s}  Nif:  '
              f'{ref.get("total_pl_points", np.nan):+8.3f}  '
              f'{ref.get("ce_pl_points", np.nan):+8.3f}  '
              f'{ref.get("pe_pl_points", np.nan):+8.3f}')

    # ═══════════════════════════════════════════════════════════════════════
    # 2. Primary hypothesis
    # ═══════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 2: Primary hypothesis — PCR → CE worse, PE better')
    print('           (Confirmed if ce_pl r < 0 AND pe_pl r > 0)')
    print_sep('═')
    for feat in ['pcr_near', 'pcr_broad']:
        valid = merged.dropna(subset=[feat, 'ce_pl_points', 'pe_pl_points'])
        if len(valid) < 5:
            print(f'  {feat}: insufficient data')
            continue
        r_ce, p_ce = stats.spearmanr(valid[feat], valid['ce_pl_points'])
        r_pe, p_pe = stats.spearmanr(valid[feat], valid['pe_pl_points'])
        nifty_ce = nifty_ref.get(feat, {}).get('ce_pl_points', np.nan)
        nifty_pe = nifty_ref.get(feat, {}).get('pe_pl_points', np.nan)
        same_sign_ce = (r_ce * nifty_ce > 0) if not np.isnan(nifty_ce) else None
        same_sign_pe = (r_pe * nifty_pe > 0) if not np.isnan(nifty_pe) else None
        confirmed = r_ce < 0 and r_pe > 0
        print(f'  {feat}:')
        print(f'    ce_pl: Sensex r={r_ce:+.3f}  Nifty r={nifty_ce:+.3f}  '
              f'same sign={"YES ✓" if same_sign_ce else "NO ✗"}')
        print(f'    pe_pl: Sensex r={r_pe:+.3f}  Nifty r={nifty_pe:+.3f}  '
              f'same sign={"YES ✓" if same_sign_pe else "NO ✗"}')
        print(f'    Directional hypothesis: {"CONFIRMED ✓" if confirmed else "not confirmed"}')

    # ═══════════════════════════════════════════════════════════════════════
    # 3. Quintile P&L
    # ═══════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 3: Tercile P&L by PCR (quintiles too granular for n=27)')
    print_sep('═')
    for feat in ['pcr_near', 'pcr_broad']:
        valid = merged.dropna(subset=[feat]).copy()
        if len(valid) < 9:
            continue
        valid['t'] = pd.qcut(valid[feat], q=3, labels=['Lo','Mid','Hi'], duplicates='drop')
        gt = valid.groupby('t', observed=False)[TARGET_COLS + ['entry_time']].agg(
            {c: 'mean' for c in TARGET_COLS} | {'entry_time': 'count'}
        ).round(1)
        gt.columns = ['mean_total', 'mean_ce', 'mean_pe', 'n']
        print(f'\n  {feat}:')
        print(gt.to_string(col_space=12))

    # ═══════════════════════════════════════════════════════════════════════
    # 4. High/Low halves summary
    # ═══════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 4: High vs Low halves — PCR and wall_oi_ratio')
    print_sep('═')
    for feat in ['pcr_near', 'pcr_broad', 'wall_oi_ratio']:
        valid = merged.dropna(subset=[feat])
        if len(valid) < 6:
            continue
        hi = valid[valid[feat] > valid[feat].median()]
        lo = valid[valid[feat] <= valid[feat].median()]
        hi_ce = hi['ce_pl_points'].mean()
        lo_ce = lo['ce_pl_points'].mean()
        hi_pe = hi['pe_pl_points'].mean()
        lo_pe = lo['pe_pl_points'].mean()
        hi_tot = hi['total_pl_points'].mean()
        lo_tot = lo['total_pl_points'].mean()
        # Nifty reference (from section 7 of validate_artemis_entry.py)
        nifty_ce_dir = {
            'pcr_near': 'CE better when high ✗',
            'pcr_broad': '(not shown)',
            'wall_oi_ratio': 'CE worse when high ✓',
        }
        ce_dir = 'CE worse when high ✓' if hi_ce < lo_ce else 'CE better when high ✗'
        pe_dir = 'PE better when high ✓' if hi_pe > lo_pe else 'PE worse when high ✗'
        nifty_note = nifty_ce_dir.get(feat, '')
        print(f'  {feat} (Nifty CE direction was: {nifty_note}):')
        print(f'    Total: hi={hi_tot:+.1f}  lo={lo_tot:+.1f}')
        print(f'    CE:    hi={hi_ce:+.1f}  lo={lo_ce:+.1f}  → {ce_dir}')
        print(f'    PE:    hi={hi_pe:+.1f}  lo={lo_pe:+.1f}  → {pe_dir}')
        print()

    # ═══════════════════════════════════════════════════════════════════════
    # 5. Consistency summary
    # ═══════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 5: Sensex vs Nifty consistency summary')
    print_sep('═')
    print('Feature         ce_pl sign match   pe_pl sign match   Assessment')
    print_sep()
    for feat in ['pcr_near', 'pcr_broad', 'wall_oi_ratio']:
        valid = merged.dropna(subset=[feat, 'ce_pl_points', 'pe_pl_points'])
        if len(valid) < 5:
            print(f'  {feat}: insufficient data')
            continue
        r_ce, _ = stats.spearmanr(valid[feat], valid['ce_pl_points'])
        r_pe, _ = stats.spearmanr(valid[feat], valid['pe_pl_points'])
        ref_ce = nifty_ref.get(feat, {}).get('ce_pl_points', np.nan)
        ref_pe = nifty_ref.get(feat, {}).get('pe_pl_points', np.nan)
        ce_match = '✓' if (r_ce * ref_ce > 0) else '✗'
        pe_match = '✓' if (r_pe * ref_pe > 0) else '✗'
        strength = 'consistent' if ce_match == '✓' and pe_match == '✓' else \
                   'partial' if ce_match == '✓' or pe_match == '✓' else 'inconsistent'
        print(f'  {feat:18s}  {ce_match}  (Sx:{r_ce:+.3f} Nif:{ref_ce:+.3f})  '
              f'  {pe_match}  (Sx:{r_pe:+.3f} Nif:{ref_pe:+.3f})  → {strength}')

    print()
    print(f'Output saved to research/oi_analysis/data/artemis_sensex_entry_oi_joined.csv')


if __name__ == '__main__':
    main()
