"""
Step 1 of the hestia-backtest skill: is the price data the four engine backtests read complete and current?

For each engine instrument it reports (1) the last complete session in the live contract tree (data_pipeline/data/mcx/, synced from Delos, which keeps only the current and next
contract), (2) whether the Fyers expired-contract staging tree (data_pipeline/data/mcx_fyers/), which the backtest loaders read first, is missing contracts that have since expired:
the live tree no longer holds an expired contract, so the test is the gap between the newest staged expiry and the front live contract's expiry against the instrument's usual spacing
(1.6 times the median spacing of its last 8 staged expiries), and (3) the backtest end date to use (the last complete session). Read-only; prints JSON with --json.

    python hestia_backtest/data_check.py [--json]
"""

import argparse
import datetime
import glob
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hb_config as cfg  # noqa: E402


def expiry_of(path: str) -> str:
    return os.path.basename(path).split('_')[0]


def last_complete_session(instrument: str) -> str:
    """Latest date that has at least COMPLETE_SESSION_BARS one-minute bars in a single contract file of the live tree."""
    best = None
    for f in sorted(glob.glob(os.path.join(cfg.MCX_DIR, instrument, '*_futures.csv')))[-3:]:
        ts = pd.read_csv(f, usecols=['time_stamp'])['time_stamp'].astype(str).str[:10]
        counts = ts.value_counts()
        full = counts[counts >= cfg.COMPLETE_SESSION_BARS].index
        if len(full):
            d = max(full)
            best = d if best is None or d > best else best
    return best


def check(today: datetime.date) -> dict:
    out = {}
    for name, e in cfg.ENGINES.items():
        ins = e['instrument']
        live = sorted(expiry_of(f) for f in glob.glob(os.path.join(cfg.MCX_DIR, ins, '*_futures.csv')))
        staged = sorted(expiry_of(f) for f in glob.glob(os.path.join(cfg.FYERS_DIR, ins, '*_futures.csv')))
        front = next((x for x in live if x >= today.isoformat()), None)       # the first unexpired contract still in the live tree
        d = [datetime.date.fromisoformat(x) for x in staged[-9:]]
        spacing = sorted((b - a).days for a, b in zip(d, d[1:]))
        usual = spacing[len(spacing) // 2] if spacing else 31
        gap = (datetime.date.fromisoformat(front) - datetime.date.fromisoformat(staged[-1])).days if front and staged else 0
        out[name] = {'instrument': ins, 'last_complete_session': last_complete_session(ins), 'latest_fyers_file': staged[-1] if staged else None, 'front_live_contract': front,
                     'gap_days': gap, 'usual_spacing_days': usual, 'fyers_update_needed': gap > 1.6 * usual, 'live_contract_files': live}
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--json', action='store_true')
    a = p.parse_args()
    res = check(datetime.date.today())
    if a.json:
        print(json.dumps(res, indent=1))
        return
    for n, r in res.items():
        print(f"{n:11s} {r['instrument']:11s} last complete session {r['last_complete_session']} | newest Fyers file {r['latest_fyers_file']} | front live contract {r['front_live_contract']} "
              f"(gap {r['gap_days']} d, usual {r['usual_spacing_days']} d) | Fyers update needed: {'YES' if r['fyers_update_needed'] else 'no'}")


if __name__ == '__main__':
    main()
