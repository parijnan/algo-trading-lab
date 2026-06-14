"""
validate_athena_entry.py — OI signals at Athena trade entry.

For each of the 124 Athena VIX 16-25 trades:
  1. Matches the entry timestamp (10:30) to OI features for the sell expiry
  2. Correlates OI features with total, CE-side, and PE-side calendar P&L
  3. Tests the pre-registered hypothesis: PCR↑ → CE worse, PE better
  4. Reports correlations split 2020-2022 vs 2023-2026 to flag spurious signals

Two questions answered:
  A. Entry filter: does any OI feature predict a bad week?
  B. Strike skew: does OI directional bias predict which SIDE gets hurt?
     (CE side is the more interesting target — PE side already has the reactive wing)

Methodology notes:
  - ce_pl_points / pe_pl_points = pure calendar spread P&L per side (no reactive wing,
    no emergency hedge — those are in running_realised_pl and emer_pl separately)
  - Ratio/relative features (pcr_near, wall_dist_pct, wall_asym, wall_oi_ratio) are the
    primary signals. Absolute OI features (total_oi, ce_wall_oi, pe_wall_oi) are reported
    but flagged as year-confounded (OI grew 5-10x from 2020 to 2026).

Usage:
    python research/oi_analysis/validate_athena_entry.py

Requires: research/oi_analysis/data/nifty_oi_features.csv
    Build if missing: python research/oi_analysis/build_nifty_features.py --workers 4
"""

import os, sys
import numpy as np
import pandas as pd
from scipy import stats

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)

TRADE_SUMMARY   = os.path.join(_REPO_ROOT, 'athena_backtest', 'data', 'trade_summary.csv')
OI_FEATURES     = os.path.join(os.path.dirname(__file__), 'data', 'nifty_oi_features.csv')
OUTPUT_DIR      = os.path.join(os.path.dirname(__file__), 'data')

# Ratio/distance features: not confounded with year
RATIO_FEATURES = [
    'pcr_near', 'pcr_broad',
    'ce_wall_dist_pct', 'pe_wall_dist_pct',
    'wall_asym',        # = ce_wall_dist_pct - pe_wall_dist_pct
    'wall_oi_ratio',    # = pe_wall_oi / ce_wall_oi
    'max_pain_dist_pts',
]

# Absolute OI features: confounded with year (OI grew ~10x 2020→2026)
ABS_FEATURES = ['total_oi', 'ce_wall_oi', 'pe_wall_oi']

ALL_FEATURES = RATIO_FEATURES + ABS_FEATURES

TARGET_COLS = ['total_pl_points', 'ce_pl_points', 'pe_pl_points']
SPLIT_YEAR  = 2023   # 2020-2022 = early period; 2023-2026 = recent


def load_oi() -> pd.DataFrame:
    df = pd.read_csv(OI_FEATURES, parse_dates=['ts'])
    df['wall_asym']    = df['ce_wall_dist_pct'] - df['pe_wall_dist_pct']
    df['wall_oi_ratio'] = df['pe_wall_oi'] / df['ce_wall_oi']
    return df


def load_trades() -> pd.DataFrame:
    df = pd.read_csv(TRADE_SUMMARY, parse_dates=['entry_time'])
    df['emer_pl'] = pd.to_numeric(df['emer_pl'], errors='coerce').fillna(0)
    df['year'] = df['entry_time'].dt.year
    df['period'] = df['year'].apply(lambda y: 'early(20-22)' if y < SPLIT_YEAR else 'recent(23-26)')
    return df


def match_oi_to_trades(trades: pd.DataFrame, oi: pd.DataFrame) -> pd.DataFrame:
    """
    For each trade, find the OI features row at the sell_expiry and entry_time.
    Returns trades DataFrame enriched with OI features.
    """
    records = []
    for _, row in trades.iterrows():
        expiry = str(row['sell_expiry'])[:10]
        entry_ts = row['entry_time']
        sub = oi[(oi['expiry'] == expiry) & (oi['ts'] <= entry_ts)]
        if sub.empty:
            feat = {f: np.nan for f in ALL_FEATURES}
        else:
            last = sub.iloc[-1]
            feat = {f: last.get(f, np.nan) for f in ALL_FEATURES}
        feat['_lag_min'] = round((entry_ts - sub['ts'].iloc[-1]).total_seconds() / 60, 1) \
            if not sub.empty else np.nan
        records.append(feat)

    feat_df = pd.DataFrame(records, index=trades.index)
    return pd.concat([trades, feat_df], axis=1)


def spearman_table(df: pd.DataFrame, features: list, targets: list) -> pd.DataFrame:
    rows = []
    for feat in features:
        row = {'feature': feat}
        for tgt in targets:
            valid = df[[feat, tgt]].dropna()
            if len(valid) < 10:
                row[tgt] = np.nan
                row[f'{tgt}_p'] = np.nan
            else:
                r, p = stats.spearmanr(valid[feat], valid[tgt])
                row[tgt] = round(r, 3)
                row[f'{tgt}_p'] = round(p, 4)
        rows.append(row)
    return pd.DataFrame(rows).set_index('feature')


def quintile_analysis(df: pd.DataFrame, feature: str, targets: list) -> pd.DataFrame:
    valid = df.dropna(subset=[feature])
    valid = valid.copy()
    valid['q'] = pd.qcut(valid[feature], q=5, labels=['Q1','Q2','Q3','Q4','Q5'],
                         duplicates='drop')
    result = valid.groupby('q')[targets].mean().round(1)
    result.index.name = None
    result.columns = [f'mean_{t}' for t in targets]
    result['n'] = valid.groupby('q')[targets[0]].count()
    return result


def entry_filter_sim(df: pd.DataFrame, feature: str, skip_bottom: float = 0.2) -> dict:
    """Simulate skipping bottom X% of weeks by feature value. Returns P&L summary."""
    thresh = df[feature].quantile(skip_bottom)
    kept   = df[df[feature] >  thresh]
    skipped = df[df[feature] <= thresh]
    return {
        'feature':        feature,
        'threshold_pct':  f'bottom {int(skip_bottom*100)}%',
        'n_kept':         len(kept),
        'n_skipped':      len(skipped),
        'total_pl_kept':  round(kept['total_pl_points'].sum(), 1),
        'total_pl_all':   round(df['total_pl_points'].sum(), 1),
        'mean_pl_kept':   round(kept['total_pl_points'].mean(), 1),
        'mean_pl_all':    round(df['total_pl_points'].mean(), 1),
        'skipped_mean_pl': round(skipped['total_pl_points'].mean(), 1),
    }


def print_sep(char='─', width=90):
    print(char * width)


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print('Loading OI features...', end=' ', flush=True)
    oi = load_oi()
    print(f'{len(oi):,} rows, {oi["expiry"].nunique()} expiries')

    print('Loading trade summary...', end=' ', flush=True)
    trades = load_trades()
    print(f'{len(trades)} trades  '
          f'({(trades["year"]<SPLIT_YEAR).sum()} early, {(trades["year"]>=SPLIT_YEAR).sum()} recent)')

    print('Matching OI features to trade entries...', end=' ', flush=True)
    merged = match_oi_to_trades(trades, oi)
    n_matched = merged[RATIO_FEATURES[0]].notna().sum()
    print(f'{n_matched}/{len(merged)} matched')

    lag_ok = merged['_lag_min'].abs() <= 5
    if not lag_ok.all():
        print(f'  [WARN] {(~lag_ok).sum()} trades have lag > 5 min at OI match')

    # ── save enriched table ───────────────────────────────────────────────
    save_cols = ['entry_time','sell_expiry','entry_spot','entry_vix','period',
                 'ce_sell_strike','pe_sell_strike','ce_pl_points','pe_pl_points',
                 'emer_pl','total_pl_points'] + ALL_FEATURES
    merged[save_cols].to_csv(os.path.join(OUTPUT_DIR, 'athena_entry_oi_joined.csv'), index=False)

    # ═════════════════════════════════════════════════════════════════════
    # 1. Correlation table — full sample
    # ═════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 1: Spearman correlations — full sample (124 trades)')
    print_sep('═')
    print('  Ratio features (reliable — not confounded with year):')
    print_sep()
    ct_ratio = spearman_table(merged, RATIO_FEATURES, TARGET_COLS)
    print(ct_ratio[TARGET_COLS].to_string())
    print()
    print('  Absolute OI features (⚠ confounded with year — treat as exploratory):')
    print_sep()
    ct_abs = spearman_table(merged, ABS_FEATURES, TARGET_COLS)
    print(ct_abs[TARGET_COLS].to_string())

    # ═════════════════════════════════════════════════════════════════════
    # 2. Primary hypothesis — PCR → CE vs PE asymmetry
    # ═════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 2: Primary hypothesis — PCR → directional P&L asymmetry')
    print_sep('═')
    print('Pre-registered: high PCR (bullish market structure) → CE side more at risk,')
    print('                PE side safer. Confirmed if pcr corr with ce_pl < 0, pe_pl > 0.')
    print()

    for feat in ['pcr_near', 'pcr_broad', 'wall_oi_ratio', 'wall_asym']:
        valid = merged.dropna(subset=[feat, 'ce_pl_points', 'pe_pl_points'])
        r_ce, p_ce = stats.spearmanr(valid[feat], valid['ce_pl_points'])
        r_pe, p_pe = stats.spearmanr(valid[feat], valid['pe_pl_points'])
        signal = ('CONFIRMED ✓' if r_ce < -0.05 and r_pe > 0.05 else
                  'PARTIAL'   if r_ce < -0.05 or r_pe > 0.05 else
                  'none')
        print(f'  {feat:20s}  ce_pl r={r_ce:+.3f} (p={p_ce:.3f})  '
              f'pe_pl r={r_pe:+.3f} (p={p_pe:.3f})  → {signal}')

    # ═════════════════════════════════════════════════════════════════════
    # 3. Time split — early vs recent (stability check)
    # ═════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 3: Time-split stability (GO/NO-GO gate)')
    print_sep('═')
    print(f'  {"Feature":20s}  {"Period":12s}  {"total_pl":>9s}  {"ce_pl":>9s}  {"pe_pl":>9s}  n')
    print_sep()
    for period_label, mask in [('early(20-22)', merged['period']=='early(20-22)'),
                                ('recent(23-26)', merged['period']=='recent(23-26)')]:
        sub = merged[mask]
        for feat in ['pcr_near', 'wall_oi_ratio']:
            row = {'n': len(sub.dropna(subset=[feat]))}
            for tgt in TARGET_COLS:
                valid = sub.dropna(subset=[feat, tgt])
                if len(valid) < 5:
                    row[tgt] = np.nan
                else:
                    r, _ = stats.spearmanr(valid[feat], valid[tgt])
                    row[tgt] = round(r, 3)
            print(f'  {feat:20s}  {period_label:12s}  '
                  f'{str(row.get("total_pl_points","?")):>9s}  '
                  f'{str(row.get("ce_pl_points","?")):>9s}  '
                  f'{str(row.get("pe_pl_points","?")):>9s}  {row["n"]}')

    # ═════════════════════════════════════════════════════════════════════
    # 4. Quintile analysis — key features
    # ═════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 4: Quintile P&L by OI feature')
    print_sep('═')
    print('Interpretation: does ranking weeks by this feature predict P&L?')

    for feat in ['pcr_near', 'wall_oi_ratio', 'wall_asym', 'ce_wall_dist_pct', 'total_oi']:
        qt = quintile_analysis(merged, feat, TARGET_COLS)
        note = '  [⚠ year-confounded]' if feat in ABS_FEATURES else ''
        print(f'\n  {feat}{note}:')
        print(qt.to_string(col_space=14))

    # ═════════════════════════════════════════════════════════════════════
    # 5. CE vs PE side — which side drives losses by OI quintile
    # ═════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 5: CE vs PE side breakdown — where do losses originate?')
    print_sep('═')
    print('For each PCR quintile: mean ce_pl, pe_pl, emer_pl, total_pl')
    print('(PE already has reactive wing protection; focus on CE side exposure)\n')

    qt_pcr = quintile_analysis(merged, 'pcr_near',
                                ['ce_pl_points', 'pe_pl_points', 'emer_pl', 'total_pl_points'])
    print(qt_pcr.to_string(col_space=16))

    print()
    print('Spot move asymmetry by PCR quintile:')
    merged['ce_pressure'] = merged['max_spot'] - merged['ce_sell_strike']
    merged['pe_pressure'] = merged['pe_sell_strike'] - merged['min_spot']
    qt_move = quintile_analysis(merged, 'pcr_near', ['ce_pressure', 'pe_pressure'])
    print(qt_move.to_string(col_space=16))

    # ═════════════════════════════════════════════════════════════════════
    # 6. Entry filter simulation — top candidate features
    # ═════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 6: Entry filter simulation — skip bottom quintile (20%) by feature')
    print_sep('═')
    print(f'  {"Feature":22s}  {"Kept":>5s}  {"Skip":>4s}  '
          f'{"MeanPL(all)":>11s}  {"MeanPL(kept)":>12s}  {"MeanPL(skip)":>12s}  {"ΔMean":>7s}')
    print_sep()
    for feat in RATIO_FEATURES + ['total_oi']:
        if merged[feat].notna().sum() < 20:
            continue
        sim = entry_filter_sim(merged, feat, skip_bottom=0.2)
        delta = round(sim['mean_pl_kept'] - sim['mean_pl_all'], 1)
        note = ' *year' if feat in ABS_FEATURES else ''
        print(f'  {feat:22s}  {sim["n_kept"]:>5d}  {sim["n_skipped"]:>4d}  '
              f'{sim["mean_pl_all"]:>11.1f}  {sim["mean_pl_kept"]:>12.1f}  '
              f'{sim["skipped_mean_pl"]:>12.1f}  {delta:>+7.1f}{note}')

    # ═════════════════════════════════════════════════════════════════════
    # 7. Strike skew signal summary
    # ═════════════════════════════════════════════════════════════════════
    print()
    print_sep('═')
    print('SECTION 7: Strike skew signal summary')
    print_sep('═')
    print('For skew to be buildable, we need the directional signal to be:')
    print('  (a) Statistically present (r meaningful + same sign in both time periods)')
    print('  (b) Asymmetric between CE and PE sides')
    print('  (c) Not fully explained by the reactive wing (pe_pl is CE-only)')
    print()

    # PCR → ce vs pe skew signal
    for feat in ['pcr_near', 'wall_oi_ratio']:
        valid = merged.dropna(subset=[feat])
        hi = valid[valid[feat] > valid[feat].median()]
        lo = valid[valid[feat] <= valid[feat].median()]
        hi_ce = hi['ce_pl_points'].mean()
        lo_ce = lo['ce_pl_points'].mean()
        hi_pe = hi['pe_pl_points'].mean()
        lo_pe = lo['pe_pl_points'].mean()
        print(f'  {feat} (high vs low halves):')
        print(f'    CE P&L:  high={hi_ce:+.1f}  low={lo_ce:+.1f}  diff={hi_ce-lo_ce:+.1f}  '
              f'→ {"CE worse when high (skew opportunity)" if hi_ce < lo_ce else "CE better when high (no skew)"}')
        print(f'    PE P&L:  high={hi_pe:+.1f}  low={lo_pe:+.1f}  diff={hi_pe-lo_pe:+.1f}  '
              f'→ {"PE better when high (consistent)" if hi_pe > lo_pe else "PE worse when high"}')
        print()

    print('Outputs saved to research/oi_analysis/data/athena_entry_oi_joined.csv')


if __name__ == '__main__':
    main()
