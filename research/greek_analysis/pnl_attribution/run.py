"""
Branch 1 — P&L Attribution

Decompose each Athena trade's realized P&L into:
  delta_contrib:  Δspot × net_delta
  gamma_contrib:  ½ × Δspot² × net_gamma
  theta_contrib:  Δt_days × net_theta
  vega_contrib:   ΔIV_leg × net_vega  (per-leg IV, summed across legs)
  residual:       actual_bar_pnl − (delta + gamma + theta + vega)

All quantities in index points (not rupees). Multiply by LOT_SIZE=65 for rupees.

Run from repo root:
  python research/greek_analysis/pnl_attribution/run.py

Cold run (~15–30 min, computes IV for all bars via mibian and caches to parquet).
Warm run (<1 min, loads IV from parquet cache).
"""

import os
import sys
import math
import numpy as np
import pandas as pd

# Allow running from repo root or from this file's directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from research.greek_analysis.greek_engine import (
    LOT_SIZE, RISK_FREE_RATE,
    load_trade_summary, load_trade_log, build_bar_sequence,
    load_or_compute_iv, get_dte_days, compute_greeks,
    TRADE_LOGS_DIR, TRADE_SUMMARY_PATH, IV_CACHE_DIR,
)

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'data')
OUTPUT_PATH = os.path.join(OUTPUT_DIR, 'pnl_attribution.csv')

# Each leg: (iv_col, ltp_col, strike_col, expiry_key, option_type, direction, strike_source)
#   strike_source='summary': strike from trade summary row (constant, keyed by summary column name)
#   strike_source='bar':     strike from trade log bar (column in per-bar DataFrame; NaN when leg inactive)
LEGS = [
    ('ce_sell_iv', 'ce_sell_ltp', 'ce_sell_strike', 'sell_expiry', 'ce', -1, 'summary'),
    ('ce_buy_iv',  'ce_buy_ltp',  'ce_sell_strike', 'buy_expiry',  'ce', +1, 'summary'),
    ('pe_sell_iv', 'pe_sell_ltp', 'pe_sell_strike', 'sell_expiry', 'pe', -1, 'summary'),
    ('pe_buy_iv',  'pe_buy_ltp',  'pe_sell_strike', 'buy_expiry',  'pe', +1, 'summary'),
    ('ce_wing_iv', 'ce_wing_ltp', 'ce_wing_strike', 'buy_expiry',  'ce', +1, 'bar'),
    ('pe_wing_iv', 'pe_wing_ltp', 'pe_wing_strike', 'buy_expiry',  'pe', +1, 'bar'),
    ('emer_iv',    'emer_ltp',    'emer_strike',    'buy_expiry',  'ce', +1, 'bar'),
]


def _attribute_trade(bars: pd.DataFrame, iv_df: pd.DataFrame,
                     row: pd.Series) -> dict:
    """
    Compute full P&L attribution for a single trade.

    bars:   bar sequence including synthetic bar0 (N rows)
    iv_df:  per-bar IV DataFrame, same length as bars
    row:    trade_summary row for this trade

    Returns dict with:
      delta_contrib, gamma_contrib, theta_contrib, vega_contrib
      actual_mtm, residual, iv_fail_bars, n_bars
    """
    sell_exp = str(row['sell_expiry'])
    buy_exp  = str(row['buy_expiry'])
    expiry_map = {'sell_expiry': sell_exp, 'buy_expiry': buy_exp}

    totals = dict(delta=0.0, gamma=0.0, theta=0.0, vega=0.0,
                  actual=0.0, iv_fails=0)

    n = len(bars)
    for i in range(n - 1):
        bar_t  = bars.iloc[i]
        bar_t1 = bars.iloc[i + 1]
        iv_t   = iv_df.iloc[i]
        iv_t1  = iv_df.iloc[i + 1]

        ts_t  = bar_t['time_stamp']
        ts_t1 = bar_t1['time_stamp']

        spot_t  = float(bar_t['spot'])
        spot_t1 = float(bar_t1['spot'])

        d_spot = spot_t1 - spot_t
        d_t    = (ts_t1 - ts_t).total_seconds() / 86400.0  # calendar days

        for iv_col, ltp_col, strike_col, exp_key, opt_type, direction, strike_src in LEGS:
            ltp_t  = bar_t.get(ltp_col,  0.0)
            ltp_t1 = bar_t1.get(ltp_col, 0.0)

            # Only attribute for bars where this leg is active in BOTH endpoints
            ltp_t_val  = float(ltp_t)  if pd.notna(ltp_t)  else 0.0
            ltp_t1_val = float(ltp_t1) if pd.notna(ltp_t1) else 0.0
            if ltp_t_val <= 0.0 or ltp_t1_val <= 0.0:
                continue

            iv_t_val  = iv_t.get(iv_col)
            iv_t1_val = iv_t1.get(iv_col)

            # Get strike (bar-level legs: emer_strike, ce_wing_strike, pe_wing_strike)
            if strike_src == 'bar':
                strike = bar_t.get(strike_col, float('nan'))
                if pd.isna(strike):
                    continue
                strike = float(strike)
            else:
                strike = float(row[strike_col])

            expiry = expiry_map[exp_key]
            dte_t = get_dte_days(ts_t, expiry)

            # Actual MtM contribution (in points, sign-adjusted for direction)
            actual_leg = direction * (ltp_t1_val - ltp_t_val)
            totals['actual'] += actual_leg

            # Greek attribution requires IV at both endpoints
            if iv_t_val is None or iv_t1_val is None:
                totals['iv_fails'] += 1
                # Residual = actual (no Greek attribution, so it all goes to residual)
                continue

            greeks = compute_greeks(iv_t_val, spot_t, strike, dte_t, opt_type)
            if greeks is None:
                totals['iv_fails'] += 1
                continue

            d_iv = iv_t1_val - iv_t_val  # vol-points

            d_contrib = direction * greeks['delta'] * d_spot
            g_contrib = direction * 0.5 * greeks['gamma'] * d_spot ** 2
            t_contrib = direction * greeks['theta'] * d_t
            v_contrib = direction * greeks['vega']  * d_iv

            totals['delta'] += d_contrib
            totals['gamma'] += g_contrib
            totals['theta'] += t_contrib
            totals['vega']  += v_contrib

    delta_c = totals['delta']
    gamma_c = totals['gamma']
    theta_c = totals['theta']
    vega_c  = totals['vega']
    actual  = totals['actual']
    resid   = actual - (delta_c + gamma_c + theta_c + vega_c)

    return {
        'delta_contrib': delta_c,
        'gamma_contrib': gamma_c,
        'theta_contrib': theta_c,
        'vega_contrib':  vega_c,
        'actual_mtm':    actual,
        'residual':      resid,
        'iv_fail_bars':  totals['iv_fails'],
        'n_bars':        n - 1,
    }


def _reconcile(row: pd.Series, attr: dict) -> None:
    """
    Warn if actual MtM from LTP changes diverges materially from trade summary total.

    Sources of divergence (expected, not bugs):
      - Wing/emer entry price slippage: first tracked LTP ≠ actual fill price
      - Exit execution: last tracked LTP vs actual fill (usually exact in backtest)
    Tolerance of 3.0 pts covers rounding and 1-bar entry slippage.
    """
    total_pl   = float(row['total_pl_points'])
    actual_mtm = attr['actual_mtm']
    diff = actual_mtm - total_pl
    if abs(diff) > 3.0:
        print(f"  [WARN] MtM recon: actual_mtm={actual_mtm:.2f} vs total_pl={total_pl:.2f} "
              f"(diff={diff:+.2f} pts — expected for wing/emer entry slippage)")


def run():
    print("P&L Attribution — Branch 1")
    print(f"Output: {OUTPUT_PATH}")
    print()

    summary = load_trade_summary(TRADE_SUMMARY_PATH)
    print(f"Loaded {len(summary)} trades from summary.")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(IV_CACHE_DIR, exist_ok=True)

    records = []
    for _, row in summary.iterrows():
        trade_id   = int(row['trade_id'])
        entry_date = str(row['entry_date'])

        print(f"  Trade {trade_id:3d}/{len(summary)}  {entry_date}  "
              f"entry={row['entry_spot']:.0f}", end='', flush=True)

        log  = load_trade_log(trade_id, entry_date)
        bars = build_bar_sequence(row, log)

        # Load cached IV or compute fresh
        iv_df = load_or_compute_iv(trade_id, entry_date, bars, row)

        attr = _attribute_trade(bars, iv_df, row)
        _reconcile(row, attr)

        # pct_unexplained: |residual| / |actual_mtm| — 0% means fully explained.
        # Using unexplained (not explained) because the Greek components often have
        # large cancelling values, making "explained" misleadingly > 100%.
        pct_unexplained = (abs(attr['residual']) / abs(attr['actual_mtm']) * 100
                           if attr['actual_mtm'] != 0 else float('nan'))

        records.append({
            'trade_id':      trade_id,
            'entry_date':    entry_date,
            'entry_spot':    row['entry_spot'],
            'exit_reason':   row['exit_reason'],
            'total_pl_points': row['total_pl_points'],
            'actual_mtm':    round(attr['actual_mtm'],    2),
            'delta_contrib': round(attr['delta_contrib'],  2),
            'gamma_contrib': round(attr['gamma_contrib'],  2),
            'theta_contrib': round(attr['theta_contrib'],  2),
            'vega_contrib':  round(attr['vega_contrib'],   2),
            'residual':      round(attr['residual'],        2),
            'pct_unexplained': round(pct_unexplained,         1),
            'iv_fail_bars':  attr['iv_fail_bars'],
            'n_bars':        attr['n_bars'],
        })

        print(f"  total={row['total_pl_points']:+.1f}  "
              f"θ={attr['theta_contrib']:+.1f}  v={attr['vega_contrib']:+.1f}  "
              f"Δ={attr['delta_contrib']:+.1f}  Γ={attr['gamma_contrib']:+.1f}  "
              f"resid={attr['residual']:+.1f}  ({pct_unexplained:.0f}% unexp)")

    df_out = pd.DataFrame(records)
    df_out.to_csv(OUTPUT_PATH, index=False)
    print()
    print(f"Saved {len(df_out)} rows → {OUTPUT_PATH}")
    print()
    _print_summary(df_out)


def _print_summary(df: pd.DataFrame) -> None:
    n = len(df)
    print("=" * 60)
    print("AGGREGATE ATTRIBUTION SUMMARY")
    print("=" * 60)

    for col in ['delta_contrib', 'gamma_contrib', 'theta_contrib', 'vega_contrib', 'residual']:
        total = df[col].sum()
        mean  = df[col].mean()
        pct   = total / df['actual_mtm'].sum() * 100 if df['actual_mtm'].sum() != 0 else float('nan')
        print(f"  {col:>18s}:  sum={total:+8.1f}  mean={mean:+7.2f}  {pct:+5.1f}% of total MtM")

    print()
    print(f"  Total actual MtM: {df['actual_mtm'].sum():+.1f} pts  "
          f"({df['actual_mtm'].sum() * LOT_SIZE:+,.0f} INR)")
    print(f"  Total P&L (summary): {df['total_pl_points'].sum():+.1f} pts")
    print(f"  IV fail bars: {df['iv_fail_bars'].sum()} / {df['n_bars'].sum()} "
          f"({df['iv_fail_bars'].sum() / df['n_bars'].sum() * 100:.1f}%)")
    pct_unexp = df['pct_unexplained'].mean()
    print(f"  Mean %unexplained (|residual| / |actual|): {pct_unexp:.1f}%")

    print()
    print("  By exit reason:")
    for reason, grp in df.groupby('exit_reason'):
        print(f"    {reason:20s}  n={len(grp):3d}  "
              f"θ={grp['theta_contrib'].mean():+.1f}  "
              f"v={grp['vega_contrib'].mean():+.1f}  "
              f"Δ={grp['delta_contrib'].mean():+.1f}")

    print()
    print("  Winning vs losing trades:")
    wins  = df[df['total_pl_points'] > 0]
    loses = df[df['total_pl_points'] <= 0]
    for label, grp in [('wins ', wins), ('losses', loses)]:
        if len(grp) == 0:
            continue
        print(f"    {label}  n={len(grp):3d}  "
              f"θ={grp['theta_contrib'].mean():+.1f}  "
              f"v={grp['vega_contrib'].mean():+.1f}  "
              f"Δ={grp['delta_contrib'].mean():+.1f}  "
              f"Γ={grp['gamma_contrib'].mean():+.1f}  "
              f"resid={grp['residual'].mean():+.1f}")

    print()
    print("  Period split (2020–2022 vs 2023+):")
    df_dated = df.copy()
    df_dated['year'] = pd.to_datetime(df_dated['entry_date']).dt.year
    early = df_dated[df_dated['year'] <= 2022]
    late  = df_dated[df_dated['year'] >= 2023]
    for label, grp in [('2020-2022', early), ('2023+    ', late)]:
        if len(grp) == 0:
            continue
        print(f"    {label}  n={len(grp):3d}  "
              f"θ={grp['theta_contrib'].mean():+.1f}  "
              f"v={grp['vega_contrib'].mean():+.1f}  "
              f"Δ={grp['delta_contrib'].mean():+.1f}  "
              f"resid={grp['residual'].mean():+.1f}")


if __name__ == '__main__':
    run()
