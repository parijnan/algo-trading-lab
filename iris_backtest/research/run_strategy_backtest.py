"""
Iris strategy backtest — 4-condition exit simulation + parameter sweep.

For each ST_FAST signal, loads the ITM-150 option price series from entry
to 15:00 and simulates the actual exit strategy:
  1. Profit target  — option high  ≥ entry × (1 + target_pct)
  2. Stop loss      — option low   ≤ entry × (1 - stop_pct)
  3. Trend flip     — first 5-min ST flip against trade direction
  4. Time cutoff    — 15:00 hard exit

Sweeps stop_pct × target_pct combinations to calibrate configs.py values.
Output: data/strategy_backtest_trades.csv + printed sweep table.

Usage (from repo root):
    python iris_backtest/research/run_strategy_backtest.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from datetime import timedelta
from itertools import product
from configs import (OUTPUT_DIR, OPTIONS_PATH, STRIKE_STEP,
                     LOT_SIZE, ST_FAST_PERIOD, ST_FAST_MULTIPLIER)
from utils import load_nifty_1min, resample_ohlcv, compute_st

EXIT_TIME_STR  = '15:00'
BAR_PERIOD_MIN = 5      # ST_FAST entry lag

# ── Parameter sweep grid ─────────────────────────────────────────────────────
STOP_GRID     = [0.10, 0.15, 0.20, 0.25, 0.30]
TARGET_GRID   = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]
MAX_HOLD_GRID = [15, 30, 60, None]   # per-trade max hold (minutes); None = day-end only


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _atm(spot):
    return int(round(spot / STRIKE_STEP) * STRIKE_STEP)


def _itm150_strike(spot, direction):
    atm  = _atm(spot)
    sign = -1 if direction == 'bullish' else +1
    return atm + sign * 3 * STRIKE_STEP   # 3 × 50 = 150 ITM


def _load_option_series(expiry_str: str, strike: int,
                        right: str, from_ts, to_ts):
    path = OPTIONS_PATH / expiry_str / f'{strike}{right}.csv'
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=['datetime'])
    df = df.set_index('datetime').sort_index()
    df = df[(df['open'] > 0) & (df['close'] > 0)]
    return df[(df.index >= from_ts) & (df.index <= to_ts)] or None


def _load_option_series(expiry_str: str, strike: int,
                        right: str, from_ts, to_ts):
    path = OPTIONS_PATH / expiry_str / f'{strike}{right}.csv'
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=['datetime'])
    df = df.set_index('datetime').sort_index()
    df = df[(df['open'] > 0) & (df['close'] > 0)]
    df = df[(df.index >= from_ts) & (df.index <= to_ts)]
    return df if not df.empty else None


def _get_price_near(df_opt, ts, max_gap_min=5):
    """Get open price at or nearest to ts (within max_gap_min)."""
    if df_opt is None or df_opt.empty:
        return np.nan
    pos = df_opt.index.searchsorted(ts)
    for i in (pos, pos - 1):
        if 0 <= i < len(df_opt):
            gap = abs((df_opt.index[i] - ts).total_seconds()) / 60
            if gap <= max_gap_min:
                return float(df_opt.iloc[i]['open'])
    return np.nan


# ---------------------------------------------------------------------------
# Pre-compute all 5-min ST flips for opposing-flip exit detection
# ---------------------------------------------------------------------------

def build_flip_index(df_1min: pd.DataFrame) -> pd.DataFrame:
    """
    Returns all 5-min ST flips (no regime filter) with columns
    [timestamp, direction]. Used to detect trend flip exits.
    """
    print('Computing all 5-min ST flips...', end=' ', flush=True)
    df5   = resample_ohlcv(df_1min, '5min')
    st    = compute_st(df5, ST_FAST_PERIOD, ST_FAST_MULTIPLIER)
    flips = st[st['trend_flip'] & st['trend'].notna()].copy()
    out   = pd.DataFrame({
        'timestamp': flips.index,
        'direction': flips['trend'].map({True: 'bullish', False: 'bearish'}),
    }).reset_index(drop=True)
    print(f'{len(out):,} flips')
    return out


def next_opposing_flip(flip_index: pd.DataFrame,
                       entry_ts, direction: str,
                       exit_ts) -> pd.Timestamp | None:
    """Return timestamp of next 5-min flip against trade direction, or None."""
    mask = (
        (flip_index['timestamp'] > entry_ts) &
        (flip_index['timestamp'] <= exit_ts) &
        (flip_index['direction'] != direction)
    )
    sub = flip_index[mask]
    return sub['timestamp'].iloc[0] if not sub.empty else None


# ---------------------------------------------------------------------------
# Single trade simulation
# ---------------------------------------------------------------------------

def simulate_trade(price_series: pd.DataFrame,
                   entry_price: float,
                   flip_ts,
                   stop_pct: float,
                   target_pct: float,
                   exit_time_ts,
                   max_hold_ts=None) -> tuple:
    """
    Simulate one trade through the price series.
    Returns (exit_price, exit_reason, exit_ts).

    Checks intrabar high/low for target/stop triggers (realistic fills).
    Profit target and stop loss are treated as limit fills at the exact level.
    """
    profit_level = entry_price * (1 + target_pct)
    stop_level   = entry_price * (1 - stop_pct)

    for ts, bar in price_series.iterrows():
        # Stop loss: intrabar low touches stop level
        if bar['low'] <= stop_level:
            return stop_level, 'stop_loss', ts

        # Profit target: intrabar high touches target level
        if bar['high'] >= profit_level:
            return profit_level, 'profit_target', ts

        # Trend flip: exit at close of flip bar
        if flip_ts is not None and ts >= flip_ts:
            return float(bar['close']), 'trend_flip', ts

        # Per-trade max hold (scalp timer)
        if max_hold_ts is not None and ts >= max_hold_ts:
            return float(bar['close']), 'max_hold', ts

        # Time cutoff: exit at close of last bar before/at 15:00
        if ts >= exit_time_ts:
            return float(bar['close']), 'time_cutoff', ts

    # Reached end of data without any exit trigger
    if not price_series.empty:
        last = price_series.iloc[-1]
        return float(last['close']), 'eod', price_series.index[-1]
    return entry_price, 'no_data', None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    sim_path = OUTPUT_DIR / 'options_sim_results.csv'
    if not sim_path.exists():
        print('Run run_options_sim.py first.')
        sys.exit(1)

    sim = pd.read_csv(sim_path, parse_dates=['signal_ts'])
    sim = sim[sim['ITM_150_entry'].notna()].copy()
    print(f'Signals with ITM_150 entry data: {len(sim):,}\n')

    df_1min    = load_nifty_1min()
    flip_index = build_flip_index(df_1min)

    # ── Phase 1: load option series and precompute trade context ─────────
    print('Loading option price series for each trade...')
    trades = []
    option_cache = {}
    skipped = 0

    for i, (_, row) in enumerate(sim.iterrows()):
        if (i + 1) % 100 == 0:
            print(f'  {i+1}/{len(sim)}  skipped={skipped}', flush=True)

        signal_ts  = row['signal_ts']
        direction  = row['direction']
        spot       = row['spot']
        expiry_str = str(row['expiry'])
        entry_ts   = signal_ts + pd.Timedelta(minutes=BAR_PERIOD_MIN)

        right      = 'ce' if direction == 'bullish' else 'pe'
        strike     = _itm150_strike(spot, direction)
        cache_key  = (expiry_str, strike, right)

        # Load + cache option file
        if cache_key not in option_cache:
            # Load the full day; we'll slice per trade
            path = OPTIONS_PATH / expiry_str / f'{strike}{right}.csv'
            if path.exists():
                df_opt = pd.read_csv(path, parse_dates=['datetime'])
                df_opt = df_opt.set_index('datetime').sort_index()
                df_opt = df_opt[(df_opt['open'] > 0) & (df_opt['close'] > 0)]
                option_cache[cache_key] = df_opt
            else:
                option_cache[cache_key] = None

        df_opt = option_cache[cache_key]

        # Entry price: open of first bar at/after entry_ts
        entry_price = _get_price_near(df_opt, entry_ts)
        if np.isnan(entry_price):
            skipped += 1
            continue

        # Price series from entry to 15:00 same day
        eod_ts  = pd.Timestamp(f'{entry_ts.date()} {EXIT_TIME_STR}:00')
        series  = df_opt[(df_opt.index >= entry_ts) &
                         (df_opt.index <= eod_ts)].copy() if df_opt is not None else None
        if series is None or series.empty:
            skipped += 1
            continue

        # Next opposing 5-min ST flip (capped at 15:00)
        flip_ts = next_opposing_flip(flip_index, entry_ts, direction, eod_ts)

        trades.append({
            'signal_ts':   signal_ts,
            'entry_ts':    entry_ts,
            'direction':   direction,
            'spot':        spot,
            'strike':      strike,
            'right':       right,
            'expiry':      expiry_str,
            'entry_price': entry_price,
            'flip_ts':     flip_ts,
            'series':      series,
            'eod_ts':      eod_ts,
        })

    print(f'\nTrades loaded: {len(trades):,}  (skipped {skipped} — missing option data)\n')

    # ── Phase 2: parameter sweep ─────────────────────────────────────────
    n_combos = len(STOP_GRID) * len(TARGET_GRID) * len(MAX_HOLD_GRID)
    print(f'Sweeping {len(STOP_GRID)} stops × {len(TARGET_GRID)} targets × '
          f'{len(MAX_HOLD_GRID)} max-hold = {n_combos} combinations...\n')

    sweep_results = []

    for max_hold_min, stop_pct, target_pct in product(MAX_HOLD_GRID, STOP_GRID, TARGET_GRID):
        pnl_list, reasons, holds = [], [], []

        for t in trades:
            max_hold_ts = (t['entry_ts'] + pd.Timedelta(minutes=max_hold_min)
                           if max_hold_min else None)
            exit_price, reason, exit_ts = simulate_trade(
                t['series'], t['entry_price'], t['flip_ts'],
                stop_pct, target_pct, t['eod_ts'], max_hold_ts)

            pnl = exit_price - t['entry_price']
            pnl_list.append(pnl)
            reasons.append(reason)
            if exit_ts:
                holds.append((exit_ts - t['entry_ts']).total_seconds() / 60)

        arr    = np.array(pnl_list)
        n      = len(arr)
        wr     = (arr > 0).mean() * 100
        avg    = arr.mean()
        med    = np.median(arr)
        avg_rs = avg * LOT_SIZE

        reason_counts = pd.Series(reasons).value_counts()
        r_target = reason_counts.get('profit_target', 0) / n * 100
        r_stop   = reason_counts.get('stop_loss',     0) / n * 100
        r_flip   = reason_counts.get('trend_flip',    0) / n * 100
        r_time   = (reason_counts.get('time_cutoff',  0) +
                    reason_counts.get('max_hold',      0) +
                    reason_counts.get('eod',           0)) / n * 100
        avg_hold = np.mean(holds) if holds else 0

        sweep_results.append({
            'max_hold':    max_hold_min if max_hold_min else 'day',
            'stop_pct':    stop_pct,
            'target_pct':  target_pct,
            'n':           n,
            'wr':          round(wr,    1),
            'avg_pts':     round(avg,   2),
            'med_pts':     round(med,   2),
            'avg_rs':      round(avg_rs, 0),
            'avg_hold':    round(avg_hold, 1),
            'r_target':    round(r_target, 1),
            'r_stop':      round(r_stop,   1),
            'r_flip':      round(r_flip,   1),
            'r_time':      round(r_time,   1),
        })

    sweep_df = pd.DataFrame(sweep_results)

    # ── Phase 3: print ────────────────────────────────────────────────────
    print(f'{"Hold":>5}  {"Stop":>6}  {"Target":>7}  {"WR%":>6}  '
          f'{"AvgPts":>8}  {"MedPts":>8}  {"AvgRs":>8}  {"AvgHold":>8}  '
          f'{"Tgt%":>6}  {"SL%":>6}  {"Flip%":>6}  {"Time%":>6}')
    print('─' * 110)

    for _, r in sweep_df.sort_values('avg_rs', ascending=False).head(25).iterrows():
        print(f'{str(r["max_hold"])+"m":>5}  {r["stop_pct"]:>5.0%}  '
              f'{r["target_pct"]:>6.0%}  {r["wr"]:>6.1f}  '
              f'{r["avg_pts"]:>+8.2f}  {r["med_pts"]:>+8.2f}  '
              f'{r["avg_rs"]:>+8.0f}  {r["avg_hold"]:>8.1f}  '
              f'{r["r_target"]:>6.1f}  {r["r_stop"]:>6.1f}  '
              f'{r["r_flip"]:>6.1f}  {r["r_time"]:>6.1f}')

    best = sweep_df.sort_values('avg_rs', ascending=False).iloc[0]
    print(f'\nBest (avg ₹/lot): hold={best["max_hold"]}m  '
          f'stop={best["stop_pct"]:.0%}  target={best["target_pct"]:.0%}  '
          f'→ avg ₹{best["avg_rs"]:+,.0f}/lot  WR={best["wr"]:.1f}%  '
          f'avg hold={best["avg_hold"]:.0f} min')

    # Best by max_hold tier
    print('\n── Best combination per max-hold tier (ranked by avg ₹/lot) ──')
    print(f'{"Hold":>5}  {"Stop":>6}  {"Target":>7}  {"WR%":>6}  '
          f'{"AvgRs":>8}  {"MedRs":>8}  {"AvgHold":>8}  '
          f'{"Tgt%":>6}  {"SL%":>6}  {"Flip%":>6}  {"Time%":>6}')
    print('─' * 85)
    for hold in ['15', '30', '60', 'day']:
        tier = sweep_df[sweep_df['max_hold'].astype(str) == hold]
        if tier.empty:
            continue
        r = tier.sort_values('avg_rs', ascending=False).iloc[0]
        med_rs = round(r['med_pts'] * LOT_SIZE, 0)
        print(f'{str(r["max_hold"])+"m":>5}  {r["stop_pct"]:>5.0%}  '
              f'{r["target_pct"]:>6.0%}  {r["wr"]:>6.1f}  '
              f'{r["avg_rs"]:>+8.0f}  {med_rs:>+8.0f}  '
              f'{r["avg_hold"]:>8.1f}  '
              f'{r["r_target"]:>6.1f}  {r["r_stop"]:>6.1f}  '
              f'{r["r_flip"]:>6.1f}  {r["r_time"]:>6.1f}')

    # Save full sweep
    sweep_path = OUTPUT_DIR / 'strategy_sweep.csv'
    sweep_df.to_csv(sweep_path, index=False)
    print(f'Sweep saved → {sweep_path.name}')

    # ── Save per-trade detail for best params ─────────────────────────────
    best_hold = best['max_hold']
    best_hold_min = None if best_hold == 'day' else int(best_hold)
    print(f'\nSaving per-trade detail '
          f'(hold={best["max_hold"]}m  stop={best["stop_pct"]:.0%}  '
          f'target={best["target_pct"]:.0%})...')

    trade_rows = []
    for t in trades:
        max_hold_ts = (t['entry_ts'] + pd.Timedelta(minutes=best_hold_min)
                       if best_hold_min else None)
        exit_price, reason, exit_ts = simulate_trade(
            t['series'], t['entry_price'], t['flip_ts'],
            best['stop_pct'], best['target_pct'], t['eod_ts'], max_hold_ts)
        pnl      = exit_price - t['entry_price']
        hold_min = int((exit_ts - t['entry_ts']).total_seconds() // 60) \
                   if exit_ts else 0
        trade_rows.append({
            'signal_ts':   t['signal_ts'],
            'direction':   t['direction'],
            'spot':        t['spot'],
            'strike':      t['strike'],
            'entry_price': t['entry_price'],
            'exit_price':  round(exit_price, 2),
            'exit_reason': reason,
            'exit_ts':     exit_ts,
            'hold_min':    hold_min,
            'pnl_pts':     round(pnl, 2),
            'pnl_rs':      round(pnl * LOT_SIZE, 0),
        })

    trades_df = pd.DataFrame(trade_rows)
    trades_path = OUTPUT_DIR / 'strategy_backtest_trades.csv'
    trades_df.to_csv(trades_path, index=False)
    print(f'Trade detail saved → {trades_path.name}')

    # Quick summary
    print(f'\n── Trade summary ({len(trades_df)} trades) ──')
    print(trades_df.groupby('exit_reason')['pnl_rs'].agg(['count','mean','median'])
          .rename(columns={'count':'n','mean':'avg_rs','median':'med_rs'})
          .round(0).to_string())
    print(f'\nTotal P&L (1 lot):  ₹{trades_df["pnl_rs"].sum():+,.0f}')
    print(f'Avg hold duration:  {trades_df["hold_min"].mean():.1f} min')


if __name__ == '__main__':
    main()
