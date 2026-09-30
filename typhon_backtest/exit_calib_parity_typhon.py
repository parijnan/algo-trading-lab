"""
Typhon - exit calibration inside the real parity simulator (plan Step 4), replacing the
back-adjusted exit_calib_typhon.py as the source of truth for SL/target levels (real prices, real
roll execution -- see typhon_configs.py's PROVISIONAL_* section for why the back-adjusted version
is superseded).

SL-only, single lot (Selene's own shape) -- the 2-lot scale-out candidate is a separate, bigger
extension not attempted here. Staged grid search, same discipline as every other exit calibrator
in this repo (SL grid -> target grid, each stage picking the best calmar_pct with the other
pinned; DISABLED_PCT is always an explicit target candidate, letting the search conclude "ride to
the trend-flip" rather than being forced onto a real number).

Made tractable by a real speedup, not an approximation: parity_backtest_typhon.Contract.day_rows()
caches ST by (day, multiplier), so re-running simulate() with a different sl_pct/target_pct for
the SAME multiplier skips the expensive ST computation and only re-walks the (cheap) state
machine -- measured 2026-09-30: ~90x faster on a warm cache (112s cold, ~1.3s warm). Every grid
point runs the actual, correct roll-aware state machine -- no per-segment overlay, no
approximation risk.

Output (data_sweep/): parity_calib_detail_mult{X.X}.csv, parity_calib_winners.csv.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import typhon_configs as configs
from parity_backtest_typhon import load_contracts, metrics, simulate, to_trades

SL_GRID = configs.SL_GRID
TARGET_GRID = configs.TARGET_GRID + [configs.DISABLED_PCT]
MULTIPLIERS = [2.0, 2.5, 3.0]


def full_stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {'n_trades': 0}
    pnl_rs = t['pnl_pts'] * configs.LOT_SIZE
    wins, losses = pnl_rs[pnl_rs > 0], pnl_rs[pnl_rs < 0]
    cum_rs = pnl_rs.cumsum()
    dd_rs = (cum_rs - cum_rs.cummax()).min()
    cp = t['pnl_pct'].cumsum()
    dd_pct = (cp - cp.cummax()).min()
    return {
        'n_trades': len(t),
        'win_rate_pct': round((pnl_rs > 0).mean() * 100, 1),
        'n_wins': len(wins), 'n_losses': len(losses),
        'avg_win_rs': round(wins.mean(), 0) if len(wins) else None,
        'avg_loss_rs': round(losses.mean(), 0) if len(losses) else None,
        'largest_win_rs': round(wins.max(), 0) if len(wins) else None,
        'largest_loss_rs': round(losses.min(), 0) if len(losses) else None,
        'profit_factor': round(wins.sum() / abs(losses.sum()), 2) if len(losses) and losses.sum() != 0 else None,
        'total_return_rs': round(pnl_rs.sum(), 0),
        'max_drawdown_rs': round(dd_rs, 0),
        'max_dd_as_pct_of_return': round(abs(dd_rs) / pnl_rs.sum() * 100, 1) if pnl_rs.sum() else None,
        'calmar_rs': round(pnl_rs.sum() / abs(dd_rs), 2) if dd_rs else float('nan'),
        'calmar_pct': round(t['pnl_pct'].sum() / abs(dd_pct), 2) if dd_pct else float('nan'),
    }


def run(mult, sl_pct, target_pct, contracts, end):
    legs = simulate(configs.DATA_START, end, mult, sl_pct=sl_pct, target_pct=target_pct, contracts=contracts)
    trades = to_trades(legs)
    trades = trades[trades['exit_ts'].notna()]
    return trades


def calibrate(mult, contracts, end) -> tuple:
    detail = []

    stage_sl = []
    for sl in SL_GRID:
        t = run(mult, sl, None, contracts, end)
        stats = full_stats(t)
        stage_sl.append({'multiplier': mult, 'stage': 'sl_grid', 'param': 'sl_pct', 'value': sl, **stats})
    detail += stage_sl
    best_sl = max(stage_sl, key=lambda r: r['calmar_pct'] if pd.notna(r.get('calmar_pct')) else float('-inf'))['value']

    stage_t = []
    for tv in TARGET_GRID:
        t = run(mult, best_sl, tv, contracts, end)
        stats = full_stats(t)
        stage_t.append({'multiplier': mult, 'stage': 'target_grid', 'param': 'target_pct', 'value': tv, **stats})
    detail += stage_t
    best_t = max(stage_t, key=lambda r: r['calmar_pct'] if pd.notna(r.get('calmar_pct')) else float('-inf'))['value']

    final_t = run(mult, best_sl, best_t, contracts, end)
    final_stats = full_stats(final_t)
    winner = {'multiplier': mult, 'stage': 'final', 'sl_pct': best_sl,
             'target_pct': None if best_t == configs.DISABLED_PCT else best_t, **final_stats}
    return detail, winner


def main():
    end = configs.PARITY_END_EXTENDED
    os.makedirs(configs.DATA_SWEEP_DIR, exist_ok=True)
    print(f'Loading {configs.SYMBOL} per-contract 1-min data...')
    contracts = load_contracts(configs.SYMBOL, configs.DATA_START, end)
    print(f'  {len(contracts)} contracts loaded')

    all_detail, winners = [], []
    for mult in MULTIPLIERS:
        print(f'\n=== multiplier {mult} ===')
        detail, winner = calibrate(mult, contracts, end)
        all_detail += detail
        winners.append(winner)
        pd.DataFrame(detail).to_csv(os.path.join(configs.DATA_SWEEP_DIR, f'parity_calib_detail_mult{mult:.1f}.csv'), index=False)
        tgt_str = 'no target' if winner['target_pct'] is None else f"{winner['target_pct']}%"
        print(f"  winner: SL={winner['sl_pct']}%  target={tgt_str}  n={winner['n_trades']}  "
              f"win%={winner['win_rate_pct']}  profit_factor={winner['profit_factor']}  "
              f"total_return_rs={winner['total_return_rs']:,.0f}  max_dd_rs={winner['max_drawdown_rs']:,.0f}  "
              f"calmar_pct={winner['calmar_pct']}")

    wdf = pd.DataFrame(winners)
    wdf.to_csv(os.path.join(configs.DATA_SWEEP_DIR, 'parity_calib_winners.csv'), index=False)
    pd.set_option('display.width', 260)
    pd.set_option('display.max_columns', None)
    cols = ['multiplier', 'sl_pct', 'target_pct', 'n_trades', 'win_rate_pct', 'profit_factor', 'avg_win_rs',
           'avg_loss_rs', 'max_drawdown_rs', 'max_dd_as_pct_of_return', 'total_return_rs', 'calmar_rs', 'calmar_pct']
    print('\n' + wdf[cols].to_string(index=False))


if __name__ == '__main__':
    main()
