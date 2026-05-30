"""
SL Aftermath Analysis — Artemis Nifty + Sensex

For every stopped trade (index_sl, option_sl, elm), compute what the P&L
would have been if the original position had been held to expiry.

Methodology:
- "Held to expiry" P&L uses intrinsic value at expiry spot (index 1-min close):
    cf_ce = (ce_sell_entry - max(0, S-K_sell)) - (ce_buy_entry - max(0, S-K_buy))
    cf_pe = (pe_sell_entry - max(0, K_sell-S)) - (pe_buy_entry - max(0, K_buy-S))
- Actual base P&L = ce_pl_points + pe_pl_points (excludes rollover add_pl)
- Cost of stop = cf_expiry_total - actual_base (positive = stop was premature)
- Range held at expiry: for down episodes, spot_expiry < range_high;
  for up episodes, spot_expiry > range_low.
"""
import pandas as pd
import numpy as np
import os, math

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO = '/home/parijnan/scripts/algo-trading-lab'
ANN  = {
    'nifty':  os.path.join(REPO, 'research', 'range_detection', 'outputs',
                           'artemis_annotated_nifty.csv'),
    'sensex': os.path.join(REPO, 'research', 'range_detection', 'outputs',
                           'artemis_annotated_sensex.csv'),
}
IDX = {
    'nifty':  os.path.join(REPO, 'data_pipeline', 'data', 'indices', 'nifty.csv'),
    'sensex': os.path.join(REPO, 'data_pipeline', 'data', 'indices', 'sensex.csv'),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_index(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=['time_stamp'])
    df = df.set_index('time_stamp').sort_index()
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


def expiry_spot(idx: pd.DataFrame, expiry_ts: pd.Timestamp) -> float:
    """Last available close at or before expiry_ts (use 15:29 or 15:30)."""
    end = expiry_ts.normalize() + pd.Timedelta(hours=15, minutes=30)
    mask = idx.index <= end
    sub = idx.loc[mask]
    return sub['close'].iloc[-1] if len(sub) else np.nan


def intrinsic(spot: float, strike: float, right: str) -> float:
    if right == 'ce':
        return max(0.0, spot - strike)
    return max(0.0, strike - spot)


def assign_bucket(row) -> str:
    if not row.get('ep_committed', False):
        return 'other'
    kd = row.get('key_dist_pct', -1)
    if kd is None or np.isnan(kd) or kd < 0:
        return 'other'
    d = row.get('ep_direction', '')
    if d == 'down':
        return 'down_near' if kd < 50 else 'down_far'
    if d == 'up':
        return 'up_near' if kd < 50 else 'up_far'
    return 'other'


def day_of_week(ts) -> str:
    if pd.isna(ts):
        return 'unknown'
    t = pd.Timestamp(ts)
    return ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'][t.weekday()]


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------
all_records = []

for inst in ('nifty', 'sensex'):
    idx = load_index(IDX[inst])
    ann = pd.read_csv(ANN[inst])

    # Only traded rows
    ann = ann[ann['week_outcome'] == 'traded'].copy()

    for _, r in ann.iterrows():
        expiry_ts = pd.Timestamp(r['expiry'])
        spot_exp  = expiry_spot(idx, expiry_ts)

        # Actual base P&L (ce + pe, no rollover)
        actual_ce   = r['ce_pl_points']
        actual_pe   = r['pe_pl_points']
        actual_base = actual_ce + actual_pe

        # Counterfactual: original position held to expiry
        ce_sell_exp = intrinsic(spot_exp, r['ce_sell_strike'], 'ce')
        ce_buy_exp  = intrinsic(spot_exp, r['ce_buy_strike'],  'ce')
        pe_sell_exp = intrinsic(spot_exp, r['pe_sell_strike'], 'pe')
        pe_buy_exp  = intrinsic(spot_exp, r['pe_buy_strike'],  'pe')

        cf_ce  = (r['ce_sell_entry'] - ce_sell_exp) - (r['ce_buy_entry'] - ce_buy_exp)
        cf_pe  = (r['pe_sell_entry'] - pe_sell_exp) - (r['pe_buy_entry'] - pe_buy_exp)
        cf_tot = cf_ce + cf_pe

        cost_of_stop = cf_tot - actual_base  # positive = premature stop

        # Stop type (first stop that fired, considering both legs)
        ce_reason = r['ce_exit_reason'] if pd.notna(r['ce_exit_reason']) else None
        pe_reason = r['pe_exit_reason'] if pd.notna(r['pe_exit_reason']) else None
        # Classify the week: if ANY leg was stopped, the week is a "stop week"
        stop_reasons = {ce_reason, pe_reason} - {None}
        if 'index_sl' in stop_reasons:
            stop_type = 'index_sl'
        elif 'option_sl' in stop_reasons:
            stop_type = 'option_sl'
        elif 'elm' in stop_reasons:
            stop_type = 'elm'
        else:
            stop_type = 'expiry'

        # Day of first exit
        ce_exit_day = day_of_week(r['ce_exit_time']) if pd.notna(r['ce_exit_time']) else None
        pe_exit_day = day_of_week(r['pe_exit_time']) if pd.notna(r['pe_exit_time']) else None
        first_exit_day = ce_exit_day or pe_exit_day

        # Range state
        bucket = assign_bucket(r)

        # Did range hold at expiry?
        d = r.get('ep_direction', '')
        rh = r.get('ep_range_high', np.nan)
        rl = r.get('ep_range_low', np.nan)
        if pd.notna(spot_exp) and pd.notna(rh) and pd.notna(rl):
            if d == 'down':
                range_held = spot_exp < rh
            elif d == 'up':
                range_held = spot_exp > rl
            else:
                range_held = None
        else:
            range_held = None

        all_records.append({
            'instrument':     inst,
            'entry_date':     r['entry_date'],
            'expiry':         str(expiry_ts.date()),
            'entry_vix':      r['entry_vix'],
            'ep_direction':   d,
            'bucket':         bucket,
            'key_dist_pct':   r.get('key_dist_pct', np.nan),
            'ep_bars_into':   r.get('ep_bars_into', np.nan),
            'stop_type':      stop_type,
            'ce_exit_reason': ce_reason,
            'pe_exit_reason': pe_reason,
            'first_exit_day': first_exit_day,
            'actual_base':    round(actual_base, 2),
            'cf_expiry':      round(cf_tot, 2),
            'cost_of_stop':   round(cost_of_stop, 2),
            'premature':      cost_of_stop > 0,          # held would have been better
            'cf_ce':          round(cf_ce, 2),
            'cf_pe':          round(cf_pe, 2),
            'spot_expiry':    round(spot_exp, 0) if pd.notna(spot_exp) else np.nan,
            'range_high':     round(rh, 0) if pd.notna(rh) else np.nan,
            'range_low':      round(rl, 0) if pd.notna(rl) else np.nan,
            'range_held':     range_held,
            # Entry premium context
            'ce_sell_entry':  r['ce_sell_entry'],
            'ce_sell_strike': r['ce_sell_strike'],
            'pe_sell_strike': r['pe_sell_strike'],
            'total_pl_pts':   r['total_pl_points'],
        })

df = pd.DataFrame(all_records)

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
print("=" * 80)
print("SL AFTERMATH ANALYSIS — Artemis Nifty + Sensex")
print("=" * 80)

# Split: stopped vs expiry
stopped  = df[df['stop_type'] != 'expiry']
expiries = df[df['stop_type'] == 'expiry']
print(f"\nTotal traded weeks: {len(df)}  |  Stopped: {len(stopped)}  |  Expired: {len(expiries)}")
print(f"Stop rate: {len(stopped)/len(df)*100:.1f}%")

print("\n── How often was the stop premature? ────────────────────────────────────────")
print(f"{'Category':<30} {'n':>5} {'Premature%':>12} {'Avg cost':>10} {'Avg cost|premature':>18} {'Avg cost|saved':>14}")
for label, sub in [
    ('All stopped',              stopped),
    ('  index_sl',               stopped[stopped['stop_type']=='index_sl']),
    ('  option_sl',              stopped[stopped['stop_type']=='option_sl']),
    ('  elm',                    stopped[stopped['stop_type']=='elm']),
]:
    if len(sub) == 0:
        continue
    prem_pct = sub['premature'].mean() * 100
    avg_cost = sub['cost_of_stop'].mean()
    premature_cost = sub.loc[sub['premature'], 'cost_of_stop'].mean() if sub['premature'].any() else 0
    saved_cost     = sub.loc[~sub['premature'], 'cost_of_stop'].mean() if (~sub['premature']).any() else 0
    print(f"  {label:<28} {len(sub):>5} {prem_pct:>11.1f}% {avg_cost:>+10.2f} {premature_cost:>+18.2f} {saved_cost:>+14.2f}")

print("\n── By range bucket ──────────────────────────────────────────────────────────")
print(f"{'Bucket':<14} {'n_stop':>8} {'Premature%':>12} {'AvgCost':>9} {'RangeHeld%':>12} {'n_expiry':>10} {'Expiry_avg':>12}")
for bkt in ['down_near', 'down_far', 'up_near', 'up_far', 'other']:
    s = stopped[stopped['bucket']==bkt]
    e = expiries[expiries['bucket']==bkt]
    if len(s) == 0 and len(e) == 0:
        continue
    prem_pct  = s['premature'].mean()*100 if len(s) else 0
    avg_cost  = s['cost_of_stop'].mean() if len(s) else 0
    held_pct  = s['range_held'].mean()*100 if (len(s) and s['range_held'].notna().any()) else 0
    exp_avg   = e['cf_expiry'].mean() if len(e) else 0
    print(f"  {bkt:<12} {len(s):>8} {prem_pct:>11.1f}% {avg_cost:>+9.2f} {held_pct:>11.1f}% {len(e):>10} {exp_avg:>+12.2f}")

print("\n── By day of first exit ─────────────────────────────────────────────────────")
print(f"{'Day':<8} {'n':>5} {'Premature%':>12} {'AvgCost':>10}")
for day in ['Mon', 'Tue', 'Wed', 'Thu', 'unknown']:
    s = stopped[stopped['first_exit_day']==day]
    if len(s) == 0:
        continue
    print(f"  {day:<6} {len(s):>5} {s['premature'].mean()*100:>11.1f}% {s['cost_of_stop'].mean():>+10.2f}")

print("\n── index_sl: by bucket ──────────────────────────────────────────────────────")
isl = stopped[stopped['stop_type']=='index_sl']
print(f"{'Bucket':<14} {'n':>5} {'Premature%':>12} {'AvgCost':>10} {'RangeHeld%':>12} {'AvgCostIfPremature':>20}")
for bkt in ['down_near', 'down_far', 'up_near', 'up_far', 'other']:
    s = isl[isl['bucket']==bkt]
    if len(s) == 0:
        continue
    prem_pct = s['premature'].mean()*100
    avg_cost = s['cost_of_stop'].mean()
    held_pct = s['range_held'].mean()*100 if s['range_held'].notna().any() else 0
    pc = s.loc[s['premature'], 'cost_of_stop'].mean() if s['premature'].any() else 0
    print(f"  {bkt:<12} {len(s):>5} {prem_pct:>11.1f}% {avg_cost:>+10.2f} {held_pct:>11.1f}% {pc:>+20.2f}")

print("\n── option_sl: by leg (CE vs PE) ─────────────────────────────────────────────")
osl = stopped[stopped['stop_type']=='option_sl']
ce_osl = osl[osl['ce_exit_reason']=='option_sl']
pe_osl = osl[osl['pe_exit_reason']=='option_sl']
for label, sub in [('CE option_sl', ce_osl), ('PE option_sl', pe_osl)]:
    if len(sub):
        print(f"  {label}: n={len(sub)}, premature={sub['premature'].mean()*100:.0f}%, avg_cost={sub['cost_of_stop'].mean():+.2f}, range_held%={sub['range_held'].mean()*100:.0f}%")

print("\n── Range held at expiry (stopped weeks only) ────────────────────────────────")
held = stopped[stopped['range_held']==True]
broke = stopped[stopped['range_held']==False]
print(f"  Range HELD at expiry:  n={len(held):>3}, premature={held['premature'].mean()*100:.0f}%, avg_cost={held['cost_of_stop'].mean():+.2f}")
print(f"  Range BROKE at expiry: n={len(broke):>3}, premature={broke['premature'].mean()*100:.0f}%, avg_cost={broke['cost_of_stop'].mean():+.2f}")

print("\n── Total opportunity (all premature stops) ──────────────────────────────────")
prem = stopped[stopped['premature']]
total_cost = prem['cost_of_stop'].sum()
print(f"  Premature stops: {len(prem)} / {len(stopped)} ({len(prem)/len(stopped)*100:.0f}%)")
print(f"  Total cost (pts): {total_cost:+.1f}")
print(f"  Total cost (₹):   ₹{total_cost * 25:+,.0f}")
print(f"  Saved by stop (pts): {stopped[~stopped['premature']]['cost_of_stop'].sum():+.1f}")
print(f"  Saved by stop (₹):   ₹{stopped[~stopped['premature']]['cost_of_stop'].sum()*25:+,.0f}")

# Save
out = os.path.join(REPO, 'research', 'range_detection', 'outputs',
                   'sl_aftermath_analysis.csv')
df.to_csv(out, index=False)
print(f"\nFull dataset saved to {out}")
