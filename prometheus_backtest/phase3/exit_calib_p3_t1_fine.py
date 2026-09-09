"""
Prometheus - Phase 3, mult 2.0: fine-resolution TARGET1_PCT grid around
2.0-2.5%, follow-up to exit_calib_p3_t1_widen.py's finding that Calmar peaks
sharply at T1=2.25% (12.31) and drops the very next 0.25%-spaced step
(2.50% -> 9.62). That widened-grid README write-up flagged the peak as a
single narrow point -- as consistent with grid noise/overfitting as with a
genuine structural optimum -- and asked for a finer pass to check.

Same SL=2.2% (Stage 1 winner) and T2=5.0% (production value, Stage 3 winner
at T1=2.25% too) pins as the widened script, for direct comparability. Only
the T1 grid resolution changes -- 0.05% steps from 2.00% to 2.50% inclusive
(11 points) instead of 0.25% steps -- so this is still one variable changed
per experiment, just at 5x the resolution around the region of interest.

Output: data_sweep/exit_calib_p3_t1_fine_mult20.csv
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configs_p3 as configs  # noqa: E402
from exit_calib_p3 import _load_multiplier_data, _run_variant, _summarize, _best_by_calmar  # noqa: E402

OUT_FILE = os.path.join(configs.DATA_SWEEP_DIR, 'exit_calib_p3_t1_fine_mult20.csv')

MULT = 2.0
SL_PIN = 2.2   # Stage-1 winner, unchanged from the widened script
T2_PIN = 5.0   # production value / Stage-3 winner at T1=2.25%, unchanged

# 2.00 to 2.50 inclusive, 0.05% steps -- 11 points, 5x the resolution of the
# widened grid's 0.25% spacing across exactly the region that showed the peak.
T1_GRID_FINE = [round(2.00 + 0.05 * i, 2) for i in range(11)]


def main():
    trades, paths = _load_multiplier_data(MULT)
    print(f'Loaded {len(trades)} closed trades for mult {MULT} '
          f'(vintage: trade_summary.csv last entry_ts {trades["entry_ts"].max()})')
    print(f'SL pinned at {SL_PIN}%, T2 pinned at {T2_PIN}% (both unchanged from the widened script)\n')

    rows = []
    for t1 in T1_GRID_FINE:
        sim = _run_variant(trades, paths, SL_PIN, t1, T2_PIN)
        row = _summarize(sim, MULT, 'target1_grid_fine', 'target1_pct', t1)
        rows.append(row)

    detail_df = pd.DataFrame(rows)
    os.makedirs(configs.DATA_SWEEP_DIR, exist_ok=True)
    detail_df.to_csv(OUT_FILE, index=False)

    print(f'Saved {len(detail_df)} rows to {OUT_FILE}\n')
    pd.set_option('display.width', 220)
    pd.set_option('display.max_columns', None)
    print(detail_df[['value', 'n_trades', 'win_rate_pct', 'total_pnl_rs',
                      'max_drawdown_rs', 'calmar', 'lot1_hit_rate_pct', 'stop_loss_count']]
          .to_string(index=False))

    best = _best_by_calmar(rows)
    print(f"\nBest by Calmar in fine grid: T1={best['value']}%  Calmar={best['calmar']}")


if __name__ == '__main__':
    main()
