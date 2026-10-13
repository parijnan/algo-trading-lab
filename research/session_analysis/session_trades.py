"""
Session analysis, part 2: how the engines' trades fare by time of day (backtest trades at the live configs; Prometheus on its Fyers-history track).

Per engine and clock bucket: entries (count, win rate, mean and total result, profit factor, stop-out rate), exits (the same by the bucket the trade ended in, with the exit-reason mix), the
entry-by-exit bucket matrix, hold time, and WHERE THE PROFIT IS EARNED: each trade's result is split over the minutes it was held and booked to the bucket of the bar it was earned in (plus the
overnight gap), so a trade entered in the evening and closed next morning shows what the evening part and the morning part each contributed. Results are percent of entry price per lot,
so engines compare on one footing. Bucket tables flag buckets with fewer than MIN_TRADES_REPORT trades.

    python research/session_analysis/session_trades.py
"""

import os

import numpy as np
import pandas as pd

import session_configs as cfg
from session_lib import attribute_path, session_of, two_way_of
from session_market import load_prices


def load_trades() -> pd.DataFrame:
    d = pd.read_csv(cfg.LEDGER_CSV, parse_dates=['entry_ts', 'exit_ts'])
    lots = np.where(d['engine'] == 'Prometheus', 2.0, 1.0)                       # Prometheus's recorded points are the sum of two lots
    d['pct'] = d['pts'] / lots / d['entry'] * 100
    d['sign'] = np.where(d['direction'] == 'bullish', 1.0, -1.0)
    d['entry_hour'], d['exit_hour'] = d['entry_ts'].dt.hour, d['exit_ts'].dt.hour
    d['entry_session'], d['exit_session'] = d['entry_hour'].map(session_of), d['exit_hour'].map(session_of)
    d['entry_two'], d['exit_two'] = d['entry_hour'].map(two_way_of), d['exit_hour'].map(two_way_of)
    d['hold_h'] = (d['exit_ts'] - d['entry_ts']).dt.total_seconds() / 3600
    d['overnight'] = d['entry_ts'].dt.normalize() != d['exit_ts'].dt.normalize()
    d['period'] = np.where(d['entry_ts'] < pd.Timestamp(cfg.SPLIT_DATE), 'before 2026', '2026')
    d['stopped'] = d['reason'].str.contains('stop')
    return d


def summarise(g: pd.DataFrame) -> pd.Series:
    w, l = g.loc[g['pct'] > 0, 'pct'].sum(), -g.loc[g['pct'] < 0, 'pct'].sum()
    return pd.Series({'trades': len(g), 'win_pct': round((g['pct'] > 0).mean() * 100, 1), 'mean_pct': round(g['pct'].mean(), 3), 'median_pct': round(g['pct'].median(), 3),
                      'total_pct': round(g['pct'].sum(), 1), 'profit_factor': round(w / l, 2) if l else np.nan, 'stop_out_pct': round(g['stopped'].mean() * 100, 1),
                      'mean_hold_h': round(g['hold_h'].mean(), 1), 'thin': len(g) < cfg.MIN_TRADES_REPORT})


def table(d: pd.DataFrame, key: str) -> pd.DataFrame:
    t = d.groupby(['engine', key]).apply(summarise, include_groups=False).reset_index().rename(columns={key: 'bucket'})
    return t


def attribution(d: pd.DataFrame) -> tuple:
    """Per-trade pieces booked to buckets, in percent of entry per lot. Returns (pieces frame, per-trade residual frame)."""
    pieces, resid = [], []
    prom = pd.read_csv(cfg.PROMETHEUS_BESPOKE_CSV, parse_dates=['entry_ts', 'lot1_exit_ts', 'lot2_exit_ts'])
    for name, (ins, _) in cfg.ENGINES.items():
        px = load_prices(ins)
        px = px[~px.index.duplicated(keep='first')].sort_index()
        print(f'  {name}: attributing', flush=True)
        if name == 'Prometheus':
            for r in prom.itertuples():
                s = 1.0 if r.direction == 'bullish' else -1.0
                tot_piece = 0.0
                for lot, ts, xp in ((1, r.lot1_exit_ts, r.lot1_exit_price), (2, r.lot2_exit_ts, r.lot2_exit_price)):
                    bars = px.loc[r.entry_ts.floor('min'):ts.floor('min')]
                    if len(bars) < 1:
                        continue
                    p = attribute_path(bars, r.entry_price, xp, s, weight=0.5)
                    p['pct'] = p['pts'] / r.entry_price * 100
                    p['engine'], p['trade_id'] = name, r.trade_id
                    pieces.append(p)
                    tot_piece += p['pts'].sum()
                resid.append((name, r.trade_id, tot_piece - r.total_pnl_points / 2))
            continue
        for t in d[d['engine'] == name].itertuples():
            bars = px.loc[t.entry_ts.floor('min'):t.exit_ts.floor('min')]
            if len(bars) < 1:
                continue
            exit_px = t.entry + t.sign * t.pts
            p = attribute_path(bars, t.entry, exit_px, t.sign)
            p['pct'] = p['pts'] / t.entry * 100
            p['engine'], p['trade_id'] = name, t.trade_id
            pieces.append(p)
            resid.append((name, t.trade_id, p['pts'].sum() - t.pts))
    return pd.concat(pieces, ignore_index=True), pd.DataFrame(resid, columns=['engine', 'trade_id', 'residual_pts'])


def main():
    os.makedirs(cfg.OUTPUT_DIR, exist_ok=True)
    d = load_trades()
    d.to_csv(os.path.join(cfg.OUTPUT_DIR, 'trades_with_buckets.csv'), index=False)
    outs = {'entry_by_session': table(d, 'entry_session'), 'entry_by_two_way': table(d, 'entry_two'), 'entry_by_hour': table(d, 'entry_hour'),
            'exit_by_session': table(d, 'exit_session'), 'exit_by_two_way': table(d, 'exit_two')}
    d['entry_exit_two_way'] = d['entry_two'] + ' -> ' + d['exit_two']
    outs['entry_to_exit_two_way'] = table(d, 'entry_exit_two_way')
    byp = d.groupby(['engine', 'period', 'entry_two']).apply(summarise, include_groups=False).reset_index()
    byp.to_csv(os.path.join(cfg.OUTPUT_DIR, 'entry_by_two_way_and_period.csv'), index=False)
    mix = d.groupby(['engine', 'entry_two', 'reason']).size().rename('trades').reset_index()
    mix['share_pct'] = (mix['trades'] / mix.groupby(['engine', 'entry_two'])['trades'].transform('sum') * 100).round(1)
    mix.to_csv(os.path.join(cfg.OUTPUT_DIR, 'exit_reason_mix_by_entry.csv'), index=False)
    for k, v in outs.items():
        v.to_csv(os.path.join(cfg.OUTPUT_DIR, f'trades_{k}.csv'), index=False)
    print('attribution', flush=True)
    pieces, resid = attribution(d)
    pieces.to_csv(os.path.join(cfg.OUTPUT_DIR, 'attribution_pieces.csv.gz'), index=False)
    a = pieces.groupby(['engine', 'bucket'])['pct'].agg(total_pct='sum', gain_pct=lambda s: s[s > 0].sum(), loss_pct=lambda s: s[s < 0].sum(), pieces='size').reset_index()
    a['share_of_net_pct'] = a['total_pct'] / a.groupby('engine')['total_pct'].transform('sum') * 100
    a.to_csv(os.path.join(cfg.OUTPUT_DIR, 'attribution_by_bucket.csv'), index=False)
    ah = pieces.groupby(['engine', 'hour'])['pct'].sum().reset_index()
    ah.to_csv(os.path.join(cfg.OUTPUT_DIR, 'attribution_by_hour.csv'), index=False)
    resid.to_csv(os.path.join(cfg.OUTPUT_DIR, 'attribution_residuals.csv'), index=False)
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', None)
    print('\n=== entries by Day / Evening ===')
    print(outs['entry_by_two_way'].to_string(index=False))
    print('\n=== exits by Day / Evening ===')
    print(outs['exit_by_two_way'].to_string(index=False))
    print('\n=== where the profit is earned (percent of entry, all trades) ===')
    print(a.round(1).to_string(index=False))
    r = resid.groupby('engine')['residual_pts'].agg(['count', lambda s: s.abs().median(), lambda s: s.abs().quantile(.95)])
    print('\nattribution residual vs recorded points (abs median, abs p95):\n', r.to_string())


if __name__ == '__main__':
    main()
