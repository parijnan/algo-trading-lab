"""
Branch 6 — Greek Exit Triggers (Artemis)

Artemis index_sl fires before spot reaches the sell strike:
  CE: index_sl = ce_sell_strike - offset; fires when spot > index_sl
  PE: index_sl = pe_sell_strike + offset; fires when spot < index_sl

  At trigger, the sell leg is OTM by `offset` pts (50 for Nifty, 200 for Sensex).
  This is near-ATM exposure, unlike Athena's hedge which fired deep ITM (delta=0.77).

Central diagnostic question:
  When the offset trigger fires, what delta does the sell leg have?
  Does that delta vary with VIX (vol-aware justification for a delta threshold)?

If delta is consistent across VIX (std < 0.08, Mann-Whitney p > 0.05):
  → Offset already fires at consistent exposure → delta mode is redundant → close.
If delta varies with VIX:
  → Recommend a centered threshold and proceed to backtest.

Data:
  artemis_backtest/data/trade_summary_{nifty,sensex}.csv
  artemis_backtest/data/trade_logs_{nifty,sensex}/

Outputs:
  data/trigger_delta_artemis.csv — one row per index_sl event with trigger bar details
"""

import os
import sys
import glob

import numpy as np
import pandas as pd
from scipy import stats

_HERE     = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..', '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'research', 'greek_analysis'))

from greek_engine import compute_iv, compute_greeks

OUTPUT_DIR = os.path.join(_HERE, 'data')
OUTPUT_CSV = os.path.join(OUTPUT_DIR, 'trigger_delta_artemis.csv')

RISK_FREE_RATE = 5.0

# (instrument, sl_offset) — matches artemis_backtest/configs.py
INSTRUMENTS = [
    ('nifty',   50),
    ('sensex', 200),
]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _log_dir(instrument: str) -> str:
    return os.path.join(REPO_ROOT, 'artemis_backtest', 'data', f'trade_logs_{instrument}')


def _summary_path(instrument: str) -> str:
    return os.path.join(REPO_ROOT, 'artemis_backtest', 'data', f'trade_summary_{instrument}.csv')


def load_trade_log(instrument: str, expiry_date: str) -> pd.DataFrame:
    """Load trade log by expiry date. Returns empty DataFrame if not found."""
    pattern = os.path.join(_log_dir(instrument), f'trade_*_{expiry_date}.csv')
    files = sorted(glob.glob(pattern))
    if not files:
        return pd.DataFrame()
    return pd.read_csv(files[0], parse_dates=['time_stamp'])


# ---------------------------------------------------------------------------
# Trigger bar detection
# ---------------------------------------------------------------------------

def find_trigger_bar(log: pd.DataFrame, side: str, exit_time: pd.Timestamp) -> pd.Series | None:
    """
    Return the trigger bar for an index_sl exit.

    The trigger bar is the last bar where the side is in any non-closed state,
    at or just before the recorded exit_time. This handles re-entered positions:
    CE status can be 'active_additional' after a roll, and PE can be
    'adjusted_additional' — neither is 'active' but both are live.
    """
    status_col = f'{side}_status'
    if status_col not in log.columns:
        return None

    # Last bar before or at exit_time where the side has NOT yet closed
    pre_exit = log[log['time_stamp'] <= exit_time]
    not_closed = pre_exit[pre_exit[status_col] != 'closed']
    if not_closed.empty:
        return None
    return not_closed.iloc[-1]


# ---------------------------------------------------------------------------
# Delta computation at trigger bar
# ---------------------------------------------------------------------------

def compute_sell_delta(bar: pd.Series, side: str,
                       expiry_dt: pd.Timestamp) -> float | None:
    """
    Compute sell leg delta at the trigger bar.

    Returns the raw mibian delta:
      CE: positive (0 → 1)
      PE: negative (-1 → 0)
    """
    spot       = float(bar['spot'])
    strike     = float(bar[f'{side}_sell_strike'])
    sell_ltp   = float(bar[f'{side}_sell_ltp'])
    ts         = bar['time_stamp']

    dte_days = max((expiry_dt - ts).total_seconds() / 86400.0, 0.0)

    iv = compute_iv(sell_ltp, spot, strike, dte_days, side)
    if iv is None:
        return None

    greeks = compute_greeks(iv, spot, strike, dte_days, side)
    if greeks is None:
        return None
    return greeks['delta']


# ---------------------------------------------------------------------------
# Main diagnostic
# ---------------------------------------------------------------------------

def run_diagnostic() -> pd.DataFrame:
    records = []

    for instrument, sl_offset in INSTRUMENTS:
        summary_path = _summary_path(instrument)
        if not os.path.exists(summary_path):
            print(f'[WARN] Summary not found: {summary_path}')
            continue

        summary = pd.read_csv(summary_path,
                               parse_dates=['entry_time', 'pe_exit_time', 'ce_exit_time'])

        for _, row in summary.iterrows():
            expiry_dt = pd.Timestamp(str(row['expiry'])[:10]) + pd.Timedelta(hours=15, minutes=30)
            expiry_date = str(row['expiry'])[:10]
            entry_vix   = float(row['entry_vix']) if pd.notna(row['entry_vix']) else None

            for side in ('pe', 'ce'):
                exit_reason = row.get(f'{side}_exit_reason')
                exit_time   = row.get(f'{side}_exit_time')
                if exit_reason != 'index_sl' or pd.isna(exit_time):
                    continue

                log = load_trade_log(instrument, expiry_date)
                if log.empty:
                    print(f'[WARN] No log for {instrument} expiry {expiry_date}')
                    continue

                bar = find_trigger_bar(log, side, exit_time)
                if bar is None:
                    print(f'[WARN] No trigger bar for {instrument} {expiry_date} {side}')
                    continue

                delta = compute_sell_delta(bar, side, expiry_dt)
                if delta is None:
                    print(f'[WARN] Delta None for {instrument} {expiry_date} {side}')
                    continue

                sell_strike = float(bar[f'{side}_sell_strike'])
                spot        = float(bar['spot'])
                dte_days    = max((expiry_dt - bar['time_stamp']).total_seconds() / 86400.0, 0.0)
                otm_pts     = (sell_strike - spot) if side == 'ce' else (spot - sell_strike)

                records.append({
                    'instrument':  instrument,
                    'expiry_date': expiry_date,
                    'side':        side,
                    'entry_vix':   entry_vix,
                    'trigger_ts':  bar['time_stamp'],
                    'spot':        spot,
                    'sell_strike': sell_strike,
                    'otm_pts':     otm_pts,
                    'sell_ltp':    float(bar[f'{side}_sell_ltp']),
                    'dte_days':    round(dte_days, 2),
                    'delta_raw':   round(delta, 4),
                    'abs_delta':   round(abs(delta), 4),
                })

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Analysis and reporting
# ---------------------------------------------------------------------------

def vix_band(vix: float) -> str:
    if vix < 12:  return 'vix_lt12'
    if vix < 14:  return 'vix_12_14'
    if vix < 16:  return 'vix_14_16'
    return 'vix_gte16'


def print_report(df: pd.DataFrame):
    sep = '─' * 80

    print()
    print('═' * 80)
    print('ARTEMIS BRANCH 6 DIAGNOSTIC — Sell Leg Delta at index_sl Trigger')
    print('═' * 80)
    print(f'Total index_sl events: {len(df)}'
          f'  (CE: {(df["side"]=="ce").sum()}, PE: {(df["side"]=="pe").sum()})')
    print()

    # ---- Per-instrument summary ----
    for inst in df['instrument'].unique():
        sub = df[df['instrument'] == inst]
        print(f'  {inst.upper()}  (n={len(sub)}, offset={'50' if inst=="nifty" else "200"} pts)')
        print(f'    |delta|  mean={sub["abs_delta"].mean():.3f}  '
              f'std={sub["abs_delta"].std():.3f}  '
              f'median={sub["abs_delta"].median():.3f}  '
              f'[{sub["abs_delta"].min():.3f}, {sub["abs_delta"].max():.3f}]')
        print(f'    OTM pts  mean={sub["otm_pts"].mean():.1f}  '
              f'std={sub["otm_pts"].std():.1f}  '
              f'[{sub["otm_pts"].min():.1f}, {sub["otm_pts"].max():.1f}]')
        print(f'    DTE      mean={sub["dte_days"].mean():.1f}  '
              f'[{sub["dte_days"].min():.1f}, {sub["dte_days"].max():.1f}]')

    print()
    print(sep)

    # ---- Pooled delta distribution ----
    print()
    print('POOLED |delta| AT TRIGGER (all instruments, CE + PE)')
    ad = df['abs_delta']
    print(f'  n={len(ad)}  mean={ad.mean():.3f}  std={ad.std():.3f}  '
          f'median={ad.median():.3f}')

    pct = np.percentile(ad, [10, 25, 50, 75, 90])
    print(f'  Percentiles: p10={pct[0]:.3f}  p25={pct[1]:.3f}  p50={pct[2]:.3f}  '
          f'p75={pct[3]:.3f}  p90={pct[4]:.3f}')

    # ---- DTE distribution ----
    print()
    print('DTE AT TRIGGER (calendar days):')
    dte = df['dte_days']
    for lo, hi in [(0, 1), (1, 2), (2, 3), (3, 5), (5, 99)]:
        n = ((dte >= lo) & (dte < hi)).sum()
        label = f'{lo}–{hi}d' if hi < 99 else f'{lo}d+'
        print(f'  {label:8s}: {n:3d}  ({100*n/len(df):.0f}%)')

    # ---- VIX band split ----
    print()
    print('VIX BAND ANALYSIS:')
    df['vix_band'] = df['entry_vix'].apply(vix_band)
    band_stats = []
    for band in ['vix_lt12', 'vix_12_14', 'vix_14_16', 'vix_gte16']:
        sub = df[df['vix_band'] == band]['abs_delta']
        if len(sub) == 0:
            continue
        band_stats.append((band, sub))
        print(f'  {band:12s}  n={len(sub):3d}  '
              f'mean={sub.mean():.3f}  std={sub.std():.3f}  '
              f'median={sub.median():.3f}  '
              f'[{sub.min():.3f}, {sub.max():.3f}]')

    # Mann-Whitney U: low VIX (< 14) vs high VIX (14–16)
    low_vix  = df[df['entry_vix'] < 14.0]['abs_delta']
    high_vix = df[df['entry_vix'] >= 14.0]['abs_delta']
    if len(low_vix) >= 5 and len(high_vix) >= 5:
        u_stat, p_val = stats.mannwhitneyu(low_vix, high_vix, alternative='two-sided')
        print()
        print(f'Mann-Whitney U (VIX<14 vs VIX>=14):')
        print(f'  n_low={len(low_vix)}  mean={low_vix.mean():.3f}')
        print(f'  n_hi ={len(high_vix)}  mean={high_vix.mean():.3f}')
        print(f'  U={u_stat:.0f}  p={p_val:.4f}  {"** SIGNIFICANT" if p_val < 0.05 else "n.s."}')
    else:
        print()
        print(f'  VIX split: low n={len(low_vix)}, high n={len(high_vix)} — sample too small for M-W')
        p_val = 1.0

    # ---- CE vs PE comparison ----
    print()
    print('CE vs PE SPLIT:')
    for side in ('ce', 'pe'):
        sub = df[df['side'] == side]
        if sub.empty:
            continue
        print(f'  {side.upper()}  n={len(sub):3d}  '
              f'|delta| mean={sub["abs_delta"].mean():.3f}  '
              f'std={sub["abs_delta"].std():.3f}  '
              f'median={sub["abs_delta"].median():.3f}')

    # ---- Verdict ----
    print()
    print('VERDICT')
    print(sep)
    overall_std = df['abs_delta'].std()
    low_mean  = low_vix.mean()  if len(low_vix) >= 1  else float('nan')
    high_mean = high_vix.mean() if len(high_vix) >= 1 else float('nan')

    print(f'  Overall |delta| std: {overall_std:.3f}')
    print(f'  VIX effect (M-W p): {p_val:.4f}')
    print()

    threshold = round(df['abs_delta'].median(), 2)

    if overall_std < 0.08 and p_val > 0.05:
        print(f'  [CLOSE] Delta is CONSISTENT across VIX regimes (std={overall_std:.3f}, p={p_val:.4f}).')
        print(f'  The fixed offset already fires at a stable delta level (~{threshold:.2f}).')
        print(f'  A delta threshold would be REDUNDANT — no vol-awareness gain.')
        print(f'  Artemis Branch 6 CLOSED — close condition triggered.')
    elif p_val < 0.05:
        print(f'  [PROCEED] Delta varies significantly with VIX (p={p_val:.4f}).')
        print(f'  Recommended threshold: {threshold:.2f} (median; centers on current typical exposure).')
        print(f'  Proceed to artemis_backtest/backtest_greek_exit.py with SELL_DELTA_THRESHOLD={threshold}.')
    else:
        print(f'  [BORDERLINE] Delta std={overall_std:.3f} (> 0.08 threshold) but VIX effect n.s. (p={p_val:.4f}).')
        print(f'  Variation likely noise, not VIX-driven. Recommended CLOSE — marginal case.')
        print(f'  If proceeding: use threshold={threshold:.2f}.')

    print()
    print(f'Output saved to: {OUTPUT_CSV}')


if __name__ == '__main__':
    print('=== Artemis Branch 6 Diagnostic: Sell Leg Delta at index_sl Trigger ===')
    print()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    df = run_diagnostic()

    if df.empty:
        print('[ERROR] No index_sl events found — check data paths.')
        sys.exit(1)

    df.to_csv(OUTPUT_CSV, index=False)
    print(f'Trigger delta analysis: {len(df)} events saved.')
    print_report(df)
