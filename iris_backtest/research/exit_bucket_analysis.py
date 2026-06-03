"""
Exit bucket analysis for Iris strategy backtest.

Runs the 4-condition exit strategy with selected params and shows P&L
broken down by exit bucket, including wins/losses split per bucket.

Usage (from repo root):
    python iris_backtest/research/exit_bucket_analysis.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from run_strategy_backtest import (
    build_flip_index, next_opposing_flip, simulate_trade,
    _load_option_series, _get_price_near, _itm150_strike,
)
from configs import OUTPUT_DIR, OPTIONS_PATH, STRIKE_STEP, LOT_SIZE

# ── Params to analyse ─────────────────────────────────────────────────────────
STOP_PCT            = 0.25
TARGET_PCT          = 0.10
MAX_HOLD_MIN        = 30
EXIT_TIME_STR       = '15:15'   # hard EOD cutoff — all open trades exit at this bar's open
LAST_ENTRY_TIME_STR = '15:00'   # last valid entry — signals closing at/after 15:00 ignored
BAR_PERIOD_MIN      = 5

BUCKET_ORDER  = ['profit_target', 'stop_loss', 'trend_flip', 'max_hold']
BUCKET_LABELS = {
    'profit_target': '1. Profit Target',
    'stop_loss':     '2. Stop Loss',
    'trend_flip':    '3. Trend Flip',
    'max_hold':      '4. Max Hold (30 min)',
}


def main():
    from utils import load_nifty_1min

    sim_path = OUTPUT_DIR / 'options_sim_results.csv'
    sim = pd.read_csv(sim_path, parse_dates=['signal_ts'])
    sim = sim[sim['ITM_150_entry'].notna()].copy()

    df_1min    = load_nifty_1min()
    flip_index = build_flip_index(df_1min)

    print(f'Loading option series  (stop={STOP_PCT:.0%}  target={TARGET_PCT:.0%}  '
          f'max_hold={MAX_HOLD_MIN}m)...')

    records = []
    option_cache = {}
    skipped = 0
    _last_entry = pd.Timestamp(LAST_ENTRY_TIME_STR + ':00').time()

    for i, (_, row) in enumerate(sim.iterrows()):
        signal_ts  = row['signal_ts']
        direction  = row['direction']
        spot       = row['spot']
        expiry_str = str(row['expiry'])
        entry_ts   = signal_ts + pd.Timedelta(minutes=BAR_PERIOD_MIN)
        right      = 'ce' if direction == 'bullish' else 'pe'
        strike     = _itm150_strike(spot, direction)
        cache_key  = (expiry_str, strike, right)

        if cache_key not in option_cache:
            path = OPTIONS_PATH / expiry_str / f'{strike}{right}.csv'
            if path.exists():
                df_opt = pd.read_csv(path, parse_dates=['datetime'])
                df_opt = df_opt.set_index('datetime').sort_index()
                df_opt = df_opt[(df_opt['open'] > 0) & (df_opt['close'] > 0)]
                option_cache[cache_key] = df_opt
            else:
                option_cache[cache_key] = None

        # Drop signals whose entry would fall after the last valid entry time
        if entry_ts.time() > _last_entry:
            skipped += 1
            continue

        df_opt = option_cache[cache_key]
        entry_price = _get_price_near(df_opt, entry_ts)
        if np.isnan(entry_price):
            skipped += 1
            continue

        eod_ts      = pd.Timestamp(f'{entry_ts.date()} {EXIT_TIME_STR}:00')
        max_hold_ts = entry_ts + pd.Timedelta(minutes=MAX_HOLD_MIN)
        series      = df_opt[(df_opt.index >= entry_ts) &
                              (df_opt.index <= eod_ts)].copy() \
                      if df_opt is not None else None
        if series is None or series.empty:
            skipped += 1
            continue

        flip_ts = next_opposing_flip(flip_index, entry_ts, direction, eod_ts)

        exit_price, reason, exit_ts = simulate_trade(
            series, entry_price, flip_ts,
            STOP_PCT, TARGET_PCT, eod_ts, max_hold_ts)

        pnl_pts = exit_price - entry_price
        pnl_rs  = pnl_pts * LOT_SIZE
        hold_min = int((exit_ts - entry_ts).total_seconds() // 60) \
                   if exit_ts else 0

        # Normalise 'time_cutoff' / 'eod' → 'max_hold' for cleanliness
        if reason in ('time_cutoff', 'eod'):
            reason = 'max_hold'

        records.append({
            'signal_ts':   signal_ts,
            'direction':   direction,
            'entry_price': entry_price,
            'exit_price':  round(exit_price, 2),
            'pnl_pts':     round(pnl_pts, 2),
            'pnl_rs':      round(pnl_rs,  0),
            'exit_reason': reason,
            'hold_min':    hold_min,
        })

    df = pd.DataFrame(records)
    print(f'Trades: {len(df):,}  skipped: {skipped}\n')

    # ── Main grid ─────────────────────────────────────────────────────────────
    rows = []
    for bucket in BUCKET_ORDER:
        sub   = df[df['exit_reason'] == bucket]
        wins  = (sub['pnl_rs'] > 0).sum()
        losses = (sub['pnl_rs'] < 0).sum()
        be    = (sub['pnl_rs'] == 0).sum()
        wr    = wins / len(sub) * 100 if len(sub) else 0
        rows.append({
            'bucket':     BUCKET_LABELS[bucket],
            'n':          len(sub),
            'wins':       wins,
            'losses':     losses,
            'be':         be,
            'wr':         round(wr, 1),
            'avg_pnl':    round(sub['pnl_rs'].mean(), 0) if len(sub) else 0,
            'med_pnl':    round(sub['pnl_rs'].median(), 0) if len(sub) else 0,
            'best':       round(sub['pnl_rs'].max(), 0) if len(sub) else 0,
            'worst':      round(sub['pnl_rs'].min(), 0) if len(sub) else 0,
            'avg_hold':   round(sub['hold_min'].mean(), 1) if len(sub) else 0,
        })

    all_wins   = (df['pnl_rs'] > 0).sum()
    all_losses = (df['pnl_rs'] < 0).sum()
    all_be     = (df['pnl_rs'] == 0).sum()
    rows.append({
        'bucket': 'TOTAL',
        'n':       len(df),
        'wins':    all_wins,
        'losses':  all_losses,
        'be':      all_be,
        'wr':      round(all_wins / len(df) * 100, 1),
        'avg_pnl': round(df['pnl_rs'].mean(), 0),
        'med_pnl': round(df['pnl_rs'].median(), 0),
        'best':    round(df['pnl_rs'].max(), 0),
        'worst':   round(df['pnl_rs'].min(), 0),
        'avg_hold': round(df['hold_min'].mean(), 1),
    })

    gdf = pd.DataFrame(rows)

    W = 22
    print(f'{"Bucket":<{W}}  {"N":>5}  {"Wins":>5}  {"Loss":>5}  '
          f'{"WR%":>6}  {"AvgRs":>8}  {"MedRs":>8}  '
          f'{"Best":>8}  {"Worst":>8}  {"AvgHold":>8}')
    print('─' * 100)
    for _, r in gdf.iterrows():
        sep = '─' * 100 if r['bucket'] == 'TOTAL' else ''
        if sep:
            print(sep)
        be_note = f' (+{r["be"]} BE)' if r['be'] > 0 else ''
        print(f'{r["bucket"]:<{W}}  {r["n"]:>5}  {r["wins"]:>5}  '
              f'{r["losses"]:>5}{be_note:<10}  '
              f'{r["wr"]:>5.1f}  {r["avg_pnl"]:>+8.0f}  {r["med_pnl"]:>+8.0f}  '
              f'{r["best"]:>+8.0f}  {r["worst"]:>+8.0f}  {r["avg_hold"]:>8.1f}')

    print(f'\n── Bucket 4 (Max Hold) P&L distribution ──')
    b4 = df[df['exit_reason'] == 'max_hold']['pnl_rs']
    pcts = [1, 5, 10, 25, 50, 75, 90, 95, 99]
    print('  Percentile  |  ' + '  '.join(f'P{p:>2}' for p in pcts))
    print('  P&L (₹)     |  ' +
          '  '.join(f'{b4.quantile(p/100):>+5.0f}' for p in pcts))
    print(f'\n  Mean: ₹{b4.mean():+,.0f}   Std: ₹{b4.std():,.0f}   '
          f'Wins: {(b4>0).sum()} ({(b4>0).mean()*100:.1f}%)   '
          f'Losses: {(b4<0).sum()} ({(b4<0).mean()*100:.1f}%)')

    print(f'\nTotal P&L (1 lot, {len(df)} trades):  ₹{df["pnl_rs"].sum():+,.0f}')


if __name__ == '__main__':
    main()
