"""
Branch 5 — IV Skew (Athena only)

At entry: PE sell IV vs CE sell IV at the strikes actually traded.

Skew metric: (pe_near_iv - ce_near_iv) / near_iv
  Positive = market pricing more downside risk (put IV > call IV).

Strikes are delta-symmetric (both target_delta ~0.30, mean |delta diff| = 0.027),
so the IV difference is a clean risk-reversal-style vol skew measurement, not
contaminated by moneyness differences.

Hypothesis (pre-registered): low skew → more symmetric calendar → better P&L.
Predicted sign: negative IC (low skew → higher P&L).

Gauntlet: full-sample IC, VIX-controlled IC, slope-controlled IC (Branch 3 found
real surface signal at IC=−0.33; skew must survive partial correlation vs slope to
be independently informative), period split (2020-22 vs 2023+), tercile P&L.

Close condition (from plan): IC < 0.10 OR sign-unstable across periods → close.

Reuses Branch 3 output CSV (iv_term_structure.csv) — no recomputation needed.

Run from repo root:
  python research/greek_analysis/iv_skew/run.py
"""

import os
import sys

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

_HERE     = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..', '..'))

BRANCH3_CSV = os.path.join(
    REPO_ROOT, 'research', 'greek_analysis', 'iv_term_structure', 'data', 'iv_term_structure.csv'
)
OUTPUT_DIR = os.path.join(_HERE, 'data')
OUTPUT_CSV = os.path.join(OUTPUT_DIR, 'iv_skew_signal.csv')

PERIOD_CUT = '2023-01-01'


def build_dataset() -> pd.DataFrame:
    df = pd.read_csv(BRANCH3_CSV)
    # skew = (pe_near_iv - ce_near_iv) / near_iv  (positive = put premium > call premium)
    df['skew'] = (df['pe_near_iv'] - df['ce_near_iv']) / df['near_iv']
    # keep only needed columns
    out = df[['trade_id', 'entry_date', 'entry_vix', 'ce_near_iv', 'pe_near_iv',
              'near_iv', 'slope', 'skew', 'total_pl', 'period']].copy()
    return out


def _spearman(x: pd.Series, y: pd.Series, label: str) -> float:
    mask = x.notna() & y.notna()
    xm, ym = x[mask], y[mask]
    coef, p = stats.spearmanr(xm, ym)
    stars = '***' if p < 0.01 else ('**' if p < 0.05 else ('*' if p < 0.10 else '   '))
    print(f"  {label:<36s}  IC={coef:+.4f}  p={p:.4f} {stars}  n={len(xm)}")
    return coef


def _partial_ic(df: pd.DataFrame, controls: list[str], signal_col: str = 'skew') -> tuple[float, float]:
    """Spearman IC of signal vs total_pl after partialling out controls via OLS residuals."""
    mask = df[controls + [signal_col, 'total_pl']].notna().all(axis=1)
    sub = df[mask].copy()
    X = np.column_stack([sub[c].values for c in controls] + [np.ones(len(sub))])
    sig_resid = sub[signal_col].values - X @ np.linalg.lstsq(X, sub[signal_col].values, rcond=None)[0]
    pl_resid  = sub['total_pl'].values  - X @ np.linalg.lstsq(X, sub['total_pl'].values,  rcond=None)[0]
    coef, p   = stats.spearmanr(sig_resid, pl_resid)
    return coef, p


def _tercile_table(df: pd.DataFrame) -> None:
    d = df.copy()
    d['tercile'] = pd.qcut(d['skew'], q=3, labels=['low', 'mid', 'high'])
    tbl = d.groupby('tercile', observed=True)['total_pl'].agg(['mean', 'median', 'count'])
    tbl.columns = ['mean_pl', 'median_pl', 'n']
    print(f"\n  Skew tercile P&L (pts):")
    print(f"  {'tercile':<8}  {'n':>4}  {'mean':>8}  {'median':>8}")
    for lbl, row in tbl.iterrows():
        print(f"  {lbl:<8}  {int(row.n):>4}  {row.mean_pl:>+8.1f}  {row.median_pl:>+8.1f}")


def _print_analysis(df: pd.DataFrame) -> None:
    print("=" * 72)
    print("BRANCH 5 — IV SKEW (ATHENA)")
    print("=" * 72)
    print(f"\n  Trades: {len(df)}  |  {df['period'].value_counts().to_dict()}")
    print(f"\n  Pre-registered hypothesis: negative IC (low skew → better P&L)")

    # Descriptive stats
    print(f"\n  Skew at entry:")
    print(f"  mean={df['skew'].mean():+.4f}  std={df['skew'].std():.4f}  "
          f"min={df['skew'].min():+.4f}  max={df['skew'].max():+.4f}")
    pct_pos = (df['skew'] > 0).mean() * 100
    print(f"  Positive skew (put > call IV): {pct_pos:.1f}% of trades")

    # Full-sample IC
    print("\n  Full-sample IC vs total_pl:")
    ic_raw = _spearman(df['skew'], df['total_pl'], 'skew (raw)')

    # Partial correlations: VIX, slope, VIX+slope
    print("\n  Partial correlations vs total_pl:")
    ic_vix, p_vix = _partial_ic(df, ['entry_vix'])
    stars_vix = '***' if p_vix < 0.01 else ('**' if p_vix < 0.05 else ('*' if p_vix < 0.10 else '   '))
    print(f"  {'skew | VIX controlled':<36s}  IC={ic_vix:+.4f}  p={p_vix:.4f} {stars_vix}  "
          f"n={df[['entry_vix','skew','total_pl']].notna().all(axis=1).sum()}")

    ic_slope, p_slope = _partial_ic(df, ['slope'])
    stars_slope = '***' if p_slope < 0.01 else ('**' if p_slope < 0.05 else ('*' if p_slope < 0.10 else '   '))
    print(f"  {'skew | slope controlled':<36s}  IC={ic_slope:+.4f}  p={p_slope:.4f} {stars_slope}  "
          f"n={df[['slope','skew','total_pl']].notna().all(axis=1).sum()}")

    ic_both, p_both = _partial_ic(df, ['entry_vix', 'slope'])
    stars_both = '***' if p_both < 0.01 else ('**' if p_both < 0.05 else ('*' if p_both < 0.10 else '   '))
    print(f"  {'skew | VIX + slope controlled':<36s}  IC={ic_both:+.4f}  p={p_both:.4f} {stars_both}  "
          f"n={df[['entry_vix','slope','skew','total_pl']].notna().all(axis=1).sum()}")

    # IC table summary
    print(f"\n  Summary: IC_raw={ic_raw:+.4f}  IC|VIX={ic_vix:+.4f}  "
          f"IC|slope={ic_slope:+.4f}  IC|VIX+slope={ic_both:+.4f}")

    # Period split
    period_ics = {}
    print("\n  Period split IC vs total_pl:")
    for period, grp in df.groupby('period'):
        print(f"  --- {period} (n={len(grp)}) ---")
        ic_p = _spearman(grp['skew'], grp['total_pl'], 'skew')
        period_ics[period] = ic_p

    # Sign stability check
    signs = [np.sign(v) for v in period_ics.values()]
    sign_stable = len(set(signs)) == 1

    # Tercile table
    _tercile_table(df)

    # Verdict
    print("\n" + "=" * 72)
    print("VERDICT")
    print("=" * 72)
    abs_ic = abs(ic_raw)
    close_low_ic   = abs_ic < 0.10
    close_unstable = not sign_stable
    print(f"  |IC_raw| = {abs_ic:.4f}  (close if < 0.10)")
    print(f"  Sign stable across periods: {sign_stable}  "
          f"({', '.join(f'{p}={v:+.4f}' for p, v in sorted(period_ics.items()))})")
    if close_low_ic or close_unstable:
        reasons = []
        if close_low_ic:
            reasons.append(f"|IC| = {abs_ic:.4f} < 0.10")
        if close_unstable:
            reasons.append("sign unstable across periods")
        print(f"\n  CLOSE — {' AND '.join(reasons)}")
        print(f"  Branch 5 does not meet the predictive threshold.")
    else:
        print(f"\n  PASS — IC ≥ 0.10 and sign stable. "
              f"Warrants further barrier analysis before live use.")


def run():
    print("IV Skew — Branch 5 (Athena)")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    df = build_dataset()
    df.to_csv(OUTPUT_CSV, index=False)
    print(f"Saved {len(df)} rows → {OUTPUT_CSV}\n")

    _print_analysis(df)


if __name__ == '__main__':
    run()
