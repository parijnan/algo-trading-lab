"""
Prometheus - Phase 3, mult 2.0: widen the TARGET1_PCT grid past the original
0.5-2.0% range to resolve the edge-of-grid caveat flagged in README.md
("Mult 2.0's TARGET1_PCT landed on the edge of its own grid... Calmar still
climbing at the top of that range... needs widening past 2.0% before 2.0's
combo can be trusted as a genuine optimum rather than a cut-off").

Scope: mult=2.0 only (the live production candidate). One variable changed
per experiment -- reuses exit_calib_p3.py's own Stage 1 (SL grid) and Stage 2
(target1 grid) methodology verbatim, via direct import of its helper
functions, so results are computed identically and are directly comparable
to the original 0.5-2.0% grid already published. Only the T1 grid range is
new. Does not touch exit_calib_p3.py's own global grids or re-run any other
multiplier.

Same widening-until-resolved pattern already used once in this project for
the analogous multiplier-grid edge case (configs_p3.py's ST_MULTIPLIER_GRID
comment, 2026-09-01): extend past the original edge, check whether Calmar
keeps climbing or plateaus/reverses.

Output: data_sweep/exit_calib_p3_t1_widen_mult20.csv
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configs_p3 as configs  # noqa: E402
from exit_calib_p3 import (  # noqa: E402
    _load_multiplier_data, _run_variant, _summarize, _best_by_calmar,
    SL_GRID, T2_GRID, T2_STARTING_DEFAULT,
)

# T2_STARTING_DEFAULT (2.3%) is what the ORIGINAL 0.5-2.0% Stage 2 grid used
# as its pinned target2 -- but _simulate_trade asserts target2_dist must
# exceed target1_dist, so 2.3% physically cannot serve as the pin once T1
# is tested past 2.3%. Widening the grid past 2.0% therefore requires a
# different pin. Using the current mult-2.0 production T2 (5.0%, this
# script's own Stage-3-equivalent winner and prometheus_configs.py's live
# TARGET2_FLAT_PCT) instead answers the more relevant question directly:
# "given the ACTUAL decided SL/T2, is T1=2.0% still the best T1, or would a
# different T1 paired with the real T2 do better?" -- rather than the
# original grid's neutral placeholder pin. This is a deliberate methodology
# change from the original Stage 2, not a bug; the full 0.5-4.0% range below
# is computed under this single consistent pin so the shape of the curve is
# directly comparable across its own length (no splice at 2.0%).
T2_PIN = 5.0

OUT_FILE = os.path.join(configs.DATA_SWEEP_DIR, 'exit_calib_p3_t1_widen_mult20.csv')

MULT = 2.0

# Original grid (0.5-2.0, 0.25 spacing at the top end) plus new points
# extending past 2.0 at the same 0.25 spacing, out to 4.0 -- wide enough to
# see the curve clearly plateau or reverse, not just add one more point.
T1_GRID_WIDENED = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0,
                    2.25, 2.5, 2.75, 3.0, 3.25, 3.5, 3.75, 4.0]


def main():
    trades, paths = _load_multiplier_data(MULT)
    print(f'Loaded {len(trades)} closed trades for mult {MULT} '
          f'(vintage: trade_summary.csv last entry_ts {trades["entry_ts"].max()})')

    # --- Stage 1: SL grid, target1/target2 pinned at starting defaults ---
    # Re-run against current on-disk data to confirm best_sl hasn't moved
    # with the latest refresh, rather than assuming the README's 2.2%.
    stage1 = []
    for sl in SL_GRID:
        sim = _run_variant(trades, paths, sl, 1.0, T2_STARTING_DEFAULT)
        row = _summarize(sim, MULT, 'sl_grid', 'sl_pct', sl)
        stage1.append(row)
    best_sl = _best_by_calmar(stage1)['value']
    print(f'Stage 1 (SL grid, fresh data): best_sl = {best_sl}%')

    # --- Stage 2 (widened): target1 grid, SL pinned at stage-1 winner,
    # target2 pinned at T2_PIN (5.0%, see comment above -- not the original
    # T2_STARTING_DEFAULT=2.3%, which can't support T1 > 2.3%). ---
    stage2 = []
    for t1 in T1_GRID_WIDENED:
        sim = _run_variant(trades, paths, best_sl, t1, T2_PIN)
        row = _summarize(sim, MULT, 'target1_grid_widened', 'target1_pct', t1)
        stage2.append(row)

    detail_df = pd.DataFrame(stage2)
    os.makedirs(configs.DATA_SWEEP_DIR, exist_ok=True)
    detail_df.to_csv(OUT_FILE, index=False)

    print(f'\nSaved {len(detail_df)} rows to {OUT_FILE}\n')
    pd.set_option('display.width', 220)
    pd.set_option('display.max_columns', None)
    print(detail_df[['value', 'n_trades', 'win_rate_pct', 'total_pnl_rs',
                      'max_drawdown_rs', 'calmar', 'lot1_hit_rate_pct', 'stop_loss_count']]
          .to_string(index=False))

    best = _best_by_calmar(stage2)
    best_t1 = best['value']
    print(f"\nBest by Calmar across widened grid: T1={best_t1}%  Calmar={best['calmar']}")

    # --- Stage 3 (matching exit_calib_p3.py's own Stage 3 exactly): re-run
    # the T2 grid with SL/T1 pinned at their (possibly new) winners, to find
    # the true joint optimum rather than reporting the new T1 isolated
    # against a T2 that was itself never re-optimized against it. ---
    valid_t2_grid = [t2 for t2 in T2_GRID if t2 > best_t1]
    stage3 = []
    for t2 in valid_t2_grid:
        sim = _run_variant(trades, paths, best_sl, best_t1, t2)
        row = _summarize(sim, MULT, 'target2_grid_rerun', 'target2_pct', t2)
        stage3.append(row)
    best_t2 = _best_by_calmar(stage3)['value']

    final_sim = _run_variant(trades, paths, best_sl, best_t1, best_t2)
    final = _summarize(final_sim, MULT, 'final_widened', 'combo', f'sl{best_sl}_t1{best_t1}_t2{best_t2}')

    baseline_sim = _run_variant(trades, paths, 2.2, 2.0, 5.0)
    baseline = _summarize(baseline_sim, MULT, 'baseline_production', 'combo', 'sl2.2_t1_2.0_t25.0')

    print(f"\nStage 3 (T2 grid re-run at T1={best_t1}%): best_t2 = {best_t2}%\n")
    print('Joint-optimum candidate vs. current production combo (same trade set, same methodology):')
    cmp_df = pd.DataFrame([baseline, final])[
        ['param', 'value', 'n_trades', 'win_rate_pct', 'total_pnl_rs', 'max_drawdown_rs', 'calmar']]
    print(cmp_df.to_string(index=False))


if __name__ == '__main__':
    main()
