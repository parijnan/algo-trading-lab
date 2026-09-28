"""
Trade logs and the combined session report (plan section 2, Reporting; section 7, the combined P&L and margin report).

TradeLogWriter appends each `report_trade` record to `<dir>/<engine>_trades.csv` in Prometheus's fixed 26-column format
(interface.TRADE_RECORD_COLUMNS), so the file's shape never depends on which keys a trade happened to carry.

`build_session_report` is one message for all engines: per engine its state, open position, realised P&L this session and
request counts; the account's free cash; anything left UNCONFIRMED; ledger mismatches; alert counts. Rs P&L is summed from the
engines' own trade records (`total_pnl_rs`), so the report inherits each engine's per-unit convention.
"""

from __future__ import annotations

import csv
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from hestia_core.interface import OutcomeStatus, TRADE_RECORD_COLUMNS


class TradeLogWriter:

    def __init__(self, directory: os.PathLike):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, engine: str) -> Path:
        return self.dir / f'{engine}_trades.csv'

    def write(self, engine: str, record: dict) -> None:
        p = self.path(engine)
        new = not p.exists()
        with open(p, 'a', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(TRADE_RECORD_COLUMNS))
            if new:
                w.writeheader()
            w.writerow({c: ('' if record.get(c) is None else record.get(c)) for c in TRADE_RECORD_COLUMNS})


def build_session_report(core, now: datetime, session_trades: List[tuple]) -> str:
    """`session_trades` is the list of (engine, record) reported this session."""
    lines = [f'*Hestia session report* {now:%Y-%m-%d %H:%M}']
    by_engine: Dict[str, list] = {}
    for eng, rec in session_trades:
        by_engine.setdefault(eng, []).append(rec)
    total = 0.0
    for name in sorted(core._factories):
        pnl = sum(float(r['total_pnl_rs']) for r in by_engine.get(name, []) if r.get('total_pnl_rs') not in (None, ''))
        total += pnl
        positions = [(tok, v[0], v[1]) for (e, tok), v in core._ledger.items() if e == name and v[0]]
        held = ', '.join(f"{(core.data.ref_for(t).symbol if core.data.ref_for(t) else t)} {n:+d} @ {a:.2f}" if a else f'{t} {n:+d}'
                         for t, n, a in positions) or 'flat'
        statuses = Counter(r.outcome.status.value for r in core._registry.values()
                           if r.engine == name and r.outcome is not None)
        mode = 'paper' if core.broker.pool_of(name) != 'live' else 'live'
        lines.append(f"- {name} ({mode}, {core.engine_state.get(name, '?')}): {held}; {len(by_engine.get(name, []))} trade(s), "
                     f"Rs {pnl:,.0f}; requests {dict(statuses) or '-'}")
    lines.append(f'- combined realised Rs {total:,.0f}')
    try:
        lines.append(f"- account free cash Rs {core.broker.free_cash(''):,.0f}")               # the live account, not a paper pool
    except Exception:                                                # noqa: BLE001
        pass
    unconfirmed = core.unconfirmed_requests()
    if unconfirmed:
        lines.append(f'- UNCONFIRMED requests (position unknown, check the broker): {unconfirmed}')
    if core.ledger_mismatches:
        lines.append(f'- LEDGER MISMATCHES (engines vs broker book): {core.ledger_mismatches}')
    counts = Counter(a.level for a in core.alerts)
    if counts:
        lines.append(f'- alerts this session: {dict(counts)}')
    return '\n'.join(lines)
