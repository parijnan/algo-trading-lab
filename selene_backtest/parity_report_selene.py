"""
Selene - score the parity-variant runs (parity_variants_selene.py) on the same metrics and costs as optimize_selene.py, 2026-11-06.
A parity trade can span several legs (a roll closes and reopens on the next contract), so its charges and slippage are counted per leg: one entry and one
exit order per leg. Output: data_sweep/parity_variants/parity_report.csv, and a printed comparison by period.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selene_configs as configs
import optimize_selene as op
from parity_variants_selene import label


def frame(mult: float, sl: float, slip: float, extra_bps: float = 0.0) -> pd.DataFrame:
    t = pd.read_csv(os.path.join(configs.PARITY_VARIANTS_DIR, label(mult, sl) + '_trades.csv'), parse_dates=['entry_ts', 'exit_ts'])
    rows = []
    for r in t.itertuples():
        bull = r.direction == 'bullish'
        stop = r.last_reason == 'stop_loss'
        cost = r.legs * (op.charges_rs(bull, r.entry_px) + slip) + (r.entry_px * extra_bps / 1e4 if stop else 0.0)
        rows.append((r.trade_id, r.entry_ts, r.exit_ts, r.entry_px, r.pnl_pts, cost, 'stop_loss' if stop else r.last_reason))
    return pd.DataFrame(rows, columns=['trade_id', 'entry_ts', 'exit_ts', 'entry', 'pnl_pts', 'cost_rs', 'reason'])


def main():
    slip = op.measured_slippage_rs()
    out = []
    for mult, sl in configs.PARITY_VARIANTS:
        if not os.path.exists(os.path.join(configs.PARITY_VARIANTS_DIR, label(mult, sl) + '_trades.csv')):
            continue
        for stress, bps in (('base', 0.0), ('stop+10bps', configs.OPT_STOP_EXTRA_SLIP_BPS)):
            sim = frame(mult, sl, slip, bps)
            for name, a, b in configs.OPT_PERIODS:
                part = sim[(sim['entry_ts'] >= pd.Timestamp(a)) & (sim['entry_ts'] < pd.Timestamp(b))]
                out.append({'cell': label(mult, sl), 'stress': stress, 'period': name, **op.metrics(part)})
        sim = frame(mult, sl, slip)
        for y, part in sim.groupby(sim['entry_ts'].dt.year):
            m = op.metrics(part)
            out.append({'cell': label(mult, sl), 'stress': 'base', 'period': f'y{y}', 'n_trades': m['n_trades'], 'net_rs': m['net_rs'], 'gross_rs': m['gross_rs']})
    df = pd.DataFrame(out)
    df.to_csv(configs.PARITY_REPORT_FILE, index=False)
    print('wrote', configs.PARITY_REPORT_FILE)


if __name__ == '__main__':
    main()
