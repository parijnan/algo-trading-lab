"""
Branch 2 — Greek Profile (Athena)

Track net position Greek *levels* (delta, gamma, theta, vega) at each bar
from entry to exit for each Athena trade.

Branch 2 measures *exposure* — instantaneous sensitivity to market moves.
This is distinct from Branch 1's P&L *contribution* (exposure × Δmarket).
A position can be net long-vega (exposure > 0) and still post a negative vega
P&L contribution if IV fell during the trade. The two are reconcilable:

  vega_contrib = Σ_t net_vega(t) × ΔIV_t  (per-leg ΔIV, not a single net ΔIV)

Key questions (Athena):
  1. Is Athena net long-vega at entry? (calendar design assumes this; wings may flip it)
  2. Does net delta stay near zero throughout, or does it drift?
  3. When does net gamma intensify — near sell-expiry (dte_sell→0) or mid-trade?
  4. How do entry Greeks differ between winning and losing trades?

Reuses IV cache from Branch 1 (iv_cache/*.parquet). Fast (~1-2 min warm).

Run from repo root:
  python research/greek_analysis/greek_profile/run.py
"""

import os
import sys
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from research.greek_analysis.greek_engine import (
    load_trade_summary, load_trade_log, build_bar_sequence,
    load_or_compute_iv, get_dte_days, compute_greeks,
    TRADE_LOGS_DIR, TRADE_SUMMARY_PATH, IV_CACHE_DIR,
)

_HERE        = os.path.dirname(__file__)
OUTPUT_DIR   = os.path.join(_HERE, 'data')
PARQUET_PATH = os.path.join(OUTPUT_DIR, 'greek_profiles_athena.parquet')
SUMMARY_PATH = os.path.join(OUTPUT_DIR, 'greek_summary_athena.csv')
ATTR_PATH    = os.path.join(_HERE, '..', 'pnl_attribution', 'data', 'pnl_attribution.csv')

# Same leg definition as Branch 1
LEGS = [
    ('ce_sell_iv', 'ce_sell_ltp', 'ce_sell_strike', 'sell_expiry', 'ce', -1, 'summary'),
    ('ce_buy_iv',  'ce_buy_ltp',  'ce_sell_strike', 'buy_expiry',  'ce', +1, 'summary'),
    ('pe_sell_iv', 'pe_sell_ltp', 'pe_sell_strike', 'sell_expiry', 'pe', -1, 'summary'),
    ('pe_buy_iv',  'pe_buy_ltp',  'pe_sell_strike', 'buy_expiry',  'pe', +1, 'summary'),
    ('ce_wing_iv', 'ce_wing_ltp', 'ce_wing_strike', 'buy_expiry',  'ce', +1, 'bar'),
    ('pe_wing_iv', 'pe_wing_ltp', 'pe_wing_strike', 'buy_expiry',  'pe', +1, 'bar'),
    ('emer_iv',    'emer_ltp',    'emer_strike',    'buy_expiry',  'ce', +1, 'bar'),
]


def _bar_greeks(bar: pd.Series, iv_row: pd.Series,
                row: pd.Series, sell_exp: str, buy_exp: str) -> dict:
    """Net Greeks at a single bar across all active legs."""
    ts   = bar['time_stamp']
    spot = float(bar['spot'])
    expiry_map = {'sell_expiry': sell_exp, 'buy_expiry': buy_exp}

    net    = dict(delta=0.0, gamma=0.0, theta=0.0, vega=0.0)
    n_main = 0
    n_opt  = 0

    for iv_col, ltp_col, strike_col, exp_key, opt_type, direction, strike_src in LEGS:
        iv_val = iv_row.get(iv_col)
        if pd.isna(iv_val):
            continue

        ltp = bar.get(ltp_col, 0.0)
        if pd.isna(ltp) or float(ltp) == 0.0:
            continue

        if strike_src == 'bar':
            strike = bar.get(strike_col, float('nan'))
            if pd.isna(strike):
                continue
            is_optional = True
        else:
            strike = float(row[strike_col])
            is_optional = False

        expiry = expiry_map[exp_key]
        dte    = get_dte_days(ts, expiry)

        greeks = compute_greeks(float(iv_val), spot, float(strike), dte, opt_type)
        if greeks is None:
            continue
        if any(pd.isna(v) for v in greeks.values()):
            continue

        net['delta'] += direction * greeks['delta']
        net['gamma'] += direction * greeks['gamma']
        net['theta'] += direction * greeks['theta']
        net['vega']  += direction * greeks['vega']

        if is_optional:
            n_opt += 1
        else:
            n_main += 1

    return {
        'net_delta':    round(net['delta'], 5),
        'net_gamma':    round(net['gamma'], 7),
        'net_theta':    round(net['theta'], 5),
        'net_vega':     round(net['vega'],  5),
        'n_main_valid': n_main,
        'n_opt_valid':  n_opt,
    }


def run():
    print("Greek Profile — Branch 2 (Athena)")
    print(f"Output: {PARQUET_PATH}")
    print()

    summary = load_trade_summary(TRADE_SUMMARY_PATH)
    print(f"Loaded {len(summary)} trades.")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    all_records = []
    for _, row in summary.iterrows():
        trade_id   = int(row['trade_id'])
        entry_date = str(row['entry_date'])
        is_win     = bool(row['total_pl_points'] > 0)

        log   = load_trade_log(trade_id, entry_date)
        bars  = build_bar_sequence(row, log)
        iv_df = load_or_compute_iv(trade_id, entry_date, bars, row)

        sell_exp = str(row['sell_expiry'])
        buy_exp  = str(row['buy_expiry'])
        n = len(bars)

        trade_start = len(all_records)
        for i in range(n):
            bar    = bars.iloc[i]
            iv_row = iv_df.iloc[i]
            ts     = bar['time_stamp']
            g      = _bar_greeks(bar, iv_row, row, sell_exp, buy_exp)

            all_records.append({
                'trade_id':        trade_id,
                'entry_date':      entry_date,
                'is_win':          is_win,
                'bar_num':         i,
                'normalized_time': round(i / (n - 1), 5) if n > 1 else 0.0,
                'time_stamp':      ts,
                'spot':            float(bar['spot']),
                'dte_sell':        round(get_dte_days(ts, sell_exp), 3),
                'dte_buy':         round(get_dte_days(ts, buy_exp),  3),
                **g,
            })

        eg = all_records[trade_start]
        print(f"  Trade {trade_id:3d}/{len(summary)}  {entry_date}  "
              f"Δ={eg['net_delta']:+.3f}  "
              f"Γ={eg['net_gamma']:+.5f}  "
              f"θ={eg['net_theta']:+.3f}  "
              f"v={eg['net_vega']:+.3f}  "
              f"main={eg['n_main_valid']}")

    df = pd.DataFrame(all_records)
    df.to_parquet(PARQUET_PATH, index=False)
    print(f"\nSaved {len(df):,} rows → {PARQUET_PATH}")
    print()
    _print_summary(df)
    _validate_vs_branch1(df)


def _print_summary(df: pd.DataFrame) -> None:
    df4 = df[df['n_main_valid'] == 4].copy()

    print("=" * 72)
    print("ATHENA GREEK PROFILE — BRANCH 2 SUMMARY")
    print("=" * 72)
    print(f"  Total bars: {len(df):,}  "
          f"All-4-main-valid: {len(df4):,} ({len(df4)/len(df)*100:.1f}%)")
    print()

    # Entry Greeks
    entry = df[df['bar_num'] == 0].copy()
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

    pct_lv = (df4['net_vega'] > 0).mean() * 100
    print(f"  Long-vega bars (net_vega > 0, all-4-valid): {pct_lv:.1f}%")
    print(f"  Mean net_vega across all bars (all-4-valid): {df4['net_vega'].mean():+.5f}")
    print()

    # By normalized time
    df4 = df4.copy()
    df4['bucket'] = (df4['normalized_time'] * 10).astype(int).clip(upper=9)
    bkt = df4.groupby('bucket')[['net_delta', 'net_gamma', 'net_theta', 'net_vega']].mean()
    print("  Mean net Greeks by normalized time (all-4-valid):")
    print(f"  {'time':>9}  {'Δ':>9}  {'Γ':>12}  {'θ':>9}  {'v':>9}")
    for b, br in bkt.iterrows():
        print(f"  {b*10:>3d}-{(b+1)*10:<3d}%  "
              f"{br['net_delta']:>+9.4f}  "
              f"{br['net_gamma']:>+12.7f}  "
              f"{br['net_theta']:>+9.4f}  "
              f"{br['net_vega']:>+9.4f}")
    print()

    # By dte_sell
    df4['dte_bucket'] = pd.cut(df4['dte_sell'],
                                bins=[0, 0.5, 1.0, 2.0, 3.0, 5.0, 1000],
                                labels=['<0.5d', '0.5-1d', '1-2d', '2-3d', '3-5d', '>5d'])
    dte = df4.groupby('dte_bucket', observed=True)[
        ['net_delta', 'net_gamma', 'net_theta', 'net_vega']].agg(['mean', 'count'])
    print("  Mean net Greeks by dte_sell (sell-expiry DTE):")
    print(f"  {'dte':>8}  {'n':>6}  {'Δ':>9}  {'Γ':>12}  {'θ':>9}  {'v':>9}")
    for lbl, br in dte.iterrows():
        n = int(br['net_delta']['count'])
        print(f"  {lbl:>8}  {n:>6}  "
              f"{br['net_delta']['mean']:>+9.4f}  "
              f"{br['net_gamma']['mean']:>+12.7f}  "
              f"{br['net_theta']['mean']:>+9.4f}  "
              f"{br['net_vega']['mean']:>+9.4f}")
    print()

    bkt.reset_index().to_csv(SUMMARY_PATH, index=False)
    print(f"Saved summary CSV → {SUMMARY_PATH}")


def _validate_vs_branch1(df: pd.DataFrame) -> None:
    """
    Cross-check: Σ_t net_greek(t) × Δmarket_t should match Branch 1 contributions.

    Valid for delta/gamma/theta (shared market variable). Vega is excluded
    (each leg has its own per-leg ΔIV, not a shared net ΔIV).

    Small diffs expected when a bar's IV is valid in Branch 2 but the bar-pair
    straddled an IV failure in Branch 1 (Branch 1 requires IV valid at *both*
    endpoints; Branch 2 checks validity at each bar independently). Diffs
    within 5 pts confirm sign/scale correctness.
    """
    if not os.path.exists(ATTR_PATH):
        print("\n[Validation skipped — Branch 1 output not found]")
        return

    b1 = pd.read_csv(ATTR_PATH)
    b1_map = {int(r['trade_id']): r for _, r in b1.iterrows()}

    print()
    print("=" * 72)
    print("CROSS-CHECK: reconstructed contributions vs Branch 1")
    print("  Σ net_greek(t)×Δmarket_t per trade (bars where n_main_valid==4)")
    print("  Vega excluded (per-leg ΔIV, not shared net ΔIV)")
    print("=" * 72)

    df_sorted = df.sort_values(['trade_id', 'bar_num']).reset_index(drop=True)
    max_diffs = dict(delta=0.0, gamma=0.0, theta=0.0)
    n_checked = 0

    for tid, tdf in df_sorted.groupby('trade_id'):
        if tid not in b1_map:
            continue
        tdf = tdf.reset_index(drop=True)
        b1r = b1_map[tid]

        d_recon = g_recon = t_recon = 0.0
        for i in range(len(tdf) - 1):
            r0 = tdf.iloc[i]
            r1 = tdf.iloc[i + 1]
            if r0['n_main_valid'] < 4:
                continue
            d_spot = float(r1['spot']) - float(r0['spot'])
            d_t    = (r1['time_stamp'] - r0['time_stamp']).total_seconds() / 86400.0
            d_recon += float(r0['net_delta']) * d_spot
            g_recon += 0.5 * float(r0['net_gamma']) * d_spot ** 2
            t_recon += float(r0['net_theta']) * d_t

        for key, recon, b1_val in [
            ('delta', d_recon, float(b1r['delta_contrib'])),
            ('gamma', g_recon, float(b1r['gamma_contrib'])),
            ('theta', t_recon, float(b1r['theta_contrib'])),
        ]:
            max_diffs[key] = max(max_diffs[key], abs(recon - b1_val))
        n_checked += 1

    print(f"  Trades checked: {n_checked}")
    for key, md in max_diffs.items():
        status = 'OK' if md < 5.0 else 'WARN'
        print(f"  [{status}] max |{key}_recon − {key}_contrib|: {md:.4f} pts")


if __name__ == '__main__':
    run()
