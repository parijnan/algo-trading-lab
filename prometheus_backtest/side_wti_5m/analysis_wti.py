"""
Prometheus — WTI 5-minute side project: summary/Calmar table.

_summarize below is copied from phase3/exit_calib_p3.py, not imported —
that module's version reads configs_p3.LOT_SIZE at module scope (same
reason bespoke_wti.py copies _simulate_trade_detailed instead of importing
it). Same running cumsum-vs-cummax drawdown methodology already used for
the CRUDEOILM/CRUDEOIL headline tables (prometheus_backtest/README.md) —
NOT the finer per-lot-exit-event method used for those tables' own Calmar
figures; this is the coarser per-trade-cumsum approach exit_calib_p3.py's
own _summarize itself uses, kept identical here for methodology parity
with THIS function specifically, not the README's own headline numbers.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configs_wti as configs  # noqa: E402


def _summarize(sim: pd.DataFrame, mult: float, sl_pct: float, t1_pct: float, t2_pct: float) -> dict:
    sim = sim.copy()
    n = len(sim)
    pl = sim['total_pnl_rs'].astype(float)  # USD points at LOT_SIZE=1, see configs_wti.py
    wins = int((pl > 0).sum())
    cumpl = pl.cumsum()
    max_dd = (cumpl - cumpl.cummax()).min()
    total_pl = pl.sum()

    lot1_hit = int((sim['lot1_exit_reason'] == 'target1').sum())
    lot2_hit = int((sim['lot2_exit_reason'] == 'target2').sum())
    sl_count = int((sim['lot1_exit_reason'] == 'stop_loss').sum())

    return {
        'multiplier': mult, 'sl_pct': sl_pct, 't1_pct': t1_pct, 't2_pct': t2_pct,
        'n_trades': n,
        'win_rate_pct': round(wins / n * 100, 1) if n else float('nan'),
        'total_pnl_pts': round(total_pl, 1),
        'max_drawdown_pts': round(max_dd, 1),
        'calmar': round(total_pl / abs(max_dd), 2) if max_dd else float('nan'),
        'lot1_hit_rate_pct': round(lot1_hit / n * 100, 1) if n else float('nan'),
        'lot2_hit_rate_pct': round(lot2_hit / n * 100, 1) if n else float('nan'),
        'stop_loss_count': sl_count,
    }


def main():
    rows = []
    for mult, sl, t1, t2 in configs.BESPOKE_COMBOS:
        path = os.path.join(configs.DATA_SWEEP_DIR, f'mult_{mult:.1f}', 'bespoke_trade_summary.csv')
        if not os.path.exists(path):
            print(f'Missing {path} — run bespoke_wti.py first.')
            continue
        sim = pd.read_csv(path)
        rows.append(_summarize(sim, mult, sl, t1, t2))

    summary = pd.DataFrame(rows)
    out_path = os.path.join(configs.DATA_SWEEP_DIR, 'wti_summary.csv')
    summary.to_csv(out_path, index=False)
    print(summary.to_string(index=False))
    print(f'\nSaved to {out_path}')


if __name__ == '__main__':
    main()
