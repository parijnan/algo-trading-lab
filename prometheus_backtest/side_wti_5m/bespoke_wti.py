"""
Prometheus — WTI 5-minute side project: main driver. For each of Phase 3's
two already-decided bespoke combos (configs_wti.BESPOKE_COMBOS), builds the
15-min Supertrend series at that combo's own multiplier, runs the raw
signal-flip backtest (backtest_wti.run_backtest), saves per-trade 5-min
paths (reusing phase3/trade_paths_p3.save_trade_paths_p3 unchanged — it's
pure, no configs coupling, just needs a trades df + a bar-indexed df), then
applies the SL/target1/target2 exit simulation.

_simulate_trade_detailed below is copied from phase3/bespoke_2lot_p3.py,
not imported — that module's version reads configs_p3.LOT_SIZE (=10, for
CRUDEOILM) at module scope, which would silently apply the wrong lot size
here. Copied instead of monkey-patched, matching phase3_crudeoil's own
"copy the pipeline, change what's needed" precedent rather than mutating
shared module state. _target_fill_price/_stop_fill_price ARE imported
directly from exit_calib_p3.py — confirmed pure (direction/level/bar_open
args only), no configs dependency, safe to reuse as-is.

Output: data_sweep/mult_<X.X>/bespoke_trade_summary.csv (same schema as
phase3/phase3_crudeoil's own bespoke_trade_summary.csv, minus the
Rs-labeled columns' real Rs meaning — LOT_SIZE=1 here, see configs_wti.py).
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configs_wti as configs  # noqa: E402
from backtest_wti import run_backtest  # noqa: E402
from wti_data_loader import load_wti_5min  # noqa: E402

sys.path.insert(0, configs.PROMETHEUS_DIR)
from data_loader import resample_ohlcv, compute_st  # noqa: E402

sys.path.insert(0, os.path.join(configs.PROMETHEUS_DIR, 'phase3'))
from trade_paths_p3 import save_trade_paths_p3  # noqa: E402
from exit_calib_p3 import _target_fill_price, _stop_fill_price  # noqa: E402


def _simulate_trade_detailed(trade_row: pd.Series, path_df: pd.DataFrame,
                              sl_pct: float, t1_pct: float, t2_pct: float) -> dict:
    direction = trade_row['direction']
    entry_price = float(trade_row['entry_price'])

    sl_dist = entry_price * sl_pct / 100
    t1_dist = entry_price * t1_pct / 100
    t2_dist = entry_price * t2_pct / 100
    assert t2_dist > t1_dist, f"target2 dist {t2_dist:.2f} must exceed target1 dist {t1_dist:.2f}"

    if direction == 'bullish':
        sl_price = entry_price - sl_dist
        t1_price = entry_price + t1_dist
        t2_price = entry_price + t2_dist
    else:
        sl_price = entry_price + sl_dist
        t1_price = entry_price - t1_dist
        t2_price = entry_price - t2_dist

    lot1_open, lot2_open = True, True
    lot1_exit = lot2_exit = None  # (ts, price, reason)

    rows = path_df.iloc[:-1] if len(path_df) > 1 else path_df.iloc[0:0]

    for _, bar in rows.iterrows():
        if not lot1_open and not lot2_open:
            break
        bar_open, bar_high, bar_low, ts = bar['open'], bar['high'], bar['low'], bar['ts']

        sl_hit = (bar_low <= sl_price) if direction == 'bullish' else (bar_high >= sl_price)
        if sl_hit:
            fill = _stop_fill_price(direction, sl_price, bar_open)
            if lot1_open:
                lot1_exit = (ts, fill, 'stop_loss'); lot1_open = False
            if lot2_open:
                lot2_exit = (ts, fill, 'stop_loss'); lot2_open = False
            break

        if lot1_open:
            hit = (bar_high >= t1_price) if direction == 'bullish' else (bar_low <= t1_price)
            if hit:
                fill = _target_fill_price(direction, t1_price, bar_open)
                lot1_exit = (ts, fill, 'target1'); lot1_open = False

        if lot2_open:
            hit = (bar_high >= t2_price) if direction == 'bullish' else (bar_low <= t2_price)
            if hit:
                fill = _target_fill_price(direction, t2_price, bar_open)
                lot2_exit = (ts, fill, 'target2'); lot2_open = False

    flip_ts, flip_price = trade_row['exit_ts'], float(trade_row['exit_price'])
    if lot1_open:
        lot1_exit = (flip_ts, flip_price, 'trend_flip')
    if lot2_open:
        lot2_exit = (flip_ts, flip_price, 'trend_flip')

    def _pnl_pts(exit_price):
        return (exit_price - entry_price) if direction == 'bullish' else (entry_price - exit_price)

    lot1_pnl_pts = round(_pnl_pts(lot1_exit[1]), 2)
    lot2_pnl_pts = round(_pnl_pts(lot2_exit[1]), 2)

    return {
        'trade_id': int(trade_row['trade_id']),
        'contract_expiry': trade_row['contract_expiry'],
        'direction': direction,
        'entry_ts': trade_row['entry_ts'],
        'entry_price': entry_price,
        'signal_ts': trade_row['signal_ts'],
        'signal_close': trade_row['signal_close'],
        'entry_slippage_points': trade_row['entry_slippage_points'],
        'sl_price': round(sl_price, 2),
        'lot1_target': round(t1_price, 2),
        'lot2_target': round(t2_price, 2),
        'lot1_exit_ts': lot1_exit[0], 'lot1_exit_price': round(lot1_exit[1], 2),
        'lot1_exit_reason': lot1_exit[2], 'lot1_pnl_points': lot1_pnl_pts,
        'lot1_pnl_rs': round(lot1_pnl_pts * configs.LOT_SIZE, 2),
        'lot2_exit_ts': lot2_exit[0], 'lot2_exit_price': round(lot2_exit[1], 2),
        'lot2_exit_reason': lot2_exit[2], 'lot2_pnl_points': lot2_pnl_pts,
        'lot2_pnl_rs': round(lot2_pnl_pts * configs.LOT_SIZE, 2),
        'total_pnl_points': round(lot1_pnl_pts + lot2_pnl_pts, 2),
        'total_pnl_rs': round((lot1_pnl_pts + lot2_pnl_pts) * configs.LOT_SIZE, 2),
        'raw_exit_ts': flip_ts, 'raw_exit_reason_if_unmanaged': 'trend_flip',
    }


def run_combo(df_15m_raw: pd.DataFrame, df_5m: pd.DataFrame, mult: float,
              sl_pct: float, t1_pct: float, t2_pct: float) -> str:
    label = f'mult_{mult:.1f}'
    print(f'\n--- {label}: ST_PERIOD={configs.ST_PERIOD} ST_MULTIPLIER={mult} '
          f'SL={sl_pct}% T1={t1_pct}% T2={t2_pct}% ---')

    df_15m = compute_st(df_15m_raw, configs.ST_PERIOD, mult)
    trades = run_backtest(df_15m)
    closed = trades[trades['exit_ts'].notna()]
    print(f'  {len(closed)} closed trade(s) ({len(trades) - len(closed)} still open at data end)')

    run_dir = os.path.join(configs.DATA_SWEEP_DIR, label)
    logs_dir = os.path.join(run_dir, 'trade_logs')
    os.makedirs(run_dir, exist_ok=True)

    # save_trade_paths_p3 walks the finest bar-indexed df available for
    # MAE/MFE — phase3 uses 1-min; here that's the 5-min bars themselves
    # (see side_wti_5m/README.md's granularity caveat).
    path_summaries = save_trade_paths_p3(closed, df_5m, logs_dir)
    paths = {}
    for s in path_summaries:
        tid = s['trade_id']
        letter = 'B' if closed.loc[closed['trade_id'] == tid, 'direction'].iloc[0] == 'bullish' else 'S'
        entry_ts = closed.loc[closed['trade_id'] == tid, 'entry_ts'].iloc[0]
        fname = f"trade_{tid:04d}_{pd.Timestamp(entry_ts):%Y-%m-%d_%H%M}_{letter}.csv"
        fpath = os.path.join(logs_dir, fname)
        if os.path.exists(fpath):
            paths[tid] = pd.read_csv(fpath, parse_dates=['ts'])

    rows = []
    for _, t in closed.iterrows():
        tid = int(t['trade_id'])
        if tid not in paths:
            continue
        rows.append(_simulate_trade_detailed(t, paths[tid], sl_pct, t1_pct, t2_pct))

    out = pd.DataFrame(rows)
    out_path = os.path.join(run_dir, 'bespoke_trade_summary.csv')
    out.to_csv(out_path, index=False)
    wins = int((out['total_pnl_rs'] > 0).sum()) if not out.empty else 0
    print(f'  {len(out)} bespoke-simulated trade(s), {wins} wins, '
          f"total P&L {out['total_pnl_rs'].sum():,.1f} pts  -> {out_path}")
    return out_path


def main():
    os.makedirs(configs.DATA_SWEEP_DIR, exist_ok=True)
    print(f'Loading WTI 5-min data from {configs.WTI_DATA_FILE}...')
    df_5m = load_wti_5min(configs.WTI_DATA_FILE)
    print(f'Resampling to 15-min ({len(df_5m):,} 5-min bars)...')
    df_15m_raw = resample_ohlcv(df_5m, '15min')

    for mult, sl, t1, t2 in configs.BESPOKE_COMBOS:
        run_combo(df_15m_raw, df_5m, mult, sl, t1, t2)


if __name__ == '__main__':
    main()
