"""
Step 5 of the hestia-backtest skill: build the artifact page from outputs/integrated_trades.csv and outputs/comparison.json.

    python hestia_backtest/build_page.py --out hestia_integrated.html [--end YYYY-MM-DD]
"""

import argparse
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hb_config as cfg  # noqa: E402


def build(end: str) -> str:
    df = pd.read_csv(os.path.join(cfg.OUT_DIR, 'integrated_trades.csv'), parse_dates=['entry_ts', 'exit_ts'])
    df['day'] = df['exit_ts'].dt.strftime('%Y-%m-%d')
    days = sorted(df['day'].unique())
    ix = {d: i for i, d in enumerate(days)}
    names = list(cfg.ENGINES)
    rows = sorted(([names.index(r.engine), ix[r.day], round(float(r.rs), 1), round(float(r.entry), 2)] for r in df.itertuples()), key=lambda r: r[1])
    cmp_path = os.path.join(cfg.OUT_DIR, 'comparison.json')
    cmp_ = json.load(open(cmp_path)) if os.path.exists(cmp_path) else {'pulled_at': None, 'engines': {}, 'rows': []}
    for r in cmp_['rows']:                                             # NaN does not survive JSON in the page
        for k, v in list(r.items()):
            if isinstance(v, float) and v != v:
                del r[k]
    tpl = open(os.path.join(cfg.REPO, 'hestia_backtest', 'page_template.html')).read()
    return (tpl.replace('__DAYS__', json.dumps(days)).replace('__TRADES__', json.dumps(rows, separators=(',', ':')))
            .replace('__CMP__', json.dumps(cmp_, default=str, separators=(',', ':'))).replace('__META__', json.dumps({'end': end, 'tol': cfg.MATCH_TOLERANCE_MIN})))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--out', required=True)
    p.add_argument('--end', default='')
    a = p.parse_args()
    html = build(a.end)
    open(a.out, 'w').write(html)
    print(f'wrote {a.out} ({len(html):,} bytes)')


if __name__ == '__main__':
    main()
