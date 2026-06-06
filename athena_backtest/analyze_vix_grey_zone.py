"""
analyze_vix_grey_zone.py — VIX grey zone research

When VIX is in the 15–17 band at Athena/Artemis entry time (Wednesday 10:30),
does it tend to rise or fall over the following week? Can any simple signals
at entry time predict the direction?

No strategy P&L — pure VIX behaviour analysis.

Questions:
  1. How often does VIX land in 15–17 on a Wednesday entry? (frequency)
  2. Base rate: when VIX is 15–17, does it rise or fall over the next 5 days?
  3. Conditional: does recent VIX direction at entry predict subsequent direction?
  4. How persistent / sticky is VIX in the grey zone?

Usage:
    python athena_backtest/analyze_vix_grey_zone.py
"""

import os
import sys
import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

VIX_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          '..', 'data_pipeline', 'data', 'indices', 'india_vix.csv')
ENTRY_TIME = '10:30'
GREY_LOW   = 15.0
GREY_HIGH  = 17.0


def load_vix() -> pd.DataFrame:
    df = pd.read_csv(VIX_FILE, parse_dates=['time_stamp'])
    df['time_stamp'] = pd.to_datetime(df['time_stamp'], utc=False).dt.tz_localize(None)
    df = df.set_index('time_stamp').sort_index()
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    return df


def make_wednesday_series(vix_1m: pd.DataFrame) -> pd.DataFrame:
    """Extract one row per Wednesday at the entry time (or nearest available bar)."""
    entry_bars = vix_1m.between_time(ENTRY_TIME, ENTRY_TIME)
    weds = entry_bars[entry_bars.index.dayofweek == 2].copy()  # 2 = Wednesday
    weds = weds.reset_index()
    weds.columns = ['entry_ts', 'open', 'high', 'low', 'close', 'volume', 'oi']
    weds = weds[['entry_ts', 'close']].rename(columns={'close': 'vix_entry'})
    return weds.reset_index(drop=True)


def compute_signals(weds: pd.DataFrame, vix_daily: pd.DataFrame) -> pd.DataFrame:
    """
    Add direction signals computed purely from data prior to each entry:
      - vix_1d_chg    : VIX change from prior day's close to entry
      - vix_5d_chg    : VIX change over prior 5 trading days
      - vix_10d_chg   : VIX change over prior 10 trading days
      - vix_ma20      : 20-day rolling mean of VIX at prior close
      - vix_above_ma  : entry VIX > vix_ma20
      - bb_pct        : (vix - lower) / (upper - lower), 20-day 2σ BB %B
      - bb_zone       : lower_zone (<0.3) / mid_zone / upper_zone (>0.7) / above_upper (>1.0)
      - trend_1d_up   : prior daily candle was up (close > open)
      - momentum_dir  : 'up' if vix_5d_chg > 0 else 'down'
    """
    rows = []
    for _, r in weds.iterrows():
        date  = r['entry_ts'].date()
        prior = vix_daily[vix_daily.index.date < date]

        if len(prior) < 20:
            rows.append({})
            continue

        close_series = prior['close']
        ma20         = close_series.tail(20).mean()
        std20        = close_series.tail(20).std()
        upper        = ma20 + 2 * std20
        lower        = ma20 - 2 * std20
        bb_pct       = (r['vix_entry'] - lower) / (upper - lower) if (upper - lower) > 0 else 0.5

        if   bb_pct > 1.0: bb_zone = 'above_upper'
        elif bb_pct > 0.7: bb_zone = 'upper_zone'
        elif bb_pct > 0.3: bb_zone = 'mid_zone'
        else:              bb_zone = 'lower_zone'

        prev1  = float(close_series.iloc[-1])    if len(close_series) >= 1  else np.nan
        prev5  = float(close_series.iloc[-5])    if len(close_series) >= 5  else np.nan
        prev10 = float(close_series.iloc[-10])   if len(close_series) >= 10 else np.nan

        trend_1d_up   = bool(prior.iloc[-1]['close'] > prior.iloc[-1]['open'])

        rows.append({
            'vix_1d_chg':   round(r['vix_entry'] - prev1,  2) if not np.isnan(prev1)  else np.nan,
            'vix_5d_chg':   round(r['vix_entry'] - prev5,  2) if not np.isnan(prev5)  else np.nan,
            'vix_10d_chg':  round(r['vix_entry'] - prev10, 2) if not np.isnan(prev10) else np.nan,
            'vix_ma20':     round(ma20,   2),
            'vix_above_ma': r['vix_entry'] > ma20,
            'bb_pct':       round(bb_pct, 3),
            'bb_zone':      bb_zone,
            'trend_1d_up':  trend_1d_up,
            'momentum_dir': 'up' if (not np.isnan(prev5) and r['vix_entry'] > prev5) else 'down',
        })

    sig_df = pd.DataFrame(rows)
    return pd.concat([weds, sig_df], axis=1)


def add_outcomes(weds: pd.DataFrame) -> pd.DataFrame:
    """Add VIX level at the next Wednesday entry (= end of the trade window)."""
    df = weds.copy().reset_index(drop=True)
    df['vix_next'] = df['vix_entry'].shift(-1)
    df['vix_chg']  = (df['vix_next'] - df['vix_entry']).round(2)
    df['vix_rose'] = df['vix_chg'] > 0
    return df


def section(title: str):
    print(f"\n{'─'*64}")
    print(f"{title}")
    print(f"{'─'*64}")


def pct(n, d):
    return f"{n/d*100:.0f}%" if d else "—"


def show_bucket(label: str, grp: pd.DataFrame):
    if grp.empty:
        return
    n        = len(grp)
    rose     = grp['vix_rose'].sum()
    fell     = n - rose
    avg_chg  = grp['vix_chg'].mean()
    med_chg  = grp['vix_chg'].median()
    print(f"  {label:<28}  n={n:>3}  "
          f"rose={rose:>3} ({pct(rose,n):>4})  "
          f"fell={fell:>3} ({pct(fell,n):>4})  "
          f"avg_Δ={avg_chg:>+5.2f}  med_Δ={med_chg:>+5.2f}")


def main():
    print("=" * 64)
    print("VIX GREY ZONE RESEARCH")
    print(f"Grey zone: {GREY_LOW}–{GREY_HIGH}  |  Entry: Wednesday {ENTRY_TIME}")
    print("=" * 64)

    vix_1m    = load_vix()
    vix_daily = vix_1m.resample('D').agg(
        open=('close','first'), high=('close','max'),
        low=('close','min'),   close=('close','last')
    ).dropna(subset=['close'])

    weds = make_wednesday_series(vix_1m)
    weds = compute_signals(weds, vix_daily)
    weds = add_outcomes(weds)
    weds = weds.dropna(subset=['vix_entry', 'vix_chg'])

    print(f"\nWednesdays in dataset: {len(weds)}  "
          f"({weds['entry_ts'].min().date()} → {weds['entry_ts'].max().date()})")

    # ------------------------------------------------------------------
    # 1. VIX distribution at entry
    # ------------------------------------------------------------------
    section("1. VIX DISTRIBUTION AT WEDNESDAY ENTRY")
    bins = [0, 12, 14, 15, 17, 18, 20, 25, 99]
    labs = ['<12', '12–14', '14–15', '15–17', '17–18', '18–20', '20–25', '>25']
    weds['vix_bin'] = pd.cut(weds['vix_entry'], bins=bins, labels=labs)
    total = len(weds)
    print(f"\n  {'Band':>8}   {'n':>4}   {'%':>5}   rose next week")
    for band, grp in weds.groupby('vix_bin', observed=True):
        if grp.empty: continue
        rose = grp['vix_rose'].sum()
        print(f"  {str(band):>8}   {len(grp):>4}   {pct(len(grp),total):>5}   "
              f"{pct(rose,len(grp)):>5} rose")

    grey = weds[(weds['vix_entry'] >= GREY_LOW) & (weds['vix_entry'] < GREY_HIGH)].copy()
    print(f"\n  Grey zone ({GREY_LOW}–{GREY_HIGH}): {len(grey)} of {total} Wednesdays "
          f"({pct(len(grey),total)})")

    # ------------------------------------------------------------------
    # 2. Base rate in grey zone
    # ------------------------------------------------------------------
    section(f"2. BASE RATE IN GREY ZONE ({GREY_LOW}–{GREY_HIGH})")
    if grey.empty:
        print("  No data in grey zone.")
        return

    show_bucket("All grey-zone entries", grey)

    print(f"\n  VIX change distribution (grey zone):")
    for lo, hi, lbl in [(-99,-2,'< -2'), (-2,-0.5,'-2 to -0.5'), (-0.5,0.5,'±0.5'),
                        (0.5,2,'+0.5 to +2'), (2,99,'> +2')]:
        grp = grey[(grey['vix_chg'] > lo) & (grey['vix_chg'] <= hi)]
        print(f"    ΔVIX_week {lbl:>12}: {len(grp):>3} ({pct(len(grp),len(grey)):>4})")

    # ------------------------------------------------------------------
    # 3. Conditional: do direction signals predict subsequent VIX move?
    # ------------------------------------------------------------------
    section("3. SIGNAL CONDITIONING IN GREY ZONE")

    print(f"\n  5-day momentum (vix_5d_chg > 0 = was rising):")
    show_bucket("  momentum UP   (VIX rose past 5d)", grey[grey['momentum_dir']=='up'])
    show_bucket("  momentum DOWN (VIX fell past 5d)", grey[grey['momentum_dir']=='down'])

    print(f"\n  Prior daily candle direction:")
    show_bucket("  yesterday UP  (VIX close > open)", grey[grey['trend_1d_up']==True])
    show_bucket("  yesterday DOWN",                    grey[grey['trend_1d_up']==False])

    print(f"\n  Position vs 20-day MA:")
    show_bucket("  VIX above 20d MA", grey[grey['vix_above_ma']==True])
    show_bucket("  VIX below 20d MA", grey[grey['vix_above_ma']==False])

    print(f"\n  BB zone:")
    for zone in ['above_upper','upper_zone','mid_zone','lower_zone']:
        show_bucket(f"  {zone}", grey[grey['bb_zone']==zone])

    print(f"\n  1-day VIX change (momentum_intraday):")
    show_bucket("  VIX rose today   (1d_chg > 0)",  grey[grey['vix_1d_chg']  > 0])
    show_bucket("  VIX fell today   (1d_chg < 0)",  grey[grey['vix_1d_chg'] <= 0])

    # Combined: momentum + BB zone
    print(f"\n  Combined: 5d momentum + BB zone (best vs worst combos):")
    grey['combo'] = grey['momentum_dir'] + '_' + grey['bb_zone']
    for combo, grp in grey.groupby('combo', observed=True):
        if len(grp) >= 5:
            show_bucket(f"  {combo}", grp)

    # ------------------------------------------------------------------
    # 4. Skip-zone P&L impact estimate
    # ------------------------------------------------------------------
    section("4. SKIP FREQUENCY — how often grey zone fires per year")
    weds['year'] = weds['entry_ts'].dt.year
    grey['year'] = grey['entry_ts'].dt.year
    print(f"\n  {'Year':>6}  {'total Weds':>10}  {'grey zone':>10}  {'%':>5}")
    for yr, grp in weds.groupby('year'):
        g = grey[grey['year']==yr]
        print(f"  {yr:>6}  {len(grp):>10}  {len(g):>10}  {pct(len(g),len(grp)):>5}")

    # ------------------------------------------------------------------
    # 5. VIX persistence — how long does grey zone last?
    # ------------------------------------------------------------------
    section("5. VIX PERSISTENCE — after grey zone entry, where is VIX next week?")
    print(f"\n  Of {len(grey)} grey-zone Wednesdays, next-week VIX landed in:")
    bins2 = [0, 13, 15, 17, 19, 21, 99]
    labs2 = ['<13','13–15','15–17','17–19','19–21','>21']
    grey['next_bin'] = pd.cut(grey['vix_next'], bins=bins2, labels=labs2)
    for band, grp in grey.groupby('next_bin', observed=True):
        if grp.empty: continue
        print(f"    {str(band):>8}: {len(grp):>3} ({pct(len(grp),len(grey)):>4})")

    print(f"\n  i.e. stayed in grey zone: "
          f"{len(grey[(grey['vix_next']>=GREY_LOW)&(grey['vix_next']<GREY_HIGH)])} "
          f"({pct(len(grey[(grey['vix_next']>=GREY_LOW)&(grey['vix_next']<GREY_HIGH)]),len(grey))})")

    print(f"\n{'─'*64}")
    print("DONE")
    print(f"{'─'*64}")


if __name__ == "__main__":
    main()
