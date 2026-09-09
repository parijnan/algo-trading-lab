"""
Prometheus - Phase 3, CRUDEOIL cross-validation: fine-resolution pass
around the trail-level grid's apparent peak. Mirrors
phase3/lot2_trail_after_t1_fine_p3.py -- see that script and
lot2_trail_after_t1_grid_p3.py (this folder) for full rationale.

Grid: 0.6% to 1.8% in 0.05% steps (25 points) -- same range as the
CRUDEOILM fine pass, for direct comparability; if CRUDEOIL's own coarse
grid peaks somewhere else, widen this range before trusting the result.

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
        print(f"trail={trail_pct:>5.2f}%  total P&L Rs {total:>12,.0f}  max DD Rs {max_dd:>12,.0f}  Calmar {calmar:>6.2f}")

    out_df = pd.DataFrame(results)
    out_path = os.path.join(SWEEP_DIR, 'mult_2.0', 'lot2_trail_after_t1_fine.csv')
    out_df.to_csv(out_path, index=False)
    print(f'\nSaved to {out_path}')

    best = out_df.loc[out_df['calmar'].idxmax()]
    print(f"\nBest by Calmar: trail={best['trail_pct']}%  Calmar={best['calmar']}")


if __name__ == '__main__':
    main()
