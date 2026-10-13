"""
Integrated Hestia backtest, step 1: one closed-trade ledger for the four engines at their live / decided configs, in rupees for ONE unit each.
Selene, Helios, Typhon: per-trade result (percent of entry per lot) from the weekend-flatten baseline columns (the engines' own parity simulators), with each trade's exit
time found by walking the 1-minute series to the first stop or target bar (else the trend-flip bar). Prometheus: its saved per-trade ledger (phase3_fyers, mult 2.5), which
already carries rupee P&L and exit times. Unit conversions (rupees per price point for one unit) come from the live lot sizes.

    python research/hestia_integrated/build_trades.py
"""

import json
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, 'research', 'trailing_profit'))
sys.path.insert(0, os.path.join(REPO, 'research', 'early_mfe'))
sys.path.insert(0, REPO)
import trailing_overlay as TO  # noqa: E402

FLAT_DIR = os.path.join(REPO, 'research', 'weekend_flatten', 'outputs')
PROM_CSV = os.path.join(REPO, 'prometheus_backtest', 'phase3_fyers', 'data_sweep', 'mult_2.5', 'bespoke_trade_summary.csv')
# rupees per price point for ONE unit (lots per unit x rupees per point per lot): Selene 1 lot x 1, Helios 20 x 1, Typhon 2 x 250, Prometheus 2 x 10
RS_PER_POINT = {'Selene': 1, 'Helios': 20, 'Typhon': 500, 'Prometheus': 20}
OUT = os.path.join(REPO, 'research', 'hestia_integrated', 'outputs')


def exit_time(t, p, sl_pct, tgt_pct):
    """First active bar whose range touches the stop or target; else the trend-flip bar."""
    lo, end, e, bull = t['lo'], t['end'], t['entry'], t['bull']
    s = 1.0 if bull else -1.0
    sl, tg = e - s * e * sl_pct / 100, (e + s * e * tgt_pct / 100) if tgt_pct else None
    h, l, act = p.high[lo:end], p.low[lo:end], ~p.guarded[lo:end]
    hit = ((l <= sl) if bull else (h >= sl)) & act
    if tg is not None:
        hit |= ((h >= tg) if bull else (l <= tg)) & act
    i = int(hit.argmax()) if hit.any() else None
    return (p.index[lo + i], 'stop/target') if i is not None else (p.index[min(end, len(p.index) - 1)], 'flip')


def main():
    os.makedirs(OUT, exist_ok=True)
    rows = []
    for name, (trades, p, sl, tgt, _ref) in TO.load_all().items():
        base = pd.read_csv(os.path.join(FLAT_DIR, f'trades_{name}.csv')).set_index('trade_id')
        kinds = {}
        for t in trades:
            if t['trade_id'] not in base.index:
                continue
            ts, kind = exit_time(t, p, sl, tgt)
            b = base.loc[t['trade_id']]
            if b['dropped']:
                pass
            pts = b['base_pct'] / 100 * t['entry']
            kinds[kind] = kinds.get(kind, 0) + 1
            rows.append((name, str(t['entry_ts']), str(ts), t['entry'], round(float(pts), 4), round(float(pts) * RS_PER_POINT[name], 2), b['base_reason'], kind))
        print(name, len(trades), kinds, flush=True)
    pr = pd.read_csv(PROM_CSV, parse_dates=['entry_ts', 'lot1_exit_ts', 'lot2_exit_ts'])
    for _, r in pr.iterrows():
        ex = max(r['lot1_exit_ts'], r['lot2_exit_ts'])
        rows.append(('Prometheus', str(r['entry_ts']), str(ex), float(r['entry_price']), float(r['total_pnl_points']), float(r['total_pnl_rs']),
                     f"{r['lot1_exit_reason']}/{r['lot2_exit_reason']}", 'ledger'))
    df = pd.DataFrame(rows, columns=['engine', 'entry_ts', 'exit_ts', 'entry', 'pts', 'rs', 'reason', 'exit_kind'])
    df.to_csv(os.path.join(OUT, 'integrated_trades.csv'), index=False)
    print(df.groupby('engine').agg(n=('rs', 'size'), total=('rs', 'sum'), first=('entry_ts', 'min'), last=('exit_ts', 'max')))
    # stop/target vs reason parity for the single-lot engines
    print(pd.crosstab(df[df.engine != 'Prometheus']['reason'], df[df.engine != 'Prometheus']['exit_kind']))


if __name__ == '__main__':
    main()
