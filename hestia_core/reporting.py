"""
Trade logs and the combined session report (plan section 2, Reporting; section 7, the combined P&L and margin report).

TradeLogWriter appends each `report_trade` record to `<dir>/<engine>_trades.csv` in Prometheus's fixed 26-column format
(interface.TRADE_RECORD_COLUMNS), so the file's shape never depends on which keys a trade happened to carry.

RunningRowWriter appends each `report_running_row` record to `<dir>/<engine>/trade_{id:04d}_{entry_ts}.csv` (interface.
RUNNING_ROW_COLUMNS) -- one file per trade, one row roughly every 60s while it is open, ported 2026-09-29 from production's
own `append_trade_log_row`/`_append_running_row` after the user found it missing from the original build.

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

from hestia_core.interface import OutcomeStatus, RUNNING_ROW_COLUMNS, TRADE_RECORD_COLUMNS


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


class RunningRowWriter:

    def __init__(self, directory: os.PathLike):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, engine: str, record: dict) -> Path:
        d = self.dir / engine
        d.mkdir(parents=True, exist_ok=True)
        trade_id = record.get('trade_id')
        entry_ts = record.get('entry_ts') or record.get('ts') or ''       # RUNNING_ROW_COLUMNS carries entry_ts on every row,
        stamp = str(entry_ts).replace(':', '').replace('-', '')[:13]      # precisely so the filename can be stable per trade
        return d / f'trade_{int(trade_id or 0):04d}_{stamp}.csv'

    def write(self, engine: str, record: dict) -> None:
        p = self.path(engine, record)
        new = not p.exists()
        with open(p, 'a', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(RUNNING_ROW_COLUMNS))
            if new:
                w.writeheader()
            w.writerow({c: ('' if record.get(c) is None else record.get(c)) for c in RUNNING_ROW_COLUMNS})


def _hm(ts) -> str:
    """HH:MM off an ISO string or datetime; '?' if unparseable. Session-report trades are always same-day (session_trades
    is this session's own list), so no date qualifier is needed the way the standalone Prometheus process's own
    multi-day CSV-backed report needed one."""
    if not ts:
        return '?'
    try:
        return (ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))).strftime('%H:%M')
    except (ValueError, TypeError):
        return '?'


def build_session_report(core, now: datetime, session_trades: List[tuple]) -> str:
    """`session_trades` is the list of (engine, record) reported this session. Per-trade detail and an open-position line
    per engine, added 2026-09-29 after the user found this report much thinner than the standalone Prometheus process's
    own per-trade session report (entry/exit price+time+reason, P&L) -- kept as nested lines under each engine's own
    summary line rather than the standalone's flat per-instrument shape, since Hestia can host several engines in one
    report where the standalone only ever reported on itself."""
    lines = [f'*Hestia session report* {now:%Y-%m-%d %H:%M}']
    by_engine: Dict[str, list] = {}
    for eng, rec in session_trades:
        by_engine.setdefault(eng, []).append(rec)
    total = 0.0
    for name in sorted(core._factories):
        recs = by_engine.get(name, [])
        pnl = sum(float(r['total_pnl_rs']) for r in recs if r.get('total_pnl_rs') not in (None, ''))
        total += pnl
        positions = [(tok, v[0], v[1]) for (e, tok), v in core._ledger.items() if e == name and v[0]]
        held = ', '.join(f"{(core.data.ref_for(t).symbol if core.data.ref_for(t) else t)} {n:+d} @ {a:.2f}" if a else f'{t} {n:+d}'
                         for t, n, a in positions) or 'flat'
        statuses = Counter(r.outcome.status.value for r in core._registry.values()
                           if r.engine == name and r.outcome is not None)
        mode = 'paper' if core.broker.pool_of(name) != 'live' else 'live'
        lines.append(f"- {name} ({mode}, {core.engine_state.get(name, '?')}): {held}; {len(recs)} trade(s), "
                     f"Rs {pnl:,.0f}; requests {dict(statuses) or '-'}")

        for r in sorted(recs, key=lambda r: r.get('trade_id') or 0):
            direction = str(r.get('direction') or '?').capitalize()
            entry_price = r.get('entry_price')
            entry_str = f'{entry_price:.2f}' if isinstance(entry_price, (int, float)) else str(entry_price)
            exit_ts = max((t for t in (r.get('lot1_exit_ts'), r.get('lot2_exit_ts')) if t), default=None)
            exit_reason = r.get('lot2_exit_reason') or r.get('lot1_exit_reason') or '?'
            trade_units = r.get('units') or 1
            pnl_pts = r.get('total_pnl_points') or 0
            pnl_rs_per_unit = (r.get('total_pnl_rs') or 0) / trade_units
            lines.append(f"    #{r.get('trade_id')} {direction} (units {trade_units}): entry {_hm(r.get('entry_ts'))} @ "
                         f"{entry_str}, exit {_hm(exit_ts)} {exit_reason}, "
                         f"P&L {pnl_pts:+.1f} pts ({pnl_rs_per_unit:+,.0f} Rs/unit)")

        for tok, net, avg in positions:
            ref = core.data.ref_for(tok)
            symbol = ref.symbol if ref else tok
            try:
                q = core.data.ltp_quote(tok)
            except Exception:                                         # noqa: BLE001 - a quote failure never blocks the report
                q = None
            if q is not None and avg:
                pts = (q.price - avg) if net > 0 else (avg - q.price)
                lines.append(f"    open: {symbol} {net:+d} @ {avg:.2f}, LTP {q.price:.2f} ({pts:+.2f} pts unrealised/lot)")
            else:
                lines.append(f"    open: {symbol} {net:+d} @ {avg:.2f}" if avg else f"    open: {symbol} {net:+d}")
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
