"""
analyze_vix_vega.py — VIX-bleed as a source of Athena P&L loss

Advisor-mandated gating test before designing any VIX-based intervention:
  Does early-window VIX drop predict *subsequent* P&L decline?

Three questions:
  1. Loss decomposition: how much Athena loss is VIX-bleed vs. spot-breach?
  2. Gating test (trade-level, n=124): VIX_change_day1 → PL_day2plus
  3. Vega sensitivity: OLS ΔPL ~ Δspot + ΔVIX on calm (pre_expiry) trades

Usage:
    python athena_backtest/analyze_vix_vega.py
"""

import os
import glob
import numpy as np
import pandas as pd
from scipy import stats

_DIR     = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(_DIR, 'data', 'trade_logs')
SUMMARY  = os.path.join(_DIR, 'data', 'trade_summary.csv')
LOT_SIZE = 65
BARS_PER_SESSION = 325   # 10:30–15:30


def load_trade_log(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=['time_stamp'])
    df = df.replace('', np.nan)
    for col in ['vix', 'spot', 'combined_unrealised_pl', 'cumulative_pl',
                'ce_unrealised_pl', 'pe_unrealised_pl', 'running_realised_pl']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    df['vix'] = df['vix'].ffill()
    return df


def find_log(log_files: list, entry_date) -> str | None:
    date_str = str(entry_date)
    matches  = [f for f in log_files if date_str in os.path.basename(f)]
    return matches[0] if matches else None


def _interpret(r: float, p: float):
    sig      = "significant" if p < 0.05 else "NOT significant"
    strength = "strong" if abs(r) > 0.5 else "moderate" if abs(r) > 0.3 else "weak"
    direction = ("negative (early VIX drop → worse rest-of-trade P&L)"
                 if r < 0 else "positive")
    print(f"    {strength} {direction} — {sig} (p={p:.4f})")


def main():
    summary = pd.read_csv(SUMMARY, parse_dates=['entry_time', 'exit_time'])
    summary['entry_date'] = summary['entry_time'].dt.date
    log_files = sorted(glob.glob(os.path.join(LOGS_DIR, 'trade_*.csv')))

    total   = len(summary)
    winners = summary[summary['total_pl_points'] > 0]
    losers  = summary[summary['total_pl_points'] <= 0]

    print("=" * 64)
    print("ATHENA VIX-VEGA ANALYSIS")
    print("=" * 64)
    print(f"Trades: {total}  |  "
          f"Winners: {len(winners)} ({len(winners)/total*100:.1f}%)  |  "
          f"Losers:  {len(losers)}  ({len(losers)/total*100:.1f}%)")
    print(f"Total P&L: ₹{summary['total_pl_rupees'].sum():+,.0f}")

    # ------------------------------------------------------------------
    # 1. Loss decomposition
    # ------------------------------------------------------------------
    total_loss_rs = losers['total_pl_rupees'].sum()
    print(f"\n{'─'*64}")
    print(f"1. LOSS DECOMPOSITION   (total loser P&L: ₹{total_loss_rs:+,.0f})")
    print(f"{'─'*64}")

    for reason in ['pre_expiry', 'trail_stop', 'profit_target',
                   'spread_sl', 'option_sl', 'index_sl']:
        grp = losers[losers['exit_reason'] == reason]
        if grp.empty:
            continue
        rs   = grp['total_pl_rupees'].sum()
        pct  = rs / total_loss_rs * 100 if total_loss_rs != 0 else 0
        print(f"  {reason:<20}  {len(grp):>3} trades  "
              f"₹{rs:>+10,.0f}  ({pct:.0f}% of losses)")

    pre_exp_losers = losers[losers['exit_reason'] == 'pre_expiry']
    if not pre_exp_losers.empty:
        vix_fell = pre_exp_losers[pre_exp_losers['min_vix'] < pre_exp_losers['entry_vix']]
        print(f"\n  Of {len(pre_exp_losers)} pre_expiry losers:")
        print(f"    VIX fell below entry  : {len(vix_fell)}")
        print(f"    VIX stayed above entry: {len(pre_exp_losers) - len(vix_fell)}")
        if not vix_fell.empty:
            avg_drop = (vix_fell['min_vix'] - vix_fell['entry_vix']).mean()
            avg_pl   = vix_fell['total_pl_points'].mean()
            print(f"    Avg VIX drop (entry→min): {avg_drop:+.2f} pts")
            print(f"    Avg P&L in that group   : {avg_pl:+.2f} pts  "
                  f"(₹{avg_pl * LOT_SIZE:+,.0f})")

    # ------------------------------------------------------------------
    # 2. Gating test: VIX_change_day1 → PL_day2plus   (trade-level)
    # ------------------------------------------------------------------
    print(f"\n{'─'*64}")
    print(f"2. GATING TEST (trade-level, n={total})")
    print(f"   x = VIX change over entry day  (VIX_end_day1 − entry_VIX)")
    print(f"   y = P&L change after day 1     (PL_final − PL_end_day1)")
    print(f"{'─'*64}")

    rows = []
    for _, row in summary.iterrows():
        path = find_log(log_files, row['entry_date'])
        if path is None:
            continue
        try:
            log = load_trade_log(path)
        except Exception:
            continue
        if log.empty:
            continue

        day1_date = log['time_stamp'].dt.date.iloc[0]
        day1      = log[log['time_stamp'].dt.date == day1_date]
        if day1.empty:
            continue

        vix_d1  = day1['vix'].dropna()
        cpl_d1  = day1['cumulative_pl'].dropna()
        if vix_d1.empty:
            continue

        vix_end_day1 = float(vix_d1.iloc[-1])
        pl_end_day1  = float(cpl_d1.iloc[-1]) if not cpl_d1.empty else 0.0

        rows.append({
            'entry_date':      row['entry_date'],
            'exit_reason':     row['exit_reason'],
            'entry_vix':       row['entry_vix'],
            'vix_end_day1':    vix_end_day1,
            'vix_change_day1': vix_end_day1 - row['entry_vix'],
            'pl_end_day1':     pl_end_day1,
            'pl_final':        row['total_pl_points'],
            'pl_day2plus':     row['total_pl_points'] - pl_end_day1,
            'total_pl_rs':     row['total_pl_rupees'],
            'min_vix':         row['min_vix'],
            'max_vix':         row['max_vix'],
        })

    gdf = pd.DataFrame(rows)

    if len(gdf) < 10:
        print("  Insufficient matched trades for gating test.")
    else:
        r_all, p_all = stats.pearsonr(gdf['vix_change_day1'], gdf['pl_day2plus'])
        print(f"\n  All trades  n={len(gdf)}: r={r_all:+.3f}  p={p_all:.4f}")
        _interpret(r_all, p_all)

        gdf_pe = gdf[gdf['exit_reason'] == 'pre_expiry']
        if len(gdf_pe) >= 5:
            r_pe, p_pe = stats.pearsonr(gdf_pe['vix_change_day1'], gdf_pe['pl_day2plus'])
            print(f"\n  Pre-expiry only  n={len(gdf_pe)}: r={r_pe:+.3f}  p={p_pe:.4f}")
            _interpret(r_pe, p_pe)

        # Correlation: full-trade VIX drop (entry→min) vs final P&L
        r_full, p_full = stats.pearsonr(
            gdf['min_vix'] - gdf['entry_vix'],
            gdf['pl_final'])
        print(f"\n  Full-trade VIX drop (entry→min) vs final P&L  n={len(gdf)}:")
        print(f"    r={r_full:+.3f}  p={p_full:.4f}")
        _interpret(r_full, p_full)

        # Bucket analysis
        print(f"\n  Day-1 VIX change buckets vs final outcome:")
        gdf['bucket'] = pd.cut(
            gdf['vix_change_day1'],
            bins=[-99, -2, -0.5, 0.5, 2, 99],
            labels=['< -2', '-2 to -0.5', '-0.5 to +0.5', '+0.5 to +2', '> +2'])
        print(f"  {'ΔVIX_day1':>14}  {'n':>4}  {'WR%':>6}  "
              f"{'avg_pl_pts':>11}  {'avg_pl_rs':>10}")
        for bucket, grp in gdf.groupby('bucket', observed=True):
            wr  = (grp['pl_final'] > 0).mean() * 100
            apl = grp['pl_final'].mean()
            ars = grp['total_pl_rs'].mean()
            print(f"  {str(bucket):>14}  {len(grp):>4}  {wr:>6.1f}  "
                  f"{apl:>+11.2f}  ₹{ars:>+9,.0f}")

    # ------------------------------------------------------------------
    # 3. Vega sensitivity — bar-level OLS on pre_expiry trades
    # ------------------------------------------------------------------
    print(f"\n{'─'*64}")
    print(f"3. VEGA SENSITIVITY — OLS on pre_expiry trades (bar-level)")
    print(f"   ΔPL_bar ≈ α + β_spot·Δspot + β_vix·ΔVIX")
    print(f"   Note: bars within a trade are autocorrelated — R² is informational,")
    print(f"   not a significance measure. Trade-level n is the honest sample size.")
    print(f"{'─'*64}")

    dspot_all, dvix_all, dpl_all = [], [], []

    pe_trades = summary[summary['exit_reason'] == 'pre_expiry']
    for _, row in pe_trades.iterrows():
        path = find_log(log_files, row['entry_date'])
        if path is None:
            continue
        try:
            log = load_trade_log(path)
        except Exception:
            continue
        if len(log) < 3:
            continue

        spot = log['spot'].to_numpy(dtype=float)
        vix  = log['vix'].to_numpy(dtype=float)
        # Use combined_unrealised_pl (avoids CE-chute realised jumps)
        pl   = log['combined_unrealised_pl'].to_numpy(dtype=float)

        ds = np.diff(spot)
        dv = np.diff(vix)
        dp = np.diff(pl)

        mask = np.isfinite(ds) & np.isfinite(dv) & np.isfinite(dp)
        # Exclude event bars (CE-chute firing etc.) where |ΔPL| > 15 pts in 1 bar
        mask &= np.abs(dp) <= 15
        dspot_all.extend(ds[mask])
        dvix_all.extend(dv[mask])
        dpl_all.extend(dp[mask])

    if len(dspot_all) > 100:
        X  = np.column_stack([np.ones(len(dspot_all)), dspot_all, dvix_all])
        y  = np.array(dpl_all)
        co, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        yh = X @ co
        r2 = 1 - np.sum((y - yh) ** 2) / np.sum((y - y.mean()) ** 2)

        alpha, b_spot, b_vix = co
        print(f"\n  Bars: {len(y):,}  (from {len(pe_trades)} pre_expiry trades)")
        print(f"  α  (intercept / theta bleed) = {alpha:+.4f} pts/bar")
        print(f"  β_spot (delta sensitivity)   = {b_spot:+.5f} pts per spot pt")
        print(f"  β_vix  (vega sensitivity)    = {b_vix:+.4f} pts per VIX pt")
        print(f"  R²                           = {r2:.4f}")

        day_vix_drop_1pt = b_vix * BARS_PER_SESSION
        print(f"\n  Extrapolation (1 sustained VIX-pt drop over 1 full session):")
        print(f"    ≈ {day_vix_drop_1pt:+.1f} pts  →  ₹{day_vix_drop_1pt * LOT_SIZE:+,.0f} on 1 lot")

        # CE vs PE vega split
        dce_all, dpe_all = [], []
        for _, row in pe_trades.iterrows():
            path = find_log(log_files, row['entry_date'])
            if path is None:
                continue
            try:
                log = load_trade_log(path)
            except Exception:
                continue
            if len(log) < 3:
                continue
            ce = log['ce_unrealised_pl'].to_numpy(dtype=float)
            pe = log['pe_unrealised_pl'].to_numpy(dtype=float)
            vv = log['vix'].to_numpy(dtype=float)
            dv = np.diff(vv)
            dce = np.diff(ce)
            dpe = np.diff(pe)
            mask = np.isfinite(dv) & np.isfinite(dce) & np.isfinite(dpe)
            mask &= np.abs(dce) <= 15
            mask &= np.abs(dpe) <= 15
            dce_all.extend(dce[mask])
            dpe_all.extend(dpe[mask])

        if len(dce_all) > 100:
            dv_arr = np.array(dvix_all[:len(dce_all)])
            r_ce, _ = stats.pearsonr(dv_arr, dce_all[:len(dv_arr)])
            r_pe, _ = stats.pearsonr(dv_arr, dpe_all[:len(dv_arr)])
            print(f"\n  VIX ↔ CE-side P&L correlation (per bar): r={r_ce:+.3f}")
            print(f"  VIX ↔ PE-side P&L correlation (per bar): r={r_pe:+.3f}")
            print(f"  (Both should be positive — calendar is long vega on both sides)")

    print(f"\n{'─'*64}")
    print("DONE")
    print(f"{'─'*64}")


if __name__ == "__main__":
    main()
