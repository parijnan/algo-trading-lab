"""
analyze_asymmetric_sizing.py — Asymmetric leg sizing sweep for Artemis.

Hypothesis: scale the *protected* leg in structurally-favoured setups.
  down+near (down-biased range, spot close to resistance, key_dist<50%):
      CE is protected by resistance  →  scale CE up, keep PE at base
  up+near   (up-biased range, spot close to support,    key_dist<50%):
      PE is protected by support     →  scale PE up, keep CE at base

Breakout variant (E-adj-bk):
  Uncommitted directional entries (bars_into ≤ 2) treated as _near regardless
  of key_dist_pct, because the key level is already known and spot is by
  definition near it at the moment of breakout.

Formula:
  ce_comp  = ce_pl_points + ce_add_pl_points / lots   (total CE contribution)
  pe_comp  = pe_pl_points + pe_add_pl_points / lots   (total PE contribution)
  trade_pl = ce_factor × ce_comp + pe_factor × pe_comp

Capital adjustment (M_ADJ = 2/1.5 = 1.333):
  A 2× skewed iron condor costs 1.5× the margin of a balanced 1× condor.
  Protected leg: ×M_ADJ (1.333).  Unprotected leg: ×(1/M_ADJ) (0.667).

Usage:
    python analyze_asymmetric_sizing.py
    python analyze_asymmetric_sizing.py --instrument sensex

Outputs (per instrument):
    outputs/asymmetric_sizing_sweep_{inst}.csv    — config-level metrics
    outputs/asymmetric_sizing_trades_{inst}.csv   — per-trade P&L for key configs
"""

import os
import sys
import numpy as np
import pandas as pd

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
OUT_DIR    = os.path.join(BASE_DIR, 'outputs')

INSTRUMENT = 'nifty'
for arg in sys.argv[1:]:
    if arg.startswith('--instrument'):
        INSTRUMENT = arg.split('=')[-1].strip() if '=' in arg else sys.argv[sys.argv.index(arg) + 1]

INPUT_CSV   = os.path.join(OUT_DIR, f'artemis_annotated_{INSTRUMENT}.csv')
SWEEP_CSV   = os.path.join(OUT_DIR, f'asymmetric_sizing_sweep_{INSTRUMENT}.csv')
TRADES_CSV  = os.path.join(OUT_DIR, f'asymmetric_sizing_trades_{INSTRUMENT}.csv')

SL_REASONS  = {'index_sl', 'option_sl'}

M_ADJ      = 2.0 / 1.5          # 1.333... capital-adjusted protected-leg multiplier
M_UNPROT   = 1.0 / 1.5          # 0.667  capital-adjusted unprotected-leg multiplier
LOT_RUPEES = {'nifty': 65, 'sensex': 20}  # rupees per index point per lot

E_ADJ_LABEL    = 'E-adj (M=1.333, rest×0.5)'
E_ADJ_BK_LABEL = 'E-adj-bk (uncommitted→near)'

# Committed-only config: asymmetric sizing for all committed directional trades,
# baseline (1×) for uncommitted + initial. No size reduction on the "rest" bucket,
# unlike E-adj's rest×0.5 — the two differ in how they handle uncommitted/initial trades.
COMMITTED_LABEL  = 'committed: dir 80CE/40PE or 40CE/80PE + rest 60/60'
COMM_FACTORS     = (M_ADJ, M_UNPROT, 1.0, 1.0, M_UNPROT, M_ADJ, 1.0, 1.0)

# Symmetric reference from prior lot_sizing_sweep
SYM_REF = {'label': 'sym-ref: dn_near×2.0 + up_any×0.75',
            'total': 2498.3, 'sharpe': 2.754, 'max_dd': -127.8}

PRIMARY_MULTS = [1.5, 1.75, 2.0, 2.5]
DN_SCALES     = [0.75, 1.0]

# Key configs to include in year-by-year and trade-level CSV
KEY_CONFIGS_LABELS = [
    'baseline',
    'C: dn_near CE×2.0 + up_near PE×2.0',
    'E: dn_near CE×2.0 + up_near PE×2.0 + rest×0.75',
    E_ADJ_LABEL,
    'SYM-REF: dn_near×2.0 + up_any×0.75',
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def assign_buckets(df):
    """Standard bucket assignment using key_dist_pct for near/far split.

    Requires ep_committed=True (bars_into > BREAKOUT_CONFIRM = bars_into >= 3).
    Uncommitted entries and initial/null ranges fall through to 'other'.
    """
    committed = df['ep_committed'].fillna(False).astype(bool)
    down_near = (committed & (df['ep_direction'] == 'down') & (df['key_dist_pct'] < 50)).fillna(False)
    down_far  = (committed & (df['ep_direction'] == 'down') & (df['key_dist_pct'] >= 50)).fillna(False)
    up_near   = (committed & (df['ep_direction'] == 'up')   & (df['key_dist_pct'] < 50)).fillna(False)
    up_far    = (committed & (df['ep_direction'] == 'up')   & (df['key_dist_pct'] >= 50)).fillna(False)
    bucket = pd.Series('other', index=df.index)
    bucket[down_near] = 'down_near'
    bucket[down_far]  = 'down_far'
    bucket[up_near]   = 'up_near'
    bucket[up_far]    = 'up_far'
    return bucket


def assign_buckets_committed(df):
    """Committed-only bucket: reuses down_near/up_near slots for committed directional trades.

    Uncommitted directional entries and initial/null ranges → 'other' (baseline 1×).
    This is the primary axis: confirmation (3+ bars) rather than proximity to key level.
    """
    committed = df['ep_committed'].fillna(False).astype(bool)
    down = (df['ep_direction'] == 'down')
    up   = (df['ep_direction'] == 'up')
    bucket = pd.Series('other', index=df.index)
    bucket[committed & down] = 'down_near'
    bucket[committed & up]   = 'up_near'
    return bucket


def assign_buckets_bk(df):
    """Breakout variant: uncommitted directional entries → _near.

    At bars_into=1-2 the key level is already known and spot is by definition
    near it (it just crossed). The range_high/low for the far bound is still
    narrow, so key_dist_pct is unreliable. Treat all uncommitted directional
    entries as _near and apply full asymmetric sizing immediately.
    """
    uncommitted = (~df['ep_committed'].fillna(True))
    down = (df['ep_direction'] == 'down')
    up   = (df['ep_direction'] == 'up')
    down_near = (down & (uncommitted | (df['key_dist_pct'] < 50))).fillna(False)
    down_far  = (down & ~uncommitted & (df['key_dist_pct'] >= 50)).fillna(False)
    up_near   = (up   & (uncommitted | (df['key_dist_pct'] < 50))).fillna(False)
    up_far    = (up   & ~uncommitted & (df['key_dist_pct'] >= 50)).fillna(False)
    bucket = pd.Series('other', index=df.index)
    bucket[down_near] = 'down_near'
    bucket[down_far]  = 'down_far'
    bucket[up_near]   = 'up_near'
    bucket[up_far]    = 'up_far'
    return bucket


def compute_pl(df, dn_nc, dn_np, dn_fc, dn_fp, up_nc, up_np, up_fc, up_fp,
               bucket_col='bucket'):
    """Vectorised asymmetric leg P&L."""
    bk = df[bucket_col]
    ce_f = np.select(
        [bk == 'down_near', bk == 'down_far', bk == 'up_near', bk == 'up_far'],
        [dn_nc, dn_fc, up_nc, up_fc], default=1.0)
    pe_f = np.select(
        [bk == 'down_near', bk == 'down_far', bk == 'up_near', bk == 'up_far'],
        [dn_np, dn_fp, up_np, up_fp], default=1.0)
    return ce_f * df['ce_comp'] + pe_f * df['pe_comp']


def metrics(pl_series, label=''):
    n      = len(pl_series)
    total  = pl_series.sum()
    avg    = pl_series.mean()
    std    = pl_series.std()
    wr     = (pl_series > 0).mean() * 100
    sharpe = avg / std * np.sqrt(52) if std > 0 else np.nan
    cum    = pl_series.cumsum()
    max_dd = (cum - cum.cummax()).min()
    is_loss = (pl_series <= 0).astype(int).values
    consec = max_consec = 0
    for v in is_loss:
        consec = consec + 1 if v else 0
        if consec > max_consec:
            max_consec = consec
    return {
        'label':       label,
        'n':           n,
        'total_pts':   round(total, 1),
        'avg_pts':     round(avg, 2),
        'win_pct':     round(wr, 1),
        'sharpe':      round(float(sharpe), 3) if not np.isnan(sharpe) else np.nan,
        'max_dd':      round(max_dd, 1),
        'max_consec_l': int(max_consec),
    }


def print_section(title):
    print(f'\n{"─" * 80}')
    print(f'  {title}')
    print(f'{"─" * 80}')


# ---------------------------------------------------------------------------
# Build config list
# ---------------------------------------------------------------------------

def build_configs():
    configs = []
    # Stored as (label, dn_nc, dn_np, dn_fc, dn_fp, up_nc, up_np, up_fc, up_fp)

    configs.append(('baseline', 1,1, 1,1, 1,1, 1,1))

    for M in PRIMARY_MULTS:
        # A: scale CE in down_near only (CE protected by resistance)
        configs.append((f'A: dn_near CE×{M}',
                         M, 1,  1, 1,  1, 1,  1, 1))
        # B: scale PE in up_near only (PE protected by support + up-drift)
        configs.append((f'B: up_near PE×{M}',
                         1, 1,  1, 1,  1, M,  1, 1))
        # C: both A and B simultaneously (no downscaling elsewhere)
        configs.append((f'C: dn_near CE×{M} + up_near PE×{M}',
                         M, 1,  1, 1,  1, M,  1, 1))

    for M in PRIMARY_MULTS:
        for S in DN_SCALES:
            if S == 1.0:
                continue   # already in group C above
            # D: scale favourable legs + reduce up_far (weakest structural case)
            configs.append((f'D: dn_near CE×{M} + up_near PE×{M} + up_far×{S}',
                             M, 1,  1, 1,  1, M,  S, S))
            # E: scale favourable legs + reduce all other buckets (down_far + up_far)
            configs.append((f'E: dn_near CE×{M} + up_near PE×{M} + rest×{S}',
                             M, 1,  S, S,  1, M,  S, S))

    # Capital-adjusted E-adj: protected leg ×1.333, unprotected ×0.667, rest ×0.5
    # M_ADJ=2/1.5=1.333 (protected), M_UNPROT=1/1.5=0.667 (unprotected)
    configs.append((E_ADJ_LABEL,
                    M_ADJ, M_UNPROT,  0.5, 0.5,  M_UNPROT, M_ADJ,  0.5, 0.5))

    # Symmetric reference (from lot_sizing_sweep two-sided best) for direct comparison
    configs.append(('SYM-REF: dn_near×2.0 + up_any×0.75',
                     2.0, 2.0,  1.0, 1.0,  0.75, 0.75,  0.75, 0.75))

    return configs


# ---------------------------------------------------------------------------
# Per-instrument analysis
# ---------------------------------------------------------------------------

def load_instrument(instrument):
    csv_path = os.path.join(OUT_DIR, f'artemis_annotated_{instrument}.csv')
    if not os.path.exists(csv_path):
        return None
    df = pd.read_csv(csv_path, parse_dates=['entry_time'])
    df = df[df['week_outcome'] == 'traded'].copy().reset_index(drop=True)
    df = df.sort_values('entry_time').reset_index(drop=True)
    df['ce_comp'] = df['ce_pl_points'] + df['ce_add_pl_points'] / df['lots']
    df['pe_comp'] = df['pe_pl_points'] + df['pe_add_pl_points'] / df['lots']
    df['bucket']           = assign_buckets(df)
    df['bucket_bk']        = assign_buckets_bk(df)
    df['bucket_committed'] = assign_buckets_committed(df)
    df['year']      = df['entry_time'].dt.year
    df['sl_pe']     = df['pe_exit_reason'].isin(SL_REASONS)
    df['sl_ce']     = df['ce_exit_reason'].isin(SL_REASONS)
    df['any_sl']    = df['sl_pe'] | df['sl_ce']
    df['instrument'] = instrument
    return df


def main():
    if not os.path.exists(INPUT_CSV):
        print(f'ERROR: {INPUT_CSV} not found. Run annotate_artemis.py first.')
        sys.exit(1)

    df = load_instrument(INSTRUMENT)
    n  = len(df)
    print(f'\nLoaded {n} traded {INSTRUMENT.upper()} Artemis trades  '
          f'{df["entry_time"].min().date()} → {df["entry_time"].max().date()}')

    recon_err = (df['ce_comp'] + df['pe_comp'] - df['total_pl_points']).abs().max()
    print(f'CE+PE reconciliation max abs error = {recon_err:.4f}  (should be ~0)')

    counts = df['bucket'].value_counts().to_dict()
    print(f'Bucket counts (standard):   ' +
          '  '.join(f"{k}={counts.get(k,0)}" for k in ['down_near','down_far','up_near','up_far','other']))
    counts_bk = df['bucket_bk'].value_counts().to_dict()
    print(f'Bucket counts (bk variant): ' +
          '  '.join(f"{k}={counts_bk.get(k,0)}" for k in ['down_near','down_far','up_near','up_far','other']))

    # ── Unweighted bucket CE/PE profiles ─────────────────────────────────
    print_section('BUCKET CE/PE PROFILES  (unweighted, per trade)')
    hdr = (f"  {'Bucket':<12}  {'n':>4}  {'ce_avg':>8}  {'ce_win%':>8}  "
           f"{'pe_avg':>8}  {'pe_win%':>8}  {'total_avg':>10}  {'SL%':>6}")
    print(hdr)
    print('  ' + '-' * 72)
    for bk in ['down_near', 'down_far', 'up_near', 'up_far']:
        sub = df[df['bucket'] == bk]
        if sub.empty:
            continue
        ce_avg = sub['ce_comp'].mean()
        pe_avg = sub['pe_comp'].mean()
        ce_wr  = (sub['ce_comp'] > 0).mean() * 100
        pe_wr  = (sub['pe_comp'] > 0).mean() * 100
        tot    = sub['total_pl_points'].mean()
        sl_r   = sub['any_sl'].mean() * 100
        print(f'  {bk:<12}  {len(sub):>4}  {ce_avg:>+8.2f}  {ce_wr:>7.1f}%  '
              f'{pe_avg:>+8.2f}  {pe_wr:>7.1f}%  {tot:>+10.2f}  {sl_r:>5.1f}%')

    # ── Committed bucket CE/PE profiles ──────────────────────────────────
    print_section('COMMITTED-BUCKET CE/PE PROFILES  (committed directional vs rest)')
    counts_comm = df['bucket_committed'].value_counts().to_dict()
    print(f'Committed down: {counts_comm.get("down_near", 0)}  '
          f'Committed up: {counts_comm.get("up_near", 0)}  '
          f'Other (uncommitted + initial): {counts_comm.get("other", 0)}')
    print()
    print(f'  Note: "other" here is the uncommitted + initial pool that gets 60/60 (baseline).')
    print(f'  Compare with E-adj where the same pool gets 30/30 (0.5× haircut).')
    print()
    hdr2 = (f"  {'Bucket':<30}  {'n':>4}  {'ce_avg':>8}  {'ce_win%':>8}  "
            f"{'pe_avg':>8}  {'pe_win%':>8}  {'total_avg':>10}  {'SL%':>6}")
    print(hdr2)
    print('  ' + '-' * 84)
    comm_labels = [
        ('committed_down', df['bucket_committed'] == 'down_near'),
        ('committed_up',   df['bucket_committed'] == 'up_near'),
        ('other (uncommitted+initial)', df['bucket_committed'] == 'other'),
    ]
    for label, mask in comm_labels:
        sub = df[mask]
        if sub.empty:
            continue
        ce_avg = sub['ce_comp'].mean()
        pe_avg = sub['pe_comp'].mean()
        ce_wr  = (sub['ce_comp'] > 0).mean() * 100
        pe_wr  = (sub['pe_comp'] > 0).mean() * 100
        tot    = sub['total_pl_points'].mean()
        sl_r   = sub['any_sl'].mean() * 100
        print(f'  {label:<30}  {len(sub):>4}  {ce_avg:>+8.2f}  {ce_wr:>7.1f}%  '
              f'{pe_avg:>+8.2f}  {pe_wr:>7.1f}%  {tot:>+10.2f}  {sl_r:>5.1f}%')

    # ── Reclassification analysis ──────────────────────────────────────────
    print_section('BREAKOUT RECLASSIFICATION  (uncommitted entries → _near in bk variant)')
    unc = df[~df['ep_committed'].fillna(True)].copy()
    total_unc = len(unc)
    reclassified = unc[unc['bucket'] != unc['bucket_bk']].copy()
    stayed = unc[unc['bucket'] == unc['bucket_bk']]

    print(f'  Uncommitted entries total:       {total_unc}')
    print(f'  Already _near (no change):       {len(stayed)}')
    print(f'  Reclassified _far → _near:       {len(reclassified)}')

    if not reclassified.empty:
        print(f'\n  Reclassified trades:')
        print(f"  {'Date':<12}  {'Dir':<6}  {'bars_into':>9}  {'key_dist':>9}  "
              f"{'old_bucket':<12}  {'new_bucket':<12}  {'total_pl':>9}")
        print('  ' + '-' * 80)
        for _, r in reclassified.iterrows():
            print(f"  {str(r['entry_time'].date()):<12}  {r['ep_direction']:<6}  "
                  f"{int(r['ep_bars_into']):>9}  {r['key_dist_pct']:>8.1f}%  "
                  f"{r['bucket']:<12}  {r['bucket_bk']:<12}  "
                  f"{r['total_pl_points']:>+9.1f}")

    # ── Compute all configs ───────────────────────────────────────────────
    configs   = build_configs()
    base_cfg  = configs[0]
    base_pl   = compute_pl(df, *base_cfg[1:])
    base_m    = metrics(base_pl, 'baseline')
    base_total  = base_m['total_pts']
    base_sharpe = base_m['sharpe']

    rows = []
    for cfg in configs:
        lbl  = cfg[0]
        pl   = compute_pl(df, *cfg[1:])
        m    = metrics(pl, lbl)
        m['dn_near_ce'] = cfg[1]
        m['dn_near_pe'] = cfg[2]
        m['dn_far_ce']  = cfg[3]
        m['dn_far_pe']  = cfg[4]
        m['up_near_ce'] = cfg[5]
        m['up_near_pe'] = cfg[6]
        m['up_far_ce']  = cfg[7]
        m['up_far_pe']  = cfg[8]
        m['uplift_pts']   = round(m['total_pts'] - base_total, 1)
        m['uplift_pct']   = round((m['total_pts'] / base_total - 1) * 100, 1)
        m['sharpe_delta'] = round(m['sharpe'] - base_sharpe, 3) if not np.isnan(m['sharpe']) else np.nan
        rows.append(m)

    # E-adj-bk: same factors as E-adj, but use bucket_bk
    eadj_cfg = next(c for c in configs if c[0] == E_ADJ_LABEL)
    pl_bk  = compute_pl(df, *eadj_cfg[1:], bucket_col='bucket_bk')
    m_bk   = metrics(pl_bk, E_ADJ_BK_LABEL)
    m_bk['dn_near_ce'] = eadj_cfg[1]; m_bk['dn_near_pe'] = eadj_cfg[2]
    m_bk['dn_far_ce']  = eadj_cfg[3]; m_bk['dn_far_pe']  = eadj_cfg[4]
    m_bk['up_near_ce'] = eadj_cfg[5]; m_bk['up_near_pe'] = eadj_cfg[6]
    m_bk['up_far_ce']  = eadj_cfg[7]; m_bk['up_far_pe']  = eadj_cfg[8]
    m_bk['uplift_pts']   = round(m_bk['total_pts'] - base_total, 1)
    m_bk['uplift_pct']   = round((m_bk['total_pts'] / base_total - 1) * 100, 1)
    m_bk['sharpe_delta'] = round(m_bk['sharpe'] - base_sharpe, 3)
    rows.append(m_bk)

    # Committed-only config: all committed directional → 80/40 by direction, rest → 1×
    pl_comm = compute_pl(df, *COMM_FACTORS, bucket_col='bucket_committed')
    m_comm  = metrics(pl_comm, COMMITTED_LABEL)
    m_comm['dn_near_ce'] = COMM_FACTORS[0]; m_comm['dn_near_pe'] = COMM_FACTORS[1]
    m_comm['dn_far_ce']  = COMM_FACTORS[2]; m_comm['dn_far_pe']  = COMM_FACTORS[3]
    m_comm['up_near_ce'] = COMM_FACTORS[4]; m_comm['up_near_pe'] = COMM_FACTORS[5]
    m_comm['up_far_ce']  = COMM_FACTORS[6]; m_comm['up_far_pe']  = COMM_FACTORS[7]
    m_comm['uplift_pts']   = round(m_comm['total_pts'] - base_total, 1)
    m_comm['uplift_pct']   = round((m_comm['total_pts'] / base_total - 1) * 100, 1)
    m_comm['sharpe_delta'] = round(m_comm['sharpe'] - base_sharpe, 3)
    rows.append(m_comm)

    result = pd.DataFrame(rows)

    # ── Summary table ─────────────────────────────────────────────────────
    print_section(f'ASYMMETRIC LEG SIZING SWEEP  '
                  f'(baseline: {base_total:+.1f} pts, Sharpe {base_sharpe:.3f})')
    print(f'  {"Config":<50}  {"Total":>8}  {"Uplift":>8}  {"Upl%":>7}  '
          f'{"Sharpe":>7}  {"ΔSharpe":>8}  {"MaxDD":>8}  {"Win%":>6}  {"MaxCL":>6}')
    print('  ' + '-' * 115)

    prev_group = None
    for _, r in result.iterrows():
        group = r['label'][0] if r['label'][0] in ('A','B','C','D','E','S') else 'b'
        if prev_group is not None and group != prev_group:
            print()
        prev_group = group
        sd_s = f"{r['sharpe_delta']:>+.3f}" if not pd.isna(r['sharpe_delta']) else '     —'
        print(f"  {r['label']:<50}  {r['total_pts']:>+8.1f}  "
              f"{r['uplift_pts']:>+8.1f}  {r['uplift_pct']:>+6.1f}%  "
              f"  {r['sharpe']:>7.3f}  {sd_s:>8}  {r['max_dd']:>+8.1f}  "
              f"{r['win_pct']:>5.1f}%  {int(r['max_consec_l']):>6}")

    # ── Highlight best ────────────────────────────────────────────────────
    non_base = result[result['label'] != 'baseline']
    if not non_base.empty:
        best_s = non_base.loc[non_base['sharpe'].idxmax()]
        best_t = non_base.loc[non_base['total_pts'].idxmax()]
        sd_s   = f"{best_s['sharpe_delta']:+.3f}"
        sd_t   = f"{best_t['sharpe_delta']:+.3f}"
        print(f'\n── Best by Sharpe ──────────────────────────────────────────────────────')
        print(f'  {best_s["label"]}')
        print(f'  Sharpe {best_s["sharpe"]:.3f} ({sd_s})  total {best_s["total_pts"]:+.1f}  '
              f'MaxDD {best_s["max_dd"]:+.1f}  win {best_s["win_pct"]:.1f}%')
        print(f'\n── Best by total P&L ────────────────────────────────────────────────────')
        print(f'  {best_t["label"]}')
        print(f'  total {best_t["total_pts"]:+.1f} ({best_t["uplift_pts"]:+.1f}, {best_t["uplift_pct"]:+.1f}%)  '
              f'Sharpe {best_t["sharpe"]:.3f} ({sd_t})  MaxDD {best_t["max_dd"]:+.1f}')

    # ── E-adj vs E-adj-bk vs Committed focused comparison ──────────────────
    print_section('FOCUSED COMPARISON: baseline / E-adj / E-adj-bk / committed')
    focus_labels = ['baseline', E_ADJ_LABEL, E_ADJ_BK_LABEL, COMMITTED_LABEL]
    print(f'  {"Config":<50}  {"Total":>8}  {"Sharpe":>7}  {"MaxDD":>8}  {"Win%":>6}  {"MaxCL":>6}')
    print('  ' + '-' * 90)
    for lbl in focus_labels:
        r = result[result['label'] == lbl].iloc[0]
        print(f"  {r['label']:<50}  {r['total_pts']:>+8.1f}  "
              f"{r['sharpe']:>7.3f}  {r['max_dd']:>+8.1f}  "
              f"{r['win_pct']:>5.1f}%  {int(r['max_consec_l']):>6}")

    # ── Year-by-year for key configs ──────────────────────────────────────
    key_cfgs = [c for c in configs if any(lbl in c[0] for lbl in KEY_CONFIGS_LABELS)]
    for cfg in key_cfgs:
        colname = f'pl_{cfg[0]}'
        df[colname] = compute_pl(df, *cfg[1:])
    # Add E-adj-bk
    df[f'pl_{E_ADJ_BK_LABEL}'] = pl_bk

    print_section('YEAR-BY-YEAR  (key configurations)')
    print(f'  {"Config":<50}  {"Yr":>4}  {"n":>4}  {"Total":>8}  '
          f'{"Sharpe":>7}  {"MaxDD":>8}  {"Win%":>6}')
    print('  ' + '-' * 100)
    all_key = key_cfgs + [(E_ADJ_BK_LABEL,)]  # sentinel for bk config
    for cfg in all_key:
        lbl = cfg[0]
        colname = f'pl_{lbl}'
        if colname not in df.columns:
            continue
        for yr, grp in df.groupby('year'):
            m = metrics(grp[colname], lbl)
            print(f'  {lbl:<50}  {yr:>4}  {m["n"]:>4}  {m["total_pts"]:>+8.1f}  '
                  f'{m["sharpe"]:>7.3f}  {m["max_dd"]:>+8.1f}  {m["win_pct"]:>5.1f}%')
        print()

    # ── SL analysis for top configs ────────────────────────────────────────
    top_labels = [best_s['label'], 'C: dn_near CE×2.0 + up_near PE×2.0',
                  'SYM-REF: dn_near×2.0 + up_any×0.75', 'baseline']
    top_labels = list(dict.fromkeys(top_labels))

    print_section('SL ANALYSIS FOR KEY CONFIGS  (by bucket within each config)')
    print(f'  {"Config":<50}  {"Bucket":<12}  {"n":>4}  {"any_SL%":>8}  '
          f'{"CE_SL%":>8}  {"PE_SL%":>8}  {"avg_wt":>8}  {"SL_avg":>8}')
    print('  ' + '-' * 110)
    for cfg in configs:
        if cfg[0] not in top_labels:
            continue
        df['_pl'] = compute_pl(df, *cfg[1:])
        for bk in ['down_near', 'down_far', 'up_near', 'up_far', 'ALL']:
            sub = df if bk == 'ALL' else df[df['bucket'] == bk]
            if sub.empty:
                continue
            ns   = len(sub)
            sl_a = sub['any_sl'].mean() * 100
            sl_c = sub['sl_ce'].mean() * 100
            sl_p = sub['sl_pe'].mean() * 100
            avg  = sub['_pl'].mean()
            sl_avg = sub[sub['any_sl']]['_pl'].mean() if sub['any_sl'].sum() else np.nan
            sl_s = f'{sl_avg:>+.1f}' if not np.isnan(sl_avg) else '   —'
            print(f'  {cfg[0]:<50}  {bk:<12}  {ns:>4}  {sl_a:>7.1f}%  '
                  f'{sl_c:>7.1f}%  {sl_p:>7.1f}%  {avg:>+8.2f}  {sl_s:>8}')
        print()

    # ── Worst trades for best-Sharpe config ───────────────────────────────
    # E-adj-bk uses same factors as E-adj but with bucket_bk
    if best_s['label'] == E_ADJ_BK_LABEL:
        best_cfg_tuple = eadj_cfg
        df['_pl_best'] = compute_pl(df, *best_cfg_tuple[1:], bucket_col='bucket_bk')
    else:
        best_cfg_tuple = next(c for c in configs if c[0] == best_s['label'])
        df['_pl_best'] = compute_pl(df, *best_cfg_tuple[1:])
    df['_cum']     = df['_pl_best'].cumsum()
    df['_dd']      = df['_cum'] - df['_cum'].cummax()

    print_section(f'WORST 10 TRADES  —  {best_s["label"]}')
    worst = df.nsmallest(10, '_pl_best')[
        ['entry_time', 'year', 'bucket', 'entry_vix',
         'ep_direction', 'key_dist_pct', 'ep_entry_spot_pct',
         'pe_exit_reason', 'ce_exit_reason',
         'total_pl_points', '_pl_best', 'any_sl']]
    for _, r in worst.iterrows():
        sl_tag = ' ◄SL' if r['any_sl'] else ''
        print(f'  {str(r["entry_time"].date()):<12}  {r["bucket"]:<12}  '
              f'VIX={r["entry_vix"]:.1f}  dir={r["ep_direction"]:<6}  '
              f'kd={r["key_dist_pct"]:>5.1f}%  '
              f'PE:{str(r["pe_exit_reason"]):<12}  CE:{str(r["ce_exit_reason"]):<12}  '
              f'raw={r["total_pl_points"]:>+7.1f}  wt={r["_pl_best"]:>+7.1f}{sl_tag}')

    # ── Save sweep CSV ─────────────────────────────────────────────────────
    result.to_csv(SWEEP_CSV, index=False)
    print(f'\nSaved sweep: {SWEEP_CSV}')

    # ── Save per-trade CSV for key configs ─────────────────────────────────
    trade_cols = ['entry_time', 'year', 'bucket', 'bucket_bk', 'bucket_committed',
                  'ep_committed', 'ep_bars_into',
                  'ep_direction', 'key_dist_pct',
                  'entry_vix', 'ce_comp', 'pe_comp', 'total_pl_points',
                  'sl_pe', 'sl_ce', 'any_sl', 'pe_exit_reason', 'ce_exit_reason']
    trade_df = df[trade_cols].copy()
    for cfg in key_cfgs:
        colname = f'pl_{cfg[0]}'
        trade_df[colname] = df[colname]
    trade_df[f'pl_{E_ADJ_BK_LABEL}'] = pl_bk
    trade_df.to_csv(TRADES_CSV, index=False)
    print(f'Saved trades: {TRADES_CSV}')

    return df


# ---------------------------------------------------------------------------
# Combined Nifty + Sensex analysis (rupee P&L)
# ---------------------------------------------------------------------------

def run_combined():
    print_section('COMBINED NIFTY + SENSEX  (177 trades, rupee P&L)')

    dfs = {}
    for inst in ('nifty', 'sensex'):
        d = load_instrument(inst)
        if d is None:
            print(f'  WARNING: {inst} annotated CSV not found — skipping')
            continue
        rupee = LOT_RUPEES[inst]
        d['rupee_factor'] = rupee
        dfs[inst] = d
        print(f'  {inst.upper():8s}: {len(d):3d} trades × ₹{rupee}/pt  '
              f'({d["entry_time"].min().date()} → {d["entry_time"].max().date()})')

    if len(dfs) < 2:
        print('  Cannot run combined analysis — need both instruments.')
        return

    # E-adj config factors
    eadj_cfg = next(c for c in build_configs() if c[0] == E_ADJ_LABEL)
    factors  = eadj_cfg[1:]

    # Per-instrument P&L (points), then convert to rupees
    for inst, d in dfs.items():
        d['pl_base']      = compute_pl(d, 1,1,1,1,1,1,1,1)               * d['rupee_factor']
        d['pl_eadj']      = compute_pl(d, *factors)                       * d['rupee_factor']
        d['pl_eadj_bk']   = compute_pl(d, *factors, bucket_col='bucket_bk') * d['rupee_factor']
        d['pl_committed'] = compute_pl(d, *COMM_FACTORS,
                                       bucket_col='bucket_committed')     * d['rupee_factor']

    combined = pd.concat([dfs['nifty'], dfs['sensex']], ignore_index=True)
    combined = combined.sort_values('entry_time').reset_index(drop=True)
    print(f'\n  Combined: {len(combined)} trades')

    def fmt_lakh(v):
        return f'{v/1e5:>+8.2f}L'

    configs_combined = [
        ('Baseline',                        'pl_base'),
        (E_ADJ_LABEL,                       'pl_eadj'),
        (E_ADJ_BK_LABEL,                    'pl_eadj_bk'),
        (COMMITTED_LABEL,                   'pl_committed'),
    ]

    print(f'\n  {"Config":<50}  {"Total(₹)":>10}  {"Sharpe":>7}  {"MaxDD(₹)":>10}  '
          f'{"Win%":>6}  {"MaxCL":>6}')
    print('  ' + '-' * 100)
    for lbl, col in configs_combined:
        pl = combined[col]
        m  = metrics(pl, lbl)
        print(f'  {lbl:<50}  {fmt_lakh(m["total_pts"]):>10}  '
              f'{m["sharpe"]:>7.3f}  {fmt_lakh(m["max_dd"]):>10}  '
              f'{m["win_pct"]:>5.1f}%  {int(m["max_consec_l"]):>6}')

    # Year-by-year combined
    print(f'\n  Year-by-year (combined, rupees):')
    print(f'  {"Config":<50}  {"Yr":>4}  {"n":>4}  {"Total(₹)":>10}  '
          f'{"Sharpe":>7}  {"MaxDD(₹)":>10}  {"Win%":>6}')
    print('  ' + '-' * 100)
    combined['year'] = combined['entry_time'].dt.year
    for lbl, col in configs_combined:
        for yr, grp in combined.groupby('year'):
            m = metrics(grp[col], lbl)
            print(f'  {lbl:<50}  {yr:>4}  {m["n"]:>4}  '
                  f'{fmt_lakh(m["total_pts"]):>10}  {m["sharpe"]:>7.3f}  '
                  f'{fmt_lakh(m["max_dd"]):>10}  {m["win_pct"]:>5.1f}%')
        print()

    # Per-trade listing (all 177, sorted by date)
    print_section('ALL 177 TRADES — baseline vs E-adj vs committed (rupees)')
    print(f'  {"Date":<12}  {"Inst":<7}  {"Bkt(std)":<12}  {"BktComm":<14}  '
          f'{"Dir":<5}  {"Bars":>4}  {"KD%":>6}  '
          f'{"Base(₹)":>9}  {"E-adj(₹)":>9}  {"Comm(₹)":>9}  {"Δ(comm-base)":>13}')
    print('  ' + '-' * 120)
    for _, r in combined.iterrows():
        delta = r['pl_committed'] - r['pl_base']
        marker = ' ◄' if abs(delta) > 0.01 else ''
        kd = f"{r['key_dist_pct']:.1f}" if pd.notna(r['key_dist_pct']) else '  —'
        bars = int(r['ep_bars_into']) if pd.notna(r['ep_bars_into']) else '—'
        comm_bkt = r['bucket_committed'] if pd.notna(r['bucket_committed']) else 'other'
        print(f"  {str(r['entry_time'].date()):<12}  "
              f"{r['instrument'].upper():<7}  "
              f"{r['bucket']:<12}  {comm_bkt:<14}  "
              f"{str(r['ep_direction']):<5}  {str(bars):>4}  {kd:>6}  "
              f"{r['pl_base']:>+9.0f}  {r['pl_eadj']:>+9.0f}  "
              f"{r['pl_committed']:>+9.0f}  {delta:>+13.0f}{marker}")

    print(f'\n  (₹ values; ◄ = trade differs from baseline under committed rule)')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    main()
    run_combined()
