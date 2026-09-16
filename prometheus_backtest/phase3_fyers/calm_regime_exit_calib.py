"""
Prometheus - Phase 3 (Fyers track): SL/T1/T2 exit calibration for the
calm-regime raw-multiplier-sweep candidates, restricted to the same
2023-03-03 -> 2026-03-03 window as calm_regime_multiplier_sweep.py.

Follow-up to that sweep's finding: mult 4.0 (raw Calmar 2.10) and, more
weakly, mult 4.5/5.0 (1.51/1.63) stood out on RAW signal quality in the
calm window, vs mult 2.0's own raw Calmar of 1.13 there. This script
takes those candidates through the same staged SL -> target1 -> target2
grid search ../phase3/exit_calib_p3.py used for the original Phase 3
decision (same grids, same fill conventions, same Calmar-pinned staging,
imported directly, not reimplemented) -- answering "does this
multiplier's edge survive real risk management, and with what exit
percentages" rather than stopping at the raw-signal-only result.

Two-step pipeline, both restricted to entries before CUTOFF and with the
underlying 1-min/15m/Supertrend series itself sliced before CUTOFF (not
just trades filtered afterward -- Supertrend is history-dependent, same
discipline as calm_regime_multiplier_sweep.py):
  1. _prepare(): generates trade_summary.csv + trade_logs/*.csv for each
     candidate multiplier, mirroring sweep_p3.py's own output shape, into
     data_sweep/calm_regime/mult_<X.X>/ -- a SEPARATE tree from the
     full-history data_sweep/mult_2.0/ this directory's other scripts
     use; never overwrites or mixes with those.
  2. calibrate(): the same staged Calmar-pinned SL->T1->T2 search as
     exit_calib_p3.py (SL_GRID/T1_GRID/T2_GRID/T1_STARTING_DEFAULT/
     T2_STARTING_DEFAULT and _run_variant/_summarize/_best_by_calmar
     imported directly from it, not copied), reading from the calm-only
     tree instead of the full-history one.

Candidates: CANDIDATE_MULTIPLIERS below -- runs the full original grid
(2.0 through 5.5) for a complete picture, including mult 2.0 itself (the
live production value) as the fair baseline: what its OWN exits would
look like if calibrated for the calm window specifically, rather than
comparing calm-window candidates against mult 2.0's volatile-regime-tuned
exits. Change the list and re-run for a different subset.

Output:
  data_sweep/calm_regime/exit_calib_detail.csv
  data_sweep/calm_regime/exit_calib_winners.csv
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configs_p3 as configs  # noqa: E402
from backtest_p3 import run_backtest  # noqa: E402
from trade_paths_p3 import save_trade_paths_p3  # noqa: E402
from data_loader_fyers import load_futures_1min, resample_ohlcv, compute_st  # noqa: E402
from exit_calib_p3 import (  # noqa: E402
    SL_GRID, T1_GRID, T2_GRID, T1_STARTING_DEFAULT, T2_STARTING_DEFAULT,
    _run_variant, _summarize, _best_by_calmar,
)

CUTOFF = pd.Timestamp('2026-03-03')
ST_PERIOD = configs.ST_PERIOD
CANDIDATE_MULTIPLIERS = [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5]

CALM_DIR = os.path.join(configs.DATA_SWEEP_DIR, 'calm_regime')
DETAIL_FILE = os.path.join(CALM_DIR, 'exit_calib_detail.csv')
WINNERS_FILE = os.path.join(CALM_DIR, 'exit_calib_winners.csv')


def _calm_df_1m() -> pd.DataFrame:
    df_1m_full = load_futures_1min(configs.SYMBOL)
    return df_1m_full[df_1m_full.index < CUTOFF]


def _prepare(df_1m: pd.DataFrame) -> dict:
    """Writes trade_summary.csv + trade_logs/*.csv per candidate
    multiplier into data_sweep/calm_regime/mult_<X.X>/, mirroring
    sweep_p3.py's own shape. Returns the first-bar-of-day dict used by
    the NO_EXIT_BEFORE_BUFFER_MIN guard -- depends only on price data,
    not the multiplier, so computed once and reused across candidates."""
    os.makedirs(CALM_DIR, exist_ok=True)
    df_15m_raw = resample_ohlcv(df_1m, '15min')

    ts = df_1m.index.to_series()
    fbbd = ts.groupby(ts.dt.normalize()).min().to_dict()

    for mult in CANDIDATE_MULTIPLIERS:
        label = f'mult_{mult:.1f}'
        run_dir = os.path.join(CALM_DIR, label)
        logs_dir = os.path.join(run_dir, 'trade_logs')
        os.makedirs(run_dir, exist_ok=True)

        df_15m = compute_st(df_15m_raw, ST_PERIOD, mult)
        trades = run_backtest(df_15m)
        path_summaries = save_trade_paths_p3(trades, df_1m, logs_dir)

        trades_out = trades.copy()
        path_df = pd.DataFrame(path_summaries)
        if not path_df.empty:
            trades_out = trades_out.merge(path_df, on='trade_id', how='left')
        trades_out.to_csv(os.path.join(run_dir, 'trade_summary.csv'), index=False)
        print(f'  prepared {label}: {len(trades)} raw trades -> {logs_dir}')

    return fbbd


def _load_calm_multiplier_data(mult: float) -> tuple:
    label = f'mult_{mult:.1f}'
    run_dir = os.path.join(CALM_DIR, label)
    trades = pd.read_csv(os.path.join(run_dir, 'trade_summary.csv'), parse_dates=['entry_ts', 'exit_ts'])
    logs_dir = os.path.join(run_dir, 'trade_logs')
    paths = {}
    for _, t in trades.iterrows():
        tid = int(t['trade_id'])
        letter = 'B' if t['direction'] == 'bullish' else 'S'
        fname = f"trade_{tid:04d}_{pd.Timestamp(t['entry_ts']):%Y-%m-%d_%H%M}_{letter}.csv"
        fpath = os.path.join(logs_dir, fname)
        if not os.path.exists(fpath):
            continue
        paths[tid] = pd.read_csv(fpath, parse_dates=['ts'])
    return trades, paths


def calibrate(mult: float, fbbd: dict) -> tuple:
    trades, paths = _load_calm_multiplier_data(mult)
    detail_rows = []

    # --- Stage 1: SL grid, target1/target2 pinned at starting defaults ---
    stage1 = []
    for sl in SL_GRID:
        sim = _run_variant(trades, paths, sl, T1_STARTING_DEFAULT, T2_STARTING_DEFAULT, fbbd)
        stage1.append(_summarize(sim, mult, 'sl_grid', 'sl_pct', sl))
    detail_rows.extend(stage1)
    best_sl = _best_by_calmar(stage1)['value']

    # --- Stage 2: target1 grid, SL pinned at stage-1 winner ---
    stage2 = []
    for t1 in T1_GRID:
        sim = _run_variant(trades, paths, best_sl, t1, T2_STARTING_DEFAULT, fbbd)
        stage2.append(_summarize(sim, mult, 'target1_grid', 'target1_pct', t1))
    detail_rows.extend(stage2)
    best_t1 = _best_by_calmar(stage2)['value']

    # --- Stage 3: target2 grid, SL/target1 pinned at their winners ---
    valid_t2_grid = [t2 for t2 in T2_GRID if t2 > best_t1]
    stage3 = []
    for t2 in valid_t2_grid:
        sim = _run_variant(trades, paths, best_sl, best_t1, t2, fbbd)
        stage3.append(_summarize(sim, mult, 'target2_grid', 'target2_pct', t2))
    detail_rows.extend(stage3)
    best_t2 = _best_by_calmar(stage3)['value']

    winner = _summarize(_run_variant(trades, paths, best_sl, best_t1, best_t2, fbbd), mult, 'final', 'combo',
                         f'sl{best_sl}_t1{best_t1}_t2{best_t2}')
    winner['sl_pct'] = best_sl
    winner['target1_pct'] = best_t1
    winner['target2_pct'] = best_t2
    return detail_rows, winner


def main():
    print(f'Preparing calm-regime ({CUTOFF.date()} cutoff) trade logs for {CANDIDATE_MULTIPLIERS}...')
    df_1m = _calm_df_1m()
    fbbd = _prepare(df_1m)

    all_detail, winners = [], []
    for mult in CANDIDATE_MULTIPLIERS:
        print(f'\nCalibrating multiplier {mult}...')
        detail_rows, winner = calibrate(mult, fbbd)
        all_detail.extend(detail_rows)
        winners.append(winner)
        print(f"  best: SL={winner['sl_pct']}%  T1={winner['target1_pct']}%  T2={winner['target2_pct']}%  "
              f"Calmar={winner['calmar']}  total P&L={winner['total_pnl_rs']:,.0f} Rs  "
              f"max DD={winner['max_drawdown_rs']:,.0f} Rs  n={winner['n_trades']}")

    pd.DataFrame(all_detail).to_csv(DETAIL_FILE, index=False)
    winners_df = pd.DataFrame(winners)
    winners_df.to_csv(WINNERS_FILE, index=False)
    print(f'\nSaved detail -> {DETAIL_FILE}')
    print(f'Saved winners -> {WINNERS_FILE}')
    print(winners_df[['multiplier', 'sl_pct', 'target1_pct', 'target2_pct', 'n_trades', 'win_rate_pct',
                       'total_pnl_rs', 'max_drawdown_rs', 'calmar']].to_string(index=False))


if __name__ == '__main__':
    main()
