"""
Prometheus - Phase 3 exit calibration: SL%, lot1-target%, lot2-target%
grid sweep, reusing the already-materialized 1-minute trade-path logs from
the Phase 3 multiplier sweep (data_sweep/mult_<X.X>/trade_logs/*.csv)
rather than re-running the raw backtest against raw price data.

Same staged, one-variable-at-a-time methodology as
prometheus_backtest/phase2/sweep_p2.py: SL grid (target1/target2 pinned)
-> target1 grid (SL pinned at stage-1 winner) -> target2 grid (SL/target1
pinned at their winners) -- run independently against EACH of the 7 Phase 3
multipliers (2026-09-01 user decision: cross-validate across the whole
grid, not just the top-Calmar multiplier, given 2.5's edge-of-grid / high-
trade-count overfitting risk flagged earlier this session).

Positional design preserved unchanged from Phase 3 (no EOD square-off, no
entry-time gate) -- this calibrates exits on top of Phase 3's already-
decided entry/holding design; it does not reinstate Phase 2's session
structure, which would require re-deriving day-boundary/close-time info
this log-based approach deliberately avoids reloading.

Fill conventions ported from backtest_p2.py, at 1-minute granularity
(finer than Phase 2's native 15-minute bars, since 1-minute is what the
saved Phase 3 logs are): stop_loss wins any same-minute tie against a
profit target; profit targets fill at the level or the bar's open on a
favourable gap-through; the stop fills at the level or the bar's open on
an adverse gap-through (see _target_fill_price / _stop_fill_price). The
trade's own logged trend_flip exit (trade_summary.csv's exit_price) is the
fallback when neither SL nor both targets trigger -- and the log's FINAL
row is excluded from the intrabar check, since that bar is the one the
original position already exits on at its open (see _simulate_trade).

Output:
  data_sweep/exit_calib_p3_detail.csv   -- one row per (multiplier, stage, param value)
  data_sweep/exit_calib_p3_winners.csv  -- one row per multiplier: chosen SL/T1/T2 and its stats
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configs_p3 as configs  # noqa: E402

SWEEP_DIR = configs.DATA_SWEEP_DIR
DETAIL_FILE = os.path.join(SWEEP_DIR, 'exit_calib_p3_detail.csv')
WINNERS_FILE = os.path.join(SWEEP_DIR, 'exit_calib_p3_winners.csv')

SL_GRID = [1.0, 1.4, 1.8, 2.2, 2.6, 3.0, 3.5]
T1_GRID = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
T2_GRID = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0]

T1_STARTING_DEFAULT = 1.0
T2_STARTING_DEFAULT = 2.3


def _target_fill_price(direction: str, level: float, bar_open: float) -> float:
    if direction == 'bullish':
        return bar_open if bar_open > level else level
    return bar_open if bar_open < level else level


def _stop_fill_price(direction: str, level: float, bar_open: float) -> float:
    if direction == 'bullish':
        return bar_open if bar_open < level else level
    return bar_open if bar_open > level else level


def _load_multiplier_data(mult: float) -> tuple:
    label = f'mult_{mult:.1f}'
    run_dir = os.path.join(SWEEP_DIR, label)
    trades = pd.read_csv(os.path.join(run_dir, 'trade_summary.csv'), parse_dates=['entry_ts', 'exit_ts'])
    trades = trades[trades['exit_ts'].notna()].reset_index(drop=True)

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


def _simulate_trade(trade_row: pd.Series, path_df: pd.DataFrame, sl_pct, t1_pct: float, t2_pct: float) -> dict:
    direction = trade_row['direction']
    entry_price = float(trade_row['entry_price'])

    sl_dist = entry_price * sl_pct / 100 if sl_pct is not None else None
    t1_dist = entry_price * t1_pct / 100
    t2_dist = entry_price * t2_pct / 100
    assert t2_dist > t1_dist, f"target2 dist {t2_dist:.2f} must exceed target1 dist {t1_dist:.2f}"

    if direction == 'bullish':
        sl_price = entry_price - sl_dist if sl_dist is not None else None
        t1_price = entry_price + t1_dist
        t2_price = entry_price + t2_dist
    else:
        sl_price = entry_price + sl_dist if sl_dist is not None else None
        t1_price = entry_price - t1_dist
        t2_price = entry_price - t2_dist

    lot1_open, lot2_open = True, True
    lot1_exit = lot2_exit = None

    # Exclude the final logged row -- that bar is where the position already
    # exits via trend_flip at its own open (see module docstring); it was
    # never "held" through that bar's high/low under the original design.
    rows = path_df.iloc[:-1] if len(path_df) > 1 else path_df.iloc[0:0]

    for _, bar in rows.iterrows():
        if not lot1_open and not lot2_open:
            break
        bar_open, bar_high, bar_low = bar['open'], bar['high'], bar['low']

        if sl_price is not None:
            sl_hit = (bar_low <= sl_price) if direction == 'bullish' else (bar_high >= sl_price)
            if sl_hit:
                fill = _stop_fill_price(direction, sl_price, bar_open)
                if lot1_open:
                    lot1_exit = (fill, 'stop_loss')
                    lot1_open = False
                if lot2_open:
                    lot2_exit = (fill, 'stop_loss')
                    lot2_open = False
                break

        if lot1_open:
            hit = (bar_high >= t1_price) if direction == 'bullish' else (bar_low <= t1_price)
            if hit:
                fill = _target_fill_price(direction, t1_price, bar_open)
                lot1_exit = (fill, 'target1')
                lot1_open = False

        if lot2_open:
            hit = (bar_high >= t2_price) if direction == 'bullish' else (bar_low <= t2_price)
            if hit:
                fill = _target_fill_price(direction, t2_price, bar_open)
                lot2_exit = (fill, 'target2')
                lot2_open = False

    flip_price = float(trade_row['exit_price'])
    if lot1_open:
        lot1_exit = (flip_price, 'trend_flip')
    if lot2_open:
        lot2_exit = (flip_price, 'trend_flip')

    def _pnl_pts(exit_price):
        return (exit_price - entry_price) if direction == 'bullish' else (entry_price - exit_price)

    return {
        'lot1_exit_reason': lot1_exit[1], 'lot1_pnl_points': round(_pnl_pts(lot1_exit[0]), 2),
        'lot2_exit_reason': lot2_exit[1], 'lot2_pnl_points': round(_pnl_pts(lot2_exit[0]), 2),
    }


def _run_variant(trades: pd.DataFrame, paths: dict, sl_pct, t1_pct: float, t2_pct: float) -> pd.DataFrame:
    rows = []
    for _, t in trades.iterrows():
        tid = int(t['trade_id'])
        if tid not in paths:
            continue
        result = _simulate_trade(t, paths[tid], sl_pct, t1_pct, t2_pct)
        result['trade_id'] = tid
        rows.append(result)
    return pd.DataFrame(rows)


def _summarize(sim: pd.DataFrame, mult: float, stage: str, param: str, value) -> dict:
    sim = sim.copy()
    sim['total_pnl_points'] = sim['lot1_pnl_points'] + sim['lot2_pnl_points']
    sim['total_pnl_rs'] = sim['total_pnl_points'] * configs.LOT_SIZE
    n = len(sim)
    pl = sim['total_pnl_rs'].astype(float)
    wins = int((pl > 0).sum())
    cumpl = pl.cumsum()
    max_dd = (cumpl - cumpl.cummax()).min()
    total_pl = pl.sum()

    lot1_hit = int((sim['lot1_exit_reason'] == 'target1').sum())
    lot2_hit = int((sim['lot2_exit_reason'] == 'target2').sum())
    sl_count = int((sim['lot1_exit_reason'] == 'stop_loss').sum())

    return {
        'multiplier': mult, 'stage': stage, 'param': param, 'value': value, 'n_trades': n,
        'win_rate_pct': round(wins / n * 100, 1) if n else float('nan'),
        'total_pnl_rs': round(total_pl, 0),
        'max_drawdown_rs': round(max_dd, 0),
        'calmar': round(total_pl / abs(max_dd), 2) if max_dd else float('nan'),
        'lot1_hit_rate_pct': round(lot1_hit / n * 100, 1) if n else float('nan'),
        'lot2_hit_rate_pct': round(lot2_hit / n * 100, 1) if n else float('nan'),
        'stop_loss_count': sl_count,
    }


def _best_by_calmar(rows: list) -> dict:
    return max(rows, key=lambda r: (r['calmar'] if pd.notna(r['calmar']) else float('-inf')))


def calibrate_multiplier(mult: float) -> tuple:
    trades, paths = _load_multiplier_data(mult)
    detail_rows = []

    # --- Stage 1: SL grid, target1/target2 pinned at starting defaults ---
    stage1 = []
    for sl in SL_GRID:
        sim = _run_variant(trades, paths, sl, T1_STARTING_DEFAULT, T2_STARTING_DEFAULT)
        row = _summarize(sim, mult, 'sl_grid', 'sl_pct', sl)
        stage1.append(row)
    detail_rows.extend(stage1)
    best_sl = _best_by_calmar(stage1)['value']

    # --- Stage 2: target1 grid, SL pinned at stage-1 winner, target2 pinned ---
    stage2 = []
    for t1 in T1_GRID:
        sim = _run_variant(trades, paths, best_sl, t1, T2_STARTING_DEFAULT)
        row = _summarize(sim, mult, 'target1_grid', 'target1_pct', t1)
        stage2.append(row)
    detail_rows.extend(stage2)
    best_t1 = _best_by_calmar(stage2)['value']

    # --- Stage 3: target2 grid, SL and target1 pinned at their winners.
    # Only candidates strictly beyond best_t1 are valid (target2 must sit
    # farther from entry than target1 -- see _simulate_trade's assertion).
    valid_t2_grid = [t2 for t2 in T2_GRID if t2 > best_t1]
    stage3 = []
    for t2 in valid_t2_grid:
        sim = _run_variant(trades, paths, best_sl, best_t1, t2)
        row = _summarize(sim, mult, 'target2_grid', 'target2_pct', t2)
        stage3.append(row)
    detail_rows.extend(stage3)
    best_t2 = _best_by_calmar(stage3)['value']

    winner = _summarize(_run_variant(trades, paths, best_sl, best_t1, best_t2), mult, 'final', 'combo',
                         f'sl{best_sl}_t1{best_t1}_t2{best_t2}')
    winner['sl_pct'] = best_sl
    winner['target1_pct'] = best_t1
    winner['target2_pct'] = best_t2

    return detail_rows, winner


def main():
    os.makedirs(SWEEP_DIR, exist_ok=True)
    all_detail = []
    winners = []

    for mult in configs.ST_MULTIPLIER_GRID:
        print(f'Calibrating multiplier {mult}...')
        detail_rows, winner = calibrate_multiplier(mult)
        all_detail.extend(detail_rows)
        winners.append(winner)
        print(f"  best: SL={winner['sl_pct']}%  T1={winner['target1_pct']}%  T2={winner['target2_pct']}%  "
              f"Calmar={winner['calmar']}  total P&L={winner['total_pnl_rs']} Rs  max DD={winner['max_drawdown_rs']} Rs")

    detail_df = pd.DataFrame(all_detail)
    detail_df.to_csv(DETAIL_FILE, index=False)

    winners_df = pd.DataFrame(winners)
    cols = ['multiplier', 'sl_pct', 'target1_pct', 'target2_pct', 'n_trades', 'win_rate_pct',
            'total_pnl_rs', 'max_drawdown_rs', 'calmar', 'lot1_hit_rate_pct', 'lot2_hit_rate_pct', 'stop_loss_count']
    winners_df = winners_df[cols]
    winners_df.to_csv(WINNERS_FILE, index=False)

    print(f'\nSaved detail ({len(detail_df)} rows) to {DETAIL_FILE}')
    print(f'Saved winners to {WINNERS_FILE}')
    print()
    pd.set_option('display.width', 220)
    pd.set_option('display.max_columns', None)
    print(winners_df.to_string(index=False))


if __name__ == '__main__':
    main()
