"""
Prometheus - Phase 3, mult 2.0: fine-resolution pass around the trail-level
grid's apparent peak (lot2_trail_after_t1_grid_p3.py found trail=1.0% best
by Calmar, 13.38 vs. the no-rule baseline's 12.11) -- same
noise-vs-genuine-structure check already applied once this session to the
TARGET1_PCT grid (see README.md's Phase 3 caveat #1): a coarse 0.2%-step
grid can land its apparent best point on either a real plateau or a single
noisy point, and the two look identical without a finer pass.

The coarse grid's own max-drawdown column already hinted at real structure,
not noise: max DD sits at exactly -Rs 14,364 for all four points from
1.6% down to 1.0%, then jumps to -Rs 16,247 for 0.8% and below -- the
classic "one specific trade's outcome flips" signature already seen in the
T1 investigation, not something a smooth reading of Calmar values alone
would reveal.

Grid: 0.6% to 1.8% in 0.05% steps (25 points), covering the coarse grid's
entire 1.6%-1.0% plateau plus one step past each edge, reusing
lot2_trail_after_t1_grid_p3.py's own simulation and Calmar functions
unchanged -- only the grid resolution differs.

Output: data_sweep/mult_2.0/lot2_trail_after_t1_fine.csv
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configs_p3 as configs  # noqa: E402
from exit_calib_p3 import _load_multiplier_data  # noqa: E402
from lot2_trail_after_t1_grid_p3 import _simulate, per_lot_exit_calmar, SWEEP_DIR, MULT  # noqa: E402

FINE_GRID_PCT = [round(0.6 + 0.05 * i, 2) for i in range(25)]  # 0.60 .. 1.80


def main():
    trades, paths = _load_multiplier_data(MULT)

    results = []
    for trail_pct in FINE_GRID_PCT:
        sim_rows = []
        for _, t in trades.iterrows():
            tid = int(t['trade_id'])
            if tid not in paths:
                continue
            r = _simulate(t, paths[tid], trail_pct)
            r['trade_id'] = tid
            sim_rows.append(r)
        sim_df = pd.DataFrame(sim_rows)

        total, max_dd, calmar = per_lot_exit_calmar(sim_df)
        results.append({'trail_pct': trail_pct, 'total_pnl_rs': round(total, 0),
                         'max_dd_rs': round(max_dd, 0), 'calmar': round(calmar, 2)})
        print(f"trail={trail_pct:>5.2f}%  total P&L Rs {total:>10,.0f}  max DD Rs {max_dd:>10,.0f}  Calmar {calmar:>6.2f}")

    out_df = pd.DataFrame(results)
    out_path = os.path.join(SWEEP_DIR, 'mult_2.0', 'lot2_trail_after_t1_fine.csv')
    out_df.to_csv(out_path, index=False)
    print(f'\nSaved to {out_path}')

    best = out_df.loc[out_df['calmar'].idxmax()]
    print(f"\nBest by Calmar: trail={best['trail_pct']}%  Calmar={best['calmar']}")


if __name__ == '__main__':
    main()
