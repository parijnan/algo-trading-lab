"""
Build the "Prometheus vs Janus Equity" artifact page: cumulative closed-trade rupees by exit date for Prometheus (the integrated backtest's trades) and Janus (its local machine run),
on weekday dates from Janus's first session to the last complete day, with their drawdowns and a year-by-year table.

    python janus_backtest/equity_page/build_equity_page.py --out equity.html

Inputs: hestia_backtest/outputs/integrated_trades.csv (refresh with the hestia-backtest skill) and the Janus trade list in hb_config.JANUS (re-run janus_backtest/machine_run.py --source previous_session
on fresh data first). Prometheus is Rs for one unit (two lots); Janus is its summed points times Rs 10 a point, which is three lots.
"""

import argparse
import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, 'hestia_backtest'))
import hb_config as cfg  # noqa: E402
import janus_sync  # noqa: E402


def series():
    j = pd.read_csv(cfg.JANUS['trades_csv'], parse_dates=['entry_ts', 'exit_ts'])
    p = pd.read_csv(os.path.join(cfg.OUT_DIR, 'integrated_trades.csv'), parse_dates=['entry_ts', 'exit_ts'])
    p = p[p.engine == 'Prometheus']
    start = j.entry_ts.min().normalize()
    end = min(j.exit_ts.max().normalize(), janus_sync.integrated_end())            # both series stop on the same day (Prometheus may simply have no exit on it)
    p = p[(p.exit_ts.dt.normalize() >= start) & (p.exit_ts.dt.normalize() <= end)]    # trades that close from Janus's first day, whenever they entered
    j = j[j.exit_ts.dt.normalize() <= end]
    days = pd.bdate_range(start, end)

    def cum(df, col, mult):
        assert df.exit_ts.dt.normalize().isin(days).all(), 'a trade exits on a non-weekday'
        return (df.groupby(df.exit_ts.dt.normalize())[col].sum() * mult).reindex(days).fillna(0).cumsum()

    cp, cj = cum(p, 'rs', 1), cum(j, 'pnl_pts', cfg.JANUS['rs_per_point_lot'])
    data = {'dates': [d.strftime('%Y-%m-%d') for d in days], 'prom': [int(round(v)) for v in cp], 'janus': [int(round(v)) for v in cj], 'ntP': int(len(p)), 'ntJ': int(len(j))}
    return data, start, end


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--allow-stale', action='store_true', help='build although the local Janus run is behind or missing')
    a = ap.parse_args()
    state, msg = janus_sync.status()
    print(f'Janus run: {state.upper()}: {msg}')
    if state in ('behind', 'missing') and not a.allow_stale:
        sys.exit('stopping: bring the Janus run up to date first (see above), or pass --allow-stale')
    data, start, end = series()
    html = open(os.path.join(HERE, 'page_template.html')).read()
    for k, v in {'__DATA__': json.dumps(data), '__START_LONG__': f'{start.day} {start:%b %Y}', '__END_LONG__': f'{end.day} {end:%b %Y}', '__END_SHORT__': f'{end.day} {end:%b}', '__END_YEAR__': str(end.year)}.items():
        html = html.replace(k, v)
    open(a.out, 'w').write(html)
    print(f"wrote {a.out}: {start:%Y-%m-%d} to {end:%Y-%m-%d}, Prometheus {data['ntP']:,} trades Rs {data['prom'][-1]:,}, Janus {data['ntJ']:,} trades Rs {data['janus'][-1]:,}")


if __name__ == '__main__':
    main()
