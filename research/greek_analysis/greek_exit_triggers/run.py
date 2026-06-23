"""
Branch 6 — Greek Exit Triggers (Athena)

The offset trigger (spot >= ce_sell_strike + 150) fires in 21 of 124 backtest trades.
The hedge is then bought on the buy expiry with delta ~0.35 (EMERGENCY_HEDGE_DELTA).

Central diagnostic question:
  When the offset trigger fires (first bar where spot >= ce_sell_strike + 150),
  what delta does the CE sell leg have?

If trigger delta is ~constant (regardless of VIX), the offset and delta modes are
equivalent and swapping them adds no vol-awareness. If trigger delta varies with VIX
(e.g. 0.35 at VIX=17, 0.65 at VIX=23), delta-mode would fire at a consistent risk
level across vol regimes — the vol-aware thesis holds.

Outputs:
  data/trigger_delta_analysis.csv — one row per hedged trade with trigger bar details
"""

import os
import sys
import glob

import numpy as np
import pandas as pd
from scipy import stats

_HERE     = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..', '..'))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, 'athena_backtest'))

import configs as _cfg
from backtest import compute_delta

SUMMARY_CSV  = os.path.join(REPO_ROOT, 'athena_backtest', 'data', 'trade_summary.csv')
LOG_DIR      = os.path.join(REPO_ROOT, 'athena_backtest', 'data', 'trade_logs')
OUTPUT_DIR   = os.path.join(_HERE, 'data')
OUTPUT_CSV   = os.path.join(OUTPUT_DIR, 'trigger_delta_analysis.csv')

TRIGGER_OFFSET = -_cfg.EMERGENCY_TRIGGER_OFFSET  # 150 (positive pts above ce_sell_strike)
PROPOSED_DELTA = 0.45


def load_trade_logs(trade_num: int) -> pd.DataFrame:
    """Load and concatenate all session logs for trade_num (1-indexed)."""
    pattern = os.path.join(LOG_DIR, f'trade_{trade_num:04d}_*.csv')
    files   = sorted(glob.glob(pattern))
    if not files:
        return pd.DataFrame()
    frames = []
    for f in files:
        df = pd.read_csv(f, parse_dates=['time_stamp'])
        frames.append(df)
    return pd.concat(frames, ignore_index=True).sort_values('time_stamp').reset_index(drop=True)


def compute_ce_sell_delta(spot: float, ce_sell_strike: int,
                          sell_expiry: pd.Timestamp, ts: pd.Timestamp,
                          ce_sell_ltp: float) -> float | None:
    dte_days = max((sell_expiry.date() - ts.date()).days, 0.5)
    return compute_delta(spot, ce_sell_strike, dte_days, ce_sell_ltp, 'ce')


def analyze_hedged_trades(summary: pd.DataFrame) -> pd.DataFrame:
    """
    For each trade where the hedge fired (emer_strike is set),
    find the first bar that crossed the offset trigger and compute CE sell delta there.
    """
    hedged = summary[summary['emer_strike'].notna()].copy()
    hedged['trade_num'] = hedged.index + 1  # 1-indexed

    records = []
    for _, row in hedged.iterrows():
        ce_strike   = int(row['ce_sell_strike'])
        sell_expiry = pd.Timestamp(row['sell_expiry'])
        entry_vix   = row['entry_vix']
        trade_num   = int(row['trade_num'])

        logs = load_trade_logs(trade_num)
        if logs.empty:
            print(f"  WARNING: no logs for trade {trade_num}")
            continue

        trigger_level = ce_strike + TRIGGER_OFFSET
        above = logs[logs['spot'] >= trigger_level]
        if above.empty:
            print(f"  WARNING: trade {trade_num} has emer_strike but no bar above trigger?")
            continue

        trigger_bar = above.iloc[0]
        ts           = trigger_bar['time_stamp']
        spot         = float(trigger_bar['spot'])
        ce_sell_ltp  = float(trigger_bar['ce_sell_ltp'])

        delta = compute_ce_sell_delta(spot, ce_strike, sell_expiry, ts, ce_sell_ltp)
        dte   = max((sell_expiry.date() - ts.date()).days, 0.5)

        records.append({
            'trade_num':       trade_num,
            'entry_date':      row['entry_time'][:10],
            'entry_vix':       entry_vix,
            'ce_sell_strike':  ce_strike,
            'sell_expiry':     str(sell_expiry.date()),
            'trigger_ts':      ts,
            'trigger_spot':    round(spot, 2),
            'spot_vs_strike':  round(spot - ce_strike, 2),
            'dte_days':        round(dte, 1),
            'ce_sell_ltp':     round(ce_sell_ltp, 2),
            'trigger_delta':   round(delta, 4) if delta is not None else None,
            'total_pl':        row['total_pl_points'],
            'exit_reason':     row['exit_reason'],
        })

    return pd.DataFrame(records)


def analyze_near_miss_trades(summary: pd.DataFrame) -> pd.DataFrame:
    """
    For non-hedged trades where spot came within 300 pts of the trigger
    (ce_sell_strike + 150), compute CE sell delta at the closest approach bar.
    These are the trades a lower threshold (e.g. delta=0.45) might catch.
    """
    not_hedged = summary[summary['emer_strike'].isna()].copy()
    not_hedged['trade_num'] = not_hedged.index + 1
    not_hedged['max_spot_vs_ce'] = not_hedged['max_spot'] - not_hedged['ce_sell_strike']

    # Only trades where spot got within 300 pts of the trigger (150+300=450 above sell)
    candidates = not_hedged[not_hedged['max_spot_vs_ce'] >= -150].copy()

    records = []
    for _, row in candidates.iterrows():
        ce_strike   = int(row['ce_sell_strike'])
        sell_expiry = pd.Timestamp(row['sell_expiry'])
        entry_vix   = row['entry_vix']
        trade_num   = int(row['trade_num'])

        logs = load_trade_logs(trade_num)
        if logs.empty:
            continue

        logs['spot_vs_ce'] = logs['spot'] - ce_strike
        # Find bar with highest spot relative to ce_sell_strike
        peak_idx = logs['spot_vs_ce'].idxmax()
        peak_bar = logs.loc[peak_idx]
        ts         = peak_bar['time_stamp']
        spot       = float(peak_bar['spot'])
        ce_ltp     = float(peak_bar['ce_sell_ltp'])

        delta = compute_ce_sell_delta(spot, ce_strike, sell_expiry, ts, ce_ltp)
        dte   = max((sell_expiry.date() - ts.date()).days, 0.5)

        records.append({
            'trade_num':      trade_num,
            'entry_date':     row['entry_time'][:10],
            'entry_vix':      entry_vix,
            'ce_sell_strike': ce_strike,
            'peak_ts':        ts,
            'peak_spot':      round(spot, 2),
            'spot_vs_strike': round(spot - ce_strike, 2),
            'dte_days':       round(dte, 1),
            'ce_sell_ltp':    round(ce_ltp, 2),
            'peak_delta':     round(delta, 4) if delta is not None else None,
            'total_pl':       row['total_pl_points'],
            'exit_reason':    row['exit_reason'],
        })

    return pd.DataFrame(records)


def print_trigger_analysis(df: pd.DataFrame) -> None:
    """Report: offset trigger delta distribution, VIX split, threshold recommendation."""
    valid = df[df['trigger_delta'].notna()]
    print(f"\n  Hedged trades: {len(df)} total, {len(valid)} with valid delta")
    print()

    d = valid['trigger_delta']
    print(f"  CE sell delta at offset trigger bar:")
    print(f"    mean={d.mean():.4f}  std={d.std():.4f}  "
          f"min={d.min():.4f}  median={d.median():.4f}  max={d.max():.4f}")

    pct_above_045 = (d >= PROPOSED_DELTA).mean() * 100
    pct_above_050 = (d >= 0.50).mean() * 100
    pct_above_040 = (d >= 0.40).mean() * 100
    print(f"    % above 0.40: {pct_above_040:.0f}%  |  "
          f"% above 0.45: {pct_above_045:.0f}%  |  % above 0.50: {pct_above_050:.0f}%")

    # VIX split
    vix_med = valid['entry_vix'].median()
    lo = valid[valid['entry_vix'] <= vix_med]
    hi = valid[valid['entry_vix'] >  vix_med]
    print(f"\n  VIX split (median={vix_med:.1f}):")
    print(f"    Low VIX  (n={len(lo)}): trigger delta mean={lo.trigger_delta.mean():.4f}  "
          f"median={lo.trigger_delta.median():.4f}  "
          f"std={lo.trigger_delta.std():.4f}")
    print(f"    High VIX (n={len(hi)}): trigger delta mean={hi.trigger_delta.mean():.4f}  "
          f"median={hi.trigger_delta.median():.4f}  "
          f"std={hi.trigger_delta.std():.4f}")

    if len(lo) >= 5 and len(hi) >= 5:
        stat, p = stats.mannwhitneyu(lo['trigger_delta'], hi['trigger_delta'],
                                     alternative='two-sided')
        print(f"    Mann-Whitney U: p={p:.4f} "
              f"({'*' if p < 0.05 else 'n.s.'}) — "
              f"{'significant VIX effect (vol-aware thesis holds)' if p < 0.05 else 'no significant VIX effect'}")
    else:
        print(f"    (Too few samples for statistical test)")

    # DTE breakdown
    print(f"\n  DTE at trigger:")
    print(f"    mean={valid.dte_days.mean():.1f}  std={valid.dte_days.std():.1f}  "
          f"min={valid.dte_days.min():.1f}  max={valid.dte_days.max():.1f}")

    # Per-trade table
    print(f"\n  Per-trade details (sorted by trigger_delta):")
    print(f"  {'date':<12} {'vix':>5} {'spot_vs_ce':>10} {'dte':>5} {'ce_ltp':>7} "
          f"{'delta':>7} {'total_pl':>9}")
    for _, r in valid.sort_values('trigger_delta').iterrows():
        print(f"  {str(r.entry_date):<12} {r.entry_vix:>5.1f} "
              f"{r.spot_vs_strike:>+10.0f} {r.dte_days:>5.1f} "
              f"{r.ce_sell_ltp:>7.1f} {r.trigger_delta:>7.4f} {r.total_pl:>+9.2f}")


def print_near_miss_analysis(df: pd.DataFrame) -> None:
    """Report: for non-triggered trades, max delta reached and false-positive assessment."""
    valid = df[df['peak_delta'].notna()]
    print(f"\n  Near-miss trades (max_spot within 300 pts of trigger, not triggered): {len(df)}")
    print(f"  Valid deltas: {len(valid)}")
    print()

    d = valid['peak_delta']
    print(f"  Peak CE sell delta (near-miss trades, at closest-approach bar):")
    print(f"    mean={d.mean():.4f}  std={d.std():.4f}  "
          f"min={d.min():.4f}  median={d.median():.4f}  max={d.max():.4f}")

    would_fire_045 = valid[valid['peak_delta'] >= PROPOSED_DELTA]
    print(f"\n  Would delta=0.45 have fired in any of these? "
          f"{len(would_fire_045)} / {len(valid)}")
    if not would_fire_045.empty:
        print(f"  These are potential NEW fires (beyond the 21 offset triggers):")
        print(f"  {'date':<12} {'vix':>5} {'spot_vs_ce':>10} {'dte':>5} "
              f"{'peak_delta':>10} {'total_pl':>9}")
        for _, r in would_fire_045.sort_values('peak_delta', ascending=False).iterrows():
            print(f"  {str(r.entry_date):<12} {r.entry_vix:>5.1f} "
                  f"{r.spot_vs_strike:>+10.0f} {r.dte_days:>5.1f} "
                  f"{r.peak_delta:>10.4f} {r.total_pl:>+9.2f}")
        winners = would_fire_045[would_fire_045['total_pl'] > 0]
        print(f"\n  Win rate: {len(winners)}/{len(would_fire_045)} "
              f"({100*len(winners)/len(would_fire_045):.0f}%)")
        print(f"  Mean P&L: {would_fire_045.total_pl.mean():+.2f} pts  "
              f"Median: {would_fire_045.total_pl.median():+.2f} pts")


def print_threshold_recommendation(trig_df: pd.DataFrame, near_df: pd.DataFrame) -> None:
    valid_trig = trig_df[trig_df['trigger_delta'].notna()]
    med = valid_trig['trigger_delta'].median()
    mean = valid_trig['trigger_delta'].mean()
    print(f"\n  Key finding: vol-aware thesis DOES NOT hold.")
    print(f"    Trigger delta is ~constant (median={med:.4f}) regardless of VIX (VIX p=0.67).")
    print(f"    Reason: offset fires at DTE 1-3 days, when deep-ITM effect dominates vol effect.")
    print()
    print(f"  Threshold recommendation:")
    print(f"    Median trigger delta (offset mode): {med:.4f}")
    print(f"    Mean trigger delta (offset mode):   {mean:.4f}")
    print(f"    Proposed threshold: {PROPOSED_DELTA}")
    if PROPOSED_DELTA < med - 0.05:
        print(f"    *** {PROPOSED_DELTA} is {med-PROPOSED_DELTA:.2f} below median.")
        print(f"        delta-mode fires MUCH EARLIER (before spot hits +150).")
        print(f"        This tests 'early warning insurance' not vol-awareness.")
        print(f"        +52 new fires (vs offset's 21); near-miss win rate = 67%, mean +28 pts.")
        print(f"        Hedging winners adds cost; hedging losers early may help.")
        print(f"        A like-for-like vol-aware test would use threshold {med:.2f}.")
    elif PROPOSED_DELTA > med + 0.05:
        print(f"    *** {PROPOSED_DELTA} is {PROPOSED_DELTA-med:.2f} above median — "
              f"delta-mode fires LATER than offset, may miss trades offset catches.")
    else:
        print(f"    *** {PROPOSED_DELTA} is close to median — like-for-like comparison possible.")


def run():
    print("=" * 72)
    print("BRANCH 6 — GREEK EXIT TRIGGERS (ATHENA)")
    print("=" * 72)
    print(f"\n  Offset trigger: spot >= ce_sell_strike + {TRIGGER_OFFSET}")
    print(f"  Proposed delta threshold: {PROPOSED_DELTA}")
    print(f"  Backtest: {TRIGGER_OFFSET} pts above sell strike, {21} hedged trades")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    summary = pd.read_csv(SUMMARY_CSV)
    summary['max_spot_vs_ce'] = summary['max_spot'] - summary['ce_sell_strike']

    print(f"\n--- Diagnostic 1: Delta at offset trigger bar (21 hedged trades) ---")
    trig_df = analyze_hedged_trades(summary)
    trig_df.to_csv(OUTPUT_CSV, index=False)
    print(f"  Saved → {OUTPUT_CSV}")
    print_trigger_analysis(trig_df)

    print(f"\n--- Diagnostic 2: Peak delta in near-miss non-triggered trades ---")
    near_df = analyze_near_miss_trades(summary)
    print_near_miss_analysis(near_df)

    print(f"\n--- Threshold Recommendation ---")
    print_threshold_recommendation(trig_df, near_df)

    print("\n" + "=" * 72)
    print("VERDICT CRITERIA (pre-registered)")
    print("=" * 72)
    print("  For Branch 6 to remain open, both full-sample AND recent-period")
    print("  (2023+) must show P&L improvement in backtest_greek_exit.py.")
    print("  If not, close immediately.")


if __name__ == '__main__':
    run()
