"""
Iris full backtest — per-trade logs + trailing stop analysis.

Runs the 4-condition exit strategy and writes:
  data/iris_backtest_summary.csv  — one row per trade, full metrics
  data/trade_logs/trade_NNNN_YYYY-MM-DD_HHMM_direction.csv — per-minute log

Each per-trade log tracks option LTP, unrealised P&L, running MFE,
and trailing stop levels (15/20/25/30% from peak) every available bar.

Usage (from repo root):
    python iris_backtest/research/run_full_backtest.py
"""
import sys, os, csv
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from run_strategy_backtest import (
    build_flip_index, next_opposing_flip, _itm150_strike, _get_price_near,
)
from configs import OUTPUT_DIR, OPTIONS_PATH, LOT_SIZE, STRIKE_STEP, SKIP_ENTRY_WINDOWS

# ── Strategy params ───────────────────────────────────────────────────────────
STOP_PCT       = 0.25
TARGET_PCT     = 0.10
MAX_HOLD_MIN   = 30
EXIT_TIME_STR  = '15:00'
BAR_PERIOD_MIN = 5

# Trailing stop levels to simulate (% drop from peak LTP).
# A trail fires only after the trade has gone into profit at all (LTP > entry).
TRAIL_LEVELS = [0.15, 0.20, 0.25, 0.30]

LOG_DIR = OUTPUT_DIR / 'trade_logs'

# Pre-parse skip windows once
_SKIP_WINDOWS = [
    (pd.Timestamp(s).time(), pd.Timestamp(e).time())
    for s, e in SKIP_ENTRY_WINDOWS
]

def _in_skip_window(ts) -> bool:
    t = ts.time() if hasattr(ts, 'time') else ts
    return any(start <= t < end for start, end in _SKIP_WINDOWS)


# ---------------------------------------------------------------------------

def simulate_with_logs(series: pd.DataFrame,
                       entry_price: float,
                       flip_ts,
                       stop_pct: float,
                       target_pct: float,
                       max_hold_ts,
                       eod_ts) -> dict:
    """
    Simulate the trade and return a dict with:
      - exit_price, exit_reason, exit_ts
      - full bar-by-bar log as a list of dicts
      - MFE/MAE reached during trade
      - per-trail-level simulated exit price + reason
    """
    profit_level = entry_price * (1 + target_pct)
    stop_level   = entry_price * (1 - stop_pct)

    peak_ltp  = entry_price      # highest LTP seen (for trailing)
    trough_ltp = entry_price     # lowest LTP seen (for MAE)
    mfe_pts   = 0.0
    mae_pts   = 0.0
    mfe_ts    = None
    mae_ts    = None

    # Per-trail tracking
    trail_exits = {lvl: None for lvl in TRAIL_LEVELS}  # (exit_price, exit_ts)

    bar_log   = []
    result    = {}

    for ts, bar in series.iterrows():
        ltp = float(bar['close'])

        still_open = 'exit_price' not in result

        # ── Track extremes and trails only while trade is open ─────────────
        if still_open:
            if ltp > peak_ltp:
                peak_ltp = ltp
                mfe_pts  = peak_ltp - entry_price
                mfe_ts   = ts
            if ltp < trough_ltp:
                trough_ltp = ltp
                mae_pts    = entry_price - trough_ltp
                mae_ts     = ts

            for lvl in TRAIL_LEVELS:
                if peak_ltp > entry_price:
                    trail_stop = peak_ltp * (1 - lvl)
                    if trail_exits[lvl] is None and ltp <= trail_stop:
                        trail_exits[lvl] = (ltp, ts, 'trail_stop')

        unr_pts = ltp - entry_price
        unr_pct = unr_pts / entry_price

        # Trail stop levels for logging (always compute, even post-exit)
        trail_stops = {}
        for lvl in TRAIL_LEVELS:
            trail_stops[lvl] = (round(peak_ltp * (1 - lvl), 2)
                                if peak_ltp > entry_price else None)

        bar_log.append({
            'ts':          ts,
            'option_ltp':  round(ltp, 2),
            'unr_pts':     round(unr_pts, 2),
            'unr_pct':     round(unr_pct * 100, 2),
            'unr_rs':      round(unr_pts * LOT_SIZE, 0),
            'mfe_pts':     round(mfe_pts, 2),
            'mfe_pct':     round(mfe_pts / entry_price * 100, 2),
            **{f'trail_stop_{int(lvl*100)}': trail_stops[lvl]
               for lvl in TRAIL_LEVELS},
        })

        # ── Primary exit checks (skip if already exited) ───────────────────
        if not still_open:
            continue

        if bar['low'] <= stop_level:
            result = {'exit_price': stop_level, 'exit_reason': 'stop_loss',    'exit_ts': ts}
        elif bar['high'] >= profit_level:
            result = {'exit_price': profit_level, 'exit_reason': 'profit_target', 'exit_ts': ts}
        elif flip_ts is not None and ts >= flip_ts:
            result = {'exit_price': ltp, 'exit_reason': 'trend_flip',   'exit_ts': ts}
        elif max_hold_ts is not None and ts >= max_hold_ts:
            result = {'exit_price': ltp, 'exit_reason': 'max_hold',     'exit_ts': ts}
        elif ts >= eod_ts:
            result = {'exit_price': ltp, 'exit_reason': 'time_cutoff',  'exit_ts': ts}

    if 'exit_price' not in result and bar_log:
        last = bar_log[-1]
        result = {'exit_price': last['option_ltp'],
                  'exit_reason': 'eod', 'exit_ts': bar_log[-1]['ts']}

    result['mfe_pts']  = round(mfe_pts, 2)
    result['mfe_pct']  = round(mfe_pts / entry_price * 100, 2)
    result['mfe_ts']   = mfe_ts
    result['mae_pts']  = round(mae_pts, 2)
    result['mae_pct']  = round(mae_pts / entry_price * 100, 2)
    result['mae_ts']   = mae_ts
    result['bar_log']  = bar_log

    for lvl in TRAIL_LEVELS:
        key = f'trail_{int(lvl*100)}'
        te  = trail_exits[lvl]
        if te:
            result[f'{key}_exit_pts'] = round(te[0] - entry_price, 2)
            result[f'{key}_exit_rs']  = round((te[0] - entry_price) * LOT_SIZE, 0)
            result[f'{key}_exit_ts']  = te[1]
            result[f'{key}_reason']   = te[2]
        else:
            # Trail never fired — use actual exit as the fallback
            ep = result.get('exit_price', entry_price)
            result[f'{key}_exit_pts'] = round(ep - entry_price, 2)
            result[f'{key}_exit_rs']  = round((ep - entry_price) * LOT_SIZE, 0)
            result[f'{key}_exit_ts']  = result.get('exit_ts')
            result[f'{key}_reason']   = result.get('exit_reason')

    return result


# ---------------------------------------------------------------------------

def main():
    from utils import load_nifty_1min

    sim_path = OUTPUT_DIR / 'options_sim_results.csv'
    sim = pd.read_csv(sim_path, parse_dates=['signal_ts'])
    sim = sim[sim['ITM_150_entry'].notna()].copy()

    df_1min    = load_nifty_1min()
    flip_index = build_flip_index(df_1min)

    LOG_DIR.mkdir(exist_ok=True)

    print(f'Running full backtest  (stop={STOP_PCT:.0%}  target={TARGET_PCT:.0%}  '
          f'max_hold={MAX_HOLD_MIN}m)\n')

    summary_rows = []
    option_cache = {}
    skipped      = 0
    trade_num    = 0

    for i, (_, row) in enumerate(sim.iterrows()):
        if (i + 1) % 200 == 0:
            print(f'  {i+1}/{len(sim)}  written={trade_num}  skipped={skipped}',
                  flush=True)

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

        # Skip entry windows
        if _in_skip_window(entry_ts):
            skipped += 1
            continue

        df_opt      = option_cache[cache_key]
        entry_price = _get_price_near(df_opt, entry_ts)
        if np.isnan(entry_price):
            skipped += 1
            continue

        eod_ts      = pd.Timestamp(f'{entry_ts.date()} {EXIT_TIME_STR}:00')
        max_hold_ts = entry_ts + pd.Timedelta(minutes=MAX_HOLD_MIN)
        series      = (df_opt[(df_opt.index >= entry_ts) & (df_opt.index <= eod_ts)]
                       if df_opt is not None else None)
        if series is None or series.empty:
            skipped += 1
            continue

        flip_ts = next_opposing_flip(flip_index, entry_ts, direction, eod_ts)

        res = simulate_with_logs(
            series, entry_price, flip_ts,
            STOP_PCT, TARGET_PCT, max_hold_ts, eod_ts)

        trade_num += 1
        exit_price  = res['exit_price']
        exit_reason = res['exit_reason']
        if exit_reason in ('time_cutoff', 'eod'):
            exit_reason = 'max_hold'
        exit_ts    = res['exit_ts']
        pnl_pts    = round(exit_price - entry_price, 2)
        pnl_rs     = round(pnl_pts * LOT_SIZE, 0)
        hold_min   = int((exit_ts - entry_ts).total_seconds() // 60) if exit_ts else 0

        # ── Write per-trade log ───────────────────────────────────────────
        log_name = (f'trade_{trade_num:04d}_'
                    f'{entry_ts.strftime("%Y-%m-%d_%H%M")}_'
                    f'{direction[0].upper()}.csv')
        log_path = LOG_DIR / log_name

        trail_hdr = []
        for lvl in TRAIL_LEVELS:
            trail_hdr.append(f'trail_stop_{int(lvl*100)}')

        with open(log_path, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=[
                'elapsed_min', 'ts', 'option_ltp',
                'unr_pts', 'unr_pct', 'unr_rs',
                'mfe_pts', 'mfe_pct',
                *trail_hdr,
            ])
            w.writeheader()
            for j, b in enumerate(res['bar_log']):
                w.writerow({
                    'elapsed_min': j,
                    'ts':          b['ts'],
                    'option_ltp':  b['option_ltp'],
                    'unr_pts':     b['unr_pts'],
                    'unr_pct':     b['unr_pct'],
                    'unr_rs':      b['unr_rs'],
                    'mfe_pts':     b['mfe_pts'],
                    'mfe_pct':     b['mfe_pct'],
                    **{f'trail_stop_{int(lvl*100)}': b.get(f'trail_stop_{int(lvl*100)}', '')
                       for lvl in TRAIL_LEVELS},
                })

        # ── Summary row ───────────────────────────────────────────────────
        srow = {
            'trade_id':    f'{trade_num:04d}',
            'log_file':    log_name,
            'signal_ts':   signal_ts,
            'direction':   direction,
            'entry_ts':    entry_ts,
            'entry_price': entry_price,
            'entry_spot':  spot,
            'expiry':      expiry_str,
            'strike':      strike,
            'right':       right,
            'exit_ts':     exit_ts,
            'exit_price':  round(exit_price, 2),
            'exit_reason': exit_reason,
            'hold_min':    hold_min,
            'pnl_pts':     pnl_pts,
            'pnl_rs':      pnl_rs,
            'mfe_pts':     res['mfe_pts'],
            'mfe_pct':     res['mfe_pct'],
            'mfe_ts':      res['mfe_ts'],
            'mae_pts':     res['mae_pts'],
            'mae_pct':     res['mae_pct'],
            'mae_ts':      res['mae_ts'],
            'pnl_at_mfe_rs': round(res['mfe_pts'] * LOT_SIZE, 0),
        }
        for lvl in TRAIL_LEVELS:
            k = f'trail_{int(lvl*100)}'
            srow[f'{k}_exit_pts'] = res[f'{k}_exit_pts']
            srow[f'{k}_exit_rs']  = res[f'{k}_exit_rs']
            srow[f'{k}_reason']   = res[f'{k}_reason']

        summary_rows.append(srow)

    summary = pd.DataFrame(summary_rows)
    out_path = OUTPUT_DIR / 'iris_backtest_summary.csv'
    summary.to_csv(out_path, index=False)

    print(f'\nTrades written: {trade_num:,}  |  Skipped: {skipped}')
    print(f'Summary → {out_path.name}')
    print(f'Logs    → {LOG_DIR.name}/ ({trade_num} files)\n')

    # ── Overall summary ───────────────────────────────────────────────────
    _print_summary(summary)


def _print_summary(df: pd.DataFrame) -> None:
    bucket_map = {
        'profit_target': '1. Profit Target',
        'stop_loss':     '2. Stop Loss',
        'trend_flip':    '3. Trend Flip',
        'max_hold':      '4. Max Hold',
    }

    print(f'{"Bucket":<20}  {"N":>5}  {"Wins":>5}  {"Loss":>5}  '
          f'{"WR%":>6}  {"AvgRs":>8}  {"MedRs":>8}  '
          f'{"AvgMFE%":>8}  {"AvgMAE%":>8}')
    print('─' * 88)

    for key, label in bucket_map.items():
        sub  = df[df['exit_reason'] == key]
        if sub.empty:
            continue
        wins = (sub['pnl_rs'] > 0).sum()
        loss = (sub['pnl_rs'] < 0).sum()
        wr   = wins / len(sub) * 100
        print(f'{label:<20}  {len(sub):>5}  {wins:>5}  {loss:>5}  '
              f'{wr:>5.1f}  '
              f'{sub["pnl_rs"].mean():>+8.0f}  {sub["pnl_rs"].median():>+8.0f}  '
              f'{sub["mfe_pct"].mean():>8.1f}  {sub["mae_pct"].mean():>8.1f}')

    print('─' * 88)
    wins = (df['pnl_rs'] > 0).sum()
    loss = (df['pnl_rs'] < 0).sum()
    wr   = wins / len(df) * 100
    print(f'{"TOTAL":<20}  {len(df):>5}  {wins:>5}  {loss:>5}  '
          f'{wr:>5.1f}  '
          f'{df["pnl_rs"].mean():>+8.0f}  {df["pnl_rs"].median():>+8.0f}  '
          f'{df["mfe_pct"].mean():>8.1f}  {df["mae_pct"].mean():>8.1f}')

    # ── Trailing stop analysis (Bucket 4 only) ────────────────────────────
    b4 = df[df['exit_reason'] == 'max_hold'].copy()
    print(f'\n── Bucket 4: trailing stop comparison ({len(b4)} trades) ──\n')

    print(f'{"Exit rule":<28}  {"N fired":>8}  {"WR%":>6}  '
          f'{"AvgRs":>8}  {"MedRs":>8}  {"Total Rs":>10}')
    print('─' * 70)

    # Baseline: no trailing stop (actual exit at max_hold)
    wins = (b4['pnl_rs'] > 0).sum()
    wr   = wins / len(b4) * 100
    print(f'{"No trail (baseline)":<28}  {"—":>8}  {wr:>5.1f}  '
          f'{b4["pnl_rs"].mean():>+8.0f}  {b4["pnl_rs"].median():>+8.0f}  '
          f'{b4["pnl_rs"].sum():>+10.0f}')

    for lvl in TRAIL_LEVELS:
        k        = f'trail_{int(lvl*100)}'
        fired    = b4[b4[f'{k}_reason'] == 'trail_stop']
        col_pts  = f'{k}_exit_pts'
        col_rs   = f'{k}_exit_rs'
        # Use trail exit for those that fired, actual exit for rest
        combined_rs = pd.concat([
            fired[col_rs],
            b4.loc[b4[f'{k}_reason'] != 'trail_stop', 'pnl_rs'],
        ])
        wins = (combined_rs > 0).sum()
        wr   = wins / len(combined_rs) * 100
        print(f'{"Trail stop "+str(int(lvl*100))+"%":<28}  '
              f'{len(fired):>8}  {wr:>5.1f}  '
              f'{combined_rs.mean():>+8.0f}  {combined_rs.median():>+8.0f}  '
              f'{combined_rs.sum():>+10.0f}')

    # How deep in profit did Bucket 4 trades get?
    print(f'\nBucket 4 MFE distribution (how far in profit before reverting):')
    pcts = [0, 25, 50, 75, 90, 95, 99]
    print('  Percentile  |  ' + '  '.join(f'P{p:>2}' for p in pcts))
    print('  MFE %       |  ' +
          '  '.join(f'{b4["mfe_pct"].quantile(p/100):>+4.1f}%' for p in pcts))
    print(f'\n  Trades with MFE > 5%:   {(b4["mfe_pct"] > 5).sum():>4}  '
          f'({(b4["mfe_pct"] > 5).mean()*100:.1f}%)')
    print(f'  Trades with MFE > 10%:  {(b4["mfe_pct"] > 10).sum():>4}  '
          f'({(b4["mfe_pct"] > 10).mean()*100:.1f}%)')
    print(f'  Trades with MFE > 0%:   {(b4["mfe_pct"] > 0).sum():>4}  '
          f'({(b4["mfe_pct"] > 0).mean()*100:.1f}%)')


if __name__ == '__main__':
    main()
