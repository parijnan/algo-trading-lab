"""
Session analysis, part 1: how the market itself behaves by time of day, per engine instrument (no strategy involved).

On 15-minute bars (the engines' timeframe), per clock hour and per session bucket: bar range (high-low, percent of the bar's open), absolute close-to-close move, its spread, traded volume,
and each bucket's share of the day's total movement. Also, per day, in which bucket the day's high and low were set, and the evening-versus-day split by year. 15-minute bars are built per
calendar day (nothing crosses the overnight gap) and bars with no trade at all (volume 0) are dropped so Fyers placeholder minutes and Angel One skipped minutes read the same.

    python research/session_analysis/session_market.py
"""

import os

import numpy as np
import pandas as pd

import session_configs as cfg
from session_lib import session_of, two_way_of


def load_prices(instrument: str) -> pd.DataFrame:
    if instrument == 'SILVERMIC':
        from selene_data_loader import load_futures_1min as f
    elif instrument == 'GOLDPETAL':
        from helios_data_loader import load_futures_1min as f
    elif instrument == 'NATGASMINI':
        from typhon_data_loader import load_futures_1min as f
    else:
        from data_loader_fyers import load_futures_1min as g
        f = lambda: g('CRUDEOILM')
    return f()


def bars_15m(df: pd.DataFrame) -> pd.DataFrame:
    """15-minute bars per calendar day with columns open/high/low/close/volume and derived range_pct, move_pct (close vs the previous bar's close, same day only), session labels."""
    d = df[['open', 'high', 'low', 'close', 'volume']].copy()
    d = d[d['volume'] > 0]
    d['bar'] = d.index.floor('15min')
    out = d.groupby('bar').agg(open=('open', 'first'), high=('high', 'max'), low=('low', 'min'), close=('close', 'last'), volume=('volume', 'sum'))
    out['date'] = out.index.normalize()
    out['hour'] = out.index.hour
    out['range_pct'] = (out['high'] - out['low']) / out['open'] * 100
    prev = out.groupby('date')['close'].shift(1)
    out['move_pct'] = (out['close'] - prev) / prev * 100
    out['session'] = out['hour'].map(session_of)
    out['two_way'] = out['hour'].map(two_way_of)
    return out[out['session'] != 'Other']


def bucket_table(b: pd.DataFrame, key: str, instrument: str) -> pd.DataFrame:
    g = b.groupby(key)
    t = pd.DataFrame({'bars': g.size(), 'mean_range_pct': g['range_pct'].mean(), 'median_range_pct': g['range_pct'].median(),
                      'mean_abs_move_pct': g['move_pct'].apply(lambda s: s.abs().mean()), 'move_std_pct': g['move_pct'].std(), 'mean_volume': g['volume'].mean()})
    tot = b.assign(a=b['move_pct'].abs()).groupby(key)['a'].sum()
    t['share_of_abs_move_pct'] = tot / tot.sum() * 100
    rng = g['range_pct'].sum()
    t['share_of_summed_range_pct'] = rng / rng.sum() * 100
    t['share_of_bars_pct'] = t['bars'] / t['bars'].sum() * 100
    t.insert(0, 'instrument', instrument)
    t.index.name = 'bucket'
    return t.reset_index()


def extremes(b: pd.DataFrame, instrument: str) -> dict:
    """Per day: in which two-way bucket the day's highest high and lowest low were set (days with bars in both buckets only)."""
    rows = []
    for date, g in b.groupby('date'):
        if set(g['two_way']) != {'Day', 'Evening'}:
            continue
        rows.append((g.loc[g['high'].idxmax(), 'two_way'], g.loc[g['low'].idxmin(), 'two_way'],
                     (g[g['two_way'] == 'Evening']['high'].max() - g[g['two_way'] == 'Evening']['low'].min()) / g['open'].iloc[0] * 100,
                     (g[g['two_way'] == 'Day']['high'].max() - g[g['two_way'] == 'Day']['low'].min()) / g['open'].iloc[0] * 100))
    r = pd.DataFrame(rows, columns=['high_in', 'low_in', 'evening_range_pct', 'day_range_pct'])
    return {'instrument': instrument, 'days': len(r), 'high_set_in_evening_pct': round((r['high_in'] == 'Evening').mean() * 100, 1), 'low_set_in_evening_pct': round((r['low_in'] == 'Evening').mean() * 100, 1),
            'either_extreme_in_evening_pct': round(((r['high_in'] == 'Evening') | (r['low_in'] == 'Evening')).mean() * 100, 1),
            'mean_evening_range_pct': round(r['evening_range_pct'].mean(), 3), 'mean_day_range_pct': round(r['day_range_pct'].mean(), 3),
            'evening_range_over_day_range': round(r['evening_range_pct'].mean() / r['day_range_pct'].mean(), 2)}


def main():
    os.makedirs(cfg.OUTPUT_DIR, exist_ok=True)
    hourly, sessions, byyear, ext = [], [], [], []
    for name, (ins, _) in cfg.ENGINES.items():
        print(f'{ins}: loading', flush=True)
        b = bars_15m(load_prices(ins))
        print(f'  {len(b):,} traded 15-minute bars, {b.index.min().date()} to {b.index.max().date()}', flush=True)
        hourly.append(bucket_table(b, 'hour', ins))
        sessions.append(bucket_table(b, 'session', ins))
        sessions.append(bucket_table(b, 'two_way', ins).assign(bucket=lambda d: 'ALL ' + d['bucket']))
        for y, gy in b.groupby(b.index.year):
            t = bucket_table(gy, 'two_way', ins)
            t.insert(1, 'year', y)
            byyear.append(t)
        ext.append(extremes(b, ins))
    pd.concat(hourly).to_csv(os.path.join(cfg.OUTPUT_DIR, 'market_by_hour.csv'), index=False)
    pd.concat(sessions).to_csv(os.path.join(cfg.OUTPUT_DIR, 'market_by_session.csv'), index=False)
    pd.concat(byyear).to_csv(os.path.join(cfg.OUTPUT_DIR, 'market_by_year.csv'), index=False)
    pd.DataFrame(ext).to_csv(os.path.join(cfg.OUTPUT_DIR, 'market_day_extremes.csv'), index=False)
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', None)
    s = pd.concat(sessions)
    print('\n=== by session (15-minute bars) ===')
    print(s[['instrument', 'bucket', 'bars', 'mean_range_pct', 'mean_abs_move_pct', 'mean_volume', 'share_of_abs_move_pct', 'share_of_bars_pct']].round(3).to_string(index=False))
    print('\n=== where the day high / low is set ===')
    print(pd.DataFrame(ext).to_string(index=False))


if __name__ == '__main__':
    main()
