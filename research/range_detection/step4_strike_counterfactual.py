"""
Step 4: Resistance-anchored CE strike counterfactual for down_near trades.

For each down_near trade, compute what P&L would have been if CE sell was placed
at the first 100-pt Nifty strike strictly above range_high (instead of at the
VIX-adaptive expected-premium level used by Artemis).

Methodology:
- CF sell strike = ceil((range_high + 1) / 100) * 100
- CF buy strike  = CF sell + 300
- Entry premium: look up CF option at 10:31 on entry_date
- Exit premium:
    - Non-NaN exit reason: look up at ce_exit_time
    - NaN exit (expired): look up expiry close (or fallback = 0)
- Delta P&L = (CF CE P&L approx) - (actual CE P&L approx)
- CF total = actual total_pl_points + delta

Approximation note: ce_pl_points in trade data includes hedge-roll bookings that
aren't visible in the exit columns. Using the same approximation for both actual
and counterfactual means the unknown component cancels in the delta.
"""
import pandas as pd
import numpy as np
import math
import os

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO = '/home/parijnan/scripts/algo-trading-lab'
OPT_DIR   = os.path.join(REPO, 'data_pipeline', 'data', 'nifty', 'options')
IDX_1M    = os.path.join(REPO, 'data_pipeline', 'data', 'indices', 'nifty.csv')
ANNOTATED = os.path.join(REPO, 'research', 'range_detection', 'outputs', 'artemis_annotated_nifty.csv')

EXPIRY_FALLBACK = 0.05  # price for options that can't be found at expiry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def rh_anchor(rh: float) -> int:
    """First 100-pt Nifty strike strictly above range_high."""
    return math.ceil((rh + 1) / 100) * 100


def load_option(expiry_date: str, strike: int, right: str) -> pd.DataFrame | None:
    """Load option CSV; return None if file doesn't exist."""
    fname = os.path.join(OPT_DIR, expiry_date, f"{int(strike)}{right}.csv")
    if not os.path.exists(fname):
        return None
    df = pd.read_csv(fname, parse_dates=['datetime'])
    df = df.sort_values('datetime').reset_index(drop=True)
    if df['datetime'].dt.tz is not None:
        df['datetime'] = df['datetime'].dt.tz_localize(None)
    return df


def get_price_at(df: pd.DataFrame | None, ts: pd.Timestamp,
                 col: str = 'open', fallback: float = EXPIRY_FALLBACK) -> float:
    """Get price from option DataFrame at or near timestamp."""
    if df is None or df.empty:
        return fallback
    # find first row at or after ts
    idx = df['datetime'].searchsorted(ts, side='left')
    if idx < len(df):
        return df.iloc[idx][col]
    # past end of data — use last close
    return df.iloc[-1]['close']


def get_expiry_close(df: pd.DataFrame | None, expiry_ts: pd.Timestamp,
                     fallback: float = EXPIRY_FALLBACK) -> float:
    """Get last close at or before expiry_ts."""
    if df is None or df.empty:
        return fallback
    mask = df['datetime'] <= expiry_ts
    sub = df.loc[mask]
    if sub.empty:
        return fallback
    return sub.iloc[-1]['close']


# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
idx_1m = pd.read_csv(IDX_1M, parse_dates=['time_stamp'])
idx_1m = idx_1m.set_index('time_stamp').sort_index()
if idx_1m.index.tz is not None:
    idx_1m.index = idx_1m.index.tz_localize(None)

ann = pd.read_csv(ANNOTATED)
committed = ann['ep_committed'].fillna(False).astype(bool)
kd = ann['key_dist_pct'].fillna(-1)
dn = ann[committed & (ann['ep_direction'] == 'down') & (kd >= 0) & (kd < 50)].copy()
dn = dn.reset_index(drop=True)

# ---------------------------------------------------------------------------
# Compute weekly highs for breach detection
# ---------------------------------------------------------------------------
weekly_highs = []
for _, r in dn.iterrows():
    entry = pd.Timestamp(r['entry_date'])
    expiry = pd.Timestamp(r['expiry'])
    mask = (idx_1m.index.normalize() >= entry) & (idx_1m.index <= expiry)
    wh = idx_1m.loc[mask, 'high'].max() if mask.any() else np.nan
    weekly_highs.append(wh)
dn['weekly_high'] = weekly_highs
dn['breached_rh'] = dn['weekly_high'] > dn['ep_range_high']

# ---------------------------------------------------------------------------
# Per-trade counterfactual
# ---------------------------------------------------------------------------
records = []

for _, r in dn.iterrows():
    entry_date  = str(r['entry_date'])[:10]
    expiry_str  = pd.Timestamp(r['expiry']).strftime('%Y-%m-%d')
    entry_ts    = pd.Timestamp(f"{entry_date} 10:31:00")
    expiry_ts   = pd.Timestamp(r['expiry']).replace(hour=15, minute=30)

    ce_exit_ts  = pd.Timestamp(r['ce_exit_time']) if pd.notna(r['ce_exit_time']) else None
    has_stop    = ce_exit_ts is not None

    # Actual approximate CE P&L (same formula, NaN→0)
    se = r['ce_sell_entry']
    be = r['ce_buy_entry']
    sx = r['ce_sell_exit'] if pd.notna(r['ce_sell_exit']) else 0.0
    bx = r['ce_buy_exit']  if pd.notna(r['ce_buy_exit'])  else 0.0
    actual_ce_approx = (se - sx) - (be - bx)

    # Counterfactual strikes
    cf_sell = rh_anchor(r['ep_range_high'])
    cf_buy  = cf_sell + 300

    # Load option data
    df_sell = load_option(expiry_str, cf_sell, 'ce')
    df_buy  = load_option(expiry_str, cf_buy,  'ce')
    # Also load actual strike for scaling check
    df_actual_sell = load_option(expiry_str, int(r['ce_sell_strike']), 'ce')

    # Determine scaling factor: ratio of actual vs options-data price at entry.
    # When the backtest used a different data snapshot, options-data prices can be
    # 2-3× higher than actual. Scaling CF by the same ratio preserves relative
    # differences between strikes from the same source.
    opt_actual_open = get_price_at(df_actual_sell, entry_ts, col='open')
    if opt_actual_open > 0.1 and abs(opt_actual_open - se) > 0.6:
        # Data mismatch — use ratio to scale CF to actual price level
        scale = se / opt_actual_open
        data_ok = False
    else:
        scale = 1.0
        data_ok = True

    # Entry premiums
    cf_sell_entry = get_price_at(df_sell, entry_ts, col='open') * scale
    cf_buy_entry  = get_price_at(df_buy,  entry_ts, col='open') * scale

    # Exit premiums
    if has_stop:
        # Look up open of the stop bar
        stop_ts = ce_exit_ts
        cf_sell_exit = get_price_at(df_sell, stop_ts, col='open') * scale
        cf_buy_exit  = get_price_at(df_buy,  stop_ts, col='open') * scale
    else:
        # Expired — use expiry close
        cf_sell_exit = get_expiry_close(df_sell, expiry_ts) * scale
        cf_buy_exit  = get_expiry_close(df_buy,  expiry_ts) * scale

    # When CF strike == actual strike, force delta=0 (no change, regardless of data)
    if cf_sell == int(r['ce_sell_strike']) and cf_buy == int(r['ce_buy_strike']):
        cf_sell_entry = se
        cf_buy_entry  = be
        cf_sell_exit  = sx if pd.notna(r['ce_sell_exit']) else 0.0
        cf_buy_exit   = bx if pd.notna(r['ce_buy_exit'])  else 0.0

    cf_ce_pl = (cf_sell_entry - cf_sell_exit) - (cf_buy_entry - cf_buy_exit)

    delta     = cf_ce_pl - actual_ce_approx
    cf_total  = r['total_pl_points'] + delta

    actual_gap  = r['ce_sell_strike'] - r['ep_range_high']
    cf_gap      = cf_sell - r['ep_range_high']
    cf_struck   = r['breached_rh'] and (r['weekly_high'] - r['ep_range_high']) > cf_gap

    records.append({
        'entry_date':      entry_date,
        'expiry':          expiry_str,
        'entry_vix':       r['entry_vix'],
        'key_dist_pct':    r['key_dist_pct'],
        'ep_range_high':   r['ep_range_high'],
        'actual_ce_sell':  r['ce_sell_strike'],
        'actual_gap':      round(actual_gap, 0),
        'cf_ce_sell':      cf_sell,
        'cf_gap':          cf_gap,
        # Actual
        'actual_sell_entry': se,
        'actual_buy_entry':  be,
        'actual_net_entry':  round(se - be, 2),
        # CF
        'cf_sell_entry':     round(cf_sell_entry, 2),
        'cf_buy_entry':      round(cf_buy_entry, 2),
        'cf_net_entry':      round(cf_sell_entry - cf_buy_entry, 2),
        'delta_entry_credit': round((cf_sell_entry - cf_buy_entry) - (se - be), 2),
        # Exit
        'actual_ce_approx':  round(actual_ce_approx, 2),
        'cf_ce_pl':          round(cf_ce_pl, 2),
        'delta_ce_pl':       round(delta, 2),
        # Total
        'actual_total':      r['total_pl_points'],
        'cf_total':          round(cf_total, 2),
        # Context
        'ce_exit_reason':    r['ce_exit_reason'],
        'breached_rh':       r['breached_rh'],
        'cf_struck':         cf_struck,
        'weekly_high':       round(r['weekly_high'], 1),
        'overshoot':         round(max(r['weekly_high'] - r['ep_range_high'], 0), 1),
        'data_ok':           data_ok,
        'scale':             round(scale, 3),
    })

df_cf = pd.DataFrame(records)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("=" * 80)
print("STEP 4: Resistance-anchored CE strike counterfactual — Nifty down_near (n=29)")
print("=" * 80)

# Sanity check data availability
no_data = sum(1 for r in records if r['cf_sell_entry'] <= 0.05)
print(f"\nData availability: {len(records) - no_data}/{len(records)} trades with CF option data found")

print("\n── Entry credit delta ─────────────────────────────────────────────────────")
print(f"{'Group':<30} {'n':>4} {'Actual net':>11} {'CF net':>10} {'Δ credit':>10}")
below = df_cf[df_cf['actual_gap'] < 0]
above = df_cf[df_cf['actual_gap'] >= 0]
for label, sub in [('CE below RH (actual_gap < 0)', below),
                   ('CE above RH (actual_gap ≥ 0)', above),
                   ('All down_near',                 df_cf)]:
    print(f"  {label:<28} {len(sub):>4} {sub['actual_net_entry'].mean():>11.2f} {sub['cf_net_entry'].mean():>10.2f} {sub['delta_entry_credit'].mean():>10.2f}")

print("\n── CE P&L delta (approx) ──────────────────────────────────────────────────")
print(f"{'Group':<30} {'n':>4} {'Actual CE':>10} {'CF CE':>10} {'Δ CE':>10}")
for label, sub in [('CE below RH (actual_gap < 0)', below),
                   ('CE above RH (actual_gap ≥ 0)', above),
                   ('All down_near',                 df_cf)]:
    print(f"  {label:<28} {len(sub):>4} {sub['actual_ce_approx'].mean():>10.2f} {sub['cf_ce_pl'].mean():>10.2f} {sub['delta_ce_pl'].mean():>10.2f}")

print("\n── Total P&L impact ───────────────────────────────────────────────────────")
print(f"  Actual sum:  {df_cf['actual_total'].sum():+.2f} pts")
print(f"  CF sum:      {df_cf['cf_total'].sum():+.2f} pts")
print(f"  Δ total:     {(df_cf['cf_total'] - df_cf['actual_total']).sum():+.2f} pts")

print("\n── By breach status ───────────────────────────────────────────────────────")
print(f"{'Group':<30} {'n':>4} {'Actual':>10} {'CF':>10} {'Δ':>10}")
for label, sub in [('Held (no breach)',         df_cf[~df_cf['breached_rh']]),
                   ('Breached, CF also struck', df_cf[df_cf['breached_rh'] & df_cf['cf_struck']]),
                   ('Breached, CF spared',      df_cf[df_cf['breached_rh'] & ~df_cf['cf_struck']])]:
    if len(sub):
        print(f"  {label:<28} {len(sub):>4} {sub['actual_total'].mean():>10.2f} {sub['cf_total'].mean():>10.2f} {(sub['cf_total']-sub['actual_total']).mean():>10.2f}")

print("\n── VIX split: entry credit delta ──────────────────────────────────────────")
vix_tert = pd.qcut(df_cf['entry_vix'], q=3, labels=['Low VIX', 'Mid VIX', 'High VIX'])
for label, sub in df_cf.groupby(vix_tert):
    print(f"  {str(label):<10} n={len(sub):>2}  avg_vix={sub['entry_vix'].mean():.1f}"
          f"  Δcredit={sub['delta_entry_credit'].mean():>+6.2f}"
          f"  ΔCE_pl={sub['delta_ce_pl'].mean():>+6.2f}"
          f"  Δtotal={( sub['cf_total']-sub['actual_total']).mean():>+6.2f}")

print("\n── Trade-by-trade detail ──────────────────────────────────────────────────")
header = f"{'Date':<12} {'RH':>8} {'Act':>7} {'CF':>7} {'AΔ':>6} {'Aentry':>7} {'CFentry':>7} {'Δcredit':>8} {'ΔCEpl':>7} {'Δtotal':>7} {'Breach':>7} {'Reason':<10}"
print(header)
for _, r in df_cf.iterrows():
    b = 'B-CF+' if (r['breached_rh'] and r['cf_struck']) else ('B-ok' if r['breached_rh'] else 'held')
    print(f"{r['entry_date']:<12} {r['ep_range_high']:>8.0f} {r['actual_ce_sell']:>7.0f} {r['cf_ce_sell']:>7.0f} {r['actual_gap']:>6.0f} {r['actual_net_entry']:>7.2f} {r['cf_net_entry']:>7.2f} {r['delta_entry_credit']:>8.2f} {r['delta_ce_pl']:>7.2f} {r['delta_ce_pl'] + 0:>7.2f} {b:>7} {str(r['ce_exit_reason']):<10}")

# Save
out_path = os.path.join(REPO, 'research', 'range_detection', 'outputs', 'step4_counterfactual_nifty.csv')
df_cf.to_csv(out_path, index=False)
print(f"\nSaved to {out_path}")
