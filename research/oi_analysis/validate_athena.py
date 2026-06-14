"""
validate_athena.py — Test OI signals against Athena CE parachute (emergency hedge) events.

For each of the 21 CE parachute triggers in the Athena VIX 16-25 backtest:
  1. Finds exact trigger timestamp from the per-trade log (emer_active flips True)
  2. Builds OI profile for that expiry on-the-fly
  3. Extracts OI state at trigger time (CE wall distance, PCR, OI at sell strike, OI direction)
  4. Classifies outcome: was the hedge warranted (emer_pl > 0) or a false alarm (emer_pl < 0)?
  5. Prints discrimination table and summary statistics

Hypothesis to test:
  - At CE sell strike, OI unwinding (writers covering) → breakout likely → hedge was correct
  - At CE sell strike, OI building (writers adding/defending) → reversal likely → hedge was false alarm

Usage:
    python research/oi_analysis/validate_athena.py

No pre-built feature file required. Runs for ~5 minutes (21 expiries × ~5s each).
"""

import os, sys, glob, time
import numpy as np
import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)

from research.oi_analysis.oi_engine import build_oi_profile, oi_at_strike

TRADE_SUMMARY  = os.path.join(_REPO_ROOT, 'athena_backtest', 'data_wing_reactive_pct_150',
                               'trade_summary_wing_reactive_pct_150.csv')
TRADE_LOGS_DIR = os.path.join(_REPO_ROOT, 'athena_backtest', 'data_wing_reactive_pct_150', 'trade_logs')
NIFTY_INDEX    = os.path.join(_REPO_ROOT, 'data_pipeline', 'data', 'indices', 'nifty.csv')
NIFTY_OPT_PATH = os.path.join(_REPO_ROOT, 'data_pipeline', 'data', 'nifty', 'options')
OUTPUT_DIR     = os.path.join(os.path.dirname(__file__), 'data')

OI_LOOKBACK_BARS = 6    # 30 min of OI direction at sell strike (6 × 5-min bars)
RESAMPLE         = '5min'
# CE parachute trigger: spot >= ce_sell_strike + 150 pts (EMERGENCY_TRIGGER_OFFSET = -150)
TRIGGER_OFFSET   = 150


def load_index():
    df = pd.read_csv(NIFTY_INDEX, parse_dates=['time_stamp'])
    df = df.rename(columns={'time_stamp': 'ts'}).set_index('ts')
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


def find_trigger_ts(log_df):
    """Return timestamp when emer_active first became True."""
    triggered = log_df[log_df['emer_active'] == True]
    return triggered.index[0] if not triggered.empty else None


def outcome_label(row):
    """
    Classify the CE parachute outcome from trade-level emer_pl.
    emer_pl > 0: hedge made money → spot kept running (hedge correct)
    emer_pl < 0: hedge lost → spot reversed (false alarm)
    """
    pl = row.get('emer_pl', 0)
    try:
        pl = float(pl)
    except (TypeError, ValueError):
        pl = 0
    if pl > 5:
        return 'breakout'
    elif pl < -5:
        return 'reversal'
    else:
        return 'neutral'


def closest_before(df, ts, ts_col='ts'):
    """Return last row of df where df[ts_col] <= ts, or None."""
    before = df[df[ts_col] <= ts] if ts_col in df.columns else df[df.index <= ts]
    return before.iloc[-1] if not before.empty else None


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    summary = pd.read_csv(TRADE_SUMMARY)
    emer = summary[
        summary['emer_strike'].notna() &
        (summary['emer_strike'].astype(str).str.strip() != '') &
        (summary['emer_strike'].astype(str).str.strip() != '0')
    ].copy()

    print(f'Athena CE parachute events: {len(emer)} of {len(summary)} trades')
    print('Loading Nifty index data...', end=' ', flush=True)
    index_df = load_index()
    print(f'{len(index_df):,} rows')

    print('\nProcessing each emergency hedge event:')
    print('─' * 118)
    print(f'{"#":>3}  {"Entry":10}  {"Trigger":16}  {"Spot":>7}  {"CEsell":>6}  '
          f'{"Outcome":>8}  {"CEwall%":>7}  {"CEwallOI":>9}  '
          f'{"OI@sell":>9}  {"OIdelta":>9}  {"PCRnear":>7}  {"Signal":>8}')
    print('─' * 118)

    rows = []

    for idx, trade in emer.iterrows():
        trade_num   = idx + 1
        entry_date  = trade['entry_time'][:10]
        sell_expiry = str(trade['sell_expiry'])[:10]
        ce_sell     = int(float(trade['ce_sell_strike']))

        # ── find trigger timestamp ─────────────────────────────────────────
        log_files = sorted(glob.glob(os.path.join(TRADE_LOGS_DIR, f'trade_{trade_num:04d}_*.csv')))
        if not log_files:
            print(f'  {trade_num:3d}  {entry_date}  LOG NOT FOUND')
            continue

        log_df = pd.read_csv(log_files[0], parse_dates=['time_stamp'])
        log_df = log_df.set_index('time_stamp')
        trigger_ts = find_trigger_ts(log_df)
        if trigger_ts is None:
            print(f'  {trade_num:3d}  {entry_date}  no trigger in log (sell_expiry={sell_expiry})')
            continue

        spot_at_trig = float(log_df.loc[trigger_ts, 'spot']) if trigger_ts in log_df.index else np.nan
        outcome = outcome_label(trade)

        # ── build OI profile for this expiry ──────────────────────────────
        options_dir = os.path.join(NIFTY_OPT_PATH, sell_expiry)
        t0 = time.time()

        oi_feat = build_oi_profile(
            expiry_date=sell_expiry,
            options_dir=options_dir,
            index_df=index_df,
            strike_step=50,
            resample=RESAMPLE,
        )

        # ── OI features at trigger time ────────────────────────────────────
        ce_wall_dist_pct = ce_wall_oi_val = pcr_near_val = np.nan
        if not oi_feat.empty:
            oi_feat_ts = oi_feat.reset_index()
            # index name after reset_index is the original column name ('ts' or level_0)
            ts_col_name = oi_feat_ts.columns[0]
            oi_feat_ts = oi_feat_ts.rename(columns={ts_col_name: 'ts'})
            oi_feat_ts['ts'] = pd.to_datetime(oi_feat_ts['ts'])
            row_at_trig = closest_before(oi_feat_ts, trigger_ts)
            if row_at_trig is not None:
                ce_wall_dist_pct = float(row_at_trig.get('ce_wall_dist_pct', np.nan))
                ce_wall_oi_val   = float(row_at_trig.get('ce_wall_oi', np.nan))
                pcr_near_val     = float(row_at_trig.get('pcr_near', np.nan))

        # ── OI direction at CE sell strike ────────────────────────────────
        oi_at_sell_val = oi_delta_val = np.nan
        oi_strike_df = oi_at_strike(
            expiry_date=sell_expiry,
            options_dir=options_dir,
            strike=ce_sell,
            side='ce',
            index_df=index_df,
            lookback_bars=OI_LOOKBACK_BARS,
            resample=RESAMPLE,
        )
        if not oi_strike_df.empty:
            oi_strike_df.index = pd.to_datetime(oi_strike_df.index)
            before_oi = oi_strike_df[oi_strike_df.index <= trigger_ts]
            if not before_oi.empty:
                oi_at_sell_val = float(before_oi.iloc[-1]['oi'])
                oi_delta_val   = float(before_oi.iloc[-1]['oi_delta'])

        # ── OI signal from sell strike delta ──────────────────────────────
        if np.isnan(oi_delta_val):
            oi_signal = 'nodata'
        elif oi_delta_val < -2000:
            oi_signal = 'breakout'   # writers covering → spot likely keeps going
        elif oi_delta_val > 2000:
            oi_signal = 'reversal'   # writers adding → wall holding → likely reversal
        else:
            oi_signal = 'neutral'

        elapsed = time.time() - t0
        print(
            f'  {trade_num:3d}  {entry_date}  {str(trigger_ts)[:16]}  '
            f'{spot_at_trig:7.0f}  {ce_sell:6d}  '
            f'{outcome:>8s}  {ce_wall_dist_pct:7.2f}  {ce_wall_oi_val:9.0f}  '
            f'{oi_at_sell_val:9.0f}  {oi_delta_val:9.0f}  {pcr_near_val:7.3f}  {oi_signal:>8s}'
            f'  [{elapsed:.1f}s]'
        )

        rows.append({
            'trade_num':         trade_num,
            'entry_date':        entry_date,
            'sell_expiry':       sell_expiry,
            'trigger_ts':        trigger_ts,
            'spot_at_trigger':   spot_at_trig,
            'ce_sell_strike':    ce_sell,
            'outcome':           outcome,
            'ce_wall_dist_pct':  ce_wall_dist_pct,
            'ce_wall_oi':        ce_wall_oi_val,
            'oi_at_sell_strike': oi_at_sell_val,
            'oi_delta_30min':    oi_delta_val,
            'pcr_near':          pcr_near_val,
            'oi_signal':         oi_signal,
            'emer_pl':           float(trade.get('emer_pl', 0) or 0),
            'total_pl_points':   float(trade.get('total_pl_points', 0) or 0),
        })

    print('─' * 118)

    if not rows:
        print('No results.')
        return

    results = pd.DataFrame(rows)

    # ── discrimination summary ────────────────────────────────────────────────
    print('\n' + '=' * 60)
    print('OI DISCRIMINATION ANALYSIS — Athena CE Parachute')
    print('=' * 60)

    outcome_counts = results['outcome'].value_counts()
    print(f'\nOutcome breakdown: {dict(outcome_counts)}')

    for outcome in ['breakout', 'reversal', 'neutral']:
        sub = results[results['outcome'] == outcome]
        if sub.empty:
            continue
        n = len(sub)
        print(f'\n── {outcome.upper()} ({n} trades) ──')
        def _stat(col):
            v = sub[col].dropna()
            return f'mean={v.mean():.1f}  median={v.median():.1f}  (n={len(v)})' if len(v) else 'no data'
        print(f'  CE wall dist %  : {_stat("ce_wall_dist_pct")}')
        print(f'  CE wall OI      : {_stat("ce_wall_oi")}')
        print(f'  OI @ sell strike: {_stat("oi_at_sell_strike")}')
        print(f'  OI delta 30min  : {_stat("oi_delta_30min")}')
        print(f'  PCR near        : {_stat("pcr_near")}')
        sig_c = sub['oi_signal'].value_counts()
        print(f'  OI signal       : {dict(sig_c)}')
        print(f'  emer_pl (avg)   : {sub["emer_pl"].mean():.1f}')

    # ── signal accuracy ───────────────────────────────────────────────────────
    print()
    has_signal = results[results['oi_signal'].isin(['breakout', 'reversal'])]
    if not has_signal.empty:
        correct = (has_signal['oi_signal'] == has_signal['outcome']).sum()
        total   = len(has_signal)
        print(f'OI signal accuracy where signal available: {correct}/{total} = {correct/total*100:.1f}%')

    breakouts = results[results['outcome'] == 'breakout']
    reversals = results[results['outcome'] == 'reversal']
    if len(breakouts) > 0 and len(reversals) > 0:
        print()
        print('Key discriminators (breakout vs reversal means):')
        for col in ['ce_wall_dist_pct', 'oi_at_sell_strike', 'oi_delta_30min', 'pcr_near']:
            bv = breakouts[col].mean()
            rv = reversals[col].mean()
            direction = '↑ breakout' if bv > rv else '↑ reversal'
            print(f'  {col:22s}  breakout={bv:8.1f}  reversal={rv:8.1f}  ({direction})')

    # ── save ──────────────────────────────────────────────────────────────────
    out_path = os.path.join(OUTPUT_DIR, 'athena_emer_oi_validation.csv')
    results.to_csv(out_path, index=False)
    print(f'\nFull results → {out_path}')


if __name__ == '__main__':
    main()
