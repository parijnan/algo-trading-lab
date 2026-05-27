"""
analyze_asymmetric_sizing.py — Asymmetric leg sizing sweep for Artemis Nifty.

Hypothesis: scale the *protected* leg in structurally-favoured setups.
  down+near (down-biased range, spot close to resistance, key_dist<50%):
      CE is protected by resistance  →  scale CE up, keep PE at base
  up+near   (up-biased range, spot close to support,    key_dist<50%):
      PE is protected by support     →  scale PE up, keep CE at base

Formula:
  ce_comp  = ce_pl_points + ce_add_pl_points / lots   (total CE contribution)
  pe_comp  = pe_pl_points + pe_add_pl_points / lots   (total PE contribution)
  trade_pl = ce_factor × ce_comp + pe_factor × pe_comp

Symmetric reference from lot_sizing_sweep.py two-sided best:
  down+kd<50% ×2.0  /  up(any) ×0.75  →  Sharpe 2.754, MaxDD -127.8, total +2498.3

Usage:
    python analyze_asymmetric_sizing.py
    python analyze_asymmetric_sizing.py --instrument sensex   (if data available)

Outputs:
    outputs/asymmetric_sizing_sweep.csv    — config-level metrics
    outputs/asymmetric_sizing_trades.csv   — per-trade P&L for key configs
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
    'SYM-REF: dn_near×2.0 + up_any×0.75',
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def assign_buckets(df):
    down_near = ((df['ep_direction'] == 'down') & (df['key_dist_pct'] < 50)).fillna(False)
    down_far  = ((df['ep_direction'] == 'down') & (df['key_dist_pct'] >= 50)).fillna(False)
    up_near   = ((df['ep_direction'] == 'up')   & (df['key_dist_pct'] < 50)).fillna(False)
    up_far    = ((df['ep_direction'] == 'up')   & (df['key_dist_pct'] >= 50)).fillna(False)
    bucket = pd.Series('other', index=df.index)
    bucket[down_near] = 'down_near'
    bucket[down_far]  = 'down_far'
    bucket[up_near]   = 'up_near'
    bucket[up_far]    = 'up_far'
    return bucket


def compute_pl(df, dn_nc, dn_np, dn_fc, dn_fp, up_nc, up_np, up_fc, up_fp):
    """Vectorised asymmetric leg P&L."""
    bk = df['bucket']
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

    # Symmetric reference (from lot_sizing_sweep two-sided best) for direct comparison
    # down_near ×2.0 both, down_far neutral, up_near ×0.75 both, up_far ×0.75 both
    configs.append(('SYM-REF: dn_near×2.0 + up_any×0.75',
                     2.0, 2.0,  1.0, 1.0,  0.75, 0.75,  0.75, 0.75))

    return configs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not os.path.exists(INPUT_CSV):
        print(f'ERROR: {INPUT_CSV} not found. Run annotate_artemis.py first.')
        sys.exit(1)

    df = pd.read_csv(INPUT_CSV, parse_dates=['entry_time'])
    df = df[df['week_outcome'] == 'traded'].copy().reset_index(drop=True)
    df = df.sort_values('entry_time').reset_index(drop=True)
    n  = len(df)
    print(f'\nLoaded {n} traded {INSTRUMENT.upper()} Artemis trades  '
          f'{df["entry_time"].min().date()} → {df["entry_time"].max().date()}')

    # CE/PE decomposition
    df['ce_comp'] = df['ce_pl_points'] + df['ce_add_pl_points'] / df['lots']
    df['pe_comp'] = df['pe_pl_points'] + df['pe_add_pl_points'] / df['lots']
    recon_err = (df['ce_comp'] + df['pe_comp'] - df['total_pl_points']).abs().max()
    print(f'CE+PE reconciliation max abs error = {recon_err:.4f}  (should be ~0)')

    df['bucket'] = assign_buckets(df)
    df['year']   = df['entry_time'].dt.year
    df['sl_pe']  = df['pe_exit_reason'].isin(SL_REASONS)
    df['sl_ce']  = df['ce_exit_reason'].isin(SL_REASONS)
    df['any_sl'] = df['sl_pe'] | df['sl_ce']

    counts = df['bucket'].value_counts().to_dict()
    print(f'Bucket counts: ' +
          '  '.join(f"{k}={counts.get(k,0)}" for k in ['down_near','down_far','up_near','up_far','other']))

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

    # ── Year-by-year for key configs ──────────────────────────────────────
    key_cfgs = [c for c in configs if any(lbl in c[0] for lbl in KEY_CONFIGS_LABELS)]
    # Pre-compute P&L columns
    for cfg in key_cfgs:
        colname = f'pl_{cfg[0]}'
        df[colname] = compute_pl(df, *cfg[1:])

    print_section('YEAR-BY-YEAR  (key configurations)')
    print(f'  {"Config":<50}  {"Yr":>4}  {"n":>4}  {"Total":>8}  '
          f'{"Sharpe":>7}  {"MaxDD":>8}  {"Win%":>6}')
    print('  ' + '-' * 100)
    for cfg in key_cfgs:
        colname = f'pl_{cfg[0]}'
        for yr, grp in df.groupby('year'):
            m = metrics(grp[colname], cfg[0])
            print(f'  {cfg[0]:<50}  {yr:>4}  {m["n"]:>4}  {m["total_pts"]:>+8.1f}  '
                  f'{m["sharpe"]:>7.3f}  {m["max_dd"]:>+8.1f}  {m["win_pct"]:>5.1f}%')
        print()

    # ── SL analysis for top configs ────────────────────────────────────────
    # Pick best Sharpe config and C×2.0 for detailed SL comparison
    top_labels = [best_s['label'], 'C: dn_near CE×2.0 + up_near PE×2.0',
                  'SYM-REF: dn_near×2.0 + up_any×0.75', 'baseline']
    top_labels = list(dict.fromkeys(top_labels))  # deduplicate, preserve order

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
    trade_cols = ['entry_time', 'year', 'bucket', 'ep_direction', 'key_dist_pct',
                  'entry_vix', 'ce_comp', 'pe_comp', 'total_pl_points',
                  'sl_pe', 'sl_ce', 'any_sl', 'pe_exit_reason', 'ce_exit_reason']
    trade_df = df[trade_cols].copy()
    for cfg in key_cfgs:
        colname = f'pl_{cfg[0]}'
        trade_df[colname] = df[colname]
    trade_df.to_csv(TRADES_CSV, index=False)
    print(f'Saved trades: {TRADES_CSV}')
    print()


if __name__ == '__main__':
    main()
