"""
Step 3 of the hestia-backtest skill: one closed-trade ledger for the four engines at their live configs, in rupees for ONE unit each, and the per-engine summary.

Selene, Helios and Typhon: their production-parity backtests (per-contract Supertrend, the real roll machinery), per-lot points times rupees per point for one unit.
Prometheus: the Fyers-history track's bespoke ledger at the live 2.5 config (rupees already for one unit). Writes outputs/integrated_trades.csv and outputs/engine_summary.json
(both tracked in git; the large per-engine backtest folders are not).

    python hestia_backtest/build_ledger.py
"""

import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hb_config as cfg  # noqa: E402


def load_engine(name: str) -> pd.DataFrame:
    e = cfg.ENGINES[name]
    if e['kind'] == 'prometheus':
        d = pd.read_csv(e['backtest_csv'], parse_dates=['entry_ts', 'lot1_exit_ts', 'lot2_exit_ts'])
        return pd.DataFrame({'engine': name, 'trade_id': d['trade_id'], 'direction': d['direction'], 'entry_ts': d['entry_ts'], 'exit_ts': d[['lot1_exit_ts', 'lot2_exit_ts']].max(axis=1),
                             'entry': d['entry_price'], 'pts': d['total_pnl_points'], 'rs': d['total_pnl_rs'], 'reason': d['lot1_exit_reason'] + '/' + d['lot2_exit_reason']})
    d = pd.read_csv(e['backtest_csv'], parse_dates=['entry_ts', 'exit_ts'])
    d = d[d['exit_ts'].notna()]
    return pd.DataFrame({'engine': name, 'trade_id': d['trade_id'], 'direction': d['direction'], 'entry_ts': d['entry_ts'], 'exit_ts': d['exit_ts'], 'entry': d['entry_px'],
                         'pts': d['pnl_pts'], 'rs': d['pnl_pts'] * e['rs_per_point_unit'], 'reason': d['last_reason']})


def stats(rs: pd.Series, exit_ts: pd.Series) -> dict:
    cum = rs.cumsum()
    dd = float((cum - cum.cummax()).min()) if len(rs) else 0.0
    yrs = max((exit_ts.max() - exit_ts.min()).days / 365.25, 1e-9) if len(rs) else 1
    tot = float(rs.sum())
    return {'trades': int(len(rs)), 'win_pct': round(float((rs > 0).mean() * 100), 1) if len(rs) else 0, 'total_rs': round(tot), 'max_dd_rs': round(dd),
            'calmar_per_year': round(tot / yrs / abs(dd), 2) if dd else None, 'worst_trade_rs': round(float(rs.min())) if len(rs) else 0}


def main():
    os.makedirs(cfg.OUT_DIR, exist_ok=True)
    frames = [load_engine(n) for n in cfg.ENGINES]
    df = pd.concat(frames, ignore_index=True).sort_values(['engine', 'entry_ts']).reset_index(drop=True)
    df.to_csv(os.path.join(cfg.OUT_DIR, 'integrated_trades.csv'), index=False)
    summary = {'engines': {}}
    for n in cfg.ENGINES:
        g = df[df['engine'] == n].sort_values('exit_ts')
        s = stats(g['rs'], g['exit_ts'])
        s.update({'first_entry': str(g['entry_ts'].min()), 'last_exit': str(g['exit_ts'].max()), 'source': os.path.relpath(cfg.ENGINES[n]['backtest_csv'], cfg.REPO)})
        summary['engines'][n] = s
    allg = df.sort_values('exit_ts')
    summary['combined'] = stats(allg['rs'], allg['exit_ts'])
    common = allg[allg['exit_ts'] >= max(pd.Timestamp(v['first_entry']) for v in summary['engines'].values())]
    summary['combined_common_window'] = stats(common['rs'], common['exit_ts'])
    json.dump(summary, open(os.path.join(cfg.OUT_DIR, 'engine_summary.json'), 'w'), indent=1)
    for n, s in summary['engines'].items():
        print(f"{n:11s} {s['trades']:5d} trades  win {s['win_pct']:5.1f}%  total {s['total_rs']:>10,} Rs  maxDD {s['max_dd_rs']:>10,}  Calmar/yr {s['calmar_per_year']}  {s['first_entry'][:10]} -> {s['last_exit'][:10]}")
    c = summary['combined']
    print(f"{'Combined':11s} {c['trades']:5d} trades  win {c['win_pct']:5.1f}%  total {c['total_rs']:>10,} Rs  maxDD {c['max_dd_rs']:>10,}  Calmar/yr {c['calmar_per_year']}")


if __name__ == '__main__':
    main()
