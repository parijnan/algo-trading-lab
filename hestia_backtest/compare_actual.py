"""
Step 4 of the hestia-backtest skill: pair every LIVE closed trade with the backtest trade for the same engine and compare them.

Input: a Delos snapshot (hestia_performance/pull_snapshot.py, read-only) and outputs/integrated_trades.csv. Live trades are the ones counted by the Hestia Dashboard (trade id at or
after each engine's first live trade, hestia_performance/tracker_config.json). A live trade pairs with the backtest trade of the same direction whose entry time is closest and within
MATCH_TOLERANCE_MIN minutes. Every row is compared on one unit: points and rupees come from the engine's recorded points times rupees per point for one unit, not from the units the
trade actually ran at. Statuses: matched, live only (the live engine traded and the backtest did not), backtest only (the backtest traded after the engine went live and the live
engine did not), after backtest data (the live trade is newer than the backtest's last bar).

    python hestia_backtest/compare_actual.py --snapshot snapshot.json
"""

import argparse
import importlib.util
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hb_config as cfg  # noqa: E402


def _load_tracker():
    spec = importlib.util.spec_from_file_location('build_tracker', os.path.join(cfg.REPO, 'hestia_performance', 'build_tracker.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def norm_reason(r: str) -> str:
    return '/'.join(dict.fromkeys(str(r).split('/')))


def pair(live: list, bt: pd.DataFrame, tol_min: float) -> list:
    """live: dicts with entry_ts, direction, ...; bt: backtest rows of one engine. Returns comparison rows (one per live trade, then unmatched backtest rows)."""
    bt = bt.sort_values('entry_ts').reset_index(drop=True)
    used, rows = set(), []
    bt_end = bt['exit_ts'].max() if len(bt) else pd.Timestamp.min
    for t in sorted(live, key=lambda x: x['entry_ts']):
        e = pd.Timestamp(t['entry_ts']).floor('min')
        cand = bt[(bt['direction'] == t['direction']) & (~bt.index.isin(used))].copy()
        cand['d'] = (cand['entry_ts'] - e).abs().dt.total_seconds() / 60
        cand = cand[cand['d'] <= tol_min].sort_values('d')
        r = {'live_id': t['trade_id'], 'direction': t['direction'], 'live_entry_ts': str(e), 'live_entry': t['entry_price'], 'live_exit_ts': str(pd.Timestamp(t['exit_ts']).floor('min')),
             'live_reason': norm_reason(t['reasons']), 'live_pts': t['pts']}
        if len(cand):
            b = bt.loc[cand.index[0]]
            used.add(cand.index[0])
            r.update({'status': 'matched', 'bt_entry_ts': str(b['entry_ts']), 'bt_entry': float(b['entry']), 'bt_exit_ts': str(b['exit_ts']), 'bt_reason': norm_reason(b['reason']),
                      'bt_pts': float(b['pts']), 'bt_rs': float(b['rs'])})
        else:
            r['status'] = 'after backtest data' if e > bt_end else 'live only'
        rows.append(r)
    first_live = min((pd.Timestamp(t['entry_ts']) for t in live), default=None)
    if first_live is not None:
        for i, b in bt.iterrows():
            if i not in used and b['entry_ts'] >= first_live.floor('min'):
                rows.append({'status': 'backtest only', 'direction': b['direction'], 'bt_entry_ts': str(b['entry_ts']), 'bt_entry': float(b['entry']), 'bt_exit_ts': str(b['exit_ts']),
                             'bt_reason': norm_reason(b['reason']), 'bt_pts': float(b['pts']), 'bt_rs': float(b['rs'])})
    return rows


def compare(snapshot: dict, ledger: pd.DataFrame, tracker, perf_cfg: dict) -> dict:
    out = {'pulled_at': snapshot.get('pulled_at'), 'engines': {}, 'rows': []}
    for name, e in cfg.ENGINES.items():
        key = e['key']
        ecfg = perf_cfg['engines'][key]
        live, _ = tracker.split_live(tracker.parse_trades(snapshot['engines'][key]['trades_csv'], name, ecfg), ecfg)
        rows = pair(live, ledger[ledger['engine'] == name], cfg.MATCH_TOLERANCE_MIN)
        for r in rows:
            r['engine'] = name
            if r['status'] == 'matched':
                r['live_rs'] = r['live_pts'] * e['rs_per_point_unit']
                r['rs_diff'] = round(r['live_rs'] - r['bt_rs'], 1)
                r['entry_diff_pts'] = round(r['live_entry'] - r['bt_entry'], 4) * (1 if r['direction'] == 'bullish' else -1)   # positive = live paid worse than the backtest
                r['same_exit'] = (abs((pd.Timestamp(r['live_exit_ts']) - pd.Timestamp(r['bt_exit_ts'])).total_seconds()) <= cfg.MATCH_TOLERANCE_MIN * 60 and r['live_reason'] == r['bt_reason'])
        m = [r for r in rows if r['status'] == 'matched']
        out['engines'][name] = {'live_trades': len(live), 'matched': len(m), 'live_only': sum(r['status'] == 'live only' for r in rows),
                                'backtest_only': sum(r['status'] == 'backtest only' for r in rows), 'after_backtest_data': sum(r['status'] == 'after backtest data' for r in rows),
                                'same_exit': sum(bool(r.get('same_exit')) for r in m), 'live_rs_matched': round(sum(r['live_rs'] for r in m)), 'bt_rs_matched': round(sum(r['bt_rs'] for r in m)),
                                'avg_entry_diff_pts': round(sum(r['entry_diff_pts'] for r in m) / len(m), 4) if m else None}
        out['rows'] += rows
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--snapshot', required=True)
    a = p.parse_args()
    snap = json.load(open(a.snapshot))
    ledger = pd.read_csv(os.path.join(cfg.OUT_DIR, 'integrated_trades.csv'), parse_dates=['entry_ts', 'exit_ts'])
    res = compare(snap, ledger, _load_tracker(), json.load(open(cfg.PERF_CONFIG)))
    json.dump(res, open(os.path.join(cfg.OUT_DIR, 'comparison.json'), 'w'), indent=1, default=str)
    pd.DataFrame(res['rows']).to_csv(os.path.join(cfg.OUT_DIR, 'comparison.csv'), index=False)
    for n, s in res['engines'].items():
        print(f"{n:11s} live {s['live_trades']:3d} | matched {s['matched']:3d} (same exit {s['same_exit']}) live-only {s['live_only']} backtest-only {s['backtest_only']} after-data {s['after_backtest_data']} | "
              f"Rs matched live {s['live_rs_matched']:,} vs backtest {s['bt_rs_matched']:,} | avg entry diff {s['avg_entry_diff_pts']}")


if __name__ == '__main__':
    main()
