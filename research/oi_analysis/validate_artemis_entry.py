"""
validate_artemis_entry.py — OI signals at Artemis trade entry (Nifty, through Sep 2025).

For each of the 150 Artemis Nifty VIX < 16 trades:
  1. Matches the entry timestamp (~10:31) to OI features for the sell expiry
  2. Correlates OI features with total, CE-side, and PE-side P&L
  3. Tests the directional hypothesis: PCR↑ → CE worse, PE better
     (same hypothesis that FAILED for Athena, but Artemis's short strangle has a simpler
     directional payoff — no calendar spread profit-zone effect to mask the signal)
  4. Reports correlations split 2019-2022 vs 2023-2025 to flag spurious signals

Strategy context:
  Artemis is a short strangle with OTM protection (iron-condor-like structure).
  Sells CE and PE at different strikes, buys protective wings ~300 pts further out.
  Profits from range-bound weeks; loses when spot directionally breaks out.
  For this strategy, PCR SHOULD predict which side gets hit — high PCR (bullish)
  → spot drifts up → CE side exposed. Low PCR (bearish) → PE side exposed.

P&L note:
  ce_pl_points + pe_pl_points ≠ total_pl_points. The residual is structural —
  it represents net debit costs and adjustment positions not split per side.
  total_pl_points is the canonical outcome; ce/pe per-side are directional indicators.

Usage:
    python research/oi_analysis/validate_artemis_entry.py

Requires: research/oi_analysis/data/nifty_oi_features.csv
    Build if missing: python research/oi_analysis/build_nifty_features.py --workers 4
"""

import os, sys
import numpy as np
import pandas as pd
from scipy import stats

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)

TRADE_SUMMARY   = os.path.join(_REPO_ROOT, 'artemis_backtest', 'data', 'trade_summary_nifty_rerun.csv')
OI_FEATURES     = os.path.join(os.path.dirname(__file__), 'data', 'nifty_oi_features.csv')
OUTPUT_DIR      = os.path.join(os.path.dirname(__file__), 'data')

RATIO_FEATURES = [
    'pcr_near', 'pcr_broad',
    'ce_wall_dist_pct', 'pe_wall_dist_pct',
    'wall_asym',         # ce_wall_dist_pct - pe_wall_dist_pct
    'wall_oi_ratio',     # pe_wall_oi / ce_wall_oi
    'max_pain_dist_pts',
]
ABS_FEATURES = ['total_oi', 'ce_wall_oi', 'pe_wall_oi']
ALL_FEATURES = RATIO_FEATURES + ABS_FEATURES

TARGET_COLS = ['total_pl_points', 'ce_pl_points', 'pe_pl_points']
SPLIT_YEAR  = 2023


def load_oi() -> pd.DataFrame:
    df = pd.read_csv(OI_FEATURES, parse_dates=['ts'])
    df['wall_asym']     = df['ce_wall_dist_pct'] - df['pe_wall_dist_pct']
    df['wall_oi_ratio'] = df['pe_wall_oi'] / df['ce_wall_oi']
    return df


def load_trades() -> pd.DataFrame:
    df = pd.read_csv(TRADE_SUMMARY)
    df['entry_time'] = pd.to_datetime(df['entry_time'], errors='coerce')
    df['expiry']     = pd.to_datetime(df['expiry'],     errors='coerce')
    df = df.dropna(subset=['entry_time', 'total_pl_points']).copy()
    df['sell_expiry'] = df['expiry'].dt.date.astype(str)
    df['year']   = df['entry_time'].dt.year
    df['period'] = df['year'].apply(lambda y: 'early(19-22)' if y < SPLIT_YEAR else 'recent(23-25)')
    return df


def match_oi_to_trades(trades: pd.DataFrame, oi: pd.DataFrame) -> pd.DataFrame:
    records = []
    for _, row in trades.iterrows():
        expiry   = row['sell_expiry']
        entry_ts = row['entry_time']
        sub = oi[(oi['expiry'] == expiry) & (oi['ts'] <= entry_ts)]
        if sub.empty:
            feat = {f: np.nan for f in ALL_FEATURES}
            lag  = np.nan
        else:
            last = sub.iloc[-1]
            feat = {f: last.get(f, np.nan) for f in ALL_FEATURES}
            lag  = round((entry_ts - sub['ts'].iloc[-1]).total_seconds() / 60, 1)
        feat['_lag_min'] = lag
        records.append(feat)
    feat_df = pd.DataFrame(records, index=trades.index)
    return pd.concat([trades, feat_df], axis=1)


def spearman_table(df: pd.DataFrame, features: list, targets: list) -> pd.DataFrame:
    rows = []
    for feat in features:
        row = {'feature': feat}
        for tgt in targets:
            valid = df[[feat, tgt]].dropna()
            if len(valid) < 8:
                row[tgt] = np.nan
                row[f'{tgt}_p'] = np.nan
            else:
                r, p = stats.spearmanr(valid[feat], valid[tgt])
                row[tgt] = round(r, 3)
                row[f'{tgt}_p'] = round(p, 4)
        rows.append(row)
    return pd.DataFrame(rows).set_index('feature')


def quintile_analysis(df: pd.DataFrame, feature: str, targets: list) -> pd.DataFrame:
    valid = df.dropna(subset=[feature]).copy()
    valid['q'] = pd.qcut(valid[feature], q=5, labels=['Q1','Q2','Q3','Q4','Q5'],
                         duplicates='drop')
    result = valid.groupby('q', observed=False)[targets].mean().round(1)
    result.index.name = None
    result.columns = [f'mean_{t}' for t in targets]
    result['n'] = valid.groupby('q', observed=False)[targets[0]].count()
    return result


def entry_filter_sim(df: pd.DataFrame, feature: str, skip_bottom: float = 0.2) -> dict:
    thresh   = df[feature].quantile(skip_bottom)
    kept     = df[df[feature] >  thresh]
    skipped  = df[df[feature] <= thresh]
    return {
        'feature':          feature,
        'n_kept':           len(kept),
        'n_skipped':        len(skipped),
        'mean_pl_kept':     round(kept['total_pl_points'].mean(), 1),
        'mean_pl_all':      round(df['total_pl_points'].mean(), 1),
        'skipped_mean_pl':  round(skipped['total_pl_points'].mean(), 1),
    }


def print_sep(char='─', width=95):
    print(char * width)


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print('Loading OI features...', end=' ', flush=True)
    oi = load_oi()
    print(f'{len(oi):,} rows')

    print('Loading Artemis Nifty trades...', end=' ', flush=True)
    trades = load_trades()
    early  = (trades['year'] < SPLIT_YEAR).sum()
    recent = (trades['year'] >= SPLIT_YEAR).sum()
    print(f'{len(trades)} trades  ({early} early 2019-22, {recent} recent 2023-25)')
    print(f'  P&L: mean={trades["total_pl_points"].mean():.1f} pts, '
          f'median={trades["total_pl_points"].median():.1f} pts, '
          f'winners={( trades["total_pl_points"]>0).mean()*100:.0f}%')

    print('Matching OI features to trade entries...', end=' ', flush=True)
    merged = match_oi_to_trades(trades, oi)
    n_matched = merged[RATIO_FEATURES[0]].notna().sum()
    print(f'{n_matched}/{len(merged)} matched')

    # Save joined table
    save_cols = (['entry_time','sell_expiry','entry_spot','entry_vix','period',
                  'ce_sell_strike','pe_sell_strike','ce_pl_points','pe_pl_points',
                  'total_pl_points'] + ALL_FEATURES)
    merged[save_cols].to_csv(os.path.join(OUTPUT_DIR, 'artemis_entry_oi_joined.csv'), index=False)

    # ═══════════════════════════════════════════════════════════════════════
    # 1. Correlation table — full sample
    # ═══════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 1: Spearman correlations — full sample (150 Artemis Nifty trades)')
    print_sep('═')
    print('  Ratio features (reliable):')
    print_sep()
    ct_ratio = spearman_table(merged, RATIO_FEATURES, TARGET_COLS)
    print(ct_ratio[TARGET_COLS].to_string())
    print()
    print('  Absolute OI features (⚠ year-confounded):')
    print_sep()
    ct_abs = spearman_table(merged, ABS_FEATURES, TARGET_COLS)
    print(ct_abs[TARGET_COLS].to_string())

    # ═══════════════════════════════════════════════════════════════════════
    # 2. Primary hypothesis — PCR → directional asymmetry
    # ═══════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 2: Primary hypothesis — PCR → directional P&L asymmetry')
    print_sep('═')
    print('For Artemis short strangle: high PCR (bullish) → CE at risk, PE safe.')
    print('Confirmed if pcr corr with ce_pl < 0 AND pe_pl > 0.')
    print()

    for feat in ['pcr_near', 'pcr_broad', 'wall_oi_ratio', 'wall_asym']:
        valid = merged.dropna(subset=[feat, 'ce_pl_points', 'pe_pl_points'])
        r_ce, p_ce = stats.spearmanr(valid[feat], valid['ce_pl_points'])
        r_pe, p_pe = stats.spearmanr(valid[feat], valid['pe_pl_points'])
        both_confirmed = r_ce < -0.05 and r_pe > 0.05
        either         = r_ce < -0.05 or r_pe > 0.05
        signal = ('CONFIRMED ✓' if both_confirmed else
                  'PARTIAL'    if either         else
                  'none')
        print(f'  {feat:20s}  ce_pl r={r_ce:+.3f} (p={p_ce:.3f})  '
              f'pe_pl r={r_pe:+.3f} (p={p_pe:.3f})  → {signal}')

    # ═══════════════════════════════════════════════════════════════════════
    # 3. Time-split stability
    # ═══════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 3: Time-split stability (GO/NO-GO gate)')
    print_sep('═')
    print(f'  {"Feature":20s}  {"Period":13s}  {"total_pl":>9s}  {"ce_pl":>9s}  {"pe_pl":>9s}  n')
    print_sep()
    for period_label, mask in [('early(19-22)',  merged['period']=='early(19-22)'),
                                ('recent(23-25)', merged['period']=='recent(23-25)')]:
        sub = merged[mask]
        for feat in ['pcr_near', 'wall_oi_ratio', 'wall_asym']:
            row_n = sub[feat].notna().sum()
            row_corrs = {}
            for tgt in TARGET_COLS:
                valid = sub.dropna(subset=[feat, tgt])
                if len(valid) < 5:
                    row_corrs[tgt] = np.nan
                else:
                    r, _ = stats.spearmanr(valid[feat], valid[tgt])
                    row_corrs[tgt] = round(r, 3)
            print(f'  {feat:20s}  {period_label:13s}  '
                  f'{str(row_corrs.get("total_pl_points","?")):>9s}  '
                  f'{str(row_corrs.get("ce_pl_points","?")):>9s}  '
                  f'{str(row_corrs.get("pe_pl_points","?")):>9s}  {row_n}')

    # ═══════════════════════════════════════════════════════════════════════
    # 4. Quintile P&L by feature
    # ═══════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 4: Quintile P&L by OI feature')
    print_sep('═')

    for feat in ['pcr_near', 'wall_oi_ratio', 'wall_asym', 'ce_wall_dist_pct', 'pe_wall_dist_pct']:
        qt = quintile_analysis(merged, feat, TARGET_COLS)
        print(f'\n  {feat}:')
        print(qt.to_string(col_space=14))

    # ═══════════════════════════════════════════════════════════════════════
    # 5. CE vs PE asymmetry by PCR quintile
    # ═══════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 5: CE vs PE side breakdown by PCR_near quintile')
    print_sep('═')
    print('For skew: want to see CE P&L deteriorating (Q1→Q5) and PE improving.')
    print()

    qt_pcr = quintile_analysis(merged, 'pcr_near', ['ce_pl_points', 'pe_pl_points', 'total_pl_points'])
    print(qt_pcr.to_string(col_space=16))

    # Spot vs sell strikes at entry (static positioning, not intra-week move)
    merged['ce_distance_at_entry'] = merged['ce_sell_strike'] - merged['entry_spot']
    merged['pe_distance_at_entry'] = merged['entry_spot'] - merged['pe_sell_strike']
    print()
    print('CE/PE strike distance at entry by PCR quintile (positive = OTM buffer):')
    valid_move = merged.dropna(subset=['pcr_near']).copy()
    valid_move['q_pcr'] = pd.qcut(valid_move['pcr_near'], q=5,
                                   labels=['Q1','Q2','Q3','Q4','Q5'], duplicates='drop')
    move_qt = valid_move.groupby('q_pcr', observed=False)[['ce_distance_at_entry','pe_distance_at_entry']].mean().round(1)
    print(move_qt.to_string(col_space=22))

    # ═══════════════════════════════════════════════════════════════════════
    # 6. Entry filter simulation
    # ═══════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 6: Entry filter simulation — skip bottom 20% by feature')
    print_sep('═')
    print(f'  {"Feature":22s}  {"Kept":>5s}  {"Skip":>4s}  '
          f'{"MeanPL(all)":>11s}  {"MeanPL(kept)":>12s}  {"MeanPL(skip)":>12s}  {"ΔMean":>7s}')
    print_sep()
    for feat in RATIO_FEATURES + ['total_oi']:
        if merged[feat].notna().sum() < 15:
            continue
        sim   = entry_filter_sim(merged, feat, skip_bottom=0.2)
        delta = round(sim['mean_pl_kept'] - sim['mean_pl_all'], 1)
        note  = ' *year' if feat in ABS_FEATURES else ''
        print(f'  {feat:22s}  {sim["n_kept"]:>5d}  {sim["n_skipped"]:>4d}  '
              f'{sim["mean_pl_all"]:>11.1f}  {sim["mean_pl_kept"]:>12.1f}  '
              f'{sim["skipped_mean_pl"]:>12.1f}  {delta:>+7.1f}{note}')

    # ═══════════════════════════════════════════════════════════════════════
    # 7. Skew signal summary
    # ═══════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 7: Strike skew signal summary')
    print_sep('═')
    print('For Artemis: does OI at entry predict which SIDE to protect?')
    print('Skew is buildable if CE P&L falls and PE P&L rises as PCR increases.')
    print()

    for feat in ['pcr_near', 'wall_asym', 'wall_oi_ratio']:
        valid = merged.dropna(subset=[feat])
        hi  = valid[valid[feat] > valid[feat].median()]
        lo  = valid[valid[feat] <= valid[feat].median()]
        hi_ce  = hi['ce_pl_points'].mean()
        lo_ce  = lo['ce_pl_points'].mean()
        hi_pe  = hi['pe_pl_points'].mean()
        lo_pe  = lo['pe_pl_points'].mean()
        hi_tot = hi['total_pl_points'].mean()
        lo_tot = lo['total_pl_points'].mean()
        ce_dir = 'CE worse when high ✓' if hi_ce < lo_ce else 'CE better when high ✗'
        pe_dir = 'PE better when high ✓' if hi_pe > lo_pe else 'PE worse when high ✗'
        print(f'  {feat} (high vs low halves):')
        print(f'    Total P&L: high={hi_tot:+.1f}  low={lo_tot:+.1f}  diff={hi_tot-lo_tot:+.1f}')
        print(f'    CE P&L:    high={hi_ce:+.1f}  low={lo_ce:+.1f}  diff={hi_ce-lo_ce:+.1f}  → {ce_dir}')
        print(f'    PE P&L:    high={hi_pe:+.1f}  low={lo_pe:+.1f}  diff={hi_pe-lo_pe:+.1f}  → {pe_dir}')
        print()

    print(f'Output saved to research/oi_analysis/data/artemis_entry_oi_joined.csv')


if __name__ == '__main__':
    main()
