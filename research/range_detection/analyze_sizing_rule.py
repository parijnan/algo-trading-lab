"""
analyze_sizing_rule.py — Deep analysis of the ×2.0 / ×0.75 two-sided sizing rule.

Rule:
  down + key_dist < 50%  →  up_scale bucket   (×2.0 lots)
  up (any state)         →  dn_scale bucket   (×0.75 lots)
  everything else        →  neutral bucket    (×1.0 lots)

Outputs:
  outputs/sizing_rule_trades.csv    — per-trade detail with bucket, weighted P&L, drawdown
  Console                           — bucket profiles, SL analysis, year-by-year, stress periods
"""

import os
import sys
import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
OUT_DIR    = os.path.join(BASE_DIR, 'outputs')
INPUT_CSV  = os.path.join(OUT_DIR, 'artemis_annotated_nifty.csv')
OUTPUT_CSV = os.path.join(OUT_DIR, 'sizing_rule_trades.csv')

UP_MULT = 2.0
DN_MULT = 0.75
SL_REASONS = {'index_sl', 'option_sl'}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def assign_bucket(df):
    up_mask = ((df['ep_direction'] == 'down') & (df['key_dist_pct'] < 50)).fillna(False)
    dn_mask = (df['ep_direction'] == 'up').fillna(False)
    bucket  = pd.Series('neutral', index=df.index)
    bucket[up_mask] = 'up_scale'
    bucket[dn_mask] = 'dn_scale'
    return bucket


def drawdown_series(cum_pl: pd.Series):
    peak = cum_pl.cummax()
    return cum_pl - peak


def pct(n, total):
    return f'{n} ({n/total*100:.0f}%)' if total else '0 (0%)'


def distrib(s: pd.Series, label='') -> str:
    if s.empty:
        return f'{label}: empty'
    p = s.quantile([0.05, 0.25, 0.50, 0.75, 0.95])
    return (f'{label}  P5={p[0.05]:+.1f}  P25={p[0.25]:+.1f}  P50={p[0.50]:+.1f}'
            f'  P75={p[0.75]:+.1f}  P95={p[0.95]:+.1f}'
            f'  mean={s.mean():+.1f}  std={s.std():.1f}')


def print_section(title):
    print(f'\n{"─" * 72}')
    print(f'  {title}')
    print(f'{"─" * 72}')


def bucket_summary(sub: pd.DataFrame, label: str, base_total: float):
    n     = len(sub)
    wins  = (sub['weighted_pl'] > 0).sum()
    total = sub['weighted_pl'].sum()
    avg   = sub['weighted_pl'].mean()
    std   = sub['weighted_pl'].std()
    sharpe_w = avg / std * np.sqrt(52) if std > 0 else np.nan
    max_dd = drawdown_series(sub['weighted_pl'].cumsum()).min()
    print(f'  {label:<22}  n={n:<4}  win={pct(wins,n):<12}'
          f'  total={total:>+8.1f}  avg={avg:>+7.2f}  std={std:>6.1f}'
          f'  Sharpe={sharpe_w:>6.3f}  MaxDD={max_dd:>+8.1f}')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    df = pd.read_csv(INPUT_CSV, parse_dates=['entry_time', 'pe_exit_time', 'ce_exit_time'])
    df = df[df['week_outcome'] == 'traded'].copy().reset_index(drop=True)
    n  = len(df)

    # ── Assign buckets & weights ──────────────────────────────────────────
    df['bucket']      = assign_bucket(df)
    df['lot_factor']  = df['bucket'].map({'up_scale': UP_MULT, 'dn_scale': DN_MULT,
                                          'neutral': 1.0})
    df['weighted_pl'] = df['total_pl_points'] * df['lot_factor']
    df['year']        = df['entry_time'].dt.year

    # SL flags
    df['sl_pe']     = df['pe_exit_reason'].isin(SL_REASONS)
    df['sl_ce']     = df['ce_exit_reason'].isin(SL_REASONS)
    df['any_sl']    = df['sl_pe'] | df['sl_ce']
    df['double_sl'] = df['sl_pe'] & df['sl_ce']

    # Weighted leg P&L (for directional loss analysis)
    df['w_pe_pl'] = df['pe_pl_points'] * df['lot_factor']
    df['w_ce_pl'] = df['ce_pl_points'] * df['lot_factor']

    # Cumulative & drawdown (full portfolio, chronological order)
    df = df.sort_values('entry_time').reset_index(drop=True)
    df['cum_weighted_pl'] = df['weighted_pl'].cumsum()
    df['drawdown']        = drawdown_series(df['cum_weighted_pl'])

    # Baseline (1× every trade)
    df['cum_baseline_pl'] = df['total_pl_points'].cumsum()

    df.to_csv(OUTPUT_CSV, index=False)
    print(f'Saved: {OUTPUT_CSV}')

    base_total   = df['total_pl_points'].sum()
    scaled_total = df['weighted_pl'].sum()

    bucket_counts = df['bucket'].value_counts()
    n_up  = bucket_counts.get('up_scale', 0)
    n_dn  = bucket_counts.get('dn_scale', 0)
    n_neu = bucket_counts.get('neutral',  0)

    # ── OVERVIEW ─────────────────────────────────────────────────────────
    print_section(f'OVERVIEW  —  rule: up_scale ×{UP_MULT}  /  dn_scale ×{DN_MULT}  /  neutral ×1.0')
    print(f'  Trades: {n}  |  up_scale: {n_up} ({n_up/n*100:.0f}%)  '
          f'dn_scale: {n_dn} ({n_dn/n*100:.0f}%)  neutral: {n_neu} ({n_neu/n*100:.0f}%)')
    print(f'  Baseline total:  {base_total:>+8.1f} pts   avg lots: 1.00×')
    avg_factor = df['lot_factor'].mean()
    print(f'  Scaled total:    {scaled_total:>+8.1f} pts   avg lots: {avg_factor:.3f}×')
    print(f'  Uplift:          {scaled_total-base_total:>+8.1f} pts  '
          f'({(scaled_total/base_total-1)*100:+.1f}%)')

    # ── BUCKET PROFILES ───────────────────────────────────────────────────
    print_section('BUCKET PROFILES  (weighted P&L)')
    print(f'  {"Bucket":<22}  {"n":<5}  {"win%":<12}  {"total":>8}  '
          f'{"avg":>7}  {"std":>6}  {"Sharpe":>7}  {"MaxDD":>8}')
    print('  ' + '-' * 68)
    for lbl, mult in [('up_scale (×2.0)', UP_MULT), ('dn_scale (×0.75)', DN_MULT),
                      ('neutral (×1.0)', 1.0)]:
        key = lbl.split(' ')[0]
        sub = df[df['bucket'] == key]
        bucket_summary(sub, lbl, base_total)

    print()
    print('  Whole portfolio (weighted):')
    bucket_summary(df, f'all  ×avg{avg_factor:.2f}', base_total)

    # ── RETURN DISTRIBUTIONS ──────────────────────────────────────────────
    print_section('RETURN DISTRIBUTIONS (weighted P&L per trade)')
    for key, lbl in [('up_scale','up_scale ×2.0'), ('dn_scale','dn_scale ×0.75'),
                     ('neutral','neutral ×1.0')]:
        sub = df[df['bucket'] == key]['weighted_pl']
        print(f'  {distrib(sub, lbl)}')
    print(f'  {distrib(df["weighted_pl"], "all (weighted)")}')
    print(f'  {distrib(df["total_pl_points"], "all (baseline 1×)")}')

    # ── SL ANALYSIS ───────────────────────────────────────────────────────
    print_section('SL ANALYSIS  (index_sl + option_sl)')
    print(f'  {"Bucket":<22}  {"any SL":>10}  {"PE SL":>10}  {"CE SL":>10}  '
          f'{"double SL":>12}  {"SL avg P&L":>12}  {"no-SL avg P&L":>14}')
    print('  ' + '-' * 90)
    for key, lbl in [('up_scale','up_scale ×2.0'), ('dn_scale','dn_scale ×0.75'),
                     ('neutral','neutral ×1.0'), (None,'all (weighted)')]:
        sub  = df if key is None else df[df['bucket'] == key]
        ns   = len(sub)
        sl   = sub['any_sl'].sum()
        sl_pe = sub['sl_pe'].sum()
        sl_ce = sub['sl_ce'].sum()
        dbl  = sub['double_sl'].sum()
        sl_avg    = sub[sub['any_sl']]['weighted_pl'].mean() if sl else np.nan
        nosl_avg  = sub[~sub['any_sl']]['weighted_pl'].mean() if (ns - sl) else np.nan
        sl_avg_s    = f'{sl_avg:>+.1f}' if not np.isnan(sl_avg) else '   —'
        nosl_avg_s  = f'{nosl_avg:>+.1f}' if not np.isnan(nosl_avg) else '   —'
        print(f'  {lbl:<22}  {pct(sl,ns):>10}  {pct(sl_pe,ns):>10}  {pct(sl_ce,ns):>10}  '
              f'{pct(dbl,ns):>12}  {sl_avg_s:>12}  {nosl_avg_s:>14}')

    # ── WHICH LEG GETS HIT IN UP_SCALE ────────────────────────────────────
    print_section('UP_SCALE BUCKET: exit reason breakdown')
    up  = df[df['bucket'] == 'up_scale']
    print(f'  PE exit reasons:')
    for reason, cnt in up['pe_exit_reason'].value_counts(dropna=False).items():
        avg_pl = up[up['pe_exit_reason'].eq(reason) | (up['pe_exit_reason'].isna() & pd.isna(reason))]['weighted_pl'].mean()
        tag = '← SL' if reason in SL_REASONS else ''
        print(f'    {str(reason):<14} {cnt:>3}  avg total P&L {avg_pl:>+7.1f}  {tag}')
    print(f'  CE exit reasons:')
    for reason, cnt in up['ce_exit_reason'].value_counts(dropna=False).items():
        avg_pl = up[up['ce_exit_reason'].eq(reason) | (up['ce_exit_reason'].isna() & pd.isna(reason))]['weighted_pl'].mean()
        tag = '← SL' if reason in SL_REASONS else ''
        print(f'    {str(reason):<14} {cnt:>3}  avg total P&L {avg_pl:>+7.1f}  {tag}')

    # ── DN_SCALE BUCKET: exit reason breakdown ────────────────────────────
    print_section('DN_SCALE BUCKET: exit reason breakdown')
    dn  = df[df['bucket'] == 'dn_scale']
    print(f'  PE exit reasons:')
    for reason, cnt in dn['pe_exit_reason'].value_counts(dropna=False).items():
        avg_pl = dn[dn['pe_exit_reason'].eq(reason) | (dn['pe_exit_reason'].isna() & pd.isna(reason))]['weighted_pl'].mean()
        tag = '← SL' if reason in SL_REASONS else ''
        print(f'    {str(reason):<14} {cnt:>3}  avg total P&L {avg_pl:>+7.1f}  {tag}')
    print(f'  CE exit reasons:')
    for reason, cnt in dn['ce_exit_reason'].value_counts(dropna=False).items():
        avg_pl = dn[dn['ce_exit_reason'].eq(reason) | (dn['ce_exit_reason'].isna() & pd.isna(reason))]['weighted_pl'].mean()
        tag = '← SL' if reason in SL_REASONS else ''
        print(f'    {str(reason):<14} {cnt:>3}  avg total P&L {avg_pl:>+7.1f}  {tag}')

    # ── YEAR-BY-YEAR ──────────────────────────────────────────────────────
    print_section('YEAR-BY-YEAR  (weighted P&L)')
    print(f'  {"Year":<6}  {"n":>4}  {"up_sc":>6}  {"dn_sc":>6}  {"neut":>6}  '
          f'{"total_w":>9}  {"total_b":>9}  {"win%":>6}  {"SL%":>6}  {"MaxDD":>8}')
    print('  ' + '-' * 68)
    for yr, grp in df.groupby('year'):
        n_yr   = len(grp)
        n_up_y = (grp['bucket'] == 'up_scale').sum()
        n_dn_y = (grp['bucket'] == 'dn_scale').sum()
        n_ne_y = (grp['bucket'] == 'neutral').sum()
        tot_w  = grp['weighted_pl'].sum()
        tot_b  = grp['total_pl_points'].sum()
        win_y  = (grp['weighted_pl'] > 0).mean() * 100
        sl_y   = grp['any_sl'].mean() * 100
        # within-year drawdown on weighted
        yr_dd  = drawdown_series(grp['weighted_pl'].cumsum()).min()
        print(f'  {yr:<6}  {n_yr:>4}  {n_up_y:>6}  {n_dn_y:>6}  {n_ne_y:>6}  '
              f'{tot_w:>+9.1f}  {tot_b:>+9.1f}  {win_y:>5.1f}%  {sl_y:>5.1f}%  {yr_dd:>+8.1f}')

    # ── WORST TRADES (weighted) ───────────────────────────────────────────
    print_section('WORST 10 TRADES BY WEIGHTED P&L')
    worst = df.nsmallest(10, 'weighted_pl')[
        ['entry_time','year','bucket','lot_factor','entry_vix',
         'ep_direction','key_dist_pct','ep_entry_spot_pct',
         'pe_exit_reason','ce_exit_reason',
         'total_pl_points','weighted_pl','any_sl','double_sl']
    ]
    for _, r in worst.iterrows():
        sl_tag = ' ◄DOUBLE-SL' if r['double_sl'] else (' ◄SL' if r['any_sl'] else '')
        print(f'  {str(r["entry_time"].date()):<12}  {r["bucket"]:<10}  ×{r["lot_factor"]:.2f}'
              f'  VIX={r["entry_vix"]:.1f}'
              f'  dir={r["ep_direction"]:<5}  kd={r["key_dist_pct"]:>5.1f}%'
              f'  PE:{str(r["pe_exit_reason"]):<12}  CE:{str(r["ce_exit_reason"]):<12}'
              f'  raw={r["total_pl_points"]:>+7.1f}  wt={r["weighted_pl"]:>+7.1f}'
              f'{sl_tag}')

    # ── BEST TRADES (weighted) ────────────────────────────────────────────
    print_section('BEST 10 TRADES BY WEIGHTED P&L')
    best = df.nlargest(10, 'weighted_pl')[
        ['entry_time','year','bucket','lot_factor','entry_vix',
         'ep_direction','key_dist_pct','ep_entry_spot_pct',
         'total_pl_points','weighted_pl']
    ]
    for _, r in best.iterrows():
        print(f'  {str(r["entry_time"].date()):<12}  {r["bucket"]:<10}  ×{r["lot_factor"]:.2f}'
              f'  VIX={r["entry_vix"]:.1f}'
              f'  dir={r["ep_direction"]:<5}  kd={r["key_dist_pct"]:>5.1f}%'
              f'  raw={r["total_pl_points"]:>+7.1f}  wt={r["weighted_pl"]:>+7.1f}')

    # ── DRAWDOWN DEEP-DIVE ────────────────────────────────────────────────
    print_section('DRAWDOWN ANALYSIS')
    max_dd_val = df['drawdown'].min()
    max_dd_idx = df['drawdown'].idxmin()
    peak_idx   = df.loc[:max_dd_idx, 'cum_weighted_pl'].idxmax()
    trough_trade = df.loc[max_dd_idx]
    peak_trade   = df.loc[peak_idx]
    print(f'  Max drawdown:  {max_dd_val:+.1f} pts')
    print(f'  Peak:   {str(peak_trade["entry_time"].date())}  cum={peak_trade["cum_weighted_pl"]:+.1f}')
    print(f'  Trough: {str(trough_trade["entry_time"].date())}  cum={trough_trade["cum_weighted_pl"]:+.1f}')
    print(f'  Trades in drawdown: {max_dd_idx - peak_idx + 1}')

    # All drawdown periods > 50 pts
    print(f'\n  Drawdown periods exceeding −50 pts:')
    in_dd   = False
    dd_start = None
    dd_peak_val = 0.0
    for i, row in df.iterrows():
        dd = row['drawdown']
        if not in_dd and dd < 0:
            in_dd = True
            dd_start = i
            dd_peak_val = 0.0
        if in_dd:
            dd_peak_val = min(dd_peak_val, dd)
            if dd == 0:
                if dd_peak_val < -50:
                    d_start = df.loc[dd_start, 'entry_time'].date()
                    d_end   = df.loc[i, 'entry_time'].date()
                    trades  = i - dd_start
                    bkts    = df.loc[dd_start:i, 'bucket'].value_counts().to_dict()
                    print(f'    {d_start} → {d_end}  peak_dd={dd_peak_val:+.1f}  '
                          f'trades={trades}  {bkts}')
                in_dd = False
                dd_peak_val = 0.0
    if in_dd and dd_peak_val < -50:
        d_start = df.loc[dd_start, 'entry_time'].date()
        d_end   = df.iloc[-1]['entry_time'].date()
        print(f'    {d_start} → {d_end}  peak_dd={dd_peak_val:+.1f}  (ongoing)')

    # ── CONSECUTIVE LOSSES ────────────────────────────────────────────────
    print_section('CONSECUTIVE LOSS STREAKS  (≥3 trades)')
    streak = 0
    streaks = []
    start_i = None
    for i, row in df.iterrows():
        if row['weighted_pl'] <= 0:
            if streak == 0:
                start_i = i
            streak += 1
        else:
            if streak >= 3:
                streaks.append((start_i, i - 1, streak))
            streak = 0
    if streak >= 3:
        streaks.append((start_i, len(df) - 1, streak))

    if not streaks:
        print('  No streaks of ≥3 consecutive losses.')
    for s_i, e_i, length in streaks:
        s_sub  = df.loc[s_i:e_i]
        total_loss = s_sub['weighted_pl'].sum()
        bkts   = s_sub['bucket'].value_counts().to_dict()
        d_s    = df.loc[s_i, 'entry_time'].date()
        d_e    = df.loc[e_i, 'entry_time'].date()
        print(f'  {d_s} → {d_e}  length={length}  total={total_loss:+.1f}  {bkts}')
        for _, r in s_sub.iterrows():
            sl_tag = ' ◄DBL-SL' if r['double_sl'] else (' ◄SL' if r['any_sl'] else '')
            print(f'    {str(r["entry_time"].date())}  {r["bucket"]:<10}  ×{r["lot_factor"]:.2f}'
                  f'  VIX={r["entry_vix"]:.1f}  wt={r["weighted_pl"]:>+7.1f}{sl_tag}')

    # ── VIX AT ENTRY BY BUCKET ────────────────────────────────────────────
    print_section('VIX AT ENTRY (remember: Artemis is VIX ≤ 16 gate)')
    for key, lbl in [('up_scale','up_scale'), ('dn_scale','dn_scale'), ('neutral','neutral')]:
        sub = df[df['bucket'] == key]['entry_vix']
        if sub.empty:
            continue
        print(f'  {lbl:<12}  '
              f'P10={sub.quantile(.10):.1f}  P25={sub.quantile(.25):.1f}  '
              f'P50={sub.quantile(.50):.1f}  P75={sub.quantile(.75):.1f}  '
              f'P90={sub.quantile(.90):.1f}  mean={sub.mean():.1f}  '
              f'max={sub.max():.1f}')

    # SLs by VIX quartile within each bucket
    print(f'\n  SL rate by VIX quartile (all trades):')
    df['vix_q'] = pd.qcut(df['entry_vix'], 4, labels=['Q1 low','Q2','Q3','Q4 high'])
    for q, grp in df.groupby('vix_q', observed=True):
        sl_r  = grp['any_sl'].mean() * 100
        n_q   = len(grp)
        avg_w = grp['weighted_pl'].mean()
        bkts  = grp['bucket'].value_counts().to_dict()
        print(f'    VIX {q}  n={n_q}  SL%={sl_r:.0f}%  avg_wt={avg_w:+.1f}  {bkts}')

    print()


if __name__ == '__main__':
    main()
