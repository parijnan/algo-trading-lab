"""
Branch 7 — Vega/Theta Exit Timing (Athena)

Central question: at what near-leg DTE does the realized vega loss overtake the
theta income for the Athena double calendar condor?

Method:
  For each bar interval [t, t+1] across all 124 Athena trades (~223K bars):
    1. Compute net position theta (Σ direction_i × theta_i) and per-leg vega from
       the IV cache using vectorized scipy/numpy Black-Scholes.
    2. Compute:
         theta_bar  = theta_net_rate × Δt     (theta P&L earned this bar)
         vega_bar   = Σ direction_i × vega_i × Δiv_i   (realized vega P&L, per-leg IVs)
         tv         = theta_bar + vega_bar    (combined theta+vega P&L per bar)
    3. Bucket by near-leg DTE (sell_expiry DTE at bar t).
    4. Report mean(theta_bar), mean(vega_bar), mean(tv) by DTE bucket.
       Crossover = DTE bucket where mean(tv) goes negative (vega dominates).

Note on vega sign: Athena is theoretically net-long-vega (calendar buys more vega
than it sells). But Branch 1 found realized vega = −14.4 pts/trade (net short).
The reason is term-structure slope: near-term IV spikes more than far-term IV on
vol-event days. Short near leg loses more than long far leg gains. vega_bar captures
this correctly because it uses per-leg IVs, not a uniform vol level.

A uniform-shock breakeven (θ/V ratio) is computed as a secondary column but is NOT
the primary headline — it assumes a uniform IV shift, which is sign-inverted relative
to the actual loss mechanism for a calendar spread.

Cross-check: Σ theta_bar and Σ vega_bar across all bars should match Branch 1 trade
totals (θ=+7068 pts, v=−1781 pts).

Implementation: vectorized scipy/numpy Black-Scholes. Matches mibian to <0.01%.
7 legs per trade: ce_sell/buy, pe_sell/buy (fixed strikes from summary), plus
ce_wing, pe_wing, emer (bar-level strikes). Two expiries: sell_expiry (near),
buy_expiry (far). Wing/emer IV columns are object dtype → pd.to_numeric coerce.

Run from repo root:
  python research/greek_analysis/exit_timing/run_athena.py

Outputs:
  exit_timing/data/exit_timing_bars_athena.parquet   (per-bar rows)
  exit_timing/data/exit_timing_summary_athena.csv    (DTE-bucketed aggregates)
  exit_timing/data/exit_timing_summary_athena_wl.csv (winner/loser split)
"""

import os
import sys
import math

import numpy as np
import pandas as pd
from scipy.stats import norm as scipy_norm

_HERE     = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..', '..'))
sys.path.insert(0, REPO_ROOT)

from research.greek_analysis.greek_engine import (
    load_trade_summary, load_trade_log, build_bar_sequence,
    load_or_compute_iv,
    TRADE_SUMMARY_PATH, TRADE_LOGS_DIR, IV_CACHE_DIR,
)

RISK_FREE = 0.05  # 5% annualised (decimal), matches greek_engine.RISK_FREE_RATE/100

OUTPUT_DIR    = os.path.join(_HERE, 'data')
OUTPUT_BARS   = os.path.join(OUTPUT_DIR, 'exit_timing_bars_athena.parquet')
OUTPUT_SUMM   = os.path.join(OUTPUT_DIR, 'exit_timing_summary_athena.csv')
OUTPUT_SUMM_WL = os.path.join(OUTPUT_DIR, 'exit_timing_summary_athena_wl.csv')

# 7 legs: (iv_col, ltp_col, strike_src, expiry_key, opt_type, direction)
# strike_src: trade-summary column name for fixed legs, bar column name for bar-level legs
# expiry_key: 'sell' = sell_expiry, 'buy' = buy_expiry
LEGS = [
    ('ce_sell_iv', 'ce_sell_ltp', 'ce_sell_strike', 'sell', 'ce', -1),
    ('ce_buy_iv',  'ce_buy_ltp',  'ce_sell_strike', 'buy',  'ce', +1),
    ('pe_sell_iv', 'pe_sell_ltp', 'pe_sell_strike', 'sell', 'pe', -1),
    ('pe_buy_iv',  'pe_buy_ltp',  'pe_sell_strike', 'buy',  'pe', +1),
    ('ce_wing_iv', 'ce_wing_ltp', 'ce_wing_strike', 'buy',  'ce', +1),
    ('pe_wing_iv', 'pe_wing_ltp', 'pe_wing_strike', 'buy',  'pe', +1),
    ('emer_iv',    'emer_ltp',    'emer_strike',     'buy',  'ce', +1),
]
SUMMARY_STRIKES = {'ce_sell_strike', 'pe_sell_strike'}

DTE_BUCKETS = [
    (0.0,  0.5,  '0.0–0.5d'),
    (0.5,  1.0,  '0.5–1.0d'),
    (1.0,  2.0,  '1.0–2.0d'),
    (2.0,  3.0,  '2.0–3.0d'),
    (3.0,  5.0,  '3.0–5.0d'),
    (5.0,  8.0,  '5.0–8.0d'),
    (8.0,  99.0, '8.0d+   '),
]


# ---------------------------------------------------------------------------
# Vectorized Black-Scholes theta and vega
# ---------------------------------------------------------------------------

def _bs_theta_vega(spots: np.ndarray, strikes: np.ndarray,
                   iv_pct: np.ndarray, dte_days: np.ndarray,
                   opt_type: str, active_mask: np.ndarray
                   ) -> tuple[np.ndarray, np.ndarray]:
    """
    Vectorized BS theta and vega for one leg across all bars.

    theta: pts/calendar-day (negative — option loses value), mibian convention
    vega:  pts per 1%-IV-point (positive), mibian convention

    Returns arrays of length len(spots); inactive bars → NaN.
    """
    n = len(spots)
    theta_arr = np.full(n, np.nan)
    vega_arr  = np.full(n, np.nan)

    if not active_mask.any():
        return theta_arr, vega_arr

    S     = spots[active_mask]
    K     = strikes[active_mask]
    sigma = iv_pct[active_mask] / 100.0
    T     = np.maximum(dte_days[active_mask] / 365.0, 1e-10)
    sqT   = np.sqrt(T)

    d1     = (np.log(S / K) + (RISK_FREE + 0.5 * sigma**2) * T) / (sigma * sqT)
    d2     = d1 - sigma * sqT
    phi_d1 = scipy_norm.pdf(d1)
    exp_rT = np.exp(-RISK_FREE * T)

    base = -S * phi_d1 * sigma / (2.0 * sqT)
    if opt_type == 'ce':
        theta_arr[active_mask] = (base - RISK_FREE * K * exp_rT * scipy_norm.cdf(d2)) / 365.0
    else:
        theta_arr[active_mask] = (base + RISK_FREE * K * exp_rT * scipy_norm.cdf(-d2)) / 365.0

    vega_arr[active_mask] = S * phi_d1 * sqT / 100.0

    return theta_arr, vega_arr


def _sanity_check():
    """Verify scipy theta and vega match mibian to <0.01%."""
    import mibian
    spot, strike, iv, dte = 12127.2, 12300.0, 12.68, 7.0
    m = mibian.BS([spot, strike, 5.0, dte], volatility=iv)

    sigma = iv / 100.0
    T = dte / 365.0
    sqT = math.sqrt(T)
    d1 = (math.log(spot / strike) + (RISK_FREE + 0.5 * sigma**2) * T) / (sigma * sqT)
    d2 = d1 - sigma * sqT
    phi_d1 = scipy_norm.pdf(d1)
    exp_rT = math.exp(-RISK_FREE * T)

    my_vega  = spot * phi_d1 * sqT / 100.0
    my_theta = (-spot * phi_d1 * sigma / (2 * sqT)
                - RISK_FREE * strike * exp_rT * scipy_norm.cdf(d2)) / 365.0

    v_err = abs(my_vega  - m.vega)      / abs(m.vega)      * 100
    t_err = abs(my_theta - m.callTheta) / abs(m.callTheta) * 100

    print(f"  Sanity check (scipy vs mibian):")
    print(f"    vega:  scipy={my_vega:.6f}  mibian={m.vega:.6f}  err={v_err:.5f}%")
    print(f"    theta: scipy={my_theta:.6f}  mibian={m.callTheta:.6f}  err={t_err:.5f}%")
    assert v_err < 0.01, f"Vega mismatch: {v_err:.5f}%"
    assert t_err < 0.01, f"Theta mismatch: {t_err:.5f}%"
    print("  PASSED\n")


# ---------------------------------------------------------------------------
# Per-trade processing
# ---------------------------------------------------------------------------

def _process_trade(row: pd.Series, bars: pd.DataFrame,
                   iv_df: pd.DataFrame, outcome: str) -> list[dict]:
    """Compute per-bar (theta_bar, vega_bar) for one Athena trade."""
    n = len(bars)
    sell_exp = str(row['sell_expiry'])
    buy_exp  = str(row['buy_expiry'])

    # Vectorized DTE computation
    ts_series = pd.to_datetime(bars['time_stamp'])
    sell_dt   = pd.Timestamp(f'{sell_exp} 15:30:00')
    buy_dt    = pd.Timestamp(f'{buy_exp} 15:30:00')
    dte_sell  = np.maximum((sell_dt - ts_series).dt.total_seconds().values / 86400.0, 0.0)
    dte_buy   = np.maximum((buy_dt  - ts_series).dt.total_seconds().values / 86400.0, 0.0)

    spot_arr = bars['spot'].values.astype(float)

    # Bar durations for each interval [t, t+1]
    dt_nanos = np.diff(ts_series.astype(np.int64).values)
    dt_days  = dt_nanos / (1e9 * 86400.0)  # (n-1,)

    # Accumulators (at bar t)
    theta_net_arr = np.zeros(n)   # net theta rate pts/day
    vega_net_arr  = np.zeros(n)   # net vega pts/vol-pt
    vega_bar_arr  = np.zeros(n - 1)  # realized vega P&L per interval
    iv_fail_arr   = np.zeros(n, dtype=bool)

    for iv_col, ltp_col, strike_src, expiry_key, opt_type, direction in LEGS:
        iv_arr  = pd.to_numeric(iv_df[iv_col].values,   errors='coerce')
        ltp_arr = pd.to_numeric(bars[ltp_col].values,   errors='coerce')

        if strike_src in SUMMARY_STRIKES:
            strike_arr = np.full(n, float(row[strike_src]))
        else:
            strike_arr = pd.to_numeric(bars[strike_src].values, errors='coerce')

        dte_arr = dte_sell if expiry_key == 'sell' else dte_buy

        # Active at bar t: ltp > 0, iv finite, strike > 0
        ltp_active = np.isfinite(ltp_arr) & (ltp_arr > 0.0)
        active_t   = ltp_active & np.isfinite(iv_arr) & np.isfinite(strike_arr) & (strike_arr > 0.0)

        # Track IV failures (leg active but IV missing)
        iv_fail_arr |= ltp_active & ~np.isfinite(iv_arr)

        theta_t, vega_t = _bs_theta_vega(spot_arr, strike_arr, iv_arr, dte_arr, opt_type, active_t)

        valid_t = active_t & np.isfinite(theta_t) & np.isfinite(vega_t)
        theta_net_arr[valid_t] += direction * theta_t[valid_t]
        vega_net_arr[valid_t]  += direction * vega_t[valid_t]

        # Realized vega P&L for interval [t, t+1]: needs iv at t (in vega_t) and t+1
        iv_next      = iv_arr[1:]
        interval_ok  = valid_t[:-1] & np.isfinite(iv_next)
        d_iv         = iv_next - iv_arr[:-1]
        vega_bar_arr[interval_ok] += (direction
                                      * vega_t[:-1][interval_ok]
                                      * d_iv[interval_ok])

    # Realized near-sell IV change (mean of ce_sell and pe_sell legs, signed)
    cs_iv = pd.to_numeric(iv_df['ce_sell_iv'].values, errors='coerce')
    ps_iv = pd.to_numeric(iv_df['pe_sell_iv'].values, errors='coerce')
    d_cs  = np.diff(cs_iv)
    d_ps  = np.diff(ps_iv)
    both  = np.isfinite(d_cs) & np.isfinite(d_ps)
    only_cs = np.isfinite(d_cs) & ~np.isfinite(d_ps)
    only_ps = ~np.isfinite(d_cs) & np.isfinite(d_ps)
    delta_iv_sell = np.full(n - 1, np.nan)
    delta_iv_sell[both]    = 0.5 * (d_cs[both] + d_ps[both])
    delta_iv_sell[only_cs] = d_cs[only_cs]
    delta_iv_sell[only_ps] = d_ps[only_ps]

    records = []
    for i in range(n - 1):
        theta_b = theta_net_arr[i] * dt_days[i]
        vega_b  = vega_bar_arr[i]
        vn      = vega_net_arr[i]
        # Secondary: breakeven IV (uniform rise that zeroes theta for this bar)
        # Valid only when theta_b > 0 and vega_net is meaningfully non-zero
        be_iv = np.nan
        if theta_b > 0 and abs(vn) > 1e-8:
            be_iv = theta_b / abs(vn)

        records.append({
            'trade_id':        int(row['trade_id']),
            'entry_date':      str(row['entry_date']),
            'entry_vix':       float(row['entry_vix']),
            'bar_ts':          bars.iloc[i]['time_stamp'],
            'dte_sell':        float(dte_sell[i]),
            'dte_buy':         float(dte_buy[i]),
            'dt_days':         float(dt_days[i]),
            'spot':            float(spot_arr[i]),
            'theta_bar':       float(theta_b),
            'vega_bar':        float(vega_b),
            'theta_plus_vega': float(theta_b + vega_b),
            'theta_net_rate':  float(theta_net_arr[i]),
            'vega_net':        float(vn),
            'breakeven_iv':    float(be_iv) if np.isfinite(be_iv) else np.nan,
            'delta_iv_sell':   float(delta_iv_sell[i]) if np.isfinite(delta_iv_sell[i]) else np.nan,
            'iv_fail':         bool(iv_fail_arr[i]),
            'outcome':         outcome,
        })

    return records


# ---------------------------------------------------------------------------
# DTE bucket summary
# ---------------------------------------------------------------------------

def _assign_bucket(dte: float) -> str:
    for lo, hi, label in DTE_BUCKETS:
        if lo <= dte < hi:
            return label
    return DTE_BUCKETS[-1][2]


def _build_summary(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Compute DTE-bucketed theta/vega statistics; return (overall, winner/loser)."""
    clean = df[~df['iv_fail']].copy()
    clean['dte_bucket'] = clean['dte_sell'].map(_assign_bucket)

    bucket_order = [b[2] for b in DTE_BUCKETS]
    rows, rows_wl = [], []

    for bucket in bucket_order:
        sub = clean[clean['dte_bucket'] == bucket]
        if len(sub) == 0:
            continue

        row = {
            'dte_bucket':          bucket,
            'n_bars':              len(sub),
            'theta_mean':          sub['theta_bar'].mean(),
            'vega_mean':           sub['vega_bar'].mean(),
            'tv_mean':             sub['theta_plus_vega'].mean(),
            'pct_tv_positive':     (sub['theta_plus_vega'] > 0).mean() * 100,
            'vega_net_mean':       sub['vega_net'].mean(),
            'breakeven_iv_mean':   sub['breakeven_iv'].mean(skipna=True),
            'delta_iv_sell_mean':  sub['delta_iv_sell'].mean(skipna=True),
            'delta_iv_sell_p50':   sub['delta_iv_sell'].median(skipna=True),
        }
        rows.append(row)

        for outcome in ['win', 'loss']:
            s = sub[sub['outcome'] == outcome]
            if len(s) == 0:
                continue
            rows_wl.append({
                'dte_bucket':        bucket,
                'outcome':           outcome,
                'n_bars':            len(s),
                'theta_mean':        s['theta_bar'].mean(),
                'vega_mean':         s['vega_bar'].mean(),
                'tv_mean':           s['theta_plus_vega'].mean(),
                'pct_tv_positive':   (s['theta_plus_vega'] > 0).mean() * 100,
                'delta_iv_sell':     s['delta_iv_sell'].mean(skipna=True),
            })

    return pd.DataFrame(rows), pd.DataFrame(rows_wl)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    print("Branch 7 — Vega/Theta Exit Timing (Athena)")
    print(f"Output bars: {OUTPUT_BARS}")
    print()

    _sanity_check()

    summary = load_trade_summary(TRADE_SUMMARY_PATH)
    print(f"Loaded {len(summary)} trades.\n")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    all_records = []

    for _, row in summary.iterrows():
        trade_id   = int(row['trade_id'])
        entry_date = str(row['entry_date'])
        pl         = float(row['total_pl_points'])
        outcome    = 'win' if pl > 0 else 'loss'

        print(f"  Trade {trade_id:3d}/{len(summary)}  {entry_date}  pl={pl:+.1f}",
              end='', flush=True)

        log  = load_trade_log(trade_id, entry_date)
        bars = build_bar_sequence(row, log)
        iv_df = load_or_compute_iv(trade_id, entry_date, bars, row)

        records = _process_trade(row, bars, iv_df, outcome)
        all_records.extend(records)
        print(f"  +{len(records)} bars")

    bars_df = pd.DataFrame(all_records)
    bars_df.to_parquet(OUTPUT_BARS, index=False)
    print(f"\nSaved {len(bars_df):,} bar-records to {OUTPUT_BARS}")

    # ------------------------------------------------------------------
    # Branch 1 cross-check
    # ------------------------------------------------------------------
    total_theta = bars_df['theta_bar'].sum()
    total_vega  = bars_df['vega_bar'].sum()
    print(f"\n--- Branch 1 cross-check ---")
    print(f"  Σ theta_bar = {total_theta:+.0f} pts  (Branch 1 reference: +7068 pts)")
    print(f"  Σ vega_bar  = {total_vega:+.0f} pts  (Branch 1 reference: −1781 pts)")
    theta_ok = abs(total_theta - 7068) / 7068 < 0.10
    vega_ok  = abs(total_vega  - (-1781)) / 1781 < 0.15
    print(f"  theta within 10%: {'OK' if theta_ok else 'WARN'}")
    print(f"  vega  within 15%: {'OK' if vega_ok  else 'WARN'}")

    # ------------------------------------------------------------------
    # DTE bucket analysis
    # ------------------------------------------------------------------
    summ_df, wl_df = _build_summary(bars_df)

    print(f"\n--- DTE bucket summary (near-leg DTE, iv_fail bars excluded) ---")
    pd.set_option('display.width', 120)
    pd.set_option('display.float_format', '{:.3f}'.format)
    print(summ_df.to_string(index=False))

    print(f"\n--- Winner/Loser split by DTE ---")
    print(wl_df.to_string(index=False))

    summ_df.to_csv(OUTPUT_SUMM, index=False)
    wl_df.to_csv(OUTPUT_SUMM_WL, index=False)
    print(f"\nSaved summary  → {OUTPUT_SUMM}")
    print(f"Saved wl-split → {OUTPUT_SUMM_WL}")

    # ------------------------------------------------------------------
    # Signal summary
    # ------------------------------------------------------------------
    print(f"\n--- Crossover check ---")
    for _, r in summ_df.iterrows():
        sign = '<<< NEGATIVE (vega > theta)' if r['tv_mean'] < 0 else ''
        print(f"  {r['dte_bucket']}  tv_mean={r['tv_mean']:+.4f}  "
              f"θ={r['theta_mean']:+.4f}  v={r['vega_mean']:+.4f}  "
              f"%tv+={r['pct_tv_positive']:.1f}%  {sign}")


if __name__ == '__main__':
    run()
