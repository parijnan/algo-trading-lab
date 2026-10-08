"""
Selene - single-lot ONE-target exit test (2026-10-08). Typhon's shape (one lot, one stop, one target, trend-flip exit) applied to Selene. The staged
calibration (exit_calib_selene.py) and the structures comparison (exit_structures_selene.py) both ran two lots, so a one-lot one-target unit was never
simulated; this does it, so the question "could the Typhon engine path serve Selene" has numbers behind it.

Method, one variable at a time per the repo convention:
  * Same exit simulator as exit_calib_selene.py (stop wins a same-minute tie; targets fill at the level or the bar's open on a favourable
    gap-through, the stop at the level or the open on an adverse gap-through; the trade's own trend-flip exit is the fallback; a session's first
    1-min bar is exempt). One lot with one target is the two-lot simulator with BOTH targets at the same level (both lots then exit together),
    halved; every figure below is per ONE lot.
  * Full stop x target grid at each shortlisted multiplier, the whole surface printed, not a staged search. The DISABLED_PCT target row is the
    stop-only control: its stop 3.0% row must reproduce the decided config, which is asserted against the saved structures comparison.
  * Walk-forward, as Helios's decision did (helios plan 4g): the (stop, target) pair is chosen using ONLY entries before
    selene_configs.WALKFORWARD_SPLIT_DATE (by percent-of-price Calmar, since the price rose about 4x and the points version is dominated by 2026),
    then read out of sample on entries from that date. The same is done for stop-only so the two are compared on equal footing.

Output (data_sweep/, gitignored): single_target_grid.csv (every cell, full/pre/post), single_target_walkforward.csv.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selene_configs as configs
from selene_data_loader import load_futures_1min
from exit_calib_selene import PriceSeries, load_trades, run_variant, summarize

SWEEP_DIR = configs.DATA_SWEEP_DIR
GRID_FILE = os.path.join(SWEEP_DIR, 'single_target_grid.csv')
WALK_FILE = os.path.join(SWEEP_DIR, 'single_target_walkforward.csv')
SPLIT = pd.Timestamp(configs.WALKFORWARD_SPLIT_DATE)
LOTS = configs.SINGLE_TARGET_LOTS_IN_SIM


def run_one_lot(trades: list, p: PriceSeries, sl: float, target: float) -> pd.DataFrame:
    """Per-trade results for ONE lot with one stop and one target (target == DISABLED_PCT: stop only). Points and percent are per lot."""
    sim = run_variant(trades, p, sl, target, target)
    sim['pnl_pts'] = sim['pnl_pts'] / LOTS
    sim['pnl_pct'] = sim['pnl_pct'] / LOTS
    return sim


def cell(sim: pd.DataFrame, mult: float, sl: float, target: float) -> dict:
    r = summarize(sim, mult, 'single_target', 'combo', '')
    for k in ('stage', 'param', 'value', 'lot2_hit_rate_pct'):
        r.pop(k)
    r['target_hit_rate_pct'] = r.pop('lot1_hit_rate_pct')
    r.update({'sl_pct': sl, 'target_pct': target, 'target_on': target < configs.DISABLED_PCT})
    for tag, part in (('pre', sim[sim['entry_ts'] < SPLIT]), ('post', sim[sim['entry_ts'] >= SPLIT])):
        s = summarize(part, mult, tag, 'combo', '')
        r.update({f'{tag}_trades': s['n_trades'], f'{tag}_pnl_rs': s['total_pnl_rs'], f'{tag}_dd_rs': s['max_drawdown_rs'], f'{tag}_calmar': s['calmar'],
                  f'{tag}_calmar_pct': s['calmar_pct'], f'{tag}_win_pct': s['win_rate_pct']})
    return r


def grid(mult: float, p: PriceSeries) -> pd.DataFrame:
    trades = load_trades(mult, p)
    rows = []
    for sl in configs.SL_ONLY_GRID:
        for target in configs.SINGLE_TARGET_GRID + [configs.DISABLED_PCT]:
            rows.append(cell(run_one_lot(trades, p, sl, target), mult, sl, target))
    return pd.DataFrame(rows)


def pick(df: pd.DataFrame, by: str, with_target: bool) -> pd.Series:
    """Best row by `by` among cells with (or without) a target; ties go to the first row in grid order."""
    d = df[df['target_on'] == with_target]
    return d.loc[d[by].idxmax()]


def walkforward(df: pd.DataFrame, mult: float) -> list:
    """Choose on the pre-split period only, read the same cell out of sample. One row per (selection metric, shape)."""
    out = []
    for by in ('pre_calmar_pct', 'pre_calmar'):
        for with_target in (False, True):
            r = pick(df, by, with_target)
            out.append({'multiplier': mult, 'selected_by': by, 'shape': 'stop + one target' if with_target else 'stop only', 'sl_pct': r['sl_pct'],
                        'target_pct': r['target_pct'] if with_target else None, 'pre_pnl_rs': r['pre_pnl_rs'], 'pre_calmar': r['pre_calmar'],
                        'pre_calmar_pct': r['pre_calmar_pct'], 'post_pnl_rs': r['post_pnl_rs'], 'post_dd_rs': r['post_dd_rs'], 'post_calmar': r['post_calmar'],
                        'post_calmar_pct': r['post_calmar_pct'], 'post_win_pct': r['post_win_pct'], 'target_hit_rate_pct': r['target_hit_rate_pct'],
                        'full_pnl_rs': r['total_pnl_rs'], 'full_calmar': r['calmar'], 'full_calmar_pct': r['calmar_pct']})
    return out


def check_control(df: pd.DataFrame, mult: float) -> None:
    """The stop-only 3.0% cell must equal the saved structures comparison (two lots) halved, so this module and the decided config agree."""
    path = os.path.join(SWEEP_DIR, 'exit_structures_detail.csv')
    if not os.path.exists(path) or mult != configs.DECIDED_MULTIPLIER:
        return
    s = pd.read_csv(path)
    ref = s[(s['multiplier'] == mult) & (s['stage'] == 'A_sl_only') & (s['sl_pct'] == configs.DECIDED_SL_PCT)].iloc[0]
    me = df[(df['sl_pct'] == configs.DECIDED_SL_PCT) & (~df['target_on'])].iloc[0]
    assert abs(me['total_pnl_rs'] * LOTS - ref['total_pnl_rs']) < 1.5, (me['total_pnl_rs'] * LOTS, ref['total_pnl_rs'])
    assert abs(me['max_drawdown_rs'] * LOTS - ref['max_drawdown_rs']) < 1.5, (me['max_drawdown_rs'] * LOTS, ref['max_drawdown_rs'])
    print(f'  control check passed: stop-only {configs.DECIDED_SL_PCT}% = {me["total_pnl_rs"]:,.0f} Rs per lot (two-lot {ref["total_pnl_rs"]:,.0f})')


def main():
    os.makedirs(SWEEP_DIR, exist_ok=True)
    p = PriceSeries(load_futures_1min())
    frames, walk = [], []
    for mult in configs.CALIBRATION_MULTIPLIERS:
        print(f'multiplier {mult}...', flush=True)
        df = grid(mult, p)
        check_control(df, mult)
        frames.append(df)
        walk += walkforward(df, mult)
    pd.concat(frames).to_csv(GRID_FILE, index=False)
    w = pd.DataFrame(walk)
    w.to_csv(WALK_FILE, index=False)
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', None)
    print('\nWalk-forward (chosen on entries before', configs.WALKFORWARD_SPLIT_DATE, ', read out of sample), per lot:')
    print(w.to_string(index=False))


if __name__ == '__main__':
    main()
