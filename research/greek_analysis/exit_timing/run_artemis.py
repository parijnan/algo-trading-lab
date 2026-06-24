"""
Branch 7 — Gamma/Theta Exit Timing (Artemis: CLOSED 2026-06-24 | Athena: pending)

Artemis: no gamma/theta crossover at any DTE (0–4d). Breakeven spot move
√(2·θ_net·Δt/|γ_net|) is LARGEST at expiry (11.2 pts, 0–0.5d) because theta
(∝1/√T) accelerates faster than gamma near expiry. Median realized 1-min move ≈ 3 pts at
all DTEs. %realized>breakeven peaks 37.9% at DTE 2–2.5d, never ≥50%. Post-CE-roll PE
surviving leg: 42% — nearest crossover, still theta-positive. Losses are delta-driven
(Branch 1). No DTE-based exit rule supported. CLOSE.

Central question: at what DTE does net gamma drag overwhelm net theta harvest
for the full Artemis iron condor position?

Method:
  For each bar in each of the 173 Artemis trades (232,595 bars total):
    1. Net position Greeks (4 legs: pe_sell, pe_buy, ce_sell, ce_buy)
       using per-bar strikes from the trade log and IV from the warm cache.
       Direction: sell = −1 (short), buy = +1 (long).
    2. Compute:
         theta_net  = Σ direction_i × theta_i        (pts/calendar-day, net positive)
         gamma_net  = Σ direction_i × gamma_i        (per-pt², net negative)
         breakeven  = √(2 × theta_net × dt / |gamma_net|)
                      = spot move at which gamma P&L exactly offsets theta P&L for one bar
    3. Record per-bar: DTE, theta_net, gamma_net, breakeven, realized |Δspot|,
       adjustment state (pre/post roll on either side).

Additional analysis — surviving-leg view:
  For index_sl trades, split bars into pre- and post-adjustment phases.
  Reports whether the surviving leg's theta still covers the full position's
  gamma drag after the other side was rolled.

Note on lot scaling: breakeven = √(2θΔt/|γ|). If both sides scale by lots k,
the factor cancels. Results are lot-independent.

Implementation: Black-Scholes greeks are computed via vectorized scipy/numpy
(not per-bar mibian loops). Greek formulas follow mibian conventions:
  - sigma in decimal (IV% / 100)
  - T in calendar years (dte_days / 365)
  - theta in pts/calendar-day (divided by 365 internally)
  - gamma in 1/pt (change in delta per 1-pt spot move)

Run from repo root:
  python research/greek_analysis/exit_timing/run_artemis.py

Outputs (computed 2026-06-24):
  exit_timing/data/exit_timing_bars_artemis.parquet  (232,595 per-bar rows)
  exit_timing/data/exit_timing_summary_artemis.csv   (DTE-bucketed aggregates)
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

from research.greek_analysis.greek_engine import get_dte_days

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

NIFTY_SUMMARY   = os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_summary_nifty.csv')
SENSEX_SUMMARY  = os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_summary_sensex.csv')
NIFTY_LOGS_DIR  = os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_logs_nifty')
SENSEX_LOGS_DIR = os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_logs_sensex')
IV_CACHE_DIR    = os.path.join(REPO_ROOT, 'research', 'greek_analysis', 'data', 'iv_cache_artemis')
OUTPUT_DIR      = os.path.join(_HERE, 'data')
OUTPUT_BARS_PQ  = os.path.join(OUTPUT_DIR, 'exit_timing_bars_artemis.parquet')
OUTPUT_SUMMARY  = os.path.join(OUTPUT_DIR, 'exit_timing_summary_artemis.csv')

RISK_FREE = 0.05   # decimal (5% annualised)

# 4-leg definitions: (iv_cache_col, strike_col, option_type, direction, status_col)
LEGS = [
    ('pe_sell_iv', 'pe_sell_strike', 'pe', -1, 'pe_status'),
    ('pe_buy_iv',  'pe_buy_strike',  'pe', +1, 'pe_status'),
    ('ce_sell_iv', 'ce_sell_strike', 'ce', -1, 'ce_status'),
    ('ce_buy_iv',  'ce_buy_strike',  'ce', +1, 'ce_status'),
]

DTE_BUCKETS = [
    (0.0, 0.5,  '0.0–0.5d'),
    (0.5, 1.0,  '0.5–1.0d'),
    (1.0, 1.5,  '1.0–1.5d'),
    (1.5, 2.0,  '1.5–2.0d'),
    (2.0, 2.5,  '2.0–2.5d'),
    (2.5, 3.0,  '2.5–3.0d'),
    (3.0, 4.0,  '3.0–4.0d'),
    (4.0, 5.0,  '4.0–5.0d'),
    (5.0, 99.0, '5.0d+  '),
]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

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


def _load_iv(trade_id: int, expiry_date: str, instrument: str) -> pd.DataFrame | None:
    path = os.path.join(IV_CACHE_DIR,
                        f'trade_{trade_id:04d}_{expiry_date}_{instrument}.parquet')
    return pd.read_parquet(path) if os.path.exists(path) else None


# ---------------------------------------------------------------------------
# Vectorized Black-Scholes gamma and theta
# ---------------------------------------------------------------------------

def _bs_gamma_theta(spot: np.ndarray, strike: np.ndarray,
                    iv_pct: np.ndarray, dte_days: np.ndarray,
                    opt_type: str, valid_mask: np.ndarray
                    ) -> tuple[np.ndarray, np.ndarray]:
    """
    Vectorized BS gamma and theta for arrays of bar observations.

    gamma  = φ(d1) / (S × σ × √T)
    theta  = [−S × φ(d1) × σ / (2√T)  ±  r × K × exp(−rT) × Φ(±d2)] / 365
             sign(±) is +1 for calls (Φ(d2)), −1 for puts (Φ(−d2))
             Division by 365 converts to pts/calendar-day (mibian convention).

    Returns (gamma_arr, theta_arr); invalid bars → 0.0.
    """
    n = len(spot)
    gamma_arr = np.zeros(n)
    theta_arr = np.zeros(n)

    if not valid_mask.any():
        return gamma_arr, theta_arr

    S = spot[valid_mask]
    K = strike[valid_mask]
    sigma = iv_pct[valid_mask] / 100.0   # decimal
    T = np.maximum(dte_days[valid_mask] / 365.0, 1e-10)  # years
    sqT = np.sqrt(T)
    r = RISK_FREE

    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqT)
    d2 = d1 - sigma * sqT

    phi_d1   = scipy_norm.pdf(d1)
    exp_rT   = np.exp(-r * T)

    g = phi_d1 / (S * sigma * sqT)

    base = -S * phi_d1 * sigma / (2.0 * sqT)
    if opt_type == 'ce':
        t = (base - r * K * exp_rT * scipy_norm.cdf(d2)) / 365.0
    else:
        t = (base + r * K * exp_rT * scipy_norm.cdf(-d2)) / 365.0

    # Sanitise
    g_ok = np.isfinite(g) & (g >= 0.0)
    t_ok = np.isfinite(t)

    gamma_arr[valid_mask] = np.where(g_ok, g, 0.0)
    theta_arr[valid_mask] = np.where(t_ok, t, 0.0)

    # Mark bars where computation failed so they're excluded later
    bad_idx = np.where(valid_mask)[0][~(g_ok & t_ok)]
    if len(bad_idx):
        gamma_arr[bad_idx] = np.nan   # signal exclusion
        theta_arr[bad_idx] = np.nan

    return gamma_arr, theta_arr


# ---------------------------------------------------------------------------
# Adjustment-state classification
# ---------------------------------------------------------------------------

def _adj_state_array(pe_status: np.ndarray, ce_status: np.ndarray) -> np.ndarray:
    """Vectorized adjustment-state classification."""
    PE_ADJ = {'adjusted_additional', 'active_additional', 'active_additional_elm'}
    CE_ADJ = {'adjusted_additional', 'active_additional', 'active_additional_elm'}
    pe_adj = np.array([s in PE_ADJ for s in pe_status])
    ce_adj = np.array([s in CE_ADJ for s in ce_status])
    result = np.full(len(pe_status), 'none', dtype=object)
    result[pe_adj & ce_adj] = 'both_rolled'
    result[pe_adj & ~ce_adj] = 'pe_rolled'
    result[~pe_adj & ce_adj] = 'ce_rolled'
    return result


# ---------------------------------------------------------------------------
# Per-trade computation (vectorized)
# ---------------------------------------------------------------------------

def _compute_trade_bars(log: pd.DataFrame, iv_df: pd.DataFrame,
                        expiry_date: str, instrument: str, trade_id: int,
                        entry_vix: float,
                        pe_exit_reason: str, ce_exit_reason: str
                        ) -> pd.DataFrame:
    """
    Vectorized computation of per-bar net Greeks and breakeven for one trade.
    Returns a DataFrame with one row per consecutive bar pair.
    """
    n = len(log)
    if n < 2:
        return pd.DataFrame()

    ts_arr   = log['time_stamp'].values
    spot_arr = log['spot'].values.astype(float)

    # DTE for each bar (calendar days to expiry)
    expiry_dt = pd.Timestamp(expiry_date) + pd.Timedelta(hours=15, minutes=30)
    dte_arr = np.array(
        [(expiry_dt - pd.Timestamp(t)).total_seconds() / 86400.0 for t in ts_arr]
    )
    dte_arr = np.maximum(dte_arr, 0.0)

    # Net position Greeks (n bars each) — accumulated across 4 legs
    theta_net = np.zeros(n)
    gamma_net = np.zeros(n)
    iv_fail   = np.zeros(n, dtype=bool)   # True = at least one leg had bad IV/greek

    for iv_col, strike_col, opt_type, direction, status_col in LEGS:
        iv_vals  = iv_df[iv_col].values.astype(float)
        k_vals   = log[strike_col].values.astype(float)
        status   = log[status_col].values

        # Per-bar validity: side not closed, IV positive and finite, DTE positive
        closed   = np.array([s == 'closed' for s in status])
        iv_ok    = np.isfinite(iv_vals) & (iv_vals > 0.5)   # IV > 0.5% threshold
        dte_ok   = dte_arr > 1e-4
        k_ok     = np.isfinite(k_vals) & (k_vals > 0.0)
        valid    = ~closed & iv_ok & dte_ok & k_ok

        gamma_i, theta_i = _bs_gamma_theta(
            spot_arr, k_vals, iv_vals, dte_arr, opt_type, valid
        )

        # Bars where computation returned nan are marked as failed
        leg_bad = np.isnan(gamma_i) | np.isnan(theta_i)
        iv_fail |= leg_bad

        # Replace nan with 0 for accumulation (they're excluded via iv_fail below)
        theta_net += direction * np.nan_to_num(theta_i)
        gamma_net += direction * np.nan_to_num(gamma_i)

    # Work on consecutive pairs (bars 0..n-2 → pairs with bar 1..n-1)
    bar_idx = np.arange(n - 1)

    ts_pairs = ts_arr[bar_idx]
    ts_next  = ts_arr[bar_idx + 1]
    dt_days  = np.array(
        [(pd.Timestamp(ts_next[i]) - pd.Timestamp(ts_pairs[i])).total_seconds() / 86400.0
         for i in range(n - 1)]
    )

    spot_t   = spot_arr[bar_idx]
    spot_t1  = spot_arr[bar_idx + 1]
    dte_t    = dte_arr[bar_idx]
    theta_t  = theta_net[bar_idx]
    gamma_t  = gamma_net[bar_idx]
    bad_t    = iv_fail[bar_idx]

    pe_status = log['pe_status'].values[bar_idx]
    ce_status = log['ce_status'].values[bar_idx]

    # Skip bars where both sides are closed
    both_closed = (pe_status == 'closed') & (ce_status == 'closed')

    # Breakeven: only where theta>0, gamma<0, dt>0, no iv_fail
    be_valid = (~bad_t & ~both_closed
                & (theta_t > 0) & (gamma_t < 0) & (dt_days > 0))
    breakeven = np.full(n - 1, np.nan)
    breakeven[be_valid] = np.sqrt(
        2.0 * theta_t[be_valid] * dt_days[be_valid] / np.abs(gamma_t[be_valid])
    )

    # Realized spot move (absolute)
    realized = np.abs(spot_t1 - spot_t)

    # Adjustment state
    adj_state = _adj_state_array(pe_status, ce_status)

    # Exclude both-closed bars from output
    keep = ~both_closed

    result = pd.DataFrame({
        'instrument':     instrument,
        'trade_id':       trade_id,
        'expiry_date':    expiry_date,
        'entry_vix':      entry_vix,
        'bar_ts':         ts_pairs[keep],
        'dte':            np.round(dte_t[keep], 4),
        'dt_days':        np.round(dt_days[keep], 6),
        'spot':           spot_t[keep],
        'realized_move':  np.round(realized[keep], 2),
        'theta_net':      np.round(theta_t[keep], 6),
        'gamma_net':      np.round(gamma_t[keep], 8),
        'breakeven':      np.round(breakeven[keep], 3),
        'pe_status':      pe_status[keep],
        'ce_status':      ce_status[keep],
        'adj_state':      adj_state[keep],
        'iv_fail':        bad_t[keep],
        'pe_exit_reason': pe_exit_reason,
        'ce_exit_reason': ce_exit_reason,
    })
    return result


# ---------------------------------------------------------------------------
# Instrument runner
# ---------------------------------------------------------------------------

def _run_instrument(instrument: str, summary_path: str,
                    logs_dir: str) -> pd.DataFrame:
    summary = _load_summary(summary_path)
    n = len(summary)
    print(f"\n{instrument.upper()} — {n} traded weeks", flush=True)

    frames = []
    for _, row in summary.iterrows():
        trade_id    = int(row['trade_id'])
        expiry_date = str(row['expiry_date'])
        entry_vix   = float(row['entry_vix']) if pd.notna(row.get('entry_vix')) else float('nan')

        log   = _load_log(trade_id, expiry_date, logs_dir)
        iv_df = _load_iv(trade_id, expiry_date, instrument)
        if iv_df is None:
            print(f"  [{trade_id:3d}] WARN: no IV cache")
            continue
        if len(iv_df) != len(log):
            print(f"  [{trade_id:3d}] WARN: cache length mismatch ({len(iv_df)} vs {len(log)})")
            continue

        df = _compute_trade_bars(
            log, iv_df, expiry_date, instrument, trade_id, entry_vix,
            str(row.get('pe_exit_reason', '')),
            str(row.get('ce_exit_reason', '')),
        )
        frames.append(df)
        print(f"  [{trade_id:3d}] {expiry_date}  {len(df):4d} bars  "
              f"VIX={entry_vix:.1f}  "
              f"pe={row.get('pe_exit_reason','')}/"
              f"ce={row.get('ce_exit_reason','')}", flush=True)

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ---------------------------------------------------------------------------
# DTE-bucketed aggregation
# ---------------------------------------------------------------------------

def _dte_label(dte: float) -> str:
    for lo, hi, label in DTE_BUCKETS:
        if lo <= dte < hi:
            return label
    return '5.0d+  '


def _aggregate(bars: pd.DataFrame) -> pd.DataFrame:
    """Aggregate per-bar data into DTE buckets."""
    bars = bars[~bars['iv_fail']].copy()   # exclude IV-failed bars
    if bars.empty:
        return pd.DataFrame()

    bars['dte_bucket'] = bars['dte'].apply(_dte_label)
    # breakeven validity: not nan AND theta>0 AND gamma<0
    be_valid = bars['breakeven'].notna() & (bars['theta_net'] > 0) & (bars['gamma_net'] < 0)
    bars_be = bars[be_valid]

    records = []
    for lo, hi, label in DTE_BUCKETS:
        grp_all = bars[(bars['dte'] >= lo) & (bars['dte'] < hi)]
        grp_be  = bars_be[(bars_be['dte'] >= lo) & (bars_be['dte'] < hi)]
        if len(grp_be) == 0:
            continue
        pct_gt = (grp_be['realized_move'] > grp_be['breakeven']).mean() * 100
        records.append({
            'dte_bucket':          label,
            'n_bars':              len(grp_all),
            'n_valid':             len(grp_be),
            'mean_theta_net':      grp_be['theta_net'].mean(),
            'mean_gamma_net':      grp_be['gamma_net'].mean(),
            'mean_breakeven_pts':  grp_be['breakeven'].mean(),
            'median_breakeven_pts':grp_be['breakeven'].median(),
            'mean_realized_move':  grp_be['realized_move'].mean(),
            'p50_realized_move':   grp_be['realized_move'].median(),
            'p75_realized_move':   grp_be['realized_move'].quantile(0.75),
            'p90_realized_move':   grp_be['realized_move'].quantile(0.90),
            'pct_realized_gt_be':  round(pct_gt, 1),
        })
    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Surviving-leg analysis
# ---------------------------------------------------------------------------

def _surviving_leg_analysis(bars: pd.DataFrame) -> None:
    bars_valid = bars[~bars['iv_fail'] & bars['breakeven'].notna()]
    sl_trades = bars_valid[
        (bars_valid['pe_exit_reason'] == 'index_sl') |
        (bars_valid['ce_exit_reason'] == 'index_sl')
    ]
    if sl_trades.empty:
        print("\n  No index_sl trades in bar data.")
        return

    n_trades = sl_trades.groupby(['instrument', 'trade_id']).ngroups
    print(f"\n  index_sl trades: {n_trades} trades, {len(sl_trades)} bars")

    for phase, label in [
        ('none',      'Pre-adjustment  (both sides intact)'),
        ('pe_rolled', 'Post-PE-roll    (PE new strike, CE surviving)'),
        ('ce_rolled', 'Post-CE-roll    (CE new strike, PE surviving)'),
    ]:
        sub = sl_trades[sl_trades['adj_state'] == phase]
        if len(sub) < 5:
            continue
        pct_gt = (sub['realized_move'] > sub['breakeven']).mean() * 100
        print(f"\n  {label}:")
        print(f"    n={len(sub):,}  "
              f"θ_net={sub['theta_net'].mean():+.5f}  "
              f"γ_net={sub['gamma_net'].mean():+.8f}  "
              f"be={sub['breakeven'].mean():.1f}pts  "
              f"rlz={sub['realized_move'].mean():.1f}pts  "
              f"pct_gt={pct_gt:.0f}%")

    # Post-roll breakeven by DTE
    post_roll = sl_trades[sl_trades['adj_state'].isin(['pe_rolled', 'ce_rolled'])]
    if not post_roll.empty:
        print("\n  Post-roll breakeven by DTE:")
        agg = _aggregate(post_roll)
        for _, r in agg.iterrows():
            marker = ' ← crossover' if r['pct_realized_gt_be'] >= 50 else ''
            print(f"    {r['dte_bucket']}  "
                  f"be={r['mean_breakeven_pts']:5.1f}pts  "
                  f"rlz_p50={r['p50_realized_move']:4.1f}pts  "
                  f"pct_gt={r['pct_realized_gt_be']:4.1f}%{marker}")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _print_report(bars: pd.DataFrame, agg: pd.DataFrame) -> None:
    sep  = '─' * 90
    sep2 = '═' * 90
    bars_valid = bars[~bars['iv_fail']]

    print()
    print(sep2)
    print('ARTEMIS BRANCH 7 — Gamma/Theta Exit Timing')
    print(sep2)
    print(f"Total bars: {len(bars):,}  (excluding IV-fails: {len(bars_valid):,})")
    n_nifty  = bars[bars['instrument'] == 'nifty']['trade_id'].nunique()
    n_sensex = bars[bars['instrument'] == 'sensex']['trade_id'].nunique()
    print(f"Trades: Nifty={n_nifty}, Sensex={n_sensex}, Total={n_nifty+n_sensex}")
    print()

    # ── Full-position crossover table ──────────────────────────────────────
    print('FULL POSITION (4 legs) — Breakeven vs Realized Spot Move by DTE')
    print()
    print(f"{'DTE bucket':12s}  {'n_bars':>7s}  "
          f"{'θ_net_avg':>11s}  {'γ_net_avg':>12s}  "
          f"{'be_mean':>8s}  {'rlz_p50':>7s}  {'rlz_p90':>7s}  "
          f"{'%rlz>be':>7s}")
    print(sep)

    crossover_dte = None
    for _, r in agg.iterrows():
        marker = ''
        if r['pct_realized_gt_be'] >= 50.0 and crossover_dte is None:
            crossover_dte = r['dte_bucket'].strip()
            marker = ' ◄ crossover'
        print(f"  {r['dte_bucket']:12s}  {r['n_bars']:7,.0f}  "
              f"  {r['mean_theta_net']:+9.5f}  {r['mean_gamma_net']:+11.8f}  "
              f"  {r['mean_breakeven_pts']:6.1f}pts  "
              f"  {r['p50_realized_move']:5.1f}pts  "
              f"  {r['p90_realized_move']:5.1f}pts  "
              f"  {r['pct_realized_gt_be']:5.1f}%{marker}")

    print()
    if crossover_dte:
        print(f"  ► Crossover DTE: {crossover_dte}")
        print(f"    At this DTE, realized spot moves exceed the theta/gamma breakeven")
        print(f"    in ≥50% of bars → position is gamma-dominated in expectation.")
        print(f"    Exiting or adjusting the losing leg before this DTE avoids")
        print(f"    the gamma-dominated zone.")
    else:
        print("  ► No crossover within the observed DTE range.")
        print("    Realized moves do not exceed breakeven in >50% of bars at any DTE.")
        print("    Position remains net-theta-positive throughout its lifetime.")
    print()

    # ── Cross-check: what fraction of total gamma_contrib came from <crossover_dte?
    print(sep)
    print('NET PnL IMPACT (per-bar theta+gamma P&L contribution)')
    print('  (theta_contrib = θ_net × dt;  gamma_contrib = ½ × |γ_net| × Δspot²)')
    print()
    bv = bars_valid[bars_valid['breakeven'].notna()].copy()
    bv['theta_contrib'] = bv['theta_net'] * bv['dt_days']
    bv['gamma_contrib'] = -0.5 * np.abs(bv['gamma_net']) * bv['realized_move'] ** 2
    bv['net_contrib']   = bv['theta_contrib'] + bv['gamma_contrib']

    print(f"{'DTE bucket':12s}  {'n_bars':>7s}  "
          f"{'θ/bar':>8s}  {'γ/bar':>8s}  "
          f"{'net/bar':>8s}  {'cumθ':>8s}  {'cumγ':>8s}")
    print(sep)
    cum_t, cum_g = 0.0, 0.0
    for lo, hi, label in DTE_BUCKETS:
        grp = bv[(bv['dte'] >= lo) & (bv['dte'] < hi)]
        if len(grp) < 5:
            continue
        mean_t = grp['theta_contrib'].mean()
        mean_g = grp['gamma_contrib'].mean()
        mean_n = grp['net_contrib'].mean()
        cum_t += grp['theta_contrib'].sum()
        cum_g += grp['gamma_contrib'].sum()
        print(f"  {label:12s}  {len(grp):7,}  "
              f"  {mean_t:+6.4f}  {mean_g:+6.4f}  "
              f"  {mean_n:+6.4f}  {cum_t:+8.1f}  {cum_g:+8.1f}")
    print()
    print(f"  Totals: θ_cumulative={cum_t:+.1f}pts  γ_cumulative={cum_g:+.1f}pts  "
          f"net={cum_t+cum_g:+.1f}pts")

    # ── By adjustment state ────────────────────────────────────────────────
    print()
    print(sep)
    print('BY ADJUSTMENT STATE (breakeven analysis)')
    for state, label in [
        ('none',        'No adjustment (both sides original)'),
        ('ce_rolled',   'CE rolled (PE surviving)'),
        ('pe_rolled',   'PE rolled (CE surviving)'),
        ('both_rolled', 'Both sides rolled'),
    ]:
        sub = bars_valid[(bars_valid['adj_state'] == state) & bars_valid['breakeven'].notna()]
        if len(sub) < 5:
            continue
        pct_gt = (sub['realized_move'] > sub['breakeven']).mean() * 100
        print(f"\n  {label}  (n={len(sub):,})")
        print(f"    θ_net={sub['theta_net'].mean():+.5f}  "
              f"γ_net={sub['gamma_net'].mean():+.8f}  "
              f"be={sub['breakeven'].mean():.1f}pts  "
              f"rlz_mean={sub['realized_move'].mean():.1f}pts  "
              f"pct_gt={pct_gt:.0f}%")

    # ── Surviving-leg analysis ─────────────────────────────────────────────
    print()
    print(sep)
    print('SURVIVING-LEG ANALYSIS (index_sl trades, post-roll phase)')
    _surviving_leg_analysis(bars)

    # ── Cross-check by exit type ───────────────────────────────────────────
    print()
    print(sep)
    print('BREAKEVEN CONTEXT BY EXIT TYPE')
    for exit_type, label in [
        ('index_sl',    'index_sl exits (spot breaks through)'),
        ('option_sl',   'option_sl exits (option price SL)'),
        ('elm',         'ELM exits (expiry-day regulatory)'),
        ('market_close','market_close exits'),
    ]:
        mask = (
            (bars_valid['pe_exit_reason'] == exit_type) |
            (bars_valid['ce_exit_reason'] == exit_type)
        )
        sub = bars_valid[mask & bars_valid['breakeven'].notna()]
        if len(sub) < 5:
            continue
        pct_gt = (sub['realized_move'] > sub['breakeven']).mean() * 100
        print(f"\n  {label}  (n={len(sub):,})")
        print(f"    be={sub['breakeven'].mean():.1f}pts  "
              f"rlz_mean={sub['realized_move'].mean():.1f}pts  "
              f"pct_gt={pct_gt:.0f}%  "
              f"mean_DTE={sub['dte'].mean():.2f}d")

    print()
    print(f"  Output: {OUTPUT_BARS_PQ}")
    print(f"          {OUTPUT_SUMMARY}")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    print('Artemis Branch 7 — Gamma/Theta Exit Timing (vectorized)')
    print(f'Output directory: {OUTPUT_DIR}')
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    nifty_df  = _run_instrument('nifty',  NIFTY_SUMMARY,  NIFTY_LOGS_DIR)
    sensex_df = _run_instrument('sensex', SENSEX_SUMMARY, SENSEX_LOGS_DIR)

    if nifty_df.empty and sensex_df.empty:
        print('[ERROR] No bars computed.')
        sys.exit(1)

    bars = pd.concat([nifty_df, sensex_df], ignore_index=True)
    bars.to_parquet(OUTPUT_BARS_PQ, index=False)
    print(f'\nSaved {len(bars):,} bars → {OUTPUT_BARS_PQ}')

    agg = _aggregate(bars)
    agg.to_csv(OUTPUT_SUMMARY, index=False)
    print(f'Saved DTE summary → {OUTPUT_SUMMARY}')

    _print_report(bars, agg)


if __name__ == '__main__':
    run()
