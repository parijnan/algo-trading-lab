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
"""

import os
import sys
import pandas as pd

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))
sys.path.insert(0, BASE_DIR)

from resample import load_daily_extended
from range_detector_pa import compute_pa_ranges

TRADE_SUMMARY_FILE = os.path.join(REPO_ROOT, 'athena_backtest', 'data', 'trade_summary.csv')
OUTPUT_CSV         = os.path.join(BASE_DIR, 'outputs', 'athena_annotated.csv')

MIN_RANGE_BARS   = 3
BREAKOUT_CONFIRM = 2


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

        # Find the bar on or before entry_date
        pos = price_idx.searchsorted(entry_date, side='right') - 1

        if pos < 0:
            ann_rows.append(_null_ann())
            continue

        bar   = result.iloc[pos]
        ep_id = int(bar['episode_id'])
        if ep_id < 0:
            ann_rows.append(_null_ann())
            continue

        ep          = ep_dict[ep_id]
        bars_into   = pos - ep['start_idx'] + 1
        committed   = bars_into > BREAKOUT_CONFIRM
        established = committed and (bars_into >= MIN_RANGE_BARS)

        rh = float(bar['range_high'])
        rl = float(bar['range_low'])
        rm = float(bar['range_mid'])

        spot_pct = (round(100 * (float(entry_spot) - rl) / (rh - rl), 1)
                    if entry_spot and rh > rl else None)
        width_pct = round((rh - rl) / rm * 100, 2) if rm > 0 else None

        ann_rows.append({
            'ep_direction':      ep['direction'],
            'ep_bars_into':      int(bars_into),
            'ep_committed':      committed,
            'ep_established':    established,
            'ep_entry_spot_pct': spot_pct,
            'ep_range_high':     round(rh, 2),
            'ep_range_low':      round(rl, 2),
            'ep_range_mid':      round(rm, 2),
            'ep_width_pct':      width_pct,
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

    print()


def _null_ann() -> dict:
    return {k: None for k in [
        'ep_direction', 'ep_bars_into', 'ep_committed', 'ep_established',
        'ep_entry_spot_pct', 'ep_range_high', 'ep_range_low',
        'ep_range_mid', 'ep_width_pct',
    ]}


if __name__ == '__main__':
    main()
