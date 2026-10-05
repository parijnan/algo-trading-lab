"""
Preflight: everything that can be checked about a Hestia start WITHOUT logging in to Angel One or touching the network.

    python hestia.py --check

Reads configuration, the pipeline's data files, the runtime files under hestia_data/ and the flags, and reports each check as ok,
warn or fail. A fail means Hestia would refuse to start or would start into a known problem; a warn is worth a look. It writes
nothing and never opens a broker session: it is safe to run beside a live process and on a market holiday.
"""

from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from hestia_core.flags import FlagFiles
from hestia_core.mcx_market import ContractCatalog, MarketCalendar
from hestia_core.roll_policy import tracked_contracts
from hestia_core.session_lock import holder, pid_alive

OK, WARN, FAIL = 'ok', 'warn', 'fail'
CRED_COLUMNS = ('api_key', 'user_name', 'password', 'qr_code', 'slack_token')
STALE_DATA_DAYS = 4                       # the pipeline runs nightly on weekdays; a longer gap means it has not been syncing
HOLIDAY_HORIZON_DAYS = 30


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _tail_ts(path: Path):
    """(first, last) timestamp of a 1-minute file, reading only the time column."""
    ts = pd.read_csv(path, usecols=['time_stamp'])['time_stamp']
    ts = pd.to_datetime(ts, errors='coerce').dropna()
    return (ts.min(), ts.max()) if len(ts) else (None, None)


def run_checks(cfg, now: Optional[datetime] = None, alive: Callable[[int], bool] = pid_alive) -> List[Check]:
    now = now or datetime.now()
    today = now.date()
    out: List[Check] = []

    def add(name, status, detail):
        out.append(Check(name, status, detail))

    if getattr(cfg, 'LOCAL_OVERRIDES_PRESENT', False):
        add('trading host', OK, f"{getattr(cfg, 'HOSTNAME', '?')} is a configured trading host (or has a hestia_local.py)")
    else:
        add('trading host', WARN, f"{getattr(cfg, 'HOSTNAME', '?')} is not a trading host: nothing can be enabled here (correct anywhere but Delos)")
    engines = {n: e for n, e in cfg.ENGINES.items() if e.enabled}
    if not engines:
        add('engines', FAIL, 'no engine is enabled in hestia_config.ENGINES: Hestia exits before logging in')
    else:
        add('engines', OK, ', '.join(f'{n} ({"paper" if e.paper else "LIVE"}, {e.instrument}, {e.static_units} unit(s), cap {e.unit_cap})'
                                     for n, e in engines.items()))

    # ---- each engine builds and matches its registry entry -----------------------------------------------------------------
    for name, e in engines.items():
        try:
            module, _, attr = e.factory.partition(':')
            engine = getattr(importlib.import_module(module), attr)()
            spec = engine.spec
            if spec.instrument != e.instrument:
                add(f'engine {name}', FAIL, f'its spec says {spec.instrument} but the registry says {e.instrument}')
            elif e.static_units > e.unit_cap:
                add(f'engine {name}', FAIL, f'static_units {e.static_units} exceeds the unit cap {e.unit_cap}')
            else:
                add(f'engine {name}', OK, f'{e.factory} builds; {spec.instrument} {spec.timeframe_min}m ST({spec.st_period}, {spec.st_multiplier})')
        except Exception as exc:                                         # noqa: BLE001
            add(f'engine {name}', FAIL, f'{e.factory} did not build: {exc!r}')

    # ---- credentials (column names only, never values) ---------------------------------------------------------------------
    try:
        cols = set(pd.read_csv(cfg.CREDS_FILE, nrows=0).columns)
        missing = [c for c in CRED_COLUMNS if c not in cols]
        add('credentials', FAIL if missing else OK, f'missing columns {missing}' if missing else f'{Path(cfg.CREDS_FILE).name} has the login columns')
    except Exception as exc:                                             # noqa: BLE001
        add('credentials', FAIL, f'{cfg.CREDS_FILE}: {exc!r}')

    # ---- calendar and instrument master ------------------------------------------------------------------------------------
    calendar = MarketCalendar(cfg.MCX_HOLIDAYS_FILE)
    if calendar.missing:
        add('holidays', WARN, f'{Path(cfg.MCX_HOLIDAYS_FILE).name} not found: trading-day counts will only exclude weekends')
    else:
        last = max(calendar.rows) if calendar.rows else None
        if last is None or last < today + timedelta(days=HOLIDAY_HORIZON_DAYS):
            add('holidays', WARN, f'the holiday list ends {last}: less than {HOLIDAY_HORIZON_DAYS} days ahead, the roll-day count may be off')
        else:
            add('holidays', OK, f'holiday list to {last}')
    add('today', WARN if calendar.fully_closed(today) else OK,
        'MCX is closed today: Hestia would not start' if calendar.fully_closed(today)
        else ('evening session only today' if calendar.evening_only(today) else 'a normal trading day'))
    try:
        catalog = ContractCatalog(cfg.INSTRUMENT_MASTER_FILE, cfg.MCX_DATA_DIR)
    except Exception as exc:                                             # noqa: BLE001
        add('instrument master', FAIL, f'{cfg.INSTRUMENT_MASTER_FILE}: {exc!r}')
        catalog = None

    # ---- per-instrument contracts and history --------------------------------------------------------------------------------
    for name, e in engines.items():
        if catalog is None:
            break
        rows = catalog.live_rows(e.instrument, today)
        if not rows:
            add(f'{e.instrument} contracts', FAIL, 'no live contract in the instrument master')
            continue
        info = catalog.infos(e.instrument, today, calendar)
        add(f'{e.instrument} contracts', OK if len(rows) >= 2 else WARN,
            ', '.join(f'{i.ref.symbol} ({i.trading_days_left} trading days left)' for i in info[:3])
            + ('' if len(rows) >= 2 else ' | no next contract listed: a roll would flatten'))
        # Roll-aware: the contracts prepare() will actually try to seed today, not just the raw front-2-by-expiry --
        # a stale, already-rolled-off contract's own data gap must not be reported as ok just because rows[:2] happened
        # to include it (the 2026-09-30 incident this check exists to catch).
        n_track = cfg.LIVE_DATA.get('seed_contracts_per_instrument', 2)
        tracked_refs = set(tracked_contracts([r.ref for r in rows], today, calendar.fully_closed_dates(), n_track))
        tracked_rows = [r for r in rows if r.ref in tracked_refs]
        for r in tracked_rows:
            path = Path(r.filepath)
            if not path.exists():
                add(f'{r.ref.symbol} data', FAIL if r is tracked_rows[0] else WARN, f'{path.name} not found')
                continue
            first, last = _tail_ts(path)
            need = today - timedelta(days=cfg.LIVE_DATA['seed_days'])
            stale = last is None or (today - last.date()).days > STALE_DATA_DAYS
            short = first is None or first.date() > need
            status = FAIL if (r is tracked_rows[0] and (stale or short)) else (WARN if (stale or short) else OK)
            add(f'{r.ref.symbol} data', status, f'{path.name}: {first} to {last}' + ('; STALE' if stale else '')
                + (f'; history shorter than the {cfg.LIVE_DATA["seed_days"]}-day seed: start-up will fetch the older days from the '
                   f'broker into a private file (one-off broker candle calls)' if short else ''))

    # ---- runtime files -------------------------------------------------------------------------------------------------------
    flags = FlagFiles(cfg.FLAG_DIR)
    for name in engines:
        cmd = flags.read_command(name)
        if cmd:
            add(f'{name} flag', FAIL if cmd in ('DISABLE', 'KILL') else WARN, f'{cfg.FLAG_DIR}/{name}_command.flag says {cmd}')
    if flags.host_flag_present():
        add('host flag', WARN, 'hestia_active.flag exists: a previous run did not shut down cleanly, or Hestia is running now')
    state_path = Path(cfg.STATE_DIR)
    for name in engines:
        p = state_path / f'{name}_state.json'
        if not p.exists():
            add(f'{name} state', OK, 'no saved state: the engine starts blank (trade counter 0). Seed it first if trades already exist '
                                     '(python -m prometheus_engine.seed_state)')
            continue
        try:
            blob = json.loads(json.loads(p.read_text())['blob'])
            add(f'{name} state', OK, f"{blob.get('status')} {blob.get('direction') or ''}, trade counter {blob.get('trade_counter')}, "
                                     f"watermark {blob.get('last_processed_boundary')}".replace('  ', ' '))
        except Exception as exc:                                         # noqa: BLE001
            add(f'{name} state', FAIL, f'{p.name} is unreadable: {exc!r}')
    ledger = state_path / 'ledger.json'
    if ledger.exists():
        add('ledger', OK, f'{ledger.name} present ({ledger.stat().st_size} bytes): the broker book will be checked against it at start')

    # ---- the candle source and the Fyers token (no network: only reads the token file) ------------------------------------------
    from hestia_core.fyers_shadow import MODES, TokenGate
    cs = dict(getattr(cfg, 'CANDLE_SOURCE', None) or {})
    mode = cs.get('mode', 'angel')
    if mode not in MODES:
        add('candle source', FAIL, f'CANDLE_SOURCE mode {mode!r} is not one of {MODES}')
    else:
        if mode == 'angel':
            detail = 'angel only: no Fyers code runs'
        elif mode == 'shadow':
            detail = f"shadow: Fyers queried in parallel for {', '.join(cs.get('instruments', []))}, recorded to {cfg.SHADOW_DIR}; no decision uses it"
        else:
            detail = (f"rescue: Angel One first; Fyers asked only after {cs.get('rescue_after_attempts', 5)} failed Angel One attempt(s), "
                      f"minute must have settled {cs.get('settle_s', 0.0)}s, fills only missing minutes; also recording to {cfg.SHADOW_DIR}")
        add('candle source', OK, detail)
        if hasattr(cfg, 'FYERS_TOKEN_FILE'):
            ist = ZoneInfo('Asia/Kolkata')
            gate = TokenGate(cfg.FYERS_TOKEN_FILE, getattr(cfg, 'FYERS_OFF_FLAG', None),
                             clock=lambda: now.astimezone(ist) if now.tzinfo else now.replace(tzinfo=ist))
            st = gate.check()
            if st.ok:
                seen = ('the engines see only Angel One' if mode != 'rescue'
                        else 'Angel One first; Fyers fills a window only after the whole Angel One burst has failed')
                add('fyers token', OK, f'usable (fingerprint {st.fingerprint}); {seen}')
            else:
                add('fyers token', OK if mode == 'angel' else WARN,
                    f'not usable: {st.reason}' + ('' if mode == 'angel' else f"; {mode} would record nothing and rescue nothing today"))

    # ---- who else holds the account ----------------------------------------------------------------------------------------
    for label, pid_file in getattr(cfg, 'LEGACY_PID_FILES', {}).items():
        try:
            pid = int(Path(pid_file).read_text().strip())
        except (FileNotFoundError, ValueError):
            add(label, OK, 'no pid file')
            continue
        add(label, FAIL if alive(pid) else WARN,
            f'pid {pid} is RUNNING: Hestia would refuse to start' if alive(pid) else f'stale pid file (pid {pid} is not running)')
    held = holder(cfg.SESSION_LOCK_FILE)
    if held is not None and held.get('owner') != 'hestia':
        add('session lock', FAIL, f"held by {held.get('owner')} (pid {held.get('pid')})")
    else:
        add('session lock', OK, 'free' if held is None else f"held by hestia (pid {held.get('pid')})")
    return out


def format_report(checks: List[Check]) -> str:
    lines = [f'  [{c.status.upper():4s}] {c.name}: {c.detail}' for c in checks]
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    lines.append(f'{len(checks)} checks: {fails} fail, {warns} warn' + ('  -> Hestia would NOT start cleanly' if fails else ''))
    return '\n'.join(lines)
