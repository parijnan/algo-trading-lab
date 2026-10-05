"""
How long after a minute closes does Fyers's candle for it stop changing? The Phase 1 shadow showed Fyers returning the just-closed minute ~0.07 s after the
tick, but those first-seen values disagree with finalized history far more than finalized history disagrees with Angel One (2026-10-05: first-seen differs
from final in 22%-82% of minutes). This probe takes repeated snapshots of each just-closed minute at fixed offsets after the minute boundary, and compares
every snapshot with the finalized value fetched at the end of the run.

Fyers-only (never Angel One), read-only, laptop. One thread per boundary, one call per contract per offset.

    python research/fyers_mcx_validation/settle_probe.py --minutes 10 --out settle_probe.csv
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from hestia_core.fyers_shadow import FyersClient, TokenGate, fyers_symbol      # noqa: E402

CONTRACTS = {'CRUDEOILM': (2026, 10, 19), 'SILVERMIC': (2026, 11, 30), 'GOLDPETAL': (2026, 10, 30), 'NATGASMINI': (2026, 10, 27)}
OFFSETS = (0.1, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0)
COLS = ['open', 'high', 'low', 'close', 'volume']


def snapshot(client, auth, instrument, expiry, boundary, offset, rows, lock):
    sym = fyers_symbol(instrument, expiry)
    target = boundary + timedelta(seconds=offset)
    time.sleep(max(0.0, (target - datetime.now()).total_seconds()))
    res = client.minutes(sym, boundary - timedelta(minutes=5), boundary, auth)
    closed = boundary - timedelta(minutes=1)
    row = {'boundary': boundary, 'instrument': instrument, 'offset_s': offset, 'kind': res.kind, 'latency_ms': round(res.latency_ms, 1)}
    if res.kind == 'ok' and res.frame is not None:
        hit = res.frame[res.frame['time_stamp'] == closed]
        row['present'] = int(not hit.empty)
        if not hit.empty:
            row.update({c: float(hit[c].iloc[0]) for c in COLS})
    with lock:
        rows.append(row)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--minutes', type=int, default=10)
    ap.add_argument('--out', default='settle_probe.csv')
    ap.add_argument('--offsets', default=None, help='comma-separated seconds after the boundary, e.g. 0.1,0.2,0.3')
    ap.add_argument('--rotate', action='store_true', help='one instrument per boundary (rotating), to stay under the Fyers ~10 calls/s limit with fine offsets')
    args = ap.parse_args()
    gate = TokenGate(REPO / 'hestia_data' / 'fyers_token.json').check()
    if not gate.ok:
        print('no usable token:', gate.reason)
        return 1
    offsets = tuple(float(x) for x in args.offsets.split(',')) if args.offsets else OFFSETS
    client = FyersClient(timeout_s=10.0)
    rows, lock, threads = [], threading.Lock(), []
    first = (datetime.now() + timedelta(minutes=1)).replace(second=0, microsecond=0)
    boundaries = [first + timedelta(minutes=k) for k in range(args.minutes)]
    print(f'probing {args.minutes} boundaries from {first:%H:%M:%S}; offsets {offsets}', flush=True)
    names = list(CONTRACTS)
    for k, b in enumerate(boundaries):
        for instrument, (y, m, d) in CONTRACTS.items():
            if args.rotate and instrument != names[k % len(names)]:
                continue
            for off in offsets:
                t = threading.Thread(target=snapshot, args=(client, gate.auth, instrument, datetime(y, m, d).date(), b, off, rows, lock), daemon=True)
                t.start()
                threads.append(t)
        time.sleep(max(0.0, (b + timedelta(minutes=1) - datetime.now()).total_seconds()))
    for t in threads:
        t.join(timeout=120)
    # the finalized value of every probed minute, fetched once at the end
    time.sleep(30)
    final = {}
    for instrument, (y, m, d) in CONTRACTS.items():
        res = client.minutes(fyers_symbol(instrument, datetime(y, m, d).date()), boundaries[0] - timedelta(minutes=2), datetime.now(), gate.auth)
        if res.kind == 'ok':
            final[instrument] = res.frame.set_index('time_stamp')
    df = pd.DataFrame(rows)
    for c in COLS:
        df[f'final_{c}'] = [final.get(i, pd.DataFrame()).get(c, pd.Series(dtype=float)).get(b - timedelta(minutes=1)) for i, b in zip(df.instrument, df.boundary)]
    df.to_csv(args.out, index=False)
    print(f'wrote {len(df)} rows to {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
