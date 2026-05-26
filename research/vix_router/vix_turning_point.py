"""
vix_turning_point.py — Full-history validation of the VIX turning-point signal.

Hypothesis: "VIX near recent resistance (pos_vs_max ≥ thresh) AND rolling over
(roc < 0)" predicts forward VIX direction, specifically VIX cooling (negative
fwd change), across the full ~1,800-day daily VIX history (2019–2026).

This decouples the idea from the 121-trade Athena sample before building anything.

Approach:
  1. Linear Spearman battery — pos_vs_max and roc3/roc5 → fwd_chg_h3 / h5 / range_h5
  2. Conditional event study — when setup fires, what does VIX actually do?
  3. Threshold sensitivity — vary pos_vs_max thresh and roc window
  4. Per-year stability of the conditional effect

Run:
    python research/vix_router/vix_turning_point.py
"""

import os
import sys
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

_THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
_OUT_DIR   = os.path.join(_THIS_DIR, 'outputs')
os.makedirs(_OUT_DIR, exist_ok=True)

sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, 'research', 'range_detection'))

from research.vix_router.data_layer import load_vix_daily  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Feature construction (all no-lookahead — uses prev-day close via shift(1))
# ─────────────────────────────────────────────────────────────────────────────

def build_features(vix_daily: pd.DataFrame, roc_window: int = 3,
                   max_window: int = 20) -> pd.DataFrame:
    """
    Compute turning-point features from daily VIX close.
    All signals shifted by 1: at decision time (10:30) we know yesterday's close only.

    Returns DataFrame with columns:
      vix_close    - raw daily close (for regime conditioning)
      pos_vs_max   - VIX / rolling(max_window).max()   [proximity to resistance]
      roc          - pct_change(roc_window)             [slope / momentum]
      pos_in_range - (VIX - min20) / (max20 - min20)   [position in 20d range]
    """
    c = vix_daily['close']

    max20  = c.rolling(max_window).max()
    min20  = c.rolling(max_window).min()

    pos_vs_max   = c / max20
    roc          = c.pct_change(roc_window)
    pos_in_range = (c - min20) / (max20 - min20).replace(0, np.nan)

    df = pd.DataFrame({
        'vix_close'   : c,
        'pos_vs_max'  : pos_vs_max,
        'roc'         : roc,
        'pos_in_range': pos_in_range,
    })

    # Shift by 1: prev-day close available at 10:30 entry
    feature_cols = ['pos_vs_max', 'roc', 'pos_in_range']
    df[feature_cols] = df[feature_cols].shift(1)

    return df.dropna()


# ─────────────────────────────────────────────────────────────────────────────
# Forward targets (validation-only)
# ─────────────────────────────────────────────────────────────────────────────

def build_targets(vix_daily: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    """
    Compute forward VIX targets for each horizon h:
      fwd_chg_hN  = VIX(t+h) - VIX(t)          [direction — signed change]
      fwd_range_hN = max(VIX[t+1..t+h]) - VIX(t) [vol regime / spike magnitude]
    """
    c = vix_daily['close']
    frames = {'vix_close': c}

    for h in horizons:
        frames[f'fwd_chg_h{h}']   = c.shift(-h) - c
        # Rolling max over next h bars — forward-looking, strictly validation-only
        frames[f'fwd_range_h{h}'] = pd.concat(
            [c.shift(-i) for i in range(1, h + 1)], axis=1
        ).max(axis=1) - c

    return pd.DataFrame(frames).dropna()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Linear Spearman battery
# ─────────────────────────────────────────────────────────────────────────────

def linear_battery(features: pd.DataFrame, targets: pd.DataFrame,
                   horizons: list[int]) -> pd.DataFrame:
    """
    Spearman ρ for each feature × target pair.
    Returns summary DataFrame.
    """
    aligned = features.join(targets.drop(columns=['vix_close']), how='inner').dropna()
    rows = []
    for feat in ['pos_vs_max', 'roc', 'pos_in_range']:
        for h in horizons:
            for tgt_col in [f'fwd_chg_h{h}', f'fwd_range_h{h}']:
                sub = aligned[[feat, tgt_col]].dropna()
                rho, pval = spearmanr(sub[feat], sub[tgt_col])
                rows.append({
                    'feature'  : feat,
                    'target'   : tgt_col,
                    'n'        : len(sub),
                    'rho'      : round(rho, 4),
                    'pval'     : round(pval, 4),
                    'signif'   : pval < 0.05,
                })
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Conditional event study
# ─────────────────────────────────────────────────────────────────────────────

def event_study(features: pd.DataFrame, targets: pd.DataFrame,
                pm_thresh: float, roc_thresh: float,
                horizons: list[int]) -> dict:
    """
    When setup fires (pos_vs_max >= pm_thresh AND roc < roc_thresh):
      - count N, fire_rate
      - mean/median fwd change
      - hit_rate = P(VIX falls)
      - P(VIX cools > 1pt), P(VIX cools > 2pt)
    Compare to unconditional base rates.
    """
    aligned = features.join(targets.drop(columns=['vix_close']), how='inner').dropna()

    fired = (aligned['pos_vs_max'] >= pm_thresh) & (aligned['roc'] < roc_thresh)
    n_total = len(aligned)
    n_fired = fired.sum()

    result = {
        'pm_thresh' : pm_thresh,
        'roc_thresh': roc_thresh,
        'n_total'   : n_total,
        'n_fired'   : int(n_fired),
        'fire_rate' : round(n_fired / n_total, 4),
        'by_horizon': {},
    }

    for h in horizons:
        col = f'fwd_chg_h{h}'
        tgt     = aligned[col]
        tgt_on  = tgt[fired]
        tgt_off = tgt[~fired]

        base_hit = (tgt < 0).mean()
        on_hit   = (tgt_on < 0).mean() if len(tgt_on) > 0 else np.nan

        result['by_horizon'][h] = {
            'base_hit_rate' : round(base_hit, 4),
            'on_hit_rate'   : round(on_hit, 4),
            'hit_delta'     : round(on_hit - base_hit, 4),
            'base_mean_chg' : round(tgt.mean(), 4),
            'on_mean_chg'   : round(tgt_on.mean(), 4) if len(tgt_on) > 0 else np.nan,
            'off_mean_chg'  : round(tgt_off.mean(), 4),
            'on_median_chg' : round(tgt_on.median(), 4) if len(tgt_on) > 0 else np.nan,
            'p_cool_1pt'    : round((tgt_on < -1.0).mean(), 4) if len(tgt_on) > 0 else np.nan,
            'p_cool_2pt'    : round((tgt_on < -2.0).mean(), 4) if len(tgt_on) > 0 else np.nan,
            'base_cool_2pt' : round((tgt < -2.0).mean(), 4),
            'n_on'          : len(tgt_on),
        }

    return result


# ─────────────────────────────────────────────────────────────────────────────
# 3. Threshold sensitivity
# ─────────────────────────────────────────────────────────────────────────────

def threshold_grid(features: pd.DataFrame, targets: pd.DataFrame,
                   horizons: list[int]) -> pd.DataFrame:
    """
    Sweep pos_vs_max threshold × roc window combination.
    Primary metric: conditional hit_rate and mean_fwd_chg vs base rate.
    """
    rows = []
    pm_thresholds  = [0.80, 0.85, 0.90, 0.95]
    roc_thresholds = [0.0, -0.01]   # <0 or <-1% slope

    aligned = features.join(targets.drop(columns=['vix_close']), how='inner').dropna()

    for pm_t in pm_thresholds:
        for roc_t in roc_thresholds:
            fired = (aligned['pos_vs_max'] >= pm_t) & (aligned['roc'] < roc_t)
            n_on  = fired.sum()
            for h in horizons:
                col     = f'fwd_chg_h{h}'
                tgt_all = aligned[col]
                tgt_on  = tgt_all[fired]
                rows.append({
                    'pm_thresh' : pm_t,
                    'roc_thresh': roc_t,
                    'horizon'   : h,
                    'n_on'      : int(n_on),
                    'fire_pct'  : round(n_on / len(aligned) * 100, 1),
                    'base_hit'  : round((tgt_all < 0).mean(), 4),
                    'on_hit'    : round((tgt_on  < 0).mean(), 4) if n_on > 10 else np.nan,
                    'base_mean' : round(tgt_all.mean(), 4),
                    'on_mean'   : round(tgt_on.mean(), 4) if n_on > 10 else np.nan,
                    'hit_delta' : round(((tgt_on < 0).mean() - (tgt_all < 0).mean()), 4)
                                  if n_on > 10 else np.nan,
                })
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Per-year stability of the conditional effect
# ─────────────────────────────────────────────────────────────────────────────

def peryear_stability(features: pd.DataFrame, targets: pd.DataFrame,
                      pm_thresh: float, h: int) -> pd.DataFrame:
    """
    For the primary threshold combo, compute conditional hit_rate and mean_fwd_chg
    per calendar year. Stable if direction and magnitude are consistent.
    """
    aligned = features.join(targets[[f'fwd_chg_h{h}']], how='inner').dropna()
    fired = (aligned['pos_vs_max'] >= pm_thresh) & (aligned['roc'] < 0)

    rows = []
    for yr in sorted(aligned.index.year.unique()):
        mask_yr = aligned.index.year == yr
        yr_all  = aligned[mask_yr][f'fwd_chg_h{h}']
        yr_on   = aligned[mask_yr & fired][f'fwd_chg_h{h}']
        rows.append({
            'year'       : yr,
            'n_yr'       : len(yr_all),
            'n_fired'    : len(yr_on),
            'base_hit'   : round((yr_all < 0).mean(), 4),
            'on_hit'     : round((yr_on  < 0).mean(), 4) if len(yr_on) >= 3 else np.nan,
            'base_mean'  : round(yr_all.mean(), 4),
            'on_mean'    : round(yr_on.mean(),  4) if len(yr_on) >= 3 else np.nan,
        })
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# 5. VIX cooling specifically — empirical CDF when setup fires
# ─────────────────────────────────────────────────────────────────────────────

def cooling_profile(features: pd.DataFrame, targets: pd.DataFrame,
                    pm_thresh: float, h: int) -> None:
    """
    Print the distribution of fwd_chg when setup fires vs doesn't fire.
    Bins: <-4, -4..-2, -2..-1, -1..0, 0..1, 1..2, 2..4, >4
    """
    aligned = features.join(targets[[f'fwd_chg_h{h}']], how='inner').dropna()
    fired   = (aligned['pos_vs_max'] >= pm_thresh) & (aligned['roc'] < 0)

    bins   = [-np.inf, -4, -2, -1, 0, 1, 2, 4, np.inf]
    labels = ['<-4', '-4..-2', '-2..-1', '-1..0', '0..1', '1..2', '2..4', '>4']

    col  = f'fwd_chg_h{h}'
    on   = pd.cut(aligned[fired][col],   bins=bins, labels=labels)
    off  = pd.cut(aligned[~fired][col],  bins=bins, labels=labels)
    all_ = pd.cut(aligned[col],          bins=bins, labels=labels)

    n_on  = fired.sum()
    n_off = (~fired).sum()
    n_all = len(aligned)

    print(f'\n  VIX fwd change distribution | h={h} | pm_thresh={pm_thresh}')
    print(f'  {"Bucket":<10}  {"All%":>7}  {"Fired%":>7}  {"NotFired%":>9}')
    for lbl in labels:
        p_all = (all_ == lbl).sum() / n_all * 100
        p_on  = (on   == lbl).sum() / n_on  * 100 if n_on  > 0 else 0
        p_off = (off  == lbl).sum() / n_off * 100 if n_off > 0 else 0
        print(f'  {lbl:<10}  {p_all:>6.1f}%  {p_on:>6.1f}%  {p_off:>8.1f}%')


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print('Loading VIX daily (2019+) ...')
    vix_daily = load_vix_daily()
    n = len(vix_daily)
    print(f'  {n} trading days  '
          f'({vix_daily.index.min().date()} → {vix_daily.index.max().date()})')

    HORIZONS = [3, 5]

    # Features use roc_window=3 (3-day slope, matching the Athena-level test)
    print('\nBuilding features (roc_window=3, max_window=20) ...')
    features = build_features(vix_daily, roc_window=3, max_window=20)
    targets  = build_targets(vix_daily, horizons=HORIZONS)
    print(f'  Features: {len(features)} rows  |  Targets: {len(targets)} rows')

    # ── 1. Linear battery ────────────────────────────────────────────────────
    print('\n══ 1. Linear Spearman battery ══════════════════════════════════════════')
    bat = linear_battery(features, targets, HORIZONS)
    print(bat.to_string(index=False))
    bat.to_csv(os.path.join(_OUT_DIR, 'turning_point_linear.csv'), index=False)

    # ── 2. Primary event study (pm_thresh=0.85, roc<0) ───────────────────────
    print('\n══ 2. Primary event study (pos_vs_max≥0.85 AND roc3<0) ════════════════')
    es = event_study(features, targets, pm_thresh=0.85, roc_thresh=0.0, horizons=HORIZONS)
    print(f'  Setup fires: {es["n_fired"]}/{es["n_total"]} days  '
          f'({es["fire_rate"]*100:.1f}%)')
    for h, s in es['by_horizon'].items():
        print(f'\n  Horizon h={h}:')
        print(f'    base hit-rate : {s["base_hit_rate"]:.1%}')
        print(f'    fired hit-rate: {s["on_hit_rate"]:.1%}  '
              f'(Δ={s["hit_delta"]:+.3f})')
        print(f'    base mean chg : {s["base_mean_chg"]:+.3f}')
        print(f'    fired mean chg: {s["on_mean_chg"]:+.3f}  '
              f'(n_on={s["n_on"]})')
        print(f'    fired median  : {s["on_median_chg"]:+.3f}')
        print(f'    P(cool>1pt) fired: {s["p_cool_1pt"]:.1%}  '
              f'vs base {(targets[f"fwd_chg_h{h}"] < -1).mean():.1%}')
        print(f'    P(cool>2pt) fired: {s["p_cool_2pt"]:.1%}  '
              f'vs base {s["base_cool_2pt"]:.1%}')

    # ── 3. Cooling profile ───────────────────────────────────────────────────
    print('\n══ 3. VIX cooling profile (fwd change distribution) ═══════════════════')
    for h in HORIZONS:
        cooling_profile(features, targets, pm_thresh=0.85, h=h)

    # ── 4. Threshold sensitivity grid ────────────────────────────────────────
    print('\n══ 4. Threshold sensitivity ════════════════════════════════════════════')
    grid = threshold_grid(features, targets, HORIZONS)
    # Show h=5 only for compactness
    g5 = grid[grid['horizon'] == 5].copy()
    print(g5.to_string(index=False))
    grid.to_csv(os.path.join(_OUT_DIR, 'turning_point_grid.csv'), index=False)

    # ── 5. Per-year stability ────────────────────────────────────────────────
    print('\n══ 5. Per-year stability (pos_vs_max≥0.85 AND roc3<0, h=5) ═══════════')
    stab = peryear_stability(features, targets, pm_thresh=0.85, h=5)
    print(stab.to_string(index=False))
    stab.to_csv(os.path.join(_OUT_DIR, 'turning_point_stability.csv'), index=False)

    # ── 6. roc_window=5 check ────────────────────────────────────────────────
    print('\n══ 6. roc_window=5 (5-day slope, same primary threshold) ══════════════')
    feat5 = build_features(vix_daily, roc_window=5, max_window=20)
    es5   = event_study(feat5, targets, pm_thresh=0.85, roc_thresh=0.0, horizons=[5])
    s5    = es5['by_horizon'][5]
    print(f'  n_fired={es5["n_fired"]}  fire_rate={es5["fire_rate"]*100:.1f}%')
    print(f'  h=5  base_hit={s5["base_hit_rate"]:.1%}  '
          f'fired_hit={s5["on_hit_rate"]:.1%}  Δ={s5["hit_delta"]:+.3f}  '
          f'on_mean={s5["on_mean_chg"]:+.3f}')

    print('\nDone. Outputs written to research/vix_router/outputs/')
    print('  turning_point_linear.csv')
    print('  turning_point_grid.csv')
    print('  turning_point_stability.csv')


if __name__ == '__main__':
    main()
