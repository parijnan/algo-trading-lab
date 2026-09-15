"""
Prometheus - Phase 3 (Fyers track): raw ST_MULTIPLIER sweep restricted to
the calm, pre-regime-shift window (2023-03-03 -> 2026-03-03).

Direct follow-up to the user's own finding: the mult-2.0/bespoke-exit
backtest is near-breakeven before 2026-03-02 (Rs 24/trade average across
2,172 trades). Open question this answers: is that a property of mult 2.0
specifically, or does NO multiplier in the original Phase 3 grid show
real signal-quality edge in the calm regime?

Same methodology as ../phase3/sweep_p3.py's own original signal-quality
stage: raw signal-following only (no SL, no profit target, no EOD square-
off -- the only exit is the opposite Supertrend flip), 1 lot. Deliberately
NOT re-running SL/target exit calibration per multiplier here -- that
would conflate "does this multiplier's raw signal have edge in calm
markets" with "did we also pick the right exit percentages for it," and
the first question is what's being asked. Full grid restored from
../phase3/configs_p3.py's own comment ("Restore the full list above... if
the ST_MULTIPLIER choice itself is ever reopened") -- this script has its
OWN local grid constant; it does not touch phase3_fyers/configs_p3.py,
which stays pinned to [2.0] for every other script in this directory
(sweep_p3.py, bespoke_2lot_p3.py, the gate/regime scripts).

Price data is sliced to end at CUTOFF *before* resampling/computing
Supertrend, not just filtered at the trade level afterward -- Supertrend
is history-dependent (a ratchet), so a trade-level-only filter would still
let the indicator itself "see" the post-cutoff regime's price action
during warmup. CUTOFF's entire window predates data_loader_fyers.py's own
Angel-One gap-fill splice (2026-03-13 onward), so this run is pure,
unspliced Fyers data throughout -- no vendor-mixing question here.

Calmar here uses the same per-trade-exit cumulative-equity/max-drawdown
formula as ../phase3/lot2_trail_after_t1_grid_p3.py's own
per_lot_exit_calmar, adapted to raw (1-lot, single pnl_rs per trade)
trades rather than the 2-lot bespoke case.

Output: data_sweep/calm_regime_multiplier_sweep.csv
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configs_p3 as configs  # noqa: E402
from backtest_p3 import run_backtest  # noqa: E402
from data_loader_fyers import load_futures_1min, resample_ohlcv, compute_st  # noqa: E402

CUTOFF = pd.Timestamp('2026-03-03')   # exclusive upper bound -- "data from 2023 to Mar 3rd 2026"
ST_PERIOD = configs.ST_PERIOD          # 10, held fixed, matching every other Phase 3 sweep
# Restored from ../phase3/configs_p3.py's own pre-restriction comment --
# the full grid Phase 3 originally tested before mult 2.0 was decided.
MULTIPLIER_GRID = [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5]


def _raw_calmar(trades: pd.DataFrame) -> tuple:
    closed = trades.dropna(subset=['exit_ts']).sort_values('exit_ts')
    if closed.empty:
        return 0.0, 0.0, float('nan')
    equity = closed['pnl_rs'].cumsum()
    peak = equity.cummax()
    dd = equity - peak
    max_dd = dd.min()
    total = equity.iloc[-1]
    calmar = total / abs(max_dd) if max_dd else float('nan')
    return total, max_dd, calmar


def main():
    print(f'Loading {configs.SYMBOL} 1-min data, restricting to entries before {CUTOFF.date()}...')
    df_1m_full = load_futures_1min(configs.SYMBOL)
    df_1m = df_1m_full[df_1m_full.index < CUTOFF]
    print(f'  {len(df_1m):,} 1-min bars, {df_1m.index.min()} -> {df_1m.index.max()} '
          f'(full series would run to {df_1m_full.index.max()})')

    df_15m_raw = resample_ohlcv(df_1m, '15min')

    results = []
    for mult in MULTIPLIER_GRID:
        df_15m = compute_st(df_15m_raw, ST_PERIOD, mult)
        trades = run_backtest(df_15m)
        closed = trades[trades['exit_ts'].notna()]
        still_open = len(trades) - len(closed)
        n = len(closed)
        wins = int((closed['pnl_rs'] > 0).sum()) if n else 0
        total, max_dd, calmar = _raw_calmar(trades)

        row = {
            'st_multiplier': mult, 'n_trades': n, 'still_open_at_cutoff': still_open,
            'win_rate_pct': round(wins / n * 100, 1) if n else float('nan'),
            'total_pnl_rs': round(total, 0),
            'avg_pnl_rs': round(closed['pnl_rs'].mean(), 1) if n else float('nan'),
            'max_drawdown_rs': round(max_dd, 0), 'calmar': round(calmar, 2) if pd.notna(calmar) else None,
        }
        results.append(row)
        print(f"mult {mult}: {n} trades ({still_open} open at cutoff), win {row['win_rate_pct']}%, "
              f"total P&L Rs {row['total_pnl_rs']:,.0f}, max DD Rs {row['max_drawdown_rs']:,.0f}, "
              f"Calmar {row['calmar']}")

    summary = pd.DataFrame(results)
    out_path = os.path.join(configs.DATA_SWEEP_DIR, 'calm_regime_multiplier_sweep.csv')
    summary.to_csv(out_path, index=False)
    print(f'\nSaved to {out_path}')
    print(summary.to_string(index=False))


if __name__ == '__main__':
    main()
