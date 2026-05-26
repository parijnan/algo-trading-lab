"""
annotate_athena.py — Tag each Athena trade with PA range state at entry.

Loads daily Nifty OHLC resampled from 1-min history (2019+), runs the PA range
detector from the first available bar, then joins the Athena trade summary on
entry date.

Usage:
    python annotate_athena.py

Output:
    outputs/athena_annotated.csv   — trade summary + range annotation columns
    Console summary table          — win%/avg P&L sliced by direction, state,
                                     spot position, and range width

Added columns (ep_ prefix):
    ep_direction      'up' | 'down' | 'initial'
    ep_bars_into      bars elapsed in current episode at entry
    ep_committed      True if episode was committed (confirmed) by entry date
    ep_established    True if committed AND bars_into >= min_range_bars
    ep_entry_spot_pct entry spot position within range (0=at low, 100=at high)
    ep_range_high     range upper bound at entry bar
    ep_range_low      range lower bound at entry bar
    ep_range_mid      midpoint at entry bar
    ep_width_pct      range width as % of midpoint at entry bar
    key_dist_pct      directional distance from key level:
                        down → (range_high − spot) / width × 100
                        up   → (spot − range_low)  / width × 100
                        initial → None
    vix_st_daily      VIX daily Supertrend direction at entry ('up'/'down')
                        (uses previous day's completed bar; p=7, m=3.0)
    vix_st_75m        VIX 75-min Supertrend direction at entry ('up'/'down')
                        (09:15→10:29 bar on entry day; p=10, m=3.0)
    vix_st_signal     'both_up' | 'mixed' | 'both_down'
    vix_bb_pct        VIX %B — position within 20-day Bollinger Bands at entry
                        (0=lower band, 1=upper band; can exceed [0,1])
                        (uses previous day's completed daily bar)
    vix_bb_zone       'above_upper' (>1) | 'upper_zone' (0.7–1) | 'mid_zone' (0.3–0.7)
                        | 'lower_zone' (0–0.3) | 'below_lower' (<0)
"""

import os
import sys
import pandas as pd

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT  = os.path.dirname(os.path.dirname(BASE_DIR))
APOLLO_DIR = os.path.join(REPO_ROOT, 'apollo_backtest')
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, APOLLO_DIR)

from resample import load_daily_extended, resample_daily, resample_intraday
from range_detector_pa import compute_pa_ranges
from technical_indicators import SupertrendIndicator

TRADE_SUMMARY_FILE = os.path.join(REPO_ROOT, 'athena_backtest', 'data', 'trade_summary.csv')
OUTPUT_CSV         = os.path.join(BASE_DIR, 'outputs', 'athena_annotated.csv')
VIX_1MIN_FILE      = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'indices', 'india_vix.csv')

MIN_RANGE_BARS   = 3
BREAKOUT_CONFIRM = 2

VIX_ST_DAILY_PERIOD = 7
VIX_ST_DAILY_MULT   = 3.0
VIX_ST_75M_PERIOD   = 10
VIX_ST_75M_MULT     = 3.0

VIX_BB_PERIOD = 20
VIX_BB_STD    = 2.0


# ---------------------------------------------------------------------------
# VIX Supertrend helpers
# ---------------------------------------------------------------------------

def _bb_zone(pct) -> str | None:
    if pct is None or (isinstance(pct, float) and pd.isna(pct)):
        return None
    if pct > 1.0:  return 'above_upper'
    if pct > 0.7:  return 'upper_zone'
    if pct > 0.3:  return 'mid_zone'
    if pct >= 0.0: return 'lower_zone'
    return 'below_lower'


def _load_vix_supertrends():
    """
    Returns (daily_st_df, m75_day_df):
      daily_st_df : full daily VIX DataFrame, indexed by midnight Timestamp.
                    Columns: Close, st_dir, bb_pct, bb_zone.
      m75_day_df  : 75-min VIX ST filtered to 09:15 bars, re-indexed by date (midnight).
                    Columns: Close, Supertrend, st_dir.
    """
    vix_1min = pd.read_csv(VIX_1MIN_FILE)
    vix_1min['time_stamp'] = (pd.to_datetime(vix_1min['time_stamp'], utc=False)
                               .dt.tz_localize(None))
    vix_1min = vix_1min.sort_values('time_stamp').reset_index(drop=True)

    def _cap_cols(df):
        return df.rename(columns={'open': 'Open', 'high': 'High',
                                   'low': 'Low', 'close': 'Close'})

    def _add_dir(df):
        df = df.copy()
        df['st_dir'] = df.apply(
            lambda r: ('up' if r['Close'] >= r['Supertrend'] else 'down')
                      if pd.notna(r['Supertrend']) else None,
            axis=1,
        )
        return df

    # Daily — Supertrend + Bollinger Bands
    vix_daily = _cap_cols(resample_daily(vix_1min))
    daily_st = _add_dir(
        SupertrendIndicator(VIX_ST_DAILY_PERIOD, VIX_ST_DAILY_MULT).calculate(vix_daily)
    )
    sma  = daily_st['Close'].rolling(VIX_BB_PERIOD).mean()
    std  = daily_st['Close'].rolling(VIX_BB_PERIOD).std()
    bbu  = sma + VIX_BB_STD * std
    bbl  = sma - VIX_BB_STD * std
    daily_st['bb_pct']  = (daily_st['Close'] - bbl) / (bbu - bbl)
    daily_st['bb_zone'] = daily_st['bb_pct'].apply(_bb_zone)

    # 75-min — keep only the 09:15 bar (covers 09:15→10:29, complete before 10:30 entry)
    vix_75m = _cap_cols(resample_intraday(vix_1min, 75))
    m75_st = _add_dir(
        SupertrendIndicator(VIX_ST_75M_PERIOD, VIX_ST_75M_MULT).calculate(vix_75m)
    )
    entry_bar_time = pd.Timestamp('09:15').time()
    m75_day = m75_st[m75_st.index.time == entry_bar_time].copy()
    m75_day.index = m75_day.index.normalize()

    return daily_st, m75_day


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _stats(df: pd.DataFrame) -> dict:
    n    = len(df)
    wins = (df['total_pl_points'] > 0).sum()
    return {
        'n':      n,
        'win':    round(wins / n * 100, 1) if n else 0.0,
        'avg':    round(df['total_pl_points'].mean(),   2) if n else 0.0,
        'median': round(df['total_pl_points'].median(), 2) if n else 0.0,
        'total':  round(df['total_pl_points'].sum(),    2) if n else 0.0,
    }


def _print_table(title: str, rows: list):
    hdr = f"  {'Group':<24} {'N':>5} {'Win%':>6} {'Avg':>8} {'Median':>8} {'Total':>10}"
    sep = '  ' + '-' * 65
    print(f'\n{title}')
    print(hdr)
    print(sep)
    for label, subset in rows:
        s = _stats(subset)
        print(f"  {label:<24} {s['n']:>5} {s['win']:>5.1f}%"
              f" {s['avg']:>+8.2f} {s['median']:>+8.2f} {s['total']:>+10.2f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # --- VIX Supertrends ---
    print('Loading VIX 1-min data and computing dual-TF Supertrends…')
    vix_daily_st, vix_75m_day = _load_vix_supertrends()
    print(f'  Daily VIX ST: {len(vix_daily_st)} bars  '
          f'{vix_daily_st.index[0].date()} → {vix_daily_st.index[-1].date()}')
    print(f'  75-min VIX ST: {len(vix_75m_day)} entry-day bars')

    # --- Price data ---
    print('Loading daily data from 1-min history…')
    df_price = load_daily_extended()
    print(f'  {len(df_price)} bars  '
          f'{df_price.index[0].date()} → {df_price.index[-1].date()}')

    # --- PA ranges ---
    print(f'Computing PA ranges  '
          f'(min_range_bars={MIN_RANGE_BARS}, breakout_confirm={BREAKOUT_CONFIRM})…')
    result, episodes = compute_pa_ranges(
        df_price, start_idx=0,
        min_range_bars=MIN_RANGE_BARS,
        breakout_confirm=BREAKOUT_CONFIRM,
    )
    ep_dict = {ep['episode_id']: ep for ep in episodes}
    print(f'  {len(episodes)} episodes total')

    # --- Athena trades ---
    trades = pd.read_csv(TRADE_SUMMARY_FILE)
    trades['entry_date'] = pd.to_datetime(trades['entry_time']).dt.normalize()
    print(f'Athena trades: {len(trades)}  '
          f'({trades["entry_date"].min().date()} → {trades["entry_date"].max().date()})')

    price_idx = result.index   # DatetimeIndex of daily bars

    # --- Annotate ---
    ann_rows = []
    for _, trade in trades.iterrows():
        entry_date  = trade['entry_date']
        entry_spot  = trade.get('entry_spot')

        # VIX daily signals: most recent completed daily bar before entry_date
        pos_d = vix_daily_st.index.searchsorted(entry_date, side='left') - 1
        vix_daily  = vix_daily_st.iloc[pos_d]['st_dir']  if pos_d >= 0 else None
        _bb_pct_v  = vix_daily_st.iloc[pos_d]['bb_pct']  if pos_d >= 0 else None
        _bb_zone_v = vix_daily_st.iloc[pos_d]['bb_zone'] if pos_d >= 0 else None

        # VIX 75-min ST: 09:15→10:29 bar on entry_date (complete before 10:30 entry)
        vix_75m = (vix_75m_day.loc[entry_date, 'st_dir']
                   if entry_date in vix_75m_day.index else None)

        # Combined signal
        if vix_daily and vix_75m:
            if vix_daily == 'up' and vix_75m == 'up':
                vix_signal = 'both_up'
            elif vix_daily == 'down' and vix_75m == 'down':
                vix_signal = 'both_down'
            else:
                vix_signal = 'mixed'
        else:
            vix_signal = None

        # Find the bar on or before entry_date
        pos = price_idx.searchsorted(entry_date, side='right') - 1

        if pos < 0:
            ann_rows.append(_null_ann(vix_daily, vix_75m, vix_signal, _bb_pct_v, _bb_zone_v))
            continue

        bar   = result.iloc[pos]
        ep_id = int(bar['episode_id'])
        if ep_id < 0:
            ann_rows.append(_null_ann(vix_daily, vix_75m, vix_signal, _bb_pct_v, _bb_zone_v))
            continue

        ep          = ep_dict[ep_id]
        bars_into   = pos - ep['start_idx'] + 1
        committed   = bars_into > BREAKOUT_CONFIRM
        established = committed and (bars_into >= MIN_RANGE_BARS)
        direction   = ep['direction']

        rh = float(bar['range_high'])
        rl = float(bar['range_low'])
        rm = float(bar['range_mid'])

        spot_pct = (round(100 * (float(entry_spot) - rl) / (rh - rl), 1)
                    if entry_spot and rh > rl else None)
        width_pct = round((rh - rl) / rm * 100, 2) if rm > 0 else None

        # Directional distance from the key level
        if direction in ('up', 'down') and entry_spot and rh > rl:
            width = rh - rl
            spot_f = float(entry_spot)
            key_dist_pct = round(
                ((rh - spot_f) / width * 100) if direction == 'down'
                else ((spot_f - rl) / width * 100),
                1,
            )
        else:
            key_dist_pct = None

        ann_rows.append({
            'ep_direction':      direction,
            'ep_bars_into':      int(bars_into),
            'ep_committed':      committed,
            'ep_established':    established,
            'ep_entry_spot_pct': spot_pct,
            'ep_range_high':     round(rh, 2),
            'ep_range_low':      round(rl, 2),
            'ep_range_mid':      round(rm, 2),
            'ep_width_pct':      width_pct,
            'key_dist_pct':      key_dist_pct,
            'vix_st_daily':      vix_daily,
            'vix_st_75m':        vix_75m,
            'vix_st_signal':     vix_signal,
            'vix_bb_pct':        round(float(_bb_pct_v), 4) if _bb_pct_v is not None and not pd.isna(_bb_pct_v) else None,
            'vix_bb_zone':       _bb_zone_v,
        })

    out = pd.concat([trades.reset_index(drop=True),
                     pd.DataFrame(ann_rows)], axis=1)
    out.to_csv(OUTPUT_CSV, index=False)
    print(f'Saved: {OUTPUT_CSV}')

    # --- Summary ---
    tagged  = out[out['ep_direction'].notna()].copy()
    missing = len(out) - len(tagged)
    if missing:
        print(f'\nNote: {missing} trades had no range data (before price history start)')

    overall = _stats(tagged)
    print(f'\n{"=" * 67}')
    print(f'RANGE ANNOTATION SUMMARY  —  {len(tagged)} trades tagged')
    print(f'{"=" * 67}')
    print(f"  Overall: win% {overall['win']:.1f}%  |  "
          f"avg {overall['avg']:+.2f} pts  |  total {overall['total']:+.2f} pts")

    _print_table('By direction at entry:', [
        ('up',      tagged[tagged['ep_direction'] == 'up']),
        ('down',    tagged[tagged['ep_direction'] == 'down']),
        ('initial', tagged[tagged['ep_direction'] == 'initial']),
    ])

    _print_table('By episode state at entry:', [
        ('established',       tagged[tagged['ep_established'] == True]),
        ('committed only',    tagged[(tagged['ep_committed'] == True)
                                     & (tagged['ep_established'] == False)]),
        ('not yet committed', tagged[tagged['ep_committed'] == False]),
    ])

    _print_table('By entry spot position in range:', [
        ('lower third  (0–33%)',   tagged[tagged['ep_entry_spot_pct'] <= 33]),
        ('middle third (34–66%)',  tagged[(tagged['ep_entry_spot_pct'] > 33)
                                          & (tagged['ep_entry_spot_pct'] <= 66)]),
        ('upper third  (67–100%)', tagged[tagged['ep_entry_spot_pct'] > 66]),
    ])

    q33 = tagged['ep_width_pct'].quantile(0.33)
    q67 = tagged['ep_width_pct'].quantile(0.67)
    _print_table(f'By range width at entry  (P33={q33:.1f}%  P67={q67:.1f}%):', [
        (f'narrow  (<{q33:.1f}%)',             tagged[tagged['ep_width_pct'] < q33]),
        (f'medium  ({q33:.1f}%–{q67:.1f}%)',   tagged[(tagged['ep_width_pct'] >= q33)
                                                       & (tagged['ep_width_pct'] < q67)]),
        (f'wide    (≥{q67:.1f}%)',             tagged[tagged['ep_width_pct'] >= q67]),
    ])

    vix_tagged = out[out['vix_st_signal'].notna()].copy()
    _print_table('By VIX dual-TF Supertrend signal:', [
        ('both_up',   vix_tagged[vix_tagged['vix_st_signal'] == 'both_up']),
        ('mixed',     vix_tagged[vix_tagged['vix_st_signal'] == 'mixed']),
        ('both_down', vix_tagged[vix_tagged['vix_st_signal'] == 'both_down']),
    ])

    up_bias = tagged[tagged['ep_direction'] == 'up']
    _print_table('Up-biased × VIX signal:', [
        ('up + both_up',   up_bias[up_bias['vix_st_signal'] == 'both_up']),
        ('up + mixed',     up_bias[up_bias['vix_st_signal'] == 'mixed']),
        ('up + both_down', up_bias[up_bias['vix_st_signal'] == 'both_down']),
    ])

    vix_bb = out[out['vix_bb_zone'].notna()].copy()
    _print_table('By VIX BB zone at entry:', [
        ('above_upper (>1.0)',    vix_bb[vix_bb['vix_bb_zone'] == 'above_upper']),
        ('upper_zone  (0.7–1.0)', vix_bb[vix_bb['vix_bb_zone'] == 'upper_zone']),
        ('mid_zone    (0.3–0.7)', vix_bb[vix_bb['vix_bb_zone'] == 'mid_zone']),
        ('lower_zone  (0.0–0.3)', vix_bb[vix_bb['vix_bb_zone'] == 'lower_zone']),
        ('below_lower (<0.0)',    vix_bb[vix_bb['vix_bb_zone'] == 'below_lower']),
    ])

    _print_table('Up-biased × VIX ST × BB zone  (surgical filter):', [
        ('up + both_up + mid_zone',   up_bias[(up_bias['vix_st_signal'] == 'both_up')
                                              & (up_bias['vix_bb_zone'] == 'mid_zone')]),
        ('up + both_up + upper_zone', up_bias[(up_bias['vix_st_signal'] == 'both_up')
                                              & (up_bias['vix_bb_zone'] == 'upper_zone')]),
        ('up + both_up + other',      up_bias[(up_bias['vix_st_signal'] == 'both_up')
                                              & (~up_bias['vix_bb_zone'].isin(['mid_zone','upper_zone']))]),
        ('up + mixed + mid_zone',     up_bias[(up_bias['vix_st_signal'] == 'mixed')
                                              & (up_bias['vix_bb_zone'] == 'mid_zone')]),
        ('up + both_down + mid_zone', up_bias[(up_bias['vix_st_signal'] == 'both_down')
                                              & (up_bias['vix_bb_zone'] == 'mid_zone')]),
    ])

    print()


def _null_ann(vix_daily=None, vix_75m=None, vix_signal=None,
              bb_pct=None, bb_zone=None) -> dict:
    return {
        'ep_direction':      None,
        'ep_bars_into':      None,
        'ep_committed':      None,
        'ep_established':    None,
        'ep_entry_spot_pct': None,
        'ep_range_high':     None,
        'ep_range_low':      None,
        'ep_range_mid':      None,
        'ep_width_pct':      None,
        'key_dist_pct':      None,
        'vix_st_daily':      vix_daily,
        'vix_st_75m':        vix_75m,
        'vix_st_signal':     vix_signal,
        'vix_bb_pct':        bb_pct,
        'vix_bb_zone':       bb_zone,
    }


if __name__ == '__main__':
    main()
