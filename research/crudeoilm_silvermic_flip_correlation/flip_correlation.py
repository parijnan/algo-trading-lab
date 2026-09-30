"""
Quantifies a live-monitoring observation (user, 2026-09-29/30): CRUDEOILM and SILVERMIC ST_15
flips, at their own DECIDED production multipliers (CRUDEOILM 2.0 -- prometheus_backtest/phase3/
configs_p3.py; SILVERMIC 2.5 -- selene_backtest/selene_configs.py, both ST_PERIOD=10), appear to
land in the same 15-min window and be inverse in direction more often than chance.

Both instruments' own raw ST_15 signal is computed independently, over the overlap of their two
data windows -- CRUDEOILM only has genuine history from 2026-01-30 (prometheus_backtest/README.md
"Current coverage"), so that's the real constraint, not SILVERMIC's own much longer Fyers history.
No back-adjustment: both instruments' own production Phase 2/3 sweeps already established their
monthly roll gaps are small enough to splice naively (typhon_configs.py's own module docstring
records this explicitly for the contrast against NATGASMINI).

Two separate claims, tested separately:
  1. TIMING: do flips co-occur in the same 15-min bar more than a random-placement null would
     predict? (permutation test: shuffle which of the available session bars carry a SILVERMIC
     flip, holding its own flip COUNT fixed, many times; compare the observed same-bar overlap
     count against that null distribution.)
  2. DIRECTION: conditional on a same-bar co-occurrence, is the direction pairing inverse
     (bullish/bearish) more often than the 50% a coin-flip would give? (exact binomial test.)

Output: outputs/flip_correlation_summary.txt (both instruments' own flip counts, the
co-occurrence count at exact/±1-bar tolerance, the permutation-test p-value, and the binomial
test on direction).
"""

import os
import sys

import numpy as np
import pandas as pd

_REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
sys.path.insert(0, os.path.join(_REPO_ROOT, 'prometheus_backtest'))
sys.path.insert(0, os.path.join(_REPO_ROOT, 'selene_backtest'))
import data_loader_p3 as _p3                                    # noqa: E402
from selene_data_loader import load_futures_1min as load_silvermic   # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'outputs')

ST_PERIOD = 10
CRUDEOILM_MULT = 2.0   # decided production config, prometheus_backtest/phase3/configs_p3.py
SILVERMIC_MULT = 2.5   # decided production config, selene_backtest/selene_configs.py

START = '2026-01-30'   # CRUDEOILM's own genuine data start (prometheus_backtest/README.md);
                        # the real constraint on the overlap window, not SILVERMIC's own longer one.

N_PERMUTATIONS = 5000
RNG_SEED = 20260930


def _flip_series(df_1m: pd.DataFrame, mult: float) -> pd.DataFrame:
    df_15m = _p3.resample_ohlcv(df_1m, '15min')
    df_15m = _p3.compute_st(df_15m, ST_PERIOD, mult)
    flips = df_15m[df_15m['trend_flip'].fillna(False)].copy()
    flips['direction'] = np.where(flips['trend'], 'bullish', 'bearish')
    return df_15m, flips[['direction']]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    lines = []

    def log(s=''):
        print(s)
        lines.append(s)

    log('Loading CRUDEOILM 1-min data...')
    crude_1m = _p3.load_futures_1min('CRUDEOILM', start=START)
    log(f'  {len(crude_1m):,} 1-min bars, {crude_1m.index.min()} -> {crude_1m.index.max()}')

    log('Loading SILVERMIC 1-min data...')
    silver_1m = load_silvermic(start=START)
    log(f'  {len(silver_1m):,} 1-min bars, {silver_1m.index.min()} -> {silver_1m.index.max()}')

    crude_15m, crude_flips = _flip_series(crude_1m, CRUDEOILM_MULT)
    silver_15m, silver_flips = _flip_series(silver_1m, SILVERMIC_MULT)

    log(f'\nCRUDEOILM (mult {CRUDEOILM_MULT}): {len(crude_flips)} flips over {len(crude_15m)} 15-min bars')
    log(f'SILVERMIC (mult {SILVERMIC_MULT}): {len(silver_flips)} flips over {len(silver_15m)} 15-min bars')

    # Common bar grid: both instruments trade the same MCX session hours, but restrict to
    # timestamps that genuinely exist in BOTH 15m series (handles any residual session/holiday
    # mismatch cleanly rather than assuming the grids are identical).
    common_idx = crude_15m.index.intersection(silver_15m.index)
    log(f'\nCommon 15-min bars across both series: {len(common_idx):,} '
        f'({common_idx.min()} -> {common_idx.max()})')

    crude_flip_bars = set(crude_flips.index) & set(common_idx)
    silver_flip_bars = set(silver_flips.index) & set(common_idx)
    log(f'CRUDEOILM flip bars within the common grid: {len(crude_flip_bars)}')
    log(f'SILVERMIC flip bars within the common grid: {len(silver_flip_bars)}')

    # ---- timing: exact-bar and +/-1-bar (15 min) tolerance --------------------------------
    exact_overlap = crude_flip_bars & silver_flip_bars

    common_sorted = sorted(common_idx)
    pos = {ts: i for i, ts in enumerate(common_sorted)}

    def within_one_bar(a_bars, b_bars):
        b_positions = {pos[ts] for ts in b_bars if ts in pos}
        hits = set()
        for ts in a_bars:
            if ts not in pos:
                continue
            p = pos[ts]
            if p in b_positions or (p - 1) in b_positions or (p + 1) in b_positions:
                hits.add(ts)
        return hits

    near_overlap = within_one_bar(crude_flip_bars, silver_flip_bars)

    log(f'\nExact-same-bar co-occurrences: {len(exact_overlap)} '
        f'(out of {len(crude_flip_bars)} CRUDEOILM flips, {len(exact_overlap) / len(crude_flip_bars) * 100:.1f}% of them)')
    log(f'Within +/-1 bar (<=15 min) co-occurrences: {len(near_overlap)} '
        f'({len(near_overlap) / len(crude_flip_bars) * 100:.1f}% of CRUDEOILM flips)')

    # ---- permutation test: is the exact-bar overlap count more than random placement gives? ----
    rng = np.random.default_rng(RNG_SEED)
    common_arr = np.array(common_sorted)
    n_common = len(common_arr)
    n_silver_flips_common = len(silver_flip_bars)
    crude_flip_positions = np.array([pos[ts] for ts in crude_flip_bars if ts in pos])

    null_counts = np.empty(N_PERMUTATIONS, dtype=int)
    for i in range(N_PERMUTATIONS):
        random_silver_positions = rng.choice(n_common, size=n_silver_flips_common, replace=False)
        null_counts[i] = len(set(crude_flip_positions) & set(random_silver_positions))

    observed = len(exact_overlap)
    p_value = float((null_counts >= observed).mean())
    log(f'\nPermutation test (N={N_PERMUTATIONS}, SILVERMIC flip bars randomly reshuffled across '
        f'the common grid, CRUDEOILM flip bars held fixed):')
    log(f'  null mean overlap: {null_counts.mean():.2f}, null std: {null_counts.std():.2f}, '
        f'null max: {null_counts.max()}')
    log(f'  observed overlap: {observed}')
    log(f'  P(null overlap >= observed) = {p_value:.4f}'
        + ('  <-- ' if p_value < 0.05 else '  ')
        + ('statistically significant at 5%' if p_value < 0.05 else 'not significant at 5%'))

    # ---- direction: conditional on exact-bar co-occurrence, inverse vs same direction ----
    pairs = []
    for ts in sorted(exact_overlap):
        c_dir = crude_flips.loc[ts, 'direction']
        s_dir = silver_flips.loc[ts, 'direction']
        pairs.append((ts, c_dir, s_dir, c_dir != s_dir))
    pairs_df = pd.DataFrame(pairs, columns=['ts', 'crudeoilm_direction', 'silvermic_direction', 'inverse'])
    pairs_df.to_csv(os.path.join(OUT_DIR, 'cooccurring_flip_pairs.csv'), index=False)

    n_inverse = int(pairs_df['inverse'].sum())
    n_pairs = len(pairs_df)
    log(f'\nOf {n_pairs} exact-same-bar co-occurring flip pairs: {n_inverse} inverse-direction '
        f'({n_inverse / n_pairs * 100:.1f}%), {n_pairs - n_inverse} same-direction '
        f'({(n_pairs - n_inverse) / n_pairs * 100:.1f}%)')

    from scipy import stats
    binom_result = stats.binomtest(n_inverse, n_pairs, p=0.5, alternative='two-sided')
    log(f'Binomial test vs. a fair 50/50 coin: p-value = {binom_result.pvalue:.4f}'
        + ('  <-- statistically significant at 5%' if binom_result.pvalue < 0.05 else '  not significant at 5%'))
    ci = binom_result.proportion_ci(confidence_level=0.95)
    log(f'95% CI on the inverse-direction rate: [{ci.low * 100:.1f}%, {ci.high * 100:.1f}%]')

    log(f'\nPer-pair detail:')
    log(pairs_df.to_string(index=False))

    with open(os.path.join(OUT_DIR, 'flip_correlation_summary.txt'), 'w') as f:
        f.write('\n'.join(lines) + '\n')


if __name__ == '__main__':
    main()
