"""
Prometheus - Phase 2 calibration sweep: stop_loss grid, lot1-target grid,
and the flat-vs-pivot target2 control — one variable changed per run
(CLAUDE.md convention). Data is loaded once; each run monkeypatches the
relevant configs_p2 attribute(s) (module-level, so backtest_p2's `configs`
reference sees the same change) and restores them afterward.

Per-run per-trade logs are NOT generated here — this is calibration
(picking a value), not the tracked-run request from the earlier v1 sweep.
Re-run prometheus_backtest/phase2/run_p2.py with the chosen config values
if per-trade logs are needed for the winner.
"""

import os

import pandas as pd

import configs_p2 as configs
from data_loader_p2 import load_futures_1min, resample_ohlcv, compute_st, compute_daily_pivots
from backtest_p2 import run_backtest

SWEEP_DIR = os.path.join(configs.BASE_DIR, 'data_sweep')
SUMMARY_FILE = os.path.join(SWEEP_DIR, 'sweep_p2_summary.csv')


def _summarize(trades: pd.DataFrame, label: str, param: str, value) -> dict:
    trades = trades.copy()
    trades['total_pnl_rs'] = trades[['lot1_pnl_rs', 'lot2_pnl_rs']].sum(axis=1, min_count=1)
    n = len(trades)
    pl = trades['total_pnl_rs'].astype(float)
    wins = int((pl > 0).sum())
    cumpl = pl.cumsum()
    max_dd = (cumpl - cumpl.cummax()).min()
    total_pl = pl.sum()

    lot1_hit = int((trades['lot1_exit_reason'] == 'target_100').sum())
    lot2_reason_counts = trades['lot2_exit_reason'].value_counts().to_dict()

    return {
        'label': label, 'param': param, 'value': value, 'n_trades': n,
        'win_rate_pct': round(wins / n * 100, 1) if n else float('nan'),
        'total_pnl_rs': round(total_pl, 0),
        'max_drawdown_rs': round(max_dd, 0),
        'calmar': round(total_pl / abs(max_dd), 2) if max_dd else float('nan'),
        'lot1_hit_rate_pct': round(lot1_hit / n * 100, 1) if n else float('nan'),
        'stop_loss_count': lot2_reason_counts.get('stop_loss', 0) + int((trades['lot1_exit_reason'] == 'stop_loss').sum()),
        'lot2_target_count': (lot2_reason_counts.get('target_pivot', 0)
                               + lot2_reason_counts.get('target_flat', 0)
                               + lot2_reason_counts.get('target_flat_pct', 0)
                               + lot2_reason_counts.get('target_100_no_pivot', 0)),
    }


def _run_variant(df_15m, daily_pivots, label, param, value, **overrides):
    originals = {k: getattr(configs, k) for k in overrides}
    for k, v in overrides.items():
        setattr(configs, k, v)
    try:
        trades = run_backtest(df_15m, daily_pivots)
    finally:
        for k, v in originals.items():
            setattr(configs, k, v)
    return _summarize(trades, label, param, value)


def main():
    os.makedirs(SWEEP_DIR, exist_ok=True)

    df_1m = load_futures_1min(configs.SYMBOL)
    df_15m = resample_ohlcv(df_1m, '15min')
    df_15m = compute_st(df_15m, configs.ST_PERIOD, configs.ST_MULTIPLIER)

    day = df_15m.index.normalize()
    day_last_bar_ts = df_15m.groupby(day).apply(lambda g: g.index.max())
    df_15m['mins_to_close'] = (day.map(day_last_bar_ts) - df_15m.index).total_seconds() / 60
    df_15m['is_day_end'] = df_15m['mins_to_close'] <= 0

    daily_pivots = compute_daily_pivots(df_1m)

    results = []

    # --- baseline ---
    print('Running baseline...')
    results.append(_run_variant(df_15m, daily_pivots, 'baseline', 'none', None))

    # --- stop_loss grid (points mode, target2=pivot — pinned explicitly so
    # this reproduces the original historical experiment regardless of
    # configs_p2.py's current defaults, which have since changed) ---
    for sl in [40, 50, 60, 75, 90, 110, 130, 160]:
        label = f'sl{sl:03d}'
        print(f'Running {label} (stop_loss={sl})...')
        results.append(_run_variant(df_15m, daily_pivots, label, 'stop_loss', sl,
                                     THRESHOLD_MODE='points', STOP_LOSS_POINTS=sl,
                                     TARGET2_MODE='pivot'))

    # --- lot1 target grid, points mode (stop_loss stays off, pinned) ---
    for t1 in [60, 75, 85, 120, 140]:
        label = f'tgt1_{t1:03d}'
        print(f'Running {label} (lot1_target={t1})...')
        results.append(_run_variant(df_15m, daily_pivots, label, 'lot1_target', t1,
                                     THRESHOLD_MODE='points', LOT1_TARGET_POINTS=t1,
                                     STOP_LOSS_POINTS=None, TARGET2_MODE='pivot'))

    # --- flat-180 control for target2, points mode (pinned) ---
    print('Running flat180 control...')
    results.append(_run_variant(df_15m, daily_pivots, 'flat180', 'target2_mode', 'flat',
                                 THRESHOLD_MODE='points', STOP_LOSS_POINTS=None,
                                 TARGET2_MODE='flat', TARGET2_FLAT_POINTS=180))

    # --- pct-mode SL grid (target1 stays at the pct-mode default 1.0%,
    # target2=pivot — pinned) ---
    for sl in [0.5, 0.65, 0.8, 1.0, 1.2, 1.5, 1.8, 2.2]:
        label = f'slpct{sl:.2f}'
        print(f'Running {label} (SL_PCT={sl})...')
        results.append(_run_variant(df_15m, daily_pivots, label, 'sl_pct', sl,
                                     THRESHOLD_MODE='pct', SL_PCT=sl,
                                     TARGET1_PCT=1.0, TARGET2_MODE='pivot'))

    # --- pct-mode target1 grid (SL stays off, target2=pivot — pinned) ---
    for t1 in [0.6, 0.8, 1.0, 1.25, 1.5, 1.75]:
        label = f'tgt1pct{t1:.2f}'
        print(f'Running {label} (TARGET1_PCT={t1})...')
        results.append(_run_variant(df_15m, daily_pivots, label, 'target1_pct', t1,
                                     THRESHOLD_MODE='pct', TARGET1_PCT=t1,
                                     SL_PCT=None, TARGET2_MODE='pivot'))

    # --- FINAL: target1 sweep with SL=1.8% and target2=flat 2.3% held at
    # the new calibrated defaults (configs_p2.py) — the joint test, not a
    # from-baseline univariate one. 1.0% is the steadier end, 1.75% the
    # higher-EV/regime-concentrated end (see configs_p2.py docstring).
    for t1 in [1.00, 1.10, 1.25, 1.40, 1.50, 1.65, 1.75]:
        label = f'final_tgt1_{t1:.2f}'
        print(f'Running {label} (TARGET1_PCT={t1}, SL_PCT=1.8, target2=flat_pct 2.3)...')
        results.append(_run_variant(df_15m, daily_pivots, label, 'target1_pct_final', t1,
                                     THRESHOLD_MODE='pct', TARGET1_PCT=t1,
                                     SL_PCT=1.8, TARGET2_MODE='flat_pct', TARGET2_FLAT_PCT=2.3))

    summary = pd.DataFrame(results)
    summary.to_csv(SUMMARY_FILE, index=False)
    print(f'\nSaved {len(summary)} run(s) to {SUMMARY_FILE}')
    print(summary.to_string(index=False))


if __name__ == '__main__':
    main()
