"""
Branch 2 — Greek Profile (Artemis)

Track net position Greek *levels* (delta, gamma, theta, vega) at each bar
from entry to exit for each Artemis trade.

Branch 2 measures *exposure* — instantaneous sensitivity to market moves.
Distinct from Branch 1's P&L *contribution* (exposure × Δmarket).

Key questions (Artemis, single-expiry iron condor):
  1. Does short-gamma exposure intensify as DTE → 0 (near expiry)?
  2. Does short-vega exposure intensify as DTE → 0?
  3. Does net delta stay near zero, or does it drift after SL-triggered adjustments?
  4. How do entry Greeks differ between winning and losing trades?

Note: Nifty and Sensex results are reported separately — gamma, theta, and vega
scale with the absolute option-price level (~4.5× larger for Sensex). Blending
them would produce meaningless averages.

Base lots only. Status-gated: legs whose side is 'closed' are excluded from net
Greeks at that bar (the position truly has zero exposure on the closed side).

Reuses IV cache from Branch 1 (iv_cache_artemis/*.parquet). Fast.

Run from repo root:
  python research/greek_analysis/greek_profile/run_artemis.py
"""

import os
import sys
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from research.greek_analysis.greek_engine import get_dte_days, compute_greeks

_HERE     = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..', '..'))

NIFTY_SUMMARY   = os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_summary_nifty.csv')
SENSEX_SUMMARY  = os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_summary_sensex.csv')
NIFTY_LOGS_DIR  = os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_logs_nifty')
SENSEX_LOGS_DIR = os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_logs_sensex')
IV_CACHE_DIR    = os.path.join(REPO_ROOT, 'research', 'greek_analysis', 'data', 'iv_cache_artemis')

OUTPUT_DIR   = os.path.join(_HERE, 'data')
PARQUET_PATH = os.path.join(OUTPUT_DIR, 'greek_profiles_artemis.parquet')
SUMMARY_PATH = os.path.join(OUTPUT_DIR, 'greek_summary_artemis.csv')

# Same leg definition as Branch 1 run_artemis.py
# (iv_col, ltp_col, strike_col, opt_type, direction, status_col)
LEGS = [
    ('pe_sell_iv', 'pe_sell_ltp', 'pe_sell_strike', 'pe', -1, 'pe_status'),
    ('pe_buy_iv',  'pe_buy_ltp',  'pe_buy_strike',  'pe', +1, 'pe_status'),
    ('ce_sell_iv', 'ce_sell_ltp', 'ce_sell_strike', 'ce', -1, 'ce_status'),
    ('ce_buy_iv',  'ce_buy_ltp',  'ce_buy_strike',  'ce', +1, 'ce_status'),
]


def _load_summary(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=['entry_time', 'expiry'])
    df = df[df['week_outcome'] == 'traded'].reset_index(drop=True)
    df['trade_id']    = range(1, len(df) + 1)
    df['entry_date']  = df['entry_time'].dt.date.astype(str)
    df['expiry_date'] = df['expiry'].dt.date.astype(str)
    return df


def _load_log(trade_id: int, expiry_date: str, logs_dir: str) -> pd.DataFrame:
    path = os.path.join(logs_dir, f'trade_{trade_id:04d}_{expiry_date}.csv')
    return pd.read_csv(path, parse_dates=['time_stamp'])


def _load_iv(trade_id: int, expiry_date: str, instrument: str) -> pd.DataFrame:
    path = os.path.join(IV_CACHE_DIR,
                        f'trade_{trade_id:04d}_{expiry_date}_{instrument}.parquet')
    return pd.read_parquet(path)


def _bar_greeks(bar: pd.Series, iv_row: pd.Series, expiry_date: str) -> dict:
    """Net Greeks at a single bar across all active (non-closed) legs."""
    ts   = bar['time_stamp']
    spot = float(bar['spot'])
    dte  = get_dte_days(ts, expiry_date)

    net      = dict(delta=0.0, gamma=0.0, theta=0.0, vega=0.0)
    n_valid  = 0
    n_closed = 0

    for iv_col, ltp_col, strike_col, opt_type, direction, status_col in LEGS:
        if str(bar.get(status_col, '')) == 'closed':
            n_closed += 1
            continue

        iv_val = iv_row.get(iv_col)
        if pd.isna(iv_val):
            continue

        ltp = bar.get(ltp_col, 0.0)
        if pd.isna(ltp) or float(ltp) <= 0.0:
            continue

        strike = bar.get(strike_col, float('nan'))
        if pd.isna(strike):
            continue

        greeks = compute_greeks(float(iv_val), spot, float(strike), dte, opt_type)
        if greeks is None:
            continue
        if any(pd.isna(v) for v in greeks.values()):
            continue

        net['delta'] += direction * greeks['delta']
        net['gamma'] += direction * greeks['gamma']
        net['theta'] += direction * greeks['theta']
        net['vega']  += direction * greeks['vega']
        n_valid += 1

    return {
        'net_delta':     round(net['delta'], 5),
        'net_gamma':     round(net['gamma'], 7),
        'net_theta':     round(net['theta'], 5),
        'net_vega':      round(net['vega'],  5),
        'n_legs_valid':  n_valid,
        'n_legs_closed': n_closed,
    }


def _run_instrument(instrument: str, summary_path: str,
                    logs_dir: str) -> list[dict]:
    summary = _load_summary(summary_path)
    n_trades = len(summary)
    print(f"\n{instrument.upper()} — {n_trades} trades")

    records = []
    for _, row in summary.iterrows():
        trade_id    = int(row['trade_id'])
        expiry_date = str(row['expiry_date'])
        is_win      = bool(float(row['total_pl_points']) > 0)

        log   = _load_log(trade_id, expiry_date, logs_dir)
        iv_df = _load_iv(trade_id, expiry_date, instrument)
        n_bars = len(log)

        trade_start = len(records)
        for i in range(n_bars):
            bar    = log.iloc[i]
            iv_row = iv_df.iloc[i]
            ts     = bar['time_stamp']
            g      = _bar_greeks(bar, iv_row, expiry_date)

            records.append({
                'instrument':      instrument,
                'trade_id':        trade_id,
                'entry_date':      str(row['entry_date']),
                'entry_vix':       float(row['entry_vix']),
                'is_win':          is_win,
                'bar_num':         i,
                'normalized_time': round(i / (n_bars - 1), 5) if n_bars > 1 else 0.0,
                'time_stamp':      ts,
                'spot':            float(bar['spot']),
                'dte':             round(get_dte_days(ts, expiry_date), 3),
                **g,
            })

        eg = records[trade_start]
        print(f"  {trade_id:3d}/{n_trades}  {row['entry_date']}  "
              f"Δ={eg['net_delta']:+.3f}  "
              f"Γ={eg['net_gamma']:+.5f}  "
              f"θ={eg['net_theta']:+.3f}  "
              f"v={eg['net_vega']:+.3f}  "
              f"legs={eg['n_legs_valid']}")

    return records


def run():
    print("Greek Profile — Branch 2 (Artemis)")
    print(f"Output: {PARQUET_PATH}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    records  = _run_instrument('nifty',  NIFTY_SUMMARY,  NIFTY_LOGS_DIR)
    records += _run_instrument('sensex', SENSEX_SUMMARY, SENSEX_LOGS_DIR)

    df = pd.DataFrame(records)
    df.to_parquet(PARQUET_PATH, index=False)
    print(f"\nSaved {len(df):,} rows → {PARQUET_PATH}")
    print()
    _print_summary(df)


def _print_summary(df: pd.DataFrame) -> None:
    print("=" * 72)
    print("ARTEMIS GREEK PROFILE — BRANCH 2 SUMMARY")
    print("=" * 72)

    all_bucket_rows = []

    for instrument in ['nifty', 'sensex']:
        dfi = df[df['instrument'] == instrument].copy()
        if len(dfi) == 0:
            print(f"\n  {instrument.upper()} — no data")
            continue

        df4 = dfi[dfi['n_legs_valid'] == 4]
        n_total = len(dfi)
        n_full  = len(df4)
        print(f"\n  {instrument.upper()} — {n_total:,} bars, "
              f"{n_full:,} all-4-valid ({n_full/n_total*100:.1f}%)")

        entry = dfi[dfi['bar_num'] == 0]
        print(f"  Entry Greeks (bar_num=0, n={len(entry)}):")
        for col in ['net_delta', 'net_gamma', 'net_theta', 'net_vega']:
            m, s = entry[col].mean(), entry[col].std()
            print(f"    {col:>12s}: mean={m:+.5f}  std={s:.5f}")
        print()

        print("  Entry Greeks by outcome:")
        for label, grp in [('wins  ', entry[entry['is_win']]),
                            ('losses', entry[~entry['is_win']])]:
            print(f"    {label}  n={len(grp):3d}  "
                  f"Δ={grp['net_delta'].mean():+.4f}  "
                  f"v={grp['net_vega'].mean():+.4f}  "
                  f"Γ={grp['net_gamma'].mean():+.6f}  "
                  f"θ={grp['net_theta'].mean():+.4f}")
        print()

        # By normalized time
        df4 = df4.copy()
        df4['bucket'] = (df4['normalized_time'] * 10).astype(int).clip(upper=9)
        bkt = df4.groupby('bucket')[
            ['net_delta', 'net_gamma', 'net_theta', 'net_vega']].mean()

        print("  Mean net Greeks by normalized time (all-4-valid):")
        print(f"  {'time':>9}  {'Δ':>9}  {'Γ':>12}  {'θ':>9}  {'v':>9}")
        for b, br in bkt.iterrows():
            print(f"  {b*10:>3d}-{(b+1)*10:<3d}%  "
                  f"{br['net_delta']:>+9.4f}  "
                  f"{br['net_gamma']:>+12.7f}  "
                  f"{br['net_theta']:>+9.4f}  "
                  f"{br['net_vega']:>+9.4f}")
        print()

        # By DTE
        df4['dte_bucket'] = pd.cut(df4['dte'],
                                    bins=[0, 0.5, 1.0, 2.0, 3.0, 5.0, 1000],
                                    labels=['<0.5d', '0.5-1d', '1-2d', '2-3d', '3-5d', '>5d'])
        dte = df4.groupby('dte_bucket', observed=True)[
            ['net_delta', 'net_gamma', 'net_theta', 'net_vega']].agg(['mean', 'count'])

        print("  Mean net Greeks by DTE (short-gamma / short-vega intensification):")
        print(f"  {'dte':>8}  {'n':>6}  {'Δ':>9}  {'Γ':>12}  {'θ':>9}  {'v':>9}")
        for lbl, br in dte.iterrows():
            n = int(br['net_delta']['count'])
            print(f"  {lbl:>8}  {n:>6}  "
                  f"{br['net_delta']['mean']:>+9.4f}  "
                  f"{br['net_gamma']['mean']:>+12.7f}  "
                  f"{br['net_theta']['mean']:>+9.4f}  "
                  f"{br['net_vega']['mean']:>+9.4f}")
        print()

        for b, br in bkt.iterrows():
            all_bucket_rows.append({
                'instrument': instrument,
                'bucket':     b,
                **br.to_dict(),
            })

    if all_bucket_rows:
        pd.DataFrame(all_bucket_rows).to_csv(SUMMARY_PATH, index=False)
        print(f"Saved summary CSV → {SUMMARY_PATH}")


if __name__ == '__main__':
    run()
