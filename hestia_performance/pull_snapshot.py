"""
Runs ON DELOS, read-only: prints one JSON snapshot of everything the performance tracker needs, to stdout. It reads the four engines' closed-trade CSVs,
their saved state (for any open position), the ledger, and the last cached one-minute close of each traded contract. It writes nothing and calls no broker.

    ssh delos-ipv6 'cd ~/scripts/algo-trading-lab && python3 -I -' < hestia_performance/pull_snapshot.py > snapshot.json
"""

import csv
import datetime
import io
import json
import os

ENGINES = ('prometheus', 'selene', 'helios', 'typhon')
ROOT = os.path.expanduser('~/scripts/algo-trading-lab')
DATA = os.path.join(ROOT, 'hestia_data')


def read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def last_close(token):
    """Last row of the token's intraday cache: (timestamp, close), or None."""
    text = read_text(os.path.join(DATA, 'cache', f'{token}_today_1m.csv'))
    if not text:
        return None
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return None
    r = rows[-1]
    return {'ts': r['time_stamp'], 'close': float(r['close'])}


def main():
    out = {'pulled_at': datetime.datetime.now().isoformat(timespec='seconds'), 'engines': {}, 'ledger': None}
    for e in ENGINES:
        entry = {'trades_csv': read_text(os.path.join(DATA, 'trades', f'{e}_trades.csv')), 'state': None, 'last_price': None}
        raw = read_text(os.path.join(DATA, 'state', f'{e}_state.json'))
        if raw:
            wrapper = json.loads(raw)
            entry['state'] = json.loads(wrapper['blob'])
            entry['state_saved'] = wrapper.get('saved')
            token = entry['state'].get('contract_token')
            if token:
                entry['last_price'] = last_close(token)
        out['engines'][e] = entry
    ledger = read_text(os.path.join(DATA, 'state', 'ledger.json'))
    out['ledger'] = json.loads(ledger) if ledger else None
    print(json.dumps(out))


main()
