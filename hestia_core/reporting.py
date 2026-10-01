"""
Trade logs and the combined session report (plan section 2, Reporting; section 7, the combined P&L and margin report).

TradeLogWriter appends each `report_trade` record to `<dir>/<engine>_trades.csv` in Prometheus's fixed 26-column format
(interface.TRADE_RECORD_COLUMNS), so the file's shape never depends on which keys a trade happened to carry.

RunningRowWriter appends each `report_running_row` record to `<dir>/<engine>/trade_{id:04d}_{entry_ts}.csv` (interface.
RUNNING_ROW_COLUMNS) -- one file per trade, one row roughly every 60s while it is open, ported 2026-09-29 from production's
own `append_trade_log_row`/`_append_running_row` after the user found it missing from the original build.

`build_session_report` is one message for all engines, in the standalone Prometheus report's layout: a section per engine with a
block per closed trade, an open-position block and a Realized / Unrealized total in Rs per unit; then the account's free cash and,
only when present, UNCONFIRMED requests, ledger mismatches and warning-or-worse alert counts.
"""

from __future__ import annotations

import csv
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from hestia_core.display import display_name
from hestia_core.interface import RUNNING_ROW_COLUMNS, TRADE_RECORD_COLUMNS


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


DIVIDER = '\u2501' * 37


def _ts_str(ts, today) -> str:
    """HH:MM for a same-day timestamp, 'dd-Mon HH:MM' otherwise: a position can span sessions, and an entry from the
    previous evening must not read like a same-day time (the standalone Prometheus report's own rule, 2026-09-11)."""
    if not ts:
        return '?'
    try:
        t = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
    except (ValueError, TypeError):
        return '?'
    return t.strftime('%H:%M') if t.date() == today else t.strftime('%d-%b %H:%M')


def _units(n: float) -> str:
    return f'{n:g}'


def build_session_report(core, now: datetime, session_trades: List[tuple]) -> str:
    """The end-of-session Slack report, in the standalone Prometheus process's own layout (dividers, a block per trade, a
    Realized / Unrealized total in Rs per unit, so it tracks the strategy rather than whatever sizing was live) with one section
    per engine. `session_trades` is the list of (engine, record) closed this session. Replaced 2026-10-01: the previous
    one-line-per-engine form carried raw request-status dicts, token-level ledger text and a combined rupee total across paper and
    live engines, and was unreadable in Slack.

    Rs/unit for a closed trade is the trade's own total divided by its own unit count. For the open position it is the
    mark-to-market per lot times the lots in a unit. A part-booked position (an engine that books one lot early) shows only its
    open lots here: the booked part appears in the engine's own trade row when the trade closes."""
    today = now.date()
    owner_instrument = {eng: ins for ins, eng in core._by_instrument.items()}
    by_engine: Dict[str, list] = {}
    for eng, rec in session_trades:
        by_engine.setdefault(eng, []).append(rec)

    def mode_of(name: str) -> str:
        return 'paper' if core.broker.pool_of(name) != 'live' else 'live'

    lines = [f"\U0001f4ca *Hestia \u2014 Session Report*  |  {now:%a %d %b %Y}", '', DIVIDER, '']
    for name in sorted(core._factories, key=lambda n: (mode_of(n) != 'live', n)):
        mode = mode_of(name)
        state = core.engine_state.get(name, '?')
        flag = f'  \u00b7  \u26a0\ufe0f {state}' if state in ('killed', 'failed') else ''
        lines.append(f"*{display_name(name)}* [{owner_instrument.get(name, '?')}]  \u00b7  {mode.capitalize()}{flag}")
        lpu = core._lots_per_unit.get(name, 1) or 1
        recs = sorted(by_engine.get(name, []), key=lambda r: r.get('trade_id') or 0)
        positions = [(tok, v[0], v[1], v[2]) for (e, tok), v in sorted(core._ledger.items()) if e == name and v[0]]
        realized, unrealized, unrealized_known = 0.0, 0.0, True

        if not recs and not positions:
            lines.append('  \u21b3 No trade today')
        for r in recs:
            direction = str(r.get('direction') or '?').capitalize()
            units = r.get('units') or 1
            entry_price = r.get('entry_price')
            entry_str = f'{entry_price:,.2f}' if isinstance(entry_price, (int, float)) else '?'
            exit_ts = max((t for t in (r.get('lot1_exit_ts'), r.get('lot2_exit_ts')) if t), default=None)
            reason = str(r.get('lot2_exit_reason') or r.get('lot1_exit_reason') or '?').replace('_', ' ').title()
            pts = r.get('total_pnl_points') or 0
            per_unit = (r.get('total_pnl_rs') or 0) / units
            realized += per_unit
            lines.append(f"*Trade #{r.get('trade_id')}*  \u00b7  {direction}  |  Units: {_units(units)}")
            lines.append(f"  \u21b3 Entry: {_ts_str(r.get('entry_ts'), today)} @ {entry_str}   "
                         f"Exit: {_ts_str(exit_ts, today)}  \u00b7  {reason}")
            lines.append(f"  \u21b3 P&L        : *{pts:+.1f} pts  ({per_unit:+,.0f} Rs/unit)*")
            lines.append('')

        for tok, net, avg, since in positions:
            ref = core.data.ref_for(tok)
            direction = 'Bullish' if net > 0 else 'Bearish'
            units = abs(net) / lpu
            entry = f'{avg:,.2f}' if avg else '?'
            lines.append(f"*Open Position*  \u00b7  {direction}  |  Units: {_units(units)}")
            lines.append(f"  \u21b3 Entry: {_ts_str(since, today)} @ {entry}   Still open at session end")
            try:
                q = core.data.ltp_quote(tok)
                info = core.data.info(ref) if ref else None
            except Exception:                                         # noqa: BLE001 - a quote failure never blocks the report
                q = info = None
            if q is not None and avg and info is not None:
                pts = (q.price - avg) if net > 0 else (avg - q.price)
                per_unit = pts * info.lot_size * lpu
                unrealized += per_unit
                lines.append(f"  \u21b3 Unrealised : {pts:+.1f} pts  ({per_unit:+,.0f} Rs/unit)   LTP {q.price:,.2f}")
            else:
                unrealized_known = False
                lines.append('  \u21b3 Unrealised : price unavailable')
            lines.append('')

        if recs or positions:
            if not positions:
                unrealized_text = '*+0 Rs/unit*'
            else:
                unrealized_text = f'*{unrealized:+,.0f} Rs/unit*' if unrealized_known else 'n/a'
            lines.append(f'  \u21b3 Realized   : *{realized:+,.0f} Rs/unit*')
            lines.append(f'  \u21b3 Unrealized : {unrealized_text}')
        lines.append('')
        lines.append(DIVIDER)
        lines.append('')

    try:
        lines.append(f"Account free cash: Rs {core.broker.free_cash(''):,.0f}")          # the live account, not a paper pool
    except Exception:                                                # noqa: BLE001
        pass
    unconfirmed = core.unconfirmed_requests()
    if unconfirmed:
        lines.append(f'\U0001f6a8 UNCONFIRMED requests (position unknown, check the broker): {unconfirmed}')
    if core.ledger_mismatches:
        lines.append(f'\U0001f6a8 LEDGER MISMATCHES (engines vs broker book): {core.ledger_mismatches}')
    counts = Counter(a.level for a in core.alerts)
    noisy = [f'{counts[k]} {k}' for k in ('critical', 'error', 'warning') if counts.get(k)]
    if noisy:
        lines.append(f"\u26a0\ufe0f Alerts this session: {', '.join(noisy)}")
    return '\n'.join(lines).rstrip() + '\n'
