"""
Robustness of a candidate early-exit cell: result by calendar year, a split inside 2026, and the break-even exit cost.

    python research/early_mfe/robustness.py
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import early_configs as cfg  # noqa: E402
import early_mfe_study as S  # noqa: E402

CELLS = [(60, 'u', 0.0), (240, 'mfe', 0.5), (120, 'u', 0.0), (240, 'u', 0.0)]      # (checkpoint, kind, threshold in % of price)


def outcomes(recs, k, kind, thr, cost):
    rows = []
    for r in recs:
        pct, trig = S.rule_outcome(r, k, kind, thr)
        rows.append((r['entry_ts'], pct - (cost if trig else 0.0), r['pct'], trig))
    return pd.DataFrame(rows, columns=['entry_ts', 'pct', 'base', 'trig']).sort_values('entry_ts', kind='stable')


def report(name, recs, R):
    print(f'\n##### {name} (R={R}%)')
    inner = pd.Timestamp(cfg.INNER_SPLIT_DATE)
    for k, kind, thr in CELLS:
        thr = thr * R if kind == 'mfe' else 0.0
        tag = f"U<=0 @ {k}" if kind == 'u' else f"MFE<{thr:.2g}% @ {k}"
        d = outcomes(recs, k, kind, thr, 0.0)
        yrs = (d.groupby(d['entry_ts'].dt.year)['pct'].sum() - d.groupby(d['entry_ts'].dt.year)['base'].sum()).round(1).to_dict()
        a, b = d[d['entry_ts'] < inner], d[d['entry_ts'] >= inner]
        d26 = d[d['entry_ts'] >= pd.Timestamp(cfg.SPLIT_DATE)]
        inner_txt = ''
        if len(d26[d26['entry_ts'] < inner]) and len(b):
            h1, h2 = d26[d26['entry_ts'] < inner], b
            inner_txt = f" | 2026 H1 delta {h1['pct'].sum() - h1['base'].sum():+.1f} (n_trig {int(h1['trig'].sum())}), after {cfg.INNER_SPLIT_DATE} {h2['pct'].sum() - h2['base'].sum():+.1f} (n_trig {int(h2['trig'].sum())})"
        costs = []
        for c in cfg.COSTS:
            m = S.TO.metrics(outcomes(recs, k, kind, thr, c).assign(entry_ts=lambda x: x['entry_ts']))
            costs.append(f"{c}%: total {m['total_pct']} Calmar {m['calmar_pct']}")
        gain = d['pct'].sum() - d['base'].sum()
        per = gain / max(int(d['trig'].sum()), 1)
        print(f"{tag}: triggered {int(d['trig'].sum())}, gain at zero cost {gain:+.1f}% (break-even cost {per:.3f}% per exit) | by year delta {yrs}{inner_txt}")
        print('    cost sensitivity: ' + ' | '.join(costs))


def main():
    for name, (track, mult, R) in cfg.PROMETHEUS_TRACKS.items():
        report(name, S.build_prometheus(track, mult, name), R)


if __name__ == '__main__':
    main()
