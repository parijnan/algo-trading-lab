"""
Flatten every open position before the weekend, and nothing else changes.

Rule: in each calendar week the last session present in the data is the week's last day (a Friday holiday makes it Thursday). At the flatten time on that
session (see flatten_configs: 23:15 on a 23:30 close, 23:45 on a 23:55 close) any open position is closed at the OPEN of the flatten minute, plus an optional
extra cost, and no entry is taken at or after that minute that day. Re-entry is only on a fresh Supertrend flip (the engines' own rule), so every later trade
is exactly the baseline's: the overlay only shortens trades that span a weekend and drops entries made in the last minutes of a week.
Prometheus flattens only the lots still open (a lot already booked at its target keeps its result).
Results are per ONE lot in percent of entry price (Prometheus: the two lots averaged), as in the trailing-profit and early-MFE studies.

    python research/weekend_flatten/weekend_flatten.py
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flatten_configs as cfg  # noqa: E402
import trailing_overlay as TO  # noqa: E402


def build_calendar(index: pd.DatetimeIndex, offsets: dict) -> pd.DataFrame:
    """One row per calendar week: the week's last session date, its close (last bar + 1 minute), the flatten timestamp and the first bar after the weekend."""
    s = pd.Series(index, index=index)
    day = index.normalize()
    last_bar = s.groupby(day).max()
    iso = last_bar.index.isocalendar()
    week = (iso['year'].astype(int) * 100 + iso['week'].astype(int)).to_numpy()
    df = pd.DataFrame({'date': last_bar.index, 'last_bar': last_bar.to_numpy(), 'week': week})
    df = df.sort_values('date').groupby('week', sort=True).tail(1)
    close = df['last_bar'] + pd.Timedelta(minutes=1)
    off = close.dt.time.map(lambda t: offsets.get(t, cfg.DEFAULT_OFFSET_MIN))
    out = pd.DataFrame({'date': df['date'].to_numpy(), 'close': close.to_numpy(),
                        'flatten_ts': (close - pd.to_timedelta(off, unit='m')).to_numpy(), 'weekday': df['date'].dt.weekday.to_numpy()})
    out['next_open_idx'] = index.searchsorted(out['date'] + pd.Timedelta(days=1), side='left')
    return out.reset_index(drop=True)


def next_flatten(cal: pd.DataFrame, ts) -> int:
    """Row of the first flatten strictly after ts, or -1."""
    j = int(np.searchsorted(cal['flatten_ts'].to_numpy(), np.datetime64(ts), side='right'))
    return j if j < len(cal) else -1


def entry_dropped(cal: pd.DataFrame, entry_ts) -> bool:
    """An entry on a week's last session at or after its flatten minute would be flattened at once, so the overlay takes no such trade."""
    d = pd.Timestamp(entry_ts).normalize()
    row = cal[cal['date'] == d]
    return (not row.empty) and pd.Timestamp(entry_ts) >= pd.Timestamp(row['flatten_ts'].iloc[0])


def flatten_single(trade: dict, p, cal: pd.DataFrame, sl_pct: float, target_pct, slip_pct: float):
    """Single-lot engines. Returns dict(base_pts, flat_pts, flattened, dropped, gap_pts, reason) in the engine's own price units (pts), long-frame sign handled inside."""
    base = TO.simulate_trail(trade, p, sl_pct, target_pct, ('none',))
    if base is None:
        return None
    e, bull = trade['entry'], trade['bull']
    s = 1.0 if bull else -1.0
    if entry_dropped(cal, trade['entry_ts']):
        return {'base_pts': base[0], 'flat_pts': None, 'flattened': False, 'dropped': True, 'gap_pts': 0.0, 'reason': base[1]}
    j = next_flatten(cal, trade['entry_ts'])
    if j < 0:
        return {'base_pts': base[0], 'flat_pts': base[0], 'flattened': False, 'dropped': False, 'gap_pts': 0.0, 'reason': base[1]}
    fi = int(p.index.searchsorted(pd.Timestamp(cal['flatten_ts'].iloc[j])))
    if not (trade['lo'] < fi < trade['end']):
        return {'base_pts': base[0], 'flat_pts': base[0], 'flattened': False, 'dropped': False, 'gap_pts': 0.0, 'reason': base[1]}
    cut = dict(trade, end=fi, flip_price=float(p.open[fi]))          # truncated at the flatten bar; an unresolved trade falls through to the flatten fill
    r = TO.simulate_trail(cut, p, sl_pct, target_pct, ('none',))
    if r[1] != 'flip':                                              # a stop or target came first: the weekend is never reached
        return {'base_pts': base[0], 'flat_pts': base[0], 'flattened': False, 'dropped': False, 'gap_pts': 0.0, 'reason': base[1]}
    fill = float(p.open[fi])
    flat = s * (fill - e) - fill * slip_pct / 100
    mi = int(cal['next_open_idx'].iloc[j])
    gap = s * (float(p.open[mi]) - fill) if mi < len(p.open) else 0.0
    return {'base_pts': base[0], 'flat_pts': flat, 'flattened': True, 'dropped': False, 'gap_pts': gap, 'reason': base[1]}


def run_single(trades: list, p, cal: pd.DataFrame, sl_pct: float, target_pct, slip_pct: float) -> pd.DataFrame:
    rows = []
    for t in trades:
        r = flatten_single(t, p, cal, sl_pct, target_pct, slip_pct)
        if r is None:
            continue
        e = t['entry']
        rows.append({'trade_id': t['trade_id'], 'entry_ts': t['entry_ts'], 'entry': e, 'base_pct': r['base_pts'] / e * 100,
                     'flat_pct': None if r['dropped'] else r['flat_pts'] / e * 100, 'flattened': r['flattened'], 'dropped': r['dropped'],
                     'gap_pct': r['gap_pts'] / e * 100, 'base_reason': r['reason']})
    return pd.DataFrame(rows)


def flatten_prometheus(r, tlog: pd.DataFrame, p_open: pd.Series, cal: pd.DataFrame, slip_pct: float):
    """One Prometheus trade (a bespoke_trade_summary row plus its per-minute log). Only lots whose exit is after the flatten minute are cut."""
    e, bull = float(r.entry_price), r.direction == 'bullish'
    s = 1.0 if bull else -1.0
    base_pts = [float(r.lot1_pnl_points), float(r.lot2_pnl_points)]
    if entry_dropped(cal, r.entry_ts):
        return {'base_pct': sum(base_pts) / 2 / e * 100, 'flat_pct': None, 'flattened': False, 'dropped': True, 'gap_pct': 0.0}
    j = next_flatten(cal, r.entry_ts)
    if j < 0:
        return {'base_pct': sum(base_pts) / 2 / e * 100, 'flat_pct': sum(base_pts) / 2 / e * 100, 'flattened': False, 'dropped': False, 'gap_pct': 0.0}
    fts = pd.Timestamp(cal['flatten_ts'].iloc[j])
    exits = [pd.Timestamp(r.lot1_exit_ts), pd.Timestamp(r.lot2_exit_ts)]
    if fts not in p_open.index or not any(x > fts for x in exits):
        return {'base_pct': sum(base_pts) / 2 / e * 100, 'flat_pct': sum(base_pts) / 2 / e * 100, 'flattened': False, 'dropped': False, 'gap_pct': 0.0}
    fill = float(p_open.loc[fts])
    flat_lot = s * (fill - e) - fill * slip_pct / 100
    new = [flat_lot if x > fts else b for x, b in zip(exits, base_pts)]
    mi = int(cal['next_open_idx'].iloc[j])
    gap = s * (float(p_open.iloc[mi]) - fill) if mi < len(p_open) else 0.0
    n_cut = sum(1 for x in exits if x > fts)
    return {'base_pct': sum(base_pts) / 2 / e * 100, 'flat_pct': sum(new) / 2 / e * 100, 'flattened': True, 'dropped': False, 'gap_pct': gap * n_cut / 2 / e * 100}


def reentry_single(trade: dict, p, cal: pd.DataFrame, sl_pct: float, target_pct, slip_pct: float):
    """The second arm: after a weekend flatten, re-enter in the SAME direction at the open of the bar REENTRY_DELAY_MIN after the next session's first bar, provided the
    baseline trade would still be open then (no trend flip yet). New stop and target come from the new entry; the exit is the baseline's own trend flip. Returns the
    re-entered lot's result in percent of ITS entry (an entry cost of slip_pct on the new fill is charged), or None."""
    if entry_dropped(cal, trade['entry_ts']):
        return None
    j = next_flatten(cal, trade['entry_ts'])
    if j < 0:
        return None
    fi = int(p.index.searchsorted(pd.Timestamp(cal['flatten_ts'].iloc[j])))
    if not (trade['lo'] < fi < trade['end']):
        return None
    cut = dict(trade, end=fi, flip_price=float(p.open[fi]))
    if TO.simulate_trail(cut, p, sl_pct, target_pct, ('none',))[1] != 'flip':
        return None
    mi = int(cal['next_open_idx'].iloc[j])
    if mi >= len(p.open):
        return None
    ri = int(p.index.searchsorted(p.index[mi] + pd.Timedelta(minutes=cfg.REENTRY_DELAY_MIN), side='left'))
    if ri >= trade['end'] or ri >= len(p.open):
        return None
    e2 = float(p.open[ri])
    again = dict(trade, entry=e2, lo=ri, entry_ts=p.index[ri])
    r = TO.simulate_trail(again, p, sl_pct, target_pct, ('none',))
    if r is None:
        return None
    return (r[0] - e2 * slip_pct / 100) / e2 * 100


def run_reentry(trades: list, p, cal: pd.DataFrame, sl_pct: float, target_pct, slip_pct: float, flat_df: pd.DataFrame) -> pd.DataFrame:
    """flat_df (run_single's output) plus a column with the re-entered lot's result, and the arm's per-trade total."""
    extra = {}
    for t in trades:
        r = reentry_single(t, p, cal, sl_pct, target_pct, slip_pct)
        if r is not None:
            extra[t['trade_id']] = r
    out = flat_df.copy()
    out['reentry_pct'] = out['trade_id'].map(extra).fillna(0.0)
    out['rein_pct'] = out['flat_pct'] + out['reentry_pct']
    out['n_reentry'] = out['trade_id'].isin(extra.keys())
    return out


def run_prometheus(summary: pd.DataFrame, p_open: pd.Series, cal: pd.DataFrame, slip_pct: float) -> pd.DataFrame:
    rows = []
    for r in summary.itertuples():
        if pd.isna(r.lot2_exit_ts) or pd.isna(r.lot1_exit_ts):
            continue
        out = flatten_prometheus(r, None, p_open, cal, slip_pct)
        rows.append({'trade_id': int(r.trade_id), 'entry_ts': r.entry_ts, 'entry': float(r.entry_price), 'flattened': out['flattened'], 'dropped': out['dropped'],
                     'base_pct': out['base_pct'], 'flat_pct': out['flat_pct'], 'gap_pct': out['gap_pct'],
                     'base_reason': f'{r.lot1_exit_reason}/{r.lot2_exit_reason}'})
    return pd.DataFrame(rows)


def series_metrics(pct: pd.Series) -> dict:
    return TO.metrics(pd.DataFrame({'pct': pct.to_numpy()}))


def summarise(name: str, df: pd.DataFrame, cal: pd.DataFrame) -> dict:
    split = pd.Timestamp(cfg.SPLIT_DATE)
    kept = df[~df['dropped']]
    b, f = series_metrics(df['base_pct']), series_metrics(kept['flat_pct'])
    pre_b, post_b = series_metrics(df[df['entry_ts'] < split]['base_pct']), series_metrics(df[df['entry_ts'] >= split]['base_pct'])
    pre_f, post_f = series_metrics(kept[kept['entry_ts'] < split]['flat_pct']), series_metrics(kept[kept['entry_ts'] >= split]['flat_pct'])
    fl = df[df['flattened']]
    return {'engine': name, 'trades': len(df), 'flattened': int(df['flattened'].sum()), 'dropped': int(df['dropped'].sum()),
            'weeks': len(cal), 'weeks_suspect': int((cal['weekday'] <= cfg.SUSPECT_LAST_WEEKDAY).sum()),
            'base_total': b['total_pct'], 'base_dd': b['dd_pct'], 'base_calmar': b['calmar_pct'],
            'flat_total': f['total_pct'], 'flat_dd': f['dd_pct'], 'flat_calmar': f['calmar_pct'],
            'pre_base_total': pre_b['total_pct'], 'pre_flat_total': pre_f['total_pct'], 'post_base_total': post_b['total_pct'], 'post_flat_total': post_f['total_pct'],
            'pre_base_calmar': pre_b['calmar_pct'], 'pre_flat_calmar': pre_f['calmar_pct'], 'post_base_calmar': post_b['calmar_pct'], 'post_flat_calmar': post_f['calmar_pct'],
            'gap_sum': round(float(fl['gap_pct'].sum()), 1), 'gap_mean': round(float(fl['gap_pct'].mean()), 3) if len(fl) else float('nan'),
            'gap_worst': round(float(fl['gap_pct'].min()), 2) if len(fl) else float('nan'),
            'flattened_base_sum': round(float(fl['base_pct'].sum()), 1), 'flattened_flat_sum': round(float(fl['flat_pct'].sum()), 1),
            'dropped_base_sum': round(float(df[df['dropped']]['base_pct'].sum()), 1),
            'worst_base': round(float(df['base_pct'].min()), 2), 'worst_flat': round(float(kept['flat_pct'].min()), 2)}


def by_year(df: pd.DataFrame) -> pd.DataFrame:
    kept = df[~df['dropped']]
    y = pd.DataFrame({'base': df.groupby(df['entry_ts'].dt.year)['base_pct'].sum(), 'flat': kept.groupby(kept['entry_ts'].dt.year)['flat_pct'].sum()})
    return y.round(1)


def load_prometheus():
    import data_loader_fyers
    sys.path.insert(0, os.path.join(cfg.REPO_ROOT, 'prometheus_backtest', 'phase3_fyers'))
    df = data_loader_fyers.load_futures_1min('CRUDEOILM')
    base = os.path.join(cfg.REPO_ROOT, 'prometheus_backtest', cfg.PROMETHEUS_TRACK[0], 'data_sweep', cfg.PROMETHEUS_TRACK[1])
    summary = pd.read_csv(os.path.join(base, 'bespoke_trade_summary.csv'), parse_dates=['entry_ts', 'lot1_exit_ts', 'lot2_exit_ts'])
    return df, summary


def main():
    os.makedirs(cfg.OUTPUT_DIR, exist_ok=True)
    offsets = cfg.OFFSET_VARIANTS
    results, years, lines = [], {}, []
    engines = TO.load_all()
    sys.path.insert(0, os.path.join(cfg.REPO_ROOT, 'prometheus_backtest'))
    sys.path.insert(0, os.path.join(cfg.REPO_ROOT, 'prometheus_backtest', 'phase3_fyers'))
    pm_df, pm_sum = load_prometheus()
    for variant, off in offsets.items():
        for slip in cfg.SLIPPAGE_PCT:
            for name, (trades, p, sl, tgt, ref) in engines.items():
                cal = build_calendar(p.index, off)
                df = run_single(trades, p, cal, sl, tgt, slip)
                if variant == cfg.MAIN_VARIANT and slip == 0.0:                     # baseline reproduces the module simulator, asserted on every trade
                    refs = np.array([ref(t) for t in trades if ref(t) is not None])
                    assert len(refs) == len(df) and np.allclose(refs / df['entry'].to_numpy() * 100, df['base_pct'].to_numpy(), atol=1e-6), f'{name}: baseline mismatch'
                    df.to_csv(os.path.join(cfg.OUTPUT_DIR, f'trades_{name}.csv'), index=False)
                    years[name] = by_year(df)
                row = summarise(name, df, cal)
                rein = run_reentry(trades, p, cal, sl, tgt, slip, df)
                kept = rein[~rein['dropped']]
                m = series_metrics(kept['rein_pct'])
                split = pd.Timestamp(cfg.SPLIT_DATE)
                row.update({'rein_total': m['total_pct'], 'rein_dd': m['dd_pct'], 'rein_calmar': m['calmar_pct'], 'n_reentry': int(kept['n_reentry'].sum()),
                            'rein_pre_total': series_metrics(kept[kept['entry_ts'] < split]['rein_pct'])['total_pct'],
                            'rein_post_total': series_metrics(kept[kept['entry_ts'] >= split]['rein_pct'])['total_pct'],
                            'reentry_sum': round(float(kept['reentry_pct'].sum()), 1)})
                if variant == cfg.MAIN_VARIANT and slip == 0.0:
                    years[name] = years[name].join(kept.groupby(kept['entry_ts'].dt.year)['rein_pct'].sum().round(1).rename('rein'))
                results.append({'variant': variant, 'slip': slip, **row})
            cal = build_calendar(pm_df.index, off)
            df = run_prometheus(pm_sum, pm_df['open'], cal, slip)
            if variant == cfg.MAIN_VARIANT and slip == 0.0:
                df.to_csv(os.path.join(cfg.OUTPUT_DIR, 'trades_Prometheus.csv'), index=False)
                years['Prometheus'] = by_year(df)
            results.append({'variant': variant, 'slip': slip, **summarise('Prometheus', df, cal)})
    res = pd.DataFrame(results)
    res.to_csv(os.path.join(cfg.OUTPUT_DIR, 'weekend_flatten_results.csv'), index=False)
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', None)
    main_rows = res[(res['variant'] == cfg.MAIN_VARIANT)]
    print(main_rows[main_rows['slip'] == 0.0].drop(columns=['variant', 'slip']).T.to_string())
    for name, y in years.items():
        print(f'\n{name} by entry year (percent of entry per lot):'); print(y.to_string())


if __name__ == '__main__':
    main()
