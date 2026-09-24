"""
Selene - Phase 3b exit STRUCTURE comparison per shortlisted multiplier.

Follow-up to exit_calib_selene.py, whose staged calibration landed on its own
grid edges and picked targets that almost never fill. Two experiments, each
changing one thing (CLAUDE.md convention), using exit_calib_selene's simulator:
  A. Stop-loss only -- both targets disabled, the opposite ST_15 flip is the
     only profit exit. Grid over SL, plus a no-stop row (raw signal) as the floor.
  B. 2-lot structure -- FULL T1 x T2 grid (not staged/greedy) at the SL stage A
     picked, so the target surface is seen whole rather than one axis at a time.
Every row reports before/after selene_configs.WALKFORWARD_SPLIT_DATE, and both
Calmars (points, and percent-of-entry-price). Stage A's SL is chosen by
points-Calmar for consistency with exit_calib_selene.py, but the whole grid is
printed because the Calmar surface is jumpy (a few Jan-Feb 2026 gap trades set
most max drawdowns); total P&L and the split columns are the cross-check.

Output (data_sweep/, gitignored): exit_structures_detail.csv
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selene_configs as configs
from selene_data_loader import load_futures_1min
from exit_calib_selene import PriceSeries, load_trades, run_variant, summarize

SPLIT = pd.Timestamp(configs.WALKFORWARD_SPLIT_DATE)


def row(sim: pd.DataFrame, mult: float, stage: str, sl, t1, t2) -> dict:
    r = summarize(sim, mult, stage, 'combo', '')
    for k in ('param', 'value'):
        r.pop(k)
    r.update({'stage': stage, 'sl_pct': sl, 't1_pct': t1, 't2_pct': t2})
    for tag, part in (('pre', sim[sim['entry_ts'] < SPLIT]), ('post', sim[sim['entry_ts'] >= SPLIT])):
        s = summarize(part, mult, tag, 'combo', '')
        r.update({f'{tag}_pnl_rs': s['total_pnl_rs'], f'{tag}_calmar': s['calmar'], f'{tag}_calmar_pct': s['calmar_pct']})
    return r


def main():
    os.makedirs(configs.DATA_SWEEP_DIR, exist_ok=True)
    print('Loading 1-min series...')
    p = PriceSeries(load_futures_1min())
    off = configs.DISABLED_PCT

    rows = []
    for mult in configs.CALIBRATION_MULTIPLIERS:
        trades = load_trades(mult, p)
        print(f'\n=== multiplier {mult} ===')

        # ---- A. stop-loss only ----
        a = [row(run_variant(trades, p, off, off, off * 2), mult, 'A_raw', None, None, None)]
        for sl in configs.SL_ONLY_GRID:
            a.append(row(run_variant(trades, p, sl, off, off * 2), mult, 'A_sl_only', sl, None, None))
        rows += a
        best = max((r for r in a if r['stage'] == 'A_sl_only'), key=lambda r: r['calmar'])
        best_sl = best['sl_pct']
        cols = ['stage', 'sl_pct', 'total_pnl_rs', 'max_drawdown_rs', 'calmar', 'calmar_pct', 'win_rate_pct',
                'stop_loss_count', 'pre_pnl_rs', 'pre_calmar', 'post_pnl_rs', 'post_calmar']
        print('A. stop-loss only (raw = no stop, no targets):')
        print(pd.DataFrame(a)[cols].to_string(index=False))
        print(f'   -> stage A best SL by Calmar: {best_sl}%')

        # ---- B. full T1 x T2 grid at that SL ----
        b = []
        for t1 in configs.T1_WIDE_GRID:
            for t2 in configs.T2_WIDE_GRID:
                if t2 > t1:
                    b.append(row(run_variant(trades, p, best_sl, t1, t2), mult, 'B_grid', best_sl, t1, t2))
        rows += b
        bdf = pd.DataFrame(b)
        print(f'B. 2-lot grid at SL {best_sl}% -- top 8 by Calmar (SL-only at same SL: '
              f'P&L {best["total_pnl_rs"]:,.0f}, Calmar {best["calmar"]}):')
        cols_b = ['t1_pct', 't2_pct', 'total_pnl_rs', 'max_drawdown_rs', 'calmar', 'calmar_pct', 'lot1_hit_rate_pct',
                  'lot2_hit_rate_pct', 'pre_pnl_rs', 'pre_calmar', 'post_pnl_rs', 'post_calmar']
        print(bdf.sort_values('calmar', ascending=False).head(8)[cols_b].to_string(index=False))
        print('   P&L by (T1 rows x T2 cols), Rs:')
        print(bdf.pivot(index='t1_pct', columns='t2_pct', values='total_pnl_rs').round(0).to_string())

    pd.DataFrame(rows).to_csv(os.path.join(configs.DATA_SWEEP_DIR, 'exit_structures_detail.csv'), index=False)


if __name__ == '__main__':
    main()
