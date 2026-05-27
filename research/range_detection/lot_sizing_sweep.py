"""
lot_sizing_sweep.py — Sweep lot-sizing multipliers across range-state conditions.

Baseline: 1× lots every trade (no filtering, no scaling).
Each condition × multiplier: apply M× lots when condition is met, 1× otherwise.
Trades are always taken — no skipping.

Usage:
    python lot_sizing_sweep.py
    python lot_sizing_sweep.py --instrument sensex

Output:
    Console summary table
    outputs/lot_sizing_sweep_{instrument}.csv
"""

import os
import sys
import numpy as np
import pandas as pd
from scipy import stats

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR  = os.path.join(BASE_DIR, 'outputs')

INSTRUMENT = sys.argv[1] if len(sys.argv) > 1 else 'nifty'
INPUT_CSV  = os.path.join(OUT_DIR, f'artemis_annotated_{INSTRUMENT}.csv')

# Multipliers to test (always ≥1 — no downscaling, no skipping)
MULTIPLIERS = [1.25, 1.5, 1.75, 2.0, 2.5]

# Conditions: (label, pd.Series-returning lambda on df)
CONDITIONS = [
    ('down (any state)',
     lambda df: df['ep_direction'] == 'down'),
    ('down + committed',
     lambda df: (df['ep_direction'] == 'down') & (df['ep_committed'] == True)),
    ('down + established',
     lambda df: (df['ep_direction'] == 'down') & (df['ep_established'] == True)),
    ('up (any state)',
     lambda df: df['ep_direction'] == 'up'),
    ('up + key_dist < 50%',
     lambda df: (df['ep_direction'] == 'up') & (df['key_dist_pct'] < 50)),
    ('up + key_dist < 33%',
     lambda df: (df['ep_direction'] == 'up') & (df['key_dist_pct'] < 33)),
    ('key_dist < 50%',
     lambda df: df['key_dist_pct'] < 50),
    ('key_dist < 33%',
     lambda df: df['key_dist_pct'] < 33),
    ('down + key_dist < 50%',
     lambda df: (df['ep_direction'] == 'down') & (df['key_dist_pct'] < 50)),
    ('down + key_dist < 33%',
     lambda df: (df['ep_direction'] == 'down') & (df['key_dist_pct'] < 33)),
]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def metrics(pl_series: pd.Series, label: str) -> dict:
    n        = len(pl_series)
    total    = pl_series.sum()
    avg      = pl_series.mean()
    win_rate = (pl_series > 0).mean() * 100
    sharpe   = avg / pl_series.std() * np.sqrt(52) if pl_series.std() > 0 else np.nan

    # Max drawdown (in points)
    cum   = pl_series.cumsum()
    peak  = cum.cummax()
    dd    = cum - peak
    max_dd = dd.min()

    # Max consecutive losses
    is_loss = (pl_series <= 0).astype(int)
    consec  = 0
    max_consec = 0
    for v in is_loss:
        consec = consec + 1 if v else 0
        max_consec = max(max_consec, consec)

    return {
        'label':       label,
        'n_scaled':    None,   # filled by caller
        'pct_scaled':  None,
        'total_pts':   round(total, 1),
        'avg_pts':     round(avg, 2),
        'win_pct':     round(win_rate, 1),
        'sharpe':      round(sharpe, 3),
        'max_dd_pts':  round(max_dd, 1),
        'max_consec_l': max_consec,
    }


def weighted_pl(df: pd.DataFrame, mask: pd.Series, mult: float) -> pd.Series:
    factors = np.where(mask, mult, 1.0)
    return df['total_pl_points'] * factors


def two_sided_pl(df: pd.DataFrame,
                 up_mask: pd.Series, up_mult: float,
                 dn_mask: pd.Series, dn_mult: float) -> pd.Series:
    """Scale up where up_mask, scale down where dn_mask, 1× elsewhere."""
    factors = np.ones(len(df))
    factors[up_mask.values] = up_mult
    factors[dn_mask.values] = dn_mult
    return df['total_pl_points'] * factors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not os.path.exists(INPUT_CSV):
        print(f'ERROR: {INPUT_CSV} not found. Run annotate_artemis.py first.')
        sys.exit(1)

    df = pd.read_csv(INPUT_CSV, parse_dates=['entry_time'])
    df = df[df['total_pl_points'].notna()].copy()
    n  = len(df)
    print(f'\nLoaded {n} trades  ({INSTRUMENT})  '
          f'{df["entry_time"].min().date()} → {df["entry_time"].max().date()}')

    # Baseline
    base_pl  = df['total_pl_points']
    base_m   = metrics(base_pl, 'baseline (1×)')
    base_m['n_scaled']   = 0
    base_m['pct_scaled'] = 0.0

    rows = [base_m]

    for cond_label, cond_fn in CONDITIONS:
        # Build mask (NaN-safe: treat NaN as False)
        try:
            mask = cond_fn(df).fillna(False)
        except Exception:
            mask = pd.Series(False, index=df.index)

        n_scaled  = mask.sum()
        pct_scaled = round(n_scaled / n * 100, 1)

        for mult in MULTIPLIERS:
            pl  = weighted_pl(df, mask, mult)
            m   = metrics(pl, f'{cond_label}  ×{mult}')
            m['n_scaled']   = int(n_scaled)
            m['pct_scaled'] = pct_scaled
            rows.append(m)

    result = pd.DataFrame(rows)

    # Uplift vs baseline
    base_total  = base_m['total_pts']
    base_sharpe = base_m['sharpe']
    result['uplift_pts'] = (result['total_pts'] - base_total).round(1)
    result['uplift_pct'] = ((result['total_pts'] / base_total - 1) * 100).round(1)
    result['sharpe_delta'] = (result['sharpe'] - base_sharpe).round(3)

    # Save CSV
    out_path = os.path.join(OUT_DIR, f'lot_sizing_sweep_{INSTRUMENT}.csv')
    result.to_csv(out_path, index=False)

    # Print
    print(f'\n{"=" * 100}')
    print(f'LOT SIZING SWEEP — {INSTRUMENT.upper()}  (baseline total: {base_total:+.1f} pts, '
          f'Sharpe: {base_sharpe:.3f})')
    print(f'{"=" * 100}')
    hdr = (f"  {'Condition':<32} {'Mult':>5} {'Scaled':>10} "
           f"{'Total':>8} {'Uplift':>8} {'Uplift%':>8} "
           f"{'Avg':>7} {'Win%':>6} {'Sharpe':>7} {'MaxDD':>8} {'MaxCL':>6}")
    sep = '  ' + '-' * 96
    print(hdr)
    print(sep)

    prev_cond = None
    for _, r in result.iterrows():
        cond_part = r['label'].split('×')[0].strip() if '×' in r['label'] else r['label']
        if cond_part != prev_cond:
            if prev_cond is not None:
                print()
            prev_cond = cond_part

        mult_str   = f"×{r['label'].split('×')[1]}" if '×' in r['label'] else '×1.0'
        scaled_str = (f"{int(r['n_scaled'])} ({r['pct_scaled']:.0f}%)"
                      if r['n_scaled'] is not None and not pd.isna(r['n_scaled']) else '—')
        uplift_str = (f"{r['uplift_pts']:+.1f}" if not pd.isna(r['uplift_pts']) else '—')
        uplift_pct = (f"{r['uplift_pct']:+.1f}%" if not pd.isna(r['uplift_pct']) else '—')
        sd_str     = (f"{r['sharpe_delta']:+.3f}" if not pd.isna(r['sharpe_delta']) else '—')

        print(f"  {cond_part:<32} {mult_str:>5} {scaled_str:>10} "
              f"{r['total_pts']:>+8.1f} {uplift_str:>8} {uplift_pct:>8} "
              f"{r['avg_pts']:>+7.2f} {r['win_pct']:>5.1f}% "
              f"{r['sharpe']:>7.3f} {r['max_dd_pts']:>+8.1f} {int(r['max_consec_l']):>6}")

    print(f'\nSaved: {out_path}')

    # Best by uplift_pts (exclude baseline)
    non_base = result[result['label'] != 'baseline (1×)']
    if not non_base.empty:
        best_total  = non_base.loc[non_base['total_pts'].idxmax()]
        best_sharpe = non_base.loc[non_base['sharpe'].idxmax()]
        print(f'\n── Best by total P&L uplift ──────────────────────────────────────────')
        print(f'  {best_total["label"]}  →  {best_total["total_pts"]:+.1f} pts  '
              f'(+{best_total["uplift_pts"]:.1f},  {best_total["uplift_pct"]:+.1f}%  '
              f'Sharpe {best_total["sharpe"]:.3f})')
        print(f'── Best by Sharpe ────────────────────────────────────────────────────')
        print(f'  {best_sharpe["label"]}  →  Sharpe {best_sharpe["sharpe"]:.3f}  '
              f'({best_sharpe["sharpe_delta"]:+.3f})  total {best_sharpe["total_pts"]:+.1f} pts')

    # ------------------------------------------------------------------
    # Two-sided sweep: scale UP on down+key_dist<50%, scale DOWN on up
    # ------------------------------------------------------------------
    UP_SCALE_CONDS = [
        ('down + key_dist < 50%',
         lambda df: (df['ep_direction'] == 'down') & (df['key_dist_pct'] < 50)),
    ]
    DN_SCALE_CONDS = [
        ('up (any)',
         lambda df: df['ep_direction'] == 'up'),
        ('up + key_dist < 50%',
         lambda df: (df['ep_direction'] == 'up') & (df['key_dist_pct'] < 50)),
    ]
    UP_MULTS = [1.5, 1.75, 2.0, 2.5]
    DN_MULTS = [0.5, 0.75]

    print(f'\n{"=" * 100}')
    print(f'TWO-SIDED SWEEP  —  scale UP on down+key_dist<50% ({(df["ep_direction"]=="down") & (df["key_dist_pct"]<50).fillna(False) if False else ""}), '
          f'scale DOWN on up conditions')
    print(f'{"=" * 100}')
    hdr2 = (f"  {'Scale-up cond':<24} {'Scale-dn cond':<22} {'Up×':>5} {'Dn×':>5} "
            f"{'Total':>8} {'Uplift':>8} {'Uplift%':>8} "
            f"{'Avg':>7} {'Sharpe':>7} {'ΔSharpe':>8} {'MaxDD':>8} {'MaxCL':>6}")
    sep2 = '  ' + '-' * 103
    print(hdr2)
    print(sep2)

    two_rows = []
    prev_pair = None
    for up_lbl, up_fn in UP_SCALE_CONDS:
        up_mask = up_fn(df).fillna(False)
        for dn_lbl, dn_fn in DN_SCALE_CONDS:
            dn_mask = dn_fn(df).fillna(False)
            pair = (up_lbl, dn_lbl)
            for up_m in UP_MULTS:
                for dn_m in DN_MULTS:
                    pl  = two_sided_pl(df, up_mask, up_m, dn_mask, dn_m)
                    m   = metrics(pl, f'{up_lbl} | {dn_lbl}')
                    m['up_mult'] = up_m
                    m['dn_mult'] = dn_m
                    m['uplift_pts']   = round(m['total_pts'] - base_total, 1)
                    m['uplift_pct']   = round((m['total_pts'] / base_total - 1) * 100, 1)
                    m['sharpe_delta'] = round(m['sharpe'] - base_sharpe, 3)
                    two_rows.append(m)

                    if pair != prev_pair:
                        if prev_pair is not None:
                            print()
                        prev_pair = pair

                    print(f"  {up_lbl:<24} {dn_lbl:<22} ×{up_m:<4} ×{dn_m:<4} "
                          f"{m['total_pts']:>+8.1f} {m['uplift_pts']:>+8.1f} "
                          f"{m['uplift_pct']:>+7.1f}% "
                          f"{m['avg_pts']:>+7.2f} {m['sharpe']:>7.3f} "
                          f"{m['sharpe_delta']:>+8.3f} {m['max_dd_pts']:>+8.1f} "
                          f"{int(m['max_consec_l']):>6}")

    # Save combined CSV
    two_df = pd.DataFrame(two_rows)
    two_path = os.path.join(OUT_DIR, f'lot_sizing_two_sided_{INSTRUMENT}.csv')
    two_df.to_csv(two_path, index=False)

    if two_rows:
        best_2s_total  = max(two_rows, key=lambda r: r['total_pts'])
        best_2s_sharpe = max(two_rows, key=lambda r: r['sharpe'])
        print(f'\n── Two-sided best by total P&L ───────────────────────────────────────')
        print(f'  {best_2s_total["label"]}  up×{best_2s_total["up_mult"]} dn×{best_2s_total["dn_mult"]}'
              f'  →  {best_2s_total["total_pts"]:+.1f} pts  '
              f'(+{best_2s_total["uplift_pts"]:.1f},  {best_2s_total["uplift_pct"]:+.1f}%  '
              f'Sharpe {best_2s_total["sharpe"]:.3f})')
        print(f'── Two-sided best by Sharpe ──────────────────────────────────────────')
        print(f'  {best_2s_sharpe["label"]}  up×{best_2s_sharpe["up_mult"]} dn×{best_2s_sharpe["dn_mult"]}'
              f'  →  Sharpe {best_2s_sharpe["sharpe"]:.3f}  '
              f'({best_2s_sharpe["sharpe_delta"]:+.3f})  total {best_2s_sharpe["total_pts"]:+.1f} pts')
    print(f'\nSaved: {two_path}')
    print()


if __name__ == '__main__':
    main()
