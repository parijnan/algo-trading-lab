"""
Durable state for the Hestia core: what must survive a process restart so that a restarted engine is never told "unknown" about a
request that was in fact sent (plan sections 1.4 and 1.6).

Three things, all under one directory, all written atomically or append-only with an fsync:
  * each engine's own decision state (an opaque string it hands to `save_state`),
  * the ledger (net lots and average price per engine and token; a paper engine's position lives nowhere else),
  * a request journal: a `submit` line when a request is accepted and a `final` line when its outcome is known (and
    `unconfirmed` lines for the interim), one file per date. On restart a request with a `submit` and no `final` is IN DOUBT: it
    may have reached the broker before the crash, so it is never re-sent and never reported as unknown, and a critical alert asks
    for the broker book to be checked (the ledger is re-adopted from the broker for live engines).

Only outcomes are restored, not request objects: an engine that resumes asks `request_status(id)` and either gets the final
answer or the in-doubt one.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from hestia_core.interface import FillSummary, OutcomeStatus, RequestKind, RequestOutcome


def _plain(obj):
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _plain(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def encode_outcome(o: RequestOutcome) -> dict:
    def summary(s: Optional[FillSummary]):
        return None if s is None else {'lots': s.lots, 'avg_price': s.avg_price}
    return {'request_id': o.request_id, 'status': o.status.value, 'kind': o.kind.value, 'requested_lots': o.requested_lots,
            'ts': o.ts.isoformat(), 'closed': summary(o.closed), 'opened': summary(o.opened), 'detail': o.detail}


def decode_outcome(d: dict) -> RequestOutcome:
    def summary(s):
        return None if s is None else FillSummary(lots=s['lots'], avg_price=s['avg_price'], fills=())
    return RequestOutcome(d['request_id'], OutcomeStatus(d['status']), RequestKind(d['kind']), d['requested_lots'],
                          datetime.fromisoformat(d['ts']), summary(d.get('closed')), summary(d.get('opened')), d.get('detail', ''))


class StateStore:

    def __init__(self, directory: os.PathLike, journal_days: int = 7):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.journal_days = journal_days

    # -- engine decision state ---------------------------------------------------------------------------------------
    def _atomic(self, path: Path, text: str) -> None:
        tmp = path.with_suffix(path.suffix + '.tmp')
        with open(tmp, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def save_engine_state(self, name: str, blob: str) -> None:
        self._atomic(self.dir / f'{name}_state.json', json.dumps({'saved': datetime.now().isoformat(timespec='seconds'),
                                                                  'blob': blob}))

    def load_engine_state(self, name: str) -> Optional[str]:
        try:
            return json.loads((self.dir / f'{name}_state.json').read_text())['blob']
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            return None

    # -- ledger --------------------------------------------------------------------------------------------------------------
    def save_ledger(self, ledger: Dict[Tuple[str, str], list]) -> None:
        rows = [{'engine': e, 'token': t, 'net': v[0], 'avg': v[1], 'ts': v[2].isoformat() if v[2] else None}
                for (e, t), v in sorted(ledger.items()) if v[0]]
        self._atomic(self.dir / 'ledger.json', json.dumps({'rows': rows}))

    def load_ledger(self) -> Dict[Tuple[str, str], list]:
        try:
            rows = json.loads((self.dir / 'ledger.json').read_text())['rows']
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            return {}
        return {(r['engine'], r['token']): [r['net'], r['avg'], datetime.fromisoformat(r['ts']) if r['ts'] else None] for r in rows}

    # -- request journal -----------------------------------------------------------------------------------------------------
    def _journal_path(self, d: date) -> Path:
        return self.dir / f'requests_{d.isoformat()}.jsonl'

    def journal(self, record: dict, when: datetime) -> None:
        with open(self._journal_path(when.date()), 'a') as f:
            f.write(json.dumps(_plain(record)) + '\n')
            f.flush()
            os.fsync(f.fileno())

    def load_journal(self, today: date) -> List[dict]:
        files = sorted(self.dir.glob('requests_*.jsonl'))[-self.journal_days:]
        out = []
        for p in files:
            for line in p.read_text().splitlines():
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue                              # a torn last line after a crash: ignore it
        return out
