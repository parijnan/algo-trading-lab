"""
Prometheus — univariate SL/target/max-hold sweep.

Repo convention (CLAUDE.md): one variable changed per experiment. Each run
in the grid enables exactly one of {stop_loss, profit_target, max_hold} on
top of the v1 baseline (trend_flip + eod_squareoff, always on) — never a
joint combination.

Grid (advisor-reviewed against the baseline's MAE/MFE/hold_min percentiles,
see prometheus_backtest/data/prometheus_trade_log.csv):
  stop_loss     15,20,25,30,40,50,60,80,120   (winners' median final MAE is
                                                19, losers' is 64 — dense
                                                where that split bites)
  profit_target 30,50,75,100,125,150,200,250  (winners' median final MFE is
                                                162)
  max_hold      30,45,60,90,120,180,240       (90th-pct hold is 271 min)

SCOPE LIMIT: this sweep reuses the existing per-trade path CSVs in
data/trade_logs/, which span [entry_ts, v1_baseline_exit_ts] only. A
threshold/time exit can therefore only ever fire *earlier* than the v1
baseline exit for that trade — never later. This is exactly right for
testing "add one more preemptive exit on top of trend_flip/eod_squareoff"
(the only thing being tested here), but it CANNOT answer what happens to a
variant that extends a trade past its v1 exit (e.g. removing trend_flip
entirely, or a trailing stop that survives a flip) — that needs freshly
regenerated path data with a longer window.

Fill convention (matches the backtest.py fix applied alongside this
script): stop_loss/profit_target fill at the threshold price itself
(entry +/- points), not at a bar open — the breach happens mid-bar.
max_hold fills at its triggering bar's open (a clock fact, not a price
breach). Resolution: path files are 1-min, but production checks exits on
5-min bars, so bars are bucketed into 5-min groups (aligned for free since
entry_ts already sits on the 5-min grid) before searching for the first
breach, to reproduce exactly what enabling the check in backtest.py would
see.

Output layout mirrors the repo's per-variant convention
(athena_backtest/data_tc/trade_logs_tc/, artemis_backtest/data_vix15/):
  data_sweep/<label>/trade_log.csv        — one row per trade, this run
  data_sweep/<label>/trade_logs/*.csv     — per-trade 1-min path for this
                                             run's ACTUAL exit (bucketed to
                                             5-min, truncated at the new
                                             exit)
  data_sweep/sweep_summary.csv            — one row per run, baseline first
"""

import glob
import os

import pandas as pd

import configs

SWEEP_DIR = os.path.join(configs.BASE_DIR, 'data_sweep')
SUMMARY_FILE = os.path.join(SWEEP_DIR, 'sweep_summary.csv')

STOP_LOSS_GRID     = [15, 20, 25, 30, 40, 50, 60, 80, 120]
PROFIT_TARGET_GRID = [30, 50, 75, 100, 125, 150, 200, 250]
MAX_HOLD_GRID      = [30, 45, 60, 90, 120, 180, 240]

# Trailing stop: activation is a second variable, so it's fixed (not swept)
# alongside a distance sweep, per CLAUDE.md's one-variable-per-experiment
# rule. 40 sits between losers' median final MAE (64) and winners' (19) —
# meant to stay dormant through the entry-noise band that chops sl015/sl020.
TRAIL_ACTIVATION_POINTS = 40
TRAIL_DISTANCE_GRID = [30, 40, 50, 60, 80, 100]

# ~24 runs x 178 trades = ~4,300 per-trade files. Set False to keep only
# trade_log.csv + sweep_summary.csv if that gets unwieldy.
SAVE_PER_TRADE_LOGS = True


def _load_baseline_trades() -> pd.DataFrame:
    tl = pd.read_csv(configs.TRADE_LOG_FILE)
    tl['entry_ts'] = pd.to_datetime(tl['entry_ts'])
    tl['exit_ts']  = pd.to_datetime(tl['exit_ts'])
    return tl


def _load_bucketed_path(trade_id: int, entry_ts: pd.Timestamp) -> pd.DataFrame:
    matches = glob.glob(os.path.join(configs.TRADE_LOGS_DIR, f"trade_{trade_id:04d}_*.csv"))
    if not matches:
        return pd.DataFrame()
    path = pd.read_csv(matches[0], parse_dates=['ts'])
    path['bucket'] = path['bars_since_entry'] // 5
    bucketed = path.groupby('bucket').agg(
        open=('open', 'first'),
        high=('high', 'max'),
        low=('low', 'min'),
        running_mae=('running_mae', 'last'),
        running_mfe=('running_mfe', 'last'),
    ).reset_index()
    bucketed['ts'] = entry_ts + pd.to_timedelta(bucketed['bucket'] * 5, unit='min')
    return bucketed


def _first_trailing_stop_hit(bucketed: pd.DataFrame, direction: str, entry_price: float,
                              activation: float, trail_distance: float):
    """
    Walk buckets in order, ratchet-then-check within each: running_mfe at
    bucket i already reflects that bucket's own high (see _load_bucketed_path),
    so it IS the post-ratchet peak for bucket i — using it directly is
    correct and avoids a future-information leak. Once peak >= activation,
    the bucket's own low (bullish) / high (bearish) is compared against the
    ratcheted peak to see if this bucket's retracement breaches the trail.
    Returns (ts, exit_price) of the first breach, or None.
    """
    for _, b in bucketed.iterrows():
        peak = b['running_mfe']
        if peak < activation:
            continue
        if direction == 'bullish':
            worst_favorable_this_bucket = b['low'] - entry_price
        else:
            worst_favorable_this_bucket = entry_price - b['high']
        retracement = peak - worst_favorable_this_bucket
        if retracement >= trail_distance:
            exit_price = (entry_price + (peak - trail_distance) if direction == 'bullish'
                          else entry_price - (peak - trail_distance))
            return b['ts'], exit_price, b['bucket']
    return None


def _simulate_trade_exit(trade: pd.Series, bucketed: pd.DataFrame,
                          param_name: str, param_value: float):
    """
    Return (exit_ts, exit_price, exit_reason, path_for_run) for one trade
    under this run's single added exit check. If the check never fires
    within the available window, the trade keeps its baseline exit
    unchanged (see module docstring's scope limit).
    """
    direction   = trade['direction']
    entry_price = trade['entry_price']

    if param_name == 'stop_loss':
        hit = bucketed[bucketed['running_mae'] >= param_value]
        if not hit.empty:
            b = hit.iloc[0]
            exit_price = (entry_price - param_value if direction == 'bullish'
                          else entry_price + param_value)
            return b['ts'], exit_price, 'stop_loss', bucketed[bucketed['bucket'] <= b['bucket']]

    elif param_name == 'profit_target':
        hit = bucketed[bucketed['running_mfe'] >= param_value]
        if not hit.empty:
            b = hit.iloc[0]
            exit_price = (entry_price + param_value if direction == 'bullish'
                          else entry_price - param_value)
            return b['ts'], exit_price, 'profit_target', bucketed[bucketed['bucket'] <= b['bucket']]

    elif param_name == 'max_hold':
        hit = bucketed[(bucketed['bucket'] * 5) >= param_value]
        if not hit.empty:
            b = hit.iloc[0]
            return b['ts'], b['open'], f'max_hold ({param_value}m)', bucketed[bucketed['bucket'] <= b['bucket']]

    elif param_name == 'trailing_stop':
        hit = _first_trailing_stop_hit(bucketed, direction, entry_price,
                                        TRAIL_ACTIVATION_POINTS, param_value)
        if hit is not None:
            ts, exit_price, bucket_idx = hit
            reason = f'trailing_stop (act{TRAIL_ACTIVATION_POINTS}_trail{param_value})'
            return ts, exit_price, reason, bucketed[bucketed['bucket'] <= bucket_idx]

    return trade['exit_ts'], trade['exit_price'], trade['exit_reason'], bucketed


def _run_one(baseline: pd.DataFrame, param_name: str, param_value: float, label: str) -> dict:
    run_dir  = os.path.join(SWEEP_DIR, label)
    logs_dir = os.path.join(run_dir, 'trade_logs')
    os.makedirs(run_dir, exist_ok=True)
    if SAVE_PER_TRADE_LOGS:
        os.makedirs(logs_dir, exist_ok=True)

    rows = []
    for _, t in baseline.iterrows():
        bucketed = _load_bucketed_path(int(t['trade_id']), t['entry_ts'])
        if bucketed.empty:
            continue

        exit_ts, exit_price, exit_reason, run_path = _simulate_trade_exit(
            t, bucketed, param_name, param_value)

        direction   = t['direction']
        entry_price = t['entry_price']
        pnl_points  = (exit_price - entry_price) if direction == 'bullish' else (entry_price - exit_price)

        rows.append({
            'trade_id':        t['trade_id'],
            'contract_expiry': t['contract_expiry'],
            'direction':       direction,
            'entry_ts':        t['entry_ts'],
            'entry_price':     entry_price,
            'exit_ts':         exit_ts,
            'exit_price':      round(exit_price, 2),
            'exit_reason':     exit_reason,
            'hold_min':        round((exit_ts - t['entry_ts']).total_seconds() / 60, 1),
            'pnl_points':      round(pnl_points, 2),
            'pnl_rs':          round(pnl_points * configs.LOT_SIZE * configs.LOTS, 2),
        })

        if SAVE_PER_TRADE_LOGS:
            letter = 'B' if direction == 'bullish' else 'S'
            fname = (f"trade_{int(t['trade_id']):04d}_"
                     f"{t['entry_ts'].strftime('%Y-%m-%d_%H%M')}_{letter}.csv")
            run_path.drop(columns=['bucket']).to_csv(os.path.join(logs_dir, fname), index=False)

    run_trades = pd.DataFrame(rows)
    run_trades.to_csv(os.path.join(run_dir, 'trade_log.csv'), index=False)

    return _summarize(run_trades, baseline, label, param_name, param_value)


def _summarize(run_trades: pd.DataFrame, baseline: pd.DataFrame,
               label: str, param_name, param_value) -> dict:
    pl = run_trades['pnl_rs'].astype(float)
    n  = len(run_trades)
    wins = int((pl > 0).sum())
    cumpl = pl.cumsum()
    max_dd = (cumpl - cumpl.cummax()).min()
    total_pl = pl.sum()

    # EOD-bucket survival: of the baseline's eod_squareoff trades (the sole
    # source of profit — see CAS/Prometheus discussion), how many still
    # reach an eod_squareoff exit under this run, i.e. weren't preempted?
    baseline_eod = baseline[baseline['exit_reason'].str.startswith('eod_squareoff')]
    run_indexed  = run_trades.set_index('trade_id')
    still_eod    = run_indexed.loc[baseline_eod['trade_id']]
    still_eod    = still_eod[still_eod['exit_reason'].str.startswith('eod_squareoff')]

    return {
        'label':              label,
        'param_name':         param_name if param_name else 'baseline',
        'param_value':        param_value,
        'n_trades':           n,
        'win_rate_pct':       round(wins / n * 100, 1) if n else float('nan'),
        'expectancy_rs':      round(pl.mean(), 0) if n else float('nan'),
        'total_pnl_rs':       round(total_pl, 0),
        'max_drawdown_rs':    round(max_dd, 0),
        'calmar':             round(total_pl / abs(max_dd), 2) if max_dd != 0 else float('nan'),
        'avg_hold_min':       round(run_trades['hold_min'].mean(), 0) if n else float('nan'),
        'baseline_eod_count': len(baseline_eod),
        'baseline_eod_pnl_rs': round(baseline_eod['pnl_rs'].sum(), 0),
        'eod_survival_count': len(still_eod),
        'eod_survival_pnl_rs': round(still_eod['pnl_rs'].sum(), 0),
    }


def main():
    os.makedirs(SWEEP_DIR, exist_ok=True)
    baseline = _load_baseline_trades()

    summaries = [_summarize(baseline, baseline, 'baseline', None, None)]

    grids = [
        ('stop_loss',     STOP_LOSS_GRID),
        ('profit_target', PROFIT_TARGET_GRID),
        ('max_hold',      MAX_HOLD_GRID),
        ('trailing_stop', TRAIL_DISTANCE_GRID),
    ]
    prefixes = {'stop_loss': 'sl', 'profit_target': 'tgt', 'max_hold': 'hold',
                'trailing_stop': 'trail'}

    for param_name, grid in grids:
        for value in grid:
            if param_name == 'trailing_stop':
                label = f"trail{value:03d}_act{TRAIL_ACTIVATION_POINTS:03d}"
            else:
                label = f"{prefixes[param_name]}{value:03d}"
            print(f'Running {label} ({param_name}={value})...')
            summaries.append(_run_one(baseline, param_name, value, label))

    summary_df = pd.DataFrame(summaries)
    summary_df.to_csv(SUMMARY_FILE, index=False)
    print(f'\nSaved {len(summary_df)} run(s) to {SUMMARY_FILE}')
    print(summary_df.to_string(index=False))


if __name__ == '__main__':
    main()
