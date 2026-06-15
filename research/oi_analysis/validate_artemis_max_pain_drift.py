"""
validate_artemis_max_pain_drift.py

Tests whether max pain DRIFT — the migration of the max pain strike during the
trade week — carries independent predictive power for Artemis Nifty outcomes.

Motivation: §10.4 found that the full-week max_pain_dist (spot − max_pain_strike)
extremum has very strong IC (r=−0.665*** for max_max_pain_dist vs PE P&L). The
question is whether the max pain STRIKE itself moving (max_pain_delta) or the
max pain DISTANCE at a fixed midweek point (max_pain_dist at Tue EOD) provides
an actionable, forward-looking signal.

Two distinct features tested in parallel:

  1. max_pain_delta = max_pain_strike(t) − max_pain_strike(entry)
     - The OI strike consensus migrating up/down during the week
     - Interpretation: positive delta = writers now protecting higher strikes (bullish OI)

  2. max_pain_dist = spot(t) − max_pain_strike(t)
     - How far spot currently sits above/below the "fair value" strike
     - Interpretation: high positive = spot above OI consensus = PE exposed

Gate question: do either of these features at a fixed midweek observation point
(Tuesday EOD) predict final P&L after controlling for spot direction?

Data:
  - OI features:   research/oi_analysis/data/nifty_oi_features.csv (5-min bars)
  - Joined entry:  research/oi_analysis/data/artemis_intraday_oi_joined.csv (84 weeks)
"""

import os
import numpy as np
import pandas as pd
from scipy import stats

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
OI_FEATURES     = os.path.join(REPO, 'research', 'oi_analysis', 'data', 'nifty_oi_features.csv')
INTRADAY_JOINED = os.path.join(REPO, 'research', 'oi_analysis', 'data', 'artemis_intraday_oi_joined.csv')
OUT_CSV         = os.path.join(REPO, 'research', 'oi_analysis', 'data', 'artemis_max_pain_drift_joined.csv')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ic(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    mask = ~(np.isnan(a) | np.isnan(b))
    if mask.sum() < 10:
        return np.nan, np.nan
    return stats.spearmanr(a[mask], b[mask])


def fmt(r, p):
    stars = '***' if p < 0.001 else '**' if p < 0.01 else '*' if p < 0.05 else ''
    return f'{r:+.3f}{stars}'


def print_section(title):
    print()
    print('=' * 72)
    print(title)
    print('=' * 72)


def partial_ic(x, y, z):
    """Partial Spearman IC of x vs y controlling for z (rank residual approach)."""
    mask = ~(np.isnan(x) | np.isnan(y) | np.isnan(z))
    if mask.sum() < 10:
        return np.nan, np.nan
    rx = stats.rankdata(x[mask]).astype(float)
    ry = stats.rankdata(y[mask]).astype(float)
    rz = stats.rankdata(z[mask]).astype(float)
    def resid(a, b):
        slope = np.cov(a, b)[0, 1] / np.var(b)
        return a - slope * b
    return stats.pearsonr(resid(rx, rz), resid(ry, rz))


def next_bday(d):
    d = pd.Timestamp(d) + pd.Timedelta(days=1)
    while d.weekday() >= 5:
        d += pd.Timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# 1. Load data
# ---------------------------------------------------------------------------
print_section('1. Loading data')

oi   = pd.read_csv(OI_FEATURES, parse_dates=['ts'])
base = pd.read_csv(INTRADAY_JOINED, parse_dates=['entry_time'])
print(f'  OI features: {len(oi):,} rows, {oi["expiry"].nunique()} expiries')
print(f'  Intraday-joined base: {len(base)} trade weeks')


# ---------------------------------------------------------------------------
# 2. Per-week max pain drift feature extraction
# ---------------------------------------------------------------------------
print_section('2. Building per-week max pain drift features')

records = []

for _, row in base.iterrows():
    expiry = row['expiry'] if isinstance(row['expiry'], str) else str(row['expiry'])[:10]
    oi_exp = oi[oi['expiry'] == expiry].sort_values('ts').reset_index(drop=True)
    if oi_exp.empty:
        continue
    entry_ts = pd.Timestamp(row['entry_time'])
    entry_mp = oi_exp.loc[(oi_exp['ts'] - entry_ts).abs().idxmin(), 'max_pain_strike']
    bars     = oi_exp[oi_exp['ts'] >= entry_ts].copy()
    if bars.empty:
        continue

    # Full-week stats
    mp_delta_full  = bars.iloc[-1]['max_pain_strike'] - entry_mp
    min_mp_delta   = (bars['max_pain_strike'] - entry_mp).min()
    max_mp_delta   = (bars['max_pain_strike'] - entry_mp).max()
    min_mp_dist    = bars['max_pain_dist_pts'].min()   # spot most below max_pain
    max_mp_dist    = bars['max_pain_dist_pts'].max()   # spot most above max_pain
    entry_mp_dist  = bars.iloc[0]['max_pain_dist_pts']

    def obs_at(offset_bdays):
        """Max pain delta, dist, and spot at entry + offset_bdays trading days at 15:30."""
        obs_date = entry_ts.date()
        for _ in range(offset_bdays):
            obs_date = next_bday(obs_date).date()
        obs_ts = pd.Timestamp(f'{obs_date} 15:30:00')
        sub = bars[bars['ts'] <= obs_ts]
        if sub.empty:
            return np.nan, np.nan, np.nan
        r = sub.iloc[-1]
        return (r['max_pain_strike'] - entry_mp), r['max_pain_dist_pts'], r['spot']

    d0_delta, d0_dist, d0_spot = obs_at(0)
    d1_delta, d1_dist, d1_spot = obs_at(1)
    d2_delta, d2_dist, d2_spot = obs_at(2)
    d3_delta, d3_dist, d3_spot = obs_at(3)

    spot_move_d1 = (d1_spot - row['entry_spot']) if not np.isnan(d1_spot) else np.nan

    records.append({
        'expiry':          expiry,
        'entry_year':      entry_ts.year,
        'entry_spot':      row['entry_spot'],
        'entry_mp':        entry_mp,
        'entry_mp_dist':   entry_mp_dist,
        # Full-week stats
        'mp_delta_full':   mp_delta_full,
        'min_mp_delta':    min_mp_delta,
        'max_mp_delta':    max_mp_delta,
        'min_mp_dist':     min_mp_dist,
        'max_mp_dist':     max_mp_dist,
        # Per-day observations (d0=Mon EOD, d1=Tue, d2=Wed, d3=Thu)
        'mp_delta_d0': d0_delta, 'mp_dist_d0': d0_dist,
        'mp_delta_d1': d1_delta, 'mp_dist_d1': d1_dist, 'spot_move_d1': spot_move_d1,
        'mp_delta_d2': d2_delta, 'mp_dist_d2': d2_dist,
        'mp_delta_d3': d3_delta, 'mp_dist_d3': d3_dist,
        # Outcomes
        'ce_pl':    row['ce_pl_points'],
        'pe_pl':    row['pe_pl_points'],
        'total_pl': row['total_pl_points'],
        'ce_sl':    row['ce_sl_hit'],
        'pe_sl':    row['pe_sl_hit'],
    })

df = pd.DataFrame(records)
df.to_csv(OUT_CSV, index=False)
print(f'  Built {len(df)} trade weeks → {OUT_CSV}')
n_2023_24 = (df['entry_year'] <= 2024).sum()
n_2025    = (df['entry_year'] == 2025).sum()
print(f'  Year split: {n_2023_24} weeks (2023-2024), {n_2025} weeks (2025)')


# ---------------------------------------------------------------------------
# 3. Max pain migration statistics
# ---------------------------------------------------------------------------
print_section('3. Max pain migration statistics')
print('  Note: max_pain_dist = spot − max_pain_strike (positive = spot above max pain)')
print()

for col, label in [
    ('mp_delta_full', 'MP strike delta (full week, pts)'),
    ('min_mp_delta',  'MP strike min delta (most downward, pts)'),
    ('max_mp_delta',  'MP strike max delta (most upward, pts)'),
    ('mp_delta_d1',   'MP strike delta at Tue EOD (pts)'),
    ('entry_mp_dist', 'MP dist at entry (spot − max_pain, pts)'),
    ('mp_dist_d1',    'MP dist at Tue EOD (pts)'),
    ('min_mp_dist',   'MP dist min during week (spot furthest below max_pain)'),
    ('max_mp_dist',   'MP dist max during week (spot furthest above max_pain)'),
]:
    s = df[col].dropna()
    print(f'  {label}')
    print(f'    mean={s.mean():+.0f}  median={s.median():+.0f}  std={s.std():.0f}  '
          f'p10={s.quantile(0.10):+.0f}  p90={s.quantile(0.90):+.0f}')

pct_up = (df['mp_delta_full'] > 0).mean()
print(f'\n  MP strike moved UP (with spot) in {pct_up:.0%} of weeks')
pct_spot_above = (df['mp_dist_d1'] > 0).mean()
print(f'  Spot ABOVE max pain at Tue EOD in {pct_spot_above:.0%} of weeks')


# ---------------------------------------------------------------------------
# 4. Full-window IC — max pain delta/dist vs leg P&L
# ---------------------------------------------------------------------------
print_section('4. Full-window IC — max pain delta and dist vs leg P&L (Spearman)')
print('  NOTE: full-week extrema are computed after the fact — potentially contemporaneous.')
print()

targets = [
    ('ce_pl',    'CE P&L'),
    ('pe_pl',    'PE P&L'),
    ('total_pl', 'Total P&L'),
]
features_full = [
    ('min_mp_delta',  'Min MP delta (most downward)'),
    ('max_mp_delta',  'Max MP delta (most upward)'),
    ('mp_delta_full', 'MP delta end-vs-entry'),
    ('min_mp_dist',   'Min MP dist (spot most below max_pain)'),
    ('max_mp_dist',   'Max MP dist (spot most above max_pain)'),
]

print(f'  {"Feature":<40} {"CE P&L":>12} {"PE P&L":>12} {"Total P&L":>12}')
print('  ' + '-' * 78)
for col, label in features_full:
    vals = df[col].values.astype(float)
    row_out = [f'  {label:<40}']
    for tgt, _ in targets:
        r, p = ic(vals, df[tgt].values.astype(float))
        row_out.append(f'{fmt(r, p):>12}')
    print(''.join(row_out))

print('\n  * p<0.05  ** p<0.01  *** p<0.001  (n=84)')


# ---------------------------------------------------------------------------
# 5. Critical test: does max pain drift add anything beyond spot direction?
# ---------------------------------------------------------------------------
print_section('5. Critical test: does max pain drift add anything beyond spot?')
print('  Max pain is often described as the "fair value" strike toward which spot')
print('  is pulled. If so, max_pain_delta should closely track spot — and after')
print('  controlling for spot, add no independent signal.')
print()

spot_arr     = df['spot_move_d1'].values.astype(float)
mp_delta_arr = df['mp_delta_d1'].values.astype(float)
mp_dist_arr  = df['mp_dist_d1'].values.astype(float)

r_delta_vs_spot, p1 = ic(mp_delta_arr, spot_arr)
r_dist_vs_spot,  p2 = ic(mp_dist_arr,  spot_arr)
print(f'  MP strike delta (Tue EOD) vs spot move to Tue EOD: r={fmt(r_delta_vs_spot, p1)}')
print(f'  MP dist         (Tue EOD) vs spot move to Tue EOD: r={fmt(r_dist_vs_spot, p2)}')
print()
print('  PARTIAL IC (controlling for spot move to Tue EOD):')
print(f'  {"Feature":<36} {"CE P&L":>10} {"PE P&L":>10} {"Total P&L":>10}')
print('  ' + '-' * 68)

for arr, label in [(mp_delta_arr, 'MP strike delta (Tue EOD)'),
                   (mp_dist_arr,  'MP dist (Tue EOD)')]:
    rce, pce   = partial_ic(arr, df['ce_pl'].values.astype(float),    spot_arr)
    rpe, ppe   = partial_ic(arr, df['pe_pl'].values.astype(float),    spot_arr)
    rtot, ptot = partial_ic(arr, df['total_pl'].values.astype(float), spot_arr)
    print(f'  {label:<36} {fmt(rce, pce):>10} {fmt(rpe, ppe):>10} {fmt(rtot, ptot):>10}')

print()
print('  Interpretation guide:')
print('    r ≈ 0 after spot control → signal is PURELY tracking spot direction')
print('    r significant after spot control → INDEPENDENT OI repositioning signal')


# ---------------------------------------------------------------------------
# 6. Fixed-time IC and year-split stability
# ---------------------------------------------------------------------------
print_section('6. Fixed-time IC at Tue EOD — raw correlation and year-split')
print()

features_d1 = [
    ('mp_delta_d1', 'MP strike delta at Tue EOD'),
    ('mp_dist_d1',  'MP dist at Tue EOD'),
]

print(f'  {"Feature":<38} {"CE P&L":>12} {"PE P&L":>12} {"Total P&L":>12}')
print('  ' + '-' * 76)
for col, label in features_d1:
    vals = df[col].values.astype(float)
    row_out = [f'  {label:<38}']
    for tgt, _ in targets:
        r, p = ic(vals, df[tgt].values.astype(float))
        row_out.append(f'{fmt(r, p):>12}')
    print(''.join(row_out))

print(f'\n  n={df["mp_delta_d1"].notna().sum()}')
print()
print('  Year-split stability (2023-2024 vs 2025):')
print()

for col, label in features_d1:
    for tgt, tgt_label in targets:
        early = df[df['entry_year'] <= 2024]
        late  = df[df['entry_year'] == 2025]
        r_a, p_a = ic(df[col].values.astype(float), df[tgt].values.astype(float))
        r_e, p_e = ic(early[col].values.astype(float), early[tgt].values.astype(float))
        r_l, p_l = ic(late[col].values.astype(float), late[tgt].values.astype(float))
        print(f'  {label} → {tgt_label}:')
        print(f'    All (n={len(df):2d}): {fmt(r_a, p_a):>10}   '
              f'2023-24 (n={len(early):2d}): {fmt(r_e, p_e):>10}   '
              f'2025 (n={len(late):2d}): {fmt(r_l, p_l):>10}')
    print()


# ---------------------------------------------------------------------------
# 7. Intraday timing evolution
# ---------------------------------------------------------------------------
print_section('7. Intraday timing evolution — when does the signal emerge?')
print('  d0=Mon EOD, d1=Tue EOD, d2=Wed EOD, d3=Thu EOD')
print()

print(f'  MP strike DELTA signal evolution:')
print(f'  {"Feature":<40} {"→ CE P&L":>12} {"→ PE P&L":>12} {"→ Total P&L":>12}')
print('  ' + '-' * 80)
for col, label in [
    ('mp_delta_d0', 'MP delta: Mon EOD (entry day)'),
    ('mp_delta_d1', 'MP delta: Tue EOD (Day 2)'),
    ('mp_delta_d2', 'MP delta: Wed EOD (Day 3)'),
    ('mp_delta_d3', 'MP delta: Thu EOD (Day 4)'),
    ('mp_delta_full', 'MP delta: full-week (contemporaneous)'),
]:
    vals   = df[col].values.astype(float)
    n_valid = (~np.isnan(vals)).sum()
    row_out = [f'  {label:<40}']
    for tgt, _ in targets:
        r, p = ic(vals, df[tgt].values.astype(float))
        row_out.append(f'{fmt(r, p):>12}')
    print(''.join(row_out) + f'  (n={n_valid})')

print()
print(f'  MP DISTANCE signal evolution (spot − max_pain at each day):')
print(f'  {"Feature":<40} {"→ CE P&L":>12} {"→ PE P&L":>12} {"→ Total P&L":>12}')
print('  ' + '-' * 80)
for col, label in [
    ('mp_dist_d0', 'MP dist: Mon EOD (entry day)'),
    ('mp_dist_d1', 'MP dist: Tue EOD (Day 2)'),
    ('mp_dist_d2', 'MP dist: Wed EOD (Day 3)'),
    ('mp_dist_d3', 'MP dist: Thu EOD (Day 4)'),
    ('max_mp_dist', 'MP dist: full-week max (contemporaneous)'),
]:
    vals   = df[col].values.astype(float)
    n_valid = (~np.isnan(vals)).sum()
    row_out = [f'  {label:<40}']
    for tgt, _ in targets:
        r, p = ic(vals, df[tgt].values.astype(float))
        row_out.append(f'{fmt(r, p):>12}')
    print(''.join(row_out) + f'  (n={n_valid})')


# ---------------------------------------------------------------------------
# 8. PE SL threshold analysis — MP dist at Tue EOD
# ---------------------------------------------------------------------------
print_section('8. PE SL threshold analysis: MP dist at Tue EOD as PE SL warning')
print('  When spot is above max_pain (positive mp_dist) at Tue EOD, PE is at risk.')
print('  How well does this threshold predict eventual PE SL?')
print()

mp_dist_d1 = df['mp_dist_d1'].values.astype(float)
pe_sl_arr  = df['pe_sl'].values

print(f'  {"Threshold":>16}  {"N triggered":>12}  {"PE SL if trig":>14}  '
      f'{"PE SL if NOT":>14}  {"Sensitivity":>12}  {"Specificity":>12}')
print('  ' + '-' * 94)

pe_sl_base = pe_sl_arr.mean()
for thresh in [200, 150, 100, 75, 50, 25, 0, -25, -50]:
    mask_valid = ~np.isnan(mp_dist_d1)
    triggered  = (mp_dist_d1 >= thresh) & mask_valid
    not_trig   = (~triggered) & mask_valid
    n_trig     = triggered.sum()
    if n_trig == 0:
        continue
    sl_if_trig = pe_sl_arr[triggered].mean()
    sl_if_not  = pe_sl_arr[not_trig].mean() if not_trig.sum() > 0 else np.nan
    sensitivity = (triggered & (pe_sl_arr == 1)).sum() / (pe_sl_arr == 1).sum()
    specificity = ((~triggered) & (pe_sl_arr == 0)).sum() / (pe_sl_arr == 0).sum()
    print(f'  MP dist ≥ {thresh:+4d} pts: {n_trig:>4d}/{mask_valid.sum():2d}  '
          f'  SL rate {sl_if_trig:.0%}  vs  {sl_if_not:.0%}  '
          f'  sens={sensitivity:.0%}  spec={specificity:.0%}')

print(f'\n  Baseline PE SL rate: {pe_sl_base:.1%}')
print(f'  * mp_dist = spot − max_pain_strike at Tue EOD')
print(f'    positive = spot above max pain = PE (put sell) at elevated risk')


# ---------------------------------------------------------------------------
# 9. CE SL threshold analysis — negative MP dist at Tue EOD
# ---------------------------------------------------------------------------
print_section('9. CE SL threshold analysis: negative MP dist (spot below max_pain) as CE warning')
print('  When spot is BELOW max_pain (negative mp_dist), spot has more room to fall,')
print('  but CE (call sell) is safer when spot is low. CE is at risk when spot is HIGH.')
print('  Actually: spot ABOVE max_pain → spot ran past "fair value" → often reverses.')
print('  Testing: does spot being far ABOVE max_pain at Tue EOD predict CE SL?')
print()

ce_sl_arr = df['ce_sl'].values

print(f'  {"Threshold":>16}  {"N triggered":>12}  {"CE SL if trig":>14}  '
      f'{"CE SL if NOT":>14}  {"Sensitivity":>12}  {"Specificity":>12}')
print('  ' + '-' * 94)

ce_sl_base = ce_sl_arr.mean()
for thresh in [200, 150, 100, 75, 50, 25, 0, -25, -50]:
    mask_valid = ~np.isnan(mp_dist_d1)
    # CE at risk when spot is high (above max_pain) → trigger on positive mp_dist
    triggered  = (mp_dist_d1 >= thresh) & mask_valid
    not_trig   = (~triggered) & mask_valid
    n_trig     = triggered.sum()
    if n_trig == 0:
        continue
    sl_if_trig = ce_sl_arr[triggered].mean()
    sl_if_not  = ce_sl_arr[not_trig].mean() if not_trig.sum() > 0 else np.nan
    sensitivity = (triggered & (ce_sl_arr == 1)).sum() / (ce_sl_arr == 1).sum()
    specificity = ((~triggered) & (ce_sl_arr == 0)).sum() / (ce_sl_arr == 0).sum()
    print(f'  MP dist ≥ {thresh:+4d} pts: {n_trig:>4d}/{mask_valid.sum():2d}  '
          f'  SL rate {sl_if_trig:.0%}  vs  {sl_if_not:.0%}  '
          f'  sens={sensitivity:.0%}  spec={specificity:.0%}')

print(f'\n  Baseline CE SL rate: {ce_sl_base:.1%}')
print(f'  Note: CE P&L IC with mp_dist is POSITIVE (r≈+0.385) meaning spot above max')
print(f'  pain → CE P&L BETTER (not worse). The +IC suggests spot above max_pain')
print(f'  tends to REVERSE (pull back) rather than continue upward, helping CE.')


# ---------------------------------------------------------------------------
# 10. Gate assessment summary
# ---------------------------------------------------------------------------
print_section('10. Gate assessment — use cases for max pain DRIFT signal')
print()

r_d1_delta_ce, p1 = ic(df['mp_delta_d1'].values.astype(float), df['ce_pl'].values.astype(float))
r_d1_delta_pe, p2 = ic(df['mp_delta_d1'].values.astype(float), df['pe_pl'].values.astype(float))
r_d1_dist_pe,  p3 = ic(df['mp_dist_d1'].values.astype(float),  df['pe_pl'].values.astype(float))
r_d1_dist_ce,  p4 = ic(df['mp_dist_d1'].values.astype(float),  df['ce_pl'].values.astype(float))

print(f'  Context: observations at Tue EOD (Day-1), n=84 weeks')
print(f'  Full-window reference: mp_delta_full → CE P&L: r={fmt(*ic(df["mp_delta_full"].values.astype(float), df["ce_pl"].values.astype(float)))}')
print()

gate_rows = [
    ('SL trigger (PE): mp_dist ≥ threshold',
     f'mp_dist Tue EOD → PE P&L: r={fmt(r_d1_dist_pe, p3)}; see §8',
     'Strong raw IC but partial IC ≈ -0.09 (insignificant after spot control). '
     'Threshold at ≥+50 pts catches 54% of PE SL events with SL rate 70% vs 33% baseline — '
     'non-trivial lift, but partially explained by spot position.'),
    ('SL trigger (CE): mp_delta ≥ threshold',
     f'mp_delta Tue EOD → CE P&L: r={fmt(r_d1_delta_ce, p1)}; partial IC ≈ -0.15 (§5)',
     'Strong raw IC but spot explains most of it. CE P&L IC with mp_dist is positive '
     '(r=+0.385***) — spot above max_pain is bullish for CE (reversal signal), not bearish. CLOSED.'),
    ('Adjustment trigger (PE)',
     f'mp_dist Tue EOD → PE P&L: r={fmt(r_d1_dist_pe, p3)}; year-split §6',
     'Year-split: 2023-24 r=-0.650*** (strong), 2025 r=-0.067 (collapsed). '
     'Signal unstable across regimes. Partial IC near zero. CLOSED (guarded).'),
    ('Position skewing',
     'Does mp_dist direction predict which leg is at risk at Tue EOD?',
     'mp_dist > 0 → spot above max_pain → CE better (reversal) and PE worse. '
     'CE P&L IC is +0.385*** (positive!), PE P&L IC is -0.524***. '
     'Direction asymmetry works for PE (spot above max_pain → PE at risk), '
     'but partial IC vanishes after spot control. CLOSED.'),
    ('Adjustment timing',
     'Signal visible as early as Mon EOD: mp_delta d0: CE=-0.336**, PE=+0.305** (§7)',
     'Signal is detectable even Monday EOD. However, it is entirely explained by '
     'spot direction (r=+0.861*** for mp_delta vs spot). Early signal = early spot move. CLOSED.'),
]

for uc, evidence, verdict in gate_rows:
    print(f'  USE CASE: {uc}')
    print(f'    Evidence: {evidence}')
    print(f'    Verdict:  {verdict}')
    print()

print('  OVERALL CONCLUSION:')
print()
print('  Max pain strike delta is even more tightly coupled to spot than CE/PE wall')
print(f'  migration (r=+0.861*** vs +0.455 for wall). After controlling for spot,')
print('  all partial ICs collapse to near zero. Max pain drift carries NO independent')
print('  information about outcomes beyond knowing where spot went.')
print()
print('  Max pain DISTANCE (spot − max_pain at Tue EOD) has strong raw IC')
print(f'  (PE: r={fmt(r_d1_dist_pe, p3)}, CE: r={fmt(r_d1_dist_ce, p4)}) but the same')
print('  partial IC collapse applies. Year-split shows PE P&L IC collapses in 2025.')
print()
print('  ONE PARTIAL EXCEPTION (§8): mp_dist ≥ +50 pts at Tue EOD as a PE SL readiness')
print('  flag gives a real SL rate lift (70% vs 33% baseline), but with only ~54%')
print('  sensitivity. It is a complementary confirmatory signal, not a standalone trigger.')
print('  Whether this residual lift survives 2025 data is uncertain (year-split unstable).')
