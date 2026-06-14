"""
validate_artemis_intraday.py

Tests whether INTRADAY OI wall proximity and breach predict adverse leg
outcomes in Artemis Nifty trades (84 traded weeks, 2023-09 to 2025-08).

The entry-level PCR signal for Artemis was shown to be weak and to collapse
post-2023 (see validate_artemis_entry.py). This script tests whether the
intraday *path* of spot relative to OI walls carries incremental information
beyond what is visible at entry.

Key gate question: does min-intraday wall distance outperform entry wall
distance in predicting CE/PE SL hits? If not, all four downstream use cases
(entry strikes, skew, SL triggers, adjustments) die here.

Data:
  - OI features: research/oi_analysis/data/nifty_oi_features.csv (5-min bars)
  - Trade logs:  artemis_backtest/data/trade_logs_nifty/ (1-min bars, 102 files)
  - Outcomes:    artemis_backtest/data/trade_summary_nifty_rerun.csv
"""

import os
import sys
import glob
import numpy as np
import pandas as pd
from scipy import stats

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
OI_FEATURES = os.path.join(REPO, 'research', 'oi_analysis', 'data', 'nifty_oi_features.csv')
TRADE_SUMMARY = os.path.join(REPO, 'artemis_backtest', 'data', 'trade_summary_nifty_rerun.csv')
LOG_DIR = os.path.join(REPO, 'artemis_backtest', 'data', 'trade_logs_nifty')
OUT_CSV = os.path.join(REPO, 'research', 'oi_analysis', 'data', 'artemis_intraday_oi_joined.csv')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ic(a, b):
    """Spearman IC, (r, p) with NaN handling."""
    mask = ~(np.isnan(a) | np.isnan(b))
    if mask.sum() < 10:
        return np.nan, np.nan
    return stats.spearmanr(a[mask], b[mask])


def point_biserial(binary, continuous):
    """Point-biserial correlation of binary event with continuous feature."""
    mask = ~np.isnan(continuous)
    return stats.pointbiserialr(binary[mask], continuous[mask])


def chi2_2x2(binary_feature, binary_outcome):
    """Chi-square for 2x2 contingency."""
    ct = pd.crosstab(binary_feature, binary_outcome)
    if ct.shape != (2, 2):
        return np.nan, np.nan
    chi2, p, *_ = stats.chi2_contingency(ct)
    return chi2, p


def fmt(r, p):
    stars = '***' if p < 0.001 else '**' if p < 0.01 else '*' if p < 0.05 else ''
    return f'{r:+.3f}{stars}'


def print_section(title):
    print()
    print('=' * 70)
    print(title)
    print('=' * 70)


# ---------------------------------------------------------------------------
# 1. Load data
# ---------------------------------------------------------------------------
print_section('1. Loading data')

oi = pd.read_csv(OI_FEATURES, parse_dates=['ts'])
print(f'  OI features: {len(oi):,} rows, {oi["expiry"].nunique()} expiries')

summary = pd.read_csv(TRADE_SUMMARY, parse_dates=['expiry', 'entry_time'])
summary = summary[summary['week_outcome'] == 'traded'].copy()
summary['expiry_date'] = summary['expiry'].dt.date.astype(str)
summary_logs = summary[summary['expiry_date'] >= '2023-09-07'].copy()
print(f'  Trade summary (all traded): {len(summary)} weeks')
print(f'  Trade summary (log period 2023-09+): {len(summary_logs)} weeks')

log_files = sorted(glob.glob(os.path.join(LOG_DIR, 'trade_*.csv')))
print(f'  Log files found: {len(log_files)}')

# ---------------------------------------------------------------------------
# 2. Per-week feature extraction
# ---------------------------------------------------------------------------
print_section('2. Building per-week intraday features')

records = []

for _, row in summary_logs.iterrows():
    expiry = row['expiry_date']

    # Find the log file for this expiry
    matches = [f for f in log_files if expiry in f]
    if not matches:
        continue
    log_path = matches[0]

    # Load trade log
    log = pd.read_csv(log_path, parse_dates=['time_stamp'])

    # OI features for this expiry
    oi_exp = oi[oi['expiry'] == expiry][['ts', 'ce_wall_strike', 'ce_wall_oi',
                                          'ce_wall_dist_pct', 'pe_wall_strike',
                                          'pe_wall_oi', 'pe_wall_dist_pct',
                                          'max_pain_strike', 'max_pain_dist_pts',
                                          'pcr_near', 'pcr_broad']].sort_values('ts')

    if oi_exp.empty:
        continue

    # Join: for each 1-min bar, take last OI observation <= bar time (no lookahead)
    merged = pd.merge_asof(log.sort_values('time_stamp'), oi_exp,
                           left_on='time_stamp', right_on='ts', direction='backward')

    # Derive: wall distance vs sell strike (how far is the OI wall from our short strike)
    ce_sell = row['ce_sell_strike']
    pe_sell = row['pe_sell_strike']
    entry_spot = row['entry_spot']

    # Entry-bar values (first row of merged)
    entry = merged.iloc[0]
    entry_ce_wall_dist = entry['ce_wall_dist_pct']
    entry_pe_wall_dist = entry['pe_wall_dist_pct']
    entry_max_pain_dist = entry['max_pain_dist_pts']
    entry_pcr_near = entry['pcr_near']
    entry_pcr_broad = entry['pcr_broad']
    entry_ce_wall_strike = entry['ce_wall_strike']
    entry_pe_wall_strike = entry['pe_wall_strike']

    # CE wall relative to CE sell strike (positive = wall above sell strike = buffer exists)
    ce_wall_buffer_pct = (entry_ce_wall_strike - ce_sell) / ce_sell * 100
    # PE wall relative to PE sell strike (positive = wall below sell strike = buffer exists)
    pe_wall_buffer_pct = (pe_sell - entry_pe_wall_strike) / pe_sell * 100

    # Intraday minimums (lowest value = closest approach or breach)
    min_ce_wall_dist = merged['ce_wall_dist_pct'].min()
    min_pe_wall_dist = merged['pe_wall_dist_pct'].min()
    min_max_pain_dist = merged['max_pain_dist_pts'].min()
    max_max_pain_dist = merged['max_pain_dist_pts'].max()

    # Breach events (wall crossed intraday)
    any_ce_breach = (merged['ce_wall_dist_pct'] < 0).any()
    any_pe_breach = (merged['pe_wall_dist_pct'] < 0).any()

    # Outcome flags
    ce_sl_hit = int(row['ce_exit_reason'] in ['option_sl', 'index_sl'])
    pe_sl_hit = int(row['pe_exit_reason'] in ['option_sl', 'index_sl'])
    ce_elm = int(row['ce_exit_reason'] == 'elm')
    pe_elm = int(row['pe_exit_reason'] == 'elm')

    records.append({
        'expiry': expiry,
        'entry_time': row['entry_time'],
        'entry_spot': entry_spot,
        'ce_sell_strike': ce_sell,
        'pe_sell_strike': pe_sell,
        # Entry-bar OI features
        'entry_ce_wall_dist_pct': entry_ce_wall_dist,
        'entry_pe_wall_dist_pct': entry_pe_wall_dist,
        'entry_max_pain_dist_pts': entry_max_pain_dist,
        'entry_pcr_near': entry_pcr_near,
        'entry_pcr_broad': entry_pcr_broad,
        'entry_ce_wall_strike': entry_ce_wall_strike,
        'entry_pe_wall_strike': entry_pe_wall_strike,
        # Wall-vs-sell-strike alignment
        'ce_wall_buffer_pct': ce_wall_buffer_pct,
        'pe_wall_buffer_pct': pe_wall_buffer_pct,
        # Intraday path features
        'min_ce_wall_dist_pct': min_ce_wall_dist,
        'min_pe_wall_dist_pct': min_pe_wall_dist,
        'min_max_pain_dist_pts': min_max_pain_dist,
        'max_max_pain_dist_pts': max_max_pain_dist,
        # Breach events
        'any_ce_breach': int(any_ce_breach),
        'any_pe_breach': int(any_pe_breach),
        # Outcomes
        'ce_pl_points': row['ce_pl_points'],
        'pe_pl_points': row['pe_pl_points'],
        'total_pl_points': row['total_pl_points'],
        'ce_sl_hit': ce_sl_hit,
        'pe_sl_hit': pe_sl_hit,
    })

df = pd.DataFrame(records)
df.to_csv(OUT_CSV, index=False)
print(f'  Joined {len(df)} trade weeks → {OUT_CSV}')
print(f'  CE SL hit rate: {df["ce_sl_hit"].mean():.1%}')
print(f'  PE SL hit rate: {df["pe_sl_hit"].mean():.1%}')
print(f'  CE wall breach rate: {df["any_ce_breach"].mean():.1%}')
print(f'  PE wall breach rate: {df["any_pe_breach"].mean():.1%}')


# ---------------------------------------------------------------------------
# 3. Entry-time wall features vs outcomes  (baseline — compare to PCR)
# ---------------------------------------------------------------------------
print_section('3. Entry-time wall features vs outcomes (Spearman IC)')

entry_features = [
    ('entry_ce_wall_dist_pct', 'CE wall dist at entry'),
    ('entry_pe_wall_dist_pct', 'PE wall dist at entry'),
    ('entry_max_pain_dist_pts', 'Max pain dist at entry'),
    ('entry_pcr_near',          'PCR near at entry'),
    ('entry_pcr_broad',         'PCR broad at entry'),
    ('ce_wall_buffer_pct',      'CE wall above sell strike %'),
    ('pe_wall_buffer_pct',      'PE wall below sell strike %'),
]

targets_cont = [
    ('ce_pl_points', 'CE P&L'),
    ('pe_pl_points', 'PE P&L'),
    ('total_pl_points', 'Total P&L'),
]

print(f'\n  {"Feature":<32} {"CE P&L":>12} {"PE P&L":>12} {"Total P&L":>12}')
print('  ' + '-' * 70)
for feat, label in entry_features:
    vals = df[feat].values.astype(float)
    row_parts = [f'  {label:<32}']
    for tgt, _ in targets_cont:
        r, p = ic(vals, df[tgt].values.astype(float))
        row_parts.append(f'{fmt(r, p):>12}')
    print(''.join(row_parts))

print('\n  Note: * p<0.05  ** p<0.01  *** p<0.001  (n=84)')


# ---------------------------------------------------------------------------
# 4. Intraday minimum wall distance vs outcomes
# ---------------------------------------------------------------------------
print_section('4. Intraday minimum wall distance vs outcomes (Spearman IC)')

intraday_features = [
    ('min_ce_wall_dist_pct',  'Min CE wall dist (intraday)'),
    ('min_pe_wall_dist_pct',  'Min PE wall dist (intraday)'),
    ('min_max_pain_dist_pts', 'Min max pain dist (intraday)'),
    ('max_max_pain_dist_pts', 'Max max pain dist (intraday)'),
]

print(f'\n  {"Feature":<34} {"CE P&L":>12} {"PE P&L":>12} {"Total P&L":>12}')
print('  ' + '-' * 72)
for feat, label in intraday_features:
    vals = df[feat].values.astype(float)
    row_parts = [f'  {label:<34}']
    for tgt, _ in targets_cont:
        r, p = ic(vals, df[tgt].values.astype(float))
        row_parts.append(f'{fmt(r, p):>12}')
    print(''.join(row_parts))

print('\n  Comparison — entry vs min (should min IC dominate if path matters):')
for feat_entry, feat_min, label in [
    ('entry_ce_wall_dist_pct', 'min_ce_wall_dist_pct', 'CE wall dist'),
    ('entry_pe_wall_dist_pct', 'min_pe_wall_dist_pct', 'PE wall dist'),
]:
    r_entry, p_entry = ic(df[feat_entry].values.astype(float), df['total_pl_points'].values.astype(float))
    r_min, p_min = ic(df[feat_min].values.astype(float), df['total_pl_points'].values.astype(float))
    print(f'  {label}: entry IC={fmt(r_entry,p_entry)}, intraday-min IC={fmt(r_min,p_min)}')


# ---------------------------------------------------------------------------
# 5. Wall breach vs SL hit
# ---------------------------------------------------------------------------
print_section('5. Wall breach events vs SL hit (chi-square + point-biserial)')

print('\n  CE wall breach → CE SL hit:')
chi2, p = chi2_2x2(df['any_ce_breach'].values, df['ce_sl_hit'].values)
ct = pd.crosstab(df['any_ce_breach'], df['ce_sl_hit'],
                 rownames=['CE breach'], colnames=['CE SL hit'])
print(ct.to_string(index=True))
if not np.isnan(chi2):
    print(f'  chi2={chi2:.2f}, p={p:.4f}')

print('\n  PE wall breach → PE SL hit:')
chi2, p = chi2_2x2(df['any_pe_breach'].values, df['pe_sl_hit'].values)
ct = pd.crosstab(df['any_pe_breach'], df['pe_sl_hit'],
                 rownames=['PE breach'], colnames=['PE SL hit'])
print(ct.to_string(index=True))
if not np.isnan(chi2):
    print(f'  chi2={chi2:.2f}, p={p:.4f}')

# Point-biserial: min wall dist predicts SL hit?
print('\n  Min wall distance → SL hit (point-biserial r):')
r_ce, p_ce = point_biserial(df['ce_sl_hit'].values,
                              df['min_ce_wall_dist_pct'].values.astype(float))
r_pe, p_pe = point_biserial(df['pe_sl_hit'].values,
                              df['min_pe_wall_dist_pct'].values.astype(float))
print(f'  min_ce_wall_dist → ce_sl_hit: r={r_ce:+.3f}, p={p_ce:.4f}')
print(f'  min_pe_wall_dist → pe_sl_hit: r={r_pe:+.3f}, p={p_pe:.4f}')


# ---------------------------------------------------------------------------
# 6. CE wall alignment vs CE sell strike
# ---------------------------------------------------------------------------
print_section('6. CE wall alignment vs CE sell strike')

print('\n  Distribution of ce_wall_buffer_pct (wall above sell strike, %):')
print(df['ce_wall_buffer_pct'].describe()[['min','25%','50%','75%','max']].round(2).to_string())

# Bin into: wall well above (>1%), close (-1% to +1%), wall below (<-1%)
bins = [-999, -1, 1, 999]
labels = ['wall below sell (<-1%)', 'wall near sell (-1 to +1%)', 'wall above sell (>+1%)']
df['ce_wall_pos'] = pd.cut(df['ce_wall_buffer_pct'], bins=bins, labels=labels)
print('\n  CE P&L by wall position:')
grp = df.groupby('ce_wall_pos', observed=True)['ce_pl_points'].agg(['mean','count'])
grp.columns = ['mean CE P&L', 'n']
print(grp.to_string())

print('\n  SL hit rate by wall position:')
grp2 = df.groupby('ce_wall_pos', observed=True)['ce_sl_hit'].agg(['mean','count'])
grp2.columns = ['CE SL hit rate', 'n']
print(grp2.to_string())


# ---------------------------------------------------------------------------
# 7. Max pain analysis
# ---------------------------------------------------------------------------
print_section('7. Max pain analysis')

print('\n  max_pain_dist_pts at entry (spot - max_pain, pts):')
print(df['entry_max_pain_dist_pts'].describe()[['min','25%','50%','75%','max']].round(1).to_string())

# Quintile on entry max pain distance vs outcomes
df['mp_q'] = pd.qcut(df['entry_max_pain_dist_pts'], q=5, labels=['Q1','Q2','Q3','Q4','Q5'],
                      duplicates='drop')
print('\n  Total P&L by max pain distance quintile (Q1=spot far below max pain, Q5=far above):')
grp_mp = df.groupby('mp_q', observed=True)['total_pl_points'].agg(['mean','count'])
grp_mp.columns = ['mean Total P&L', 'n']
print(grp_mp.to_string())

r, p = ic(df['entry_max_pain_dist_pts'].values.astype(float),
          df['total_pl_points'].values.astype(float))
print(f'\n  Spearman IC (entry max pain dist → total P&L): r={r:+.3f}, p={p:.4f}')

# Does min intraday max pain dist (spot closest to max pain from above) predict better outcomes?
r_min, p_min = ic(df['min_max_pain_dist_pts'].values.astype(float),
                  df['total_pl_points'].values.astype(float))
r_max, p_max = ic(df['max_max_pain_dist_pts'].values.astype(float),
                  df['total_pl_points'].values.astype(float))
print(f'  Spearman IC (min intraday max pain dist → total P&L): r={r_min:+.3f}, p={p_min:.4f}')
print(f'  Spearman IC (max intraday max pain dist → total P&L): r={r_max:+.3f}, p={p_max:.4f}')


# ---------------------------------------------------------------------------
# 8. Year split (2023-2024 vs 2025)
# ---------------------------------------------------------------------------
print_section('8. Year split: 2023-2024 vs 2025')

df['year'] = pd.to_datetime(df['expiry']).dt.year
early = df[df['year'] <= 2024]
late  = df[df['year'] == 2025]
print(f'  2023-2024: {len(early)} weeks   |   2025: {len(late)} weeks')

split_features = [
    ('min_ce_wall_dist_pct',    'Min CE wall dist'),
    ('min_pe_wall_dist_pct',    'Min PE wall dist'),
    ('entry_ce_wall_dist_pct',  'Entry CE wall dist'),
    ('entry_pe_wall_dist_pct',  'Entry PE wall dist'),
    ('entry_pcr_near',          'Entry PCR near'),
]
print(f'\n  {"Feature":<28} {"2023-24 CE P&L":>16} {"2025 CE P&L":>14} {"2023-24 PE P&L":>16} {"2025 PE P&L":>14}')
print('  ' + '-' * 90)
for feat, label in split_features:
    row_parts = [f'  {label:<28}']
    for tgt in ['ce_pl_points', 'pe_pl_points']:
        r_e, p_e = ic(early[feat].values.astype(float), early[tgt].values.astype(float))
        r_l, p_l = ic(late[feat].values.astype(float), late[tgt].values.astype(float))
        row_parts.append(f'{fmt(r_e,p_e):>16}')
        row_parts.append(f'{fmt(r_l,p_l):>14}')
    print(''.join(row_parts))


# ---------------------------------------------------------------------------
# 9. Summary
# ---------------------------------------------------------------------------
print_section('9. Summary — gate assessment')

print("""
  The four candidate use cases for OI wall features in Artemis production:

  1. Entry strike placement (near wall):
     Gate: entry wall dist vs ce/pe P&L IC must be significant and stable.
     Result: see §3.

  2. Asymmetric skew (wall asymmetry → unequal CE/PE distance):
     Gate: ce_wall_buffer_pct vs CE P&L must be significant.
     Result: see §6.

  3. SL triggers (tighten on wall approach / breach):
     Gate: intraday min wall dist vs SL hit must be significant and
           must dominate the entry-only signal (see §4 comparison).
     Result: see §4 and §5.

  4. Adjustment triggers (near max pain → roll / adjust):
     Gate: entry or intraday max pain dist vs total P&L must be significant.
     Result: see §7.

  n=84 (2023-2025); critical IC threshold for p<0.05: |r| > 0.21
  n split: ~57 early (2023-24) / ~27 late (2025); each requires |r| > 0.26 / 0.38
""")
