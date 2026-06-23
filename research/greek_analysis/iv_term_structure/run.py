"""
Branch 3 — IV Term Structure (Athena only)

At entry, measures the volatility term structure between the near (sell) expiry
and far (buy) expiry options.  Both CE and PE legs share the same strike across
expiries, so the ratio ce_far_iv / ce_near_iv is a clean single-strike term-
structure slope — no skew contamination.  Using traded-strike IVs is *better*
than ATM here for exactly this reason.

Metrics per trade:
  near_iv   = mean(ce_near_iv, pe_near_iv)  — avg IV at sell expiry
  far_iv    = mean(ce_far_iv,  pe_far_iv)   — avg IV at buy  expiry
  slope     = far_iv / near_iv              — >1 = contango, <1 = backwardation
  spread    = near_iv - far_iv              — >0 = backwardation ("expensive near vol")

Key questions:
  1. What does the Spearman IC of slope/spread with trade P&L look like?
  2. Is it stable across periods (2020–22 vs 2023+)?
  3. Is it just a VIX proxy?

Artemis is a single-expiry iron condor — no term structure to measure.

Reuses IV cache from Branch 1 (data/iv_cache/*.parquet).  Very fast.

Run from repo root:
  python research/greek_analysis/iv_term_structure/run.py
"""

import os
import sys
import glob
import pandas as pd
import numpy as np
from scipy import stats

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

_HERE     = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..', '..'))

SUMMARY_PATH  = os.path.join(REPO_ROOT, 'athena_backtest', 'data', 'trade_summary.csv')
IV_CACHE_DIR  = os.path.join(REPO_ROOT, 'research', 'greek_analysis', 'data', 'iv_cache')
OUTPUT_DIR    = os.path.join(_HERE, 'data')
OUTPUT_CSV    = os.path.join(OUTPUT_DIR, 'iv_term_structure.csv')

PERIOD_CUT    = '2023-01-01'   # 2020–22 vs 2023+


def _load_entry_ivs(trade_id: int) -> dict | None:
    """Return bar-0 IV values from the IV cache for a given trade_id."""
    pattern = os.path.join(IV_CACHE_DIR, f'trade_{trade_id:04d}_*.parquet')
    files = glob.glob(pattern)
    if not files:
        return None
    df = pd.read_parquet(files[0])
    if df.empty:
        return None
    r = df.iloc[0]
    # IV columns: ce_sell_iv (near), ce_buy_iv (far), pe_sell_iv (near), pe_buy_iv (far)
    return {
        'ce_near_iv': float(r['ce_sell_iv']) if not pd.isna(r['ce_sell_iv']) else float('nan'),
        'ce_far_iv':  float(r['ce_buy_iv'])  if not pd.isna(r['ce_buy_iv'])  else float('nan'),
        'pe_near_iv': float(r['pe_sell_iv']) if not pd.isna(r['pe_sell_iv']) else float('nan'),
        'pe_far_iv':  float(r['pe_buy_iv'])  if not pd.isna(r['pe_buy_iv'])  else float('nan'),
    }


def build_dataset() -> pd.DataFrame:
    summary = pd.read_csv(SUMMARY_PATH, parse_dates=['entry_time'])
    summary['trade_id']   = range(1, len(summary) + 1)
    summary['entry_date'] = summary['entry_time'].dt.date.astype(str)

    rows = []
    n_missing = 0
    for _, row in summary.iterrows():
        tid  = int(row['trade_id'])
        ivs  = _load_entry_ivs(tid)
        if ivs is None or any(np.isnan(v) for v in ivs.values()):
            n_missing += 1
            continue

        near_iv = (ivs['ce_near_iv'] + ivs['pe_near_iv']) / 2.0
        far_iv  = (ivs['ce_far_iv']  + ivs['pe_far_iv'])  / 2.0
        ce_slope = ivs['ce_far_iv'] / ivs['ce_near_iv']
        pe_slope = ivs['pe_far_iv'] / ivs['pe_near_iv']

        rows.append({
            'trade_id':      tid,
            'entry_date':    row['entry_date'],
            'entry_vix':     float(row['entry_vix']),
            'ce_near_iv':    round(ivs['ce_near_iv'], 4),
            'ce_far_iv':     round(ivs['ce_far_iv'],  4),
            'pe_near_iv':    round(ivs['pe_near_iv'], 4),
            'pe_far_iv':     round(ivs['pe_far_iv'],  4),
            'near_iv':       round(near_iv,  4),
            'far_iv':        round(far_iv,   4),
            'slope':         round(far_iv / near_iv, 5),   # >1=contango, <1=backwardation
            'ce_slope':      round(ce_slope,  5),
            'pe_slope':      round(pe_slope,  5),
            'spread':        round(near_iv - far_iv, 4),   # >0=backwardation
            'total_pl':      float(row['total_pl_points']),
            'period':        '2020-22' if row['entry_date'] < PERIOD_CUT else '2023+',
        })

    if n_missing:
        print(f"  WARNING: {n_missing} trades skipped (IV cache missing or NaN)")

    return pd.DataFrame(rows)


def _spearman(x: pd.Series, y: pd.Series, label: str) -> None:
    mask = x.notna() & y.notna()
    xm, ym = x[mask], y[mask]
    coef, p = stats.spearmanr(xm, ym)
    stars = '***' if p < 0.01 else ('**' if p < 0.05 else ('*' if p < 0.10 else ''))
    print(f"  {label:<30s}  IC={coef:+.4f}  p={p:.4f} {stars}  n={len(xm)}")


def _tercile_table(df: pd.DataFrame, signal_col: str, label: str) -> None:
    df = df.copy()
    df['tercile'] = pd.qcut(df[signal_col], q=3, labels=['low', 'mid', 'high'])
    tbl = df.groupby('tercile', observed=True)['total_pl'].agg(['mean', 'median', 'count'])
    tbl.columns = ['mean_pl', 'median_pl', 'n']
    print(f"\n  {label} — tercile P&L (pts):")
    print(f"  {'tercile':<8}  {'n':>4}  {'mean':>8}  {'median':>8}")
    for lbl, row in tbl.iterrows():
        print(f"  {lbl:<8}  {int(row.n):>4}  {row.mean_pl:>+8.1f}  {row.median_pl:>+8.1f}")


def _print_analysis(df: pd.DataFrame) -> None:
    print("=" * 72)
    print("BRANCH 3 — IV TERM STRUCTURE (ATHENA)")
    print("=" * 72)
    print(f"\n  Trades: {len(df)}  |  {df['period'].value_counts().to_dict()}")

    # Descriptive stats
    print("\n  IV Term Structure at Entry:")
    print(f"  {'metric':<14}  {'mean':>8}  {'std':>8}  {'min':>8}  {'max':>8}")
    for col in ['near_iv', 'far_iv', 'slope', 'spread']:
        m, s = df[col].mean(), df[col].std()
        lo,  hi = df[col].min(), df[col].max()
        print(f"  {col:<14}  {m:>+8.4f}  {s:>8.4f}  {lo:>+8.4f}  {hi:>+8.4f}")

    contango_pct = (df['slope'] > 1).mean() * 100
    print(f"\n  Contango (slope > 1): {contango_pct:.1f}% of trades")

    # IC — full sample
    print("\n  Spearman IC vs total_pl — full sample:")
    _spearman(df['slope'],   df['total_pl'], 'slope (far/near)')
    _spearman(df['spread'],  df['total_pl'], 'spread (near-far)')
    _spearman(df['near_iv'], df['total_pl'], 'near_iv')
    _spearman(df['far_iv'],  df['total_pl'], 'far_iv')
    _spearman(df['entry_vix'], df['total_pl'], 'entry_vix (confound check)')

    # IC — period split
    for period, grp in df.groupby('period'):
        print(f"\n  Spearman IC vs total_pl — {period} (n={len(grp)}):")
        _spearman(grp['slope'],   grp['total_pl'], 'slope (far/near)')
        _spearman(grp['spread'],  grp['total_pl'], 'spread (near-far)')
        _spearman(grp['near_iv'], grp['total_pl'], 'near_iv')
        _spearman(grp['far_iv'],  grp['total_pl'], 'far_iv')
        _spearman(grp['entry_vix'], grp['total_pl'], 'entry_vix (confound check)')

    # Tercile tables (full sample)
    _tercile_table(df, 'slope',  'slope')
    _tercile_table(df, 'spread', 'spread')

    # CE vs PE slope consistency
    print("\n  CE vs PE slope correlation:")
    coef, p = stats.spearmanr(df['ce_slope'], df['pe_slope'])
    print(f"  spearman(ce_slope, pe_slope) = {coef:.4f}  p={p:.4f}")

    # VIX partial correlation (residual IC after controlling for VIX)
    print("\n  Partial correlation (VIX confound):")
    vix = df['entry_vix'].values
    X   = np.column_stack([vix, np.ones(len(vix))])
    pl_resid = df['total_pl'].values - X @ np.linalg.lstsq(X, df['total_pl'].values, rcond=None)[0]
    for sig, sig_label in [('slope', 'slope'), ('spread', 'spread')]:
        sig_resid = df[sig].values - X @ np.linalg.lstsq(X, df[sig].values, rcond=None)[0]
        coef_raw, _     = stats.spearmanr(df[sig], df['total_pl'])
        coef_par, p_par = stats.spearmanr(sig_resid, pl_resid)
        print(f"  {sig_label:<20s}  IC_raw={coef_raw:+.4f}  IC_vix_controlled={coef_par:+.4f}  p={p_par:.4f}")


def run():
    print("IV Term Structure — Branch 3 (Athena)")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    df = build_dataset()
    df.to_csv(OUTPUT_CSV, index=False)
    print(f"\nSaved {len(df)} rows → {OUTPUT_CSV}")
    print()
    _print_analysis(df)


if __name__ == '__main__':
    run()
