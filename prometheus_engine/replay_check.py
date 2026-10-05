"""
Tier 1 replay of the recorded live days through the Prometheus engine on the fake Hestia (plan hestia-p5, slice P5.4).

The engine is driven by the bars and Supertrend values live Prometheus itself logged (`recorded.LoggedReplayData`) and by the pipeline's
1-minute prices for the LTP path; the position live Prometheus carried into each day is seeded from the trades file. Each day is
replayed independently (a difference on one day never cascades into the next) and compared with what live Prometheus did: every entry
(time, direction, units) and every lot exit (lot, reason, time), taken from the log and cross-checked against `prometheus_trades.csv`.

What can and cannot be exact. Bar-driven decisions (entries, trend-flip exits) are exact: same bars in, same decisions out, to the
second. Price-driven exits (target, stop) depend on the price path: live watched real ticks every half second, replay only has the
1-minute file (open in the first half of the minute, close in the second), so a target the market touched between two minutes can
be seen a little later in replay or missed. Those are compared with a tolerance and any difference is reported, not hidden. Fill
prices differ for the same reason and are reported alongside.

    python -m prometheus_engine.replay_check [pull_dir]
"""

from __future__ import annotations

import dataclasses
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd

import hestia_core.recorded as rec
from hestia_core.fake import ContractSpec, FakeHestia
from hestia_core.interface import CommandKind, ContractRef, SizingConfig
from prometheus_engine.engine import PrometheusEngine
from prometheus_engine.engine_configs import DEFAULT, EngineConfig

# The config the recorded live days (2026-09-16 to 2026-09-28) actually ran under: ST multiplier 2.0 with SL 2.2 / T1 2.2 / T2 5.0. The engine's own
# DEFAULT moved to 2.5 on 2026-10-05, but a replay of those days only reproduces what live did if the engine runs the config live had, so every
# recorded-day entry point here defaults to this one.
RECORDED_CONFIG = dataclasses.replace(DEFAULT, st_multiplier=2.0, sl_pct=2.2, target1_pct=2.2, target2_flat_pct=5.0)
from prometheus_engine.levels import build_levels
from prometheus_engine.state import EngineState

REF = ContractRef('CRUDEOILM', '569901', 'CRUDEOILM19OCT26FUT', date(2026, 10, 19))
EXPIRY_KEY = '2026-10-19'
LOT_SIZE = 10
ENTRY_TOL_S = 10.0           # bar-driven decisions: the same boundary (live start-up decisions land a few seconds after 09:00)
BAR_EXIT_TOL_S = 10.0
PRICE_EXIT_TOL_S = 150.0     # price-driven exits: replay sees the 1-minute path, live saw ticks

_ENTRY = re.compile(r'^Entered (BULLISH|BEARISH)( \(rollover\))?\s+\S+ \| Units: (\d+)')
_EXIT = re.compile(r'^Lot(\d) exit: (\S+)\s+\(Units: (\d+)\)\s+Entry ([\d.]+) -> Exit ([\d.]+)')
BAR_REASONS = {'trend_flip', 'rollover', 'slack_exit'}


@dataclass(frozen=True)
class Decision:
    ts: datetime
    kind: str                 # 'entry' | 'exit'
    direction: Optional[str] = None
    units: Optional[int] = None
    lot: Optional[int] = None
    reason: Optional[str] = None
    price: Optional[float] = None


@dataclass
class DayReport:
    day: date
    live: List[Decision]
    replay: List[Decision]
    matched: int = 0
    differences: List[str] = field(default_factory=list)
    price_gaps: List[float] = field(default_factory=list)      # |replay price - live price| for every matched decision
    replay_orders: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def exact(self) -> bool:
        return not self.differences


# ---------------------------------------------------------------------------------------------------------------------
# The oracle and the replay's own decisions
# ---------------------------------------------------------------------------------------------------------------------

def live_decisions(session: rec.SessionRecord) -> List[Decision]:
    out = []
    for e in session.events:
        if e.kind == rec.ENTRY:
            d = e.data
            out.append(Decision(e.ts, 'entry', d['direction'], d['units'], price=d['entry']))
        elif e.kind == rec.EXIT:
            d = e.data
            out.append(Decision(e.ts, 'exit', lot=d['lot'], reason=d['reason'], price=d['exit'], units=d['units']))
    return out


def replay_decisions(h: FakeHestia) -> List[Decision]:
    """Entries and lot exits, read from the engine's own Slack alerts (each carries the simulated time and the fill price)."""
    out = []
    for a in h.alerts:
        m = _ENTRY.match(a.text)
        if m:
            entry = re.search(r'Entry: ([\d.]+)', a.text)
            out.append(Decision(a.ts, 'entry', m.group(1).lower(), int(m.group(3)), price=float(entry.group(1))))
            continue
        m = _EXIT.match(a.text)
        if m:
            out.append(Decision(a.ts, 'exit', lot=int(m.group(1)), reason=m.group(2), price=float(m.group(5)), units=int(m.group(3))))
    return out


def compare(day: date, live: Sequence[Decision], replay: Sequence[Decision]) -> DayReport:
    """Pair decisions in order within each kind and lot, and report what does not pair."""
    rep = DayReport(day, list(live), list(replay))
    used = set()

    def close_enough(a: Decision, b: Decision) -> bool:
        if a.kind != b.kind:
            return False
        dt = abs((a.ts - b.ts).total_seconds())
        if a.kind == 'entry':
            return a.direction == b.direction and a.units == b.units and dt <= ENTRY_TOL_S
        if a.lot != b.lot:
            return False
        tol = BAR_EXIT_TOL_S if (a.reason in BAR_REASONS and b.reason in BAR_REASONS) else PRICE_EXIT_TOL_S
        return a.reason == b.reason and dt <= tol

    for a in live:
        hit = next((i for i, b in enumerate(replay) if i not in used and close_enough(a, b)), None)
        if hit is None:
            rep.differences.append(f'live {a.kind} at {a.ts:%H:%M:%S}' + (f' lot{a.lot} {a.reason}' if a.kind == 'exit' else f' {a.direction} x{a.units}')
                                   + ' has no replay counterpart')
            continue
        used.add(hit)
        rep.matched += 1
        if a.price is not None and replay[hit].price is not None:
            rep.price_gaps.append(abs(a.price - replay[hit].price))
    for i, b in enumerate(replay):
        if i not in used:
            rep.differences.append(f'replay {b.kind} at {b.ts:%H:%M:%S}' + (f' lot{b.lot} {b.reason}' if b.kind == 'exit' else f' {b.direction} x{b.units}')
                                   + ' has no live counterpart')
    return rep


# ---------------------------------------------------------------------------------------------------------------------
# One replayed day
# ---------------------------------------------------------------------------------------------------------------------

def carried_state(day: date, trades: pd.DataFrame, cfg: EngineConfig, watermark: Optional[datetime]) -> Tuple[Optional[EngineState], int, float]:
    """The engine state live Prometheus carried into `day`, rebuilt from the trades file (levels recomputed, as the algo does at
    startup), and the ledger position it implies (signed lots, average price). A flat start returns a watching state."""
    start = datetime.combine(day, datetime.min.time())
    prev = trades[trades['entry_ts'] < start]
    counter = int(prev['trade_id'].max()) if len(prev) else 0
    carried = trades[(trades['entry_ts'] < start) & ((trades['lot1_exit_ts'] >= start) | (trades['lot2_exit_ts'] >= start))]
    wm = watermark.isoformat() if watermark else None
    if not len(carried):
        return EngineState(status='watching', trade_counter=counter, last_processed_boundary=wm), 0, 0.0
    t = carried.iloc[-1]
    d, u, ep = t['direction'].split('-')[0], int(t['units']), float(t['entry_price'])
    lv = build_levels(d, ep, 2 * u, u, cfg)
    l1_open, l2_open = t['lot1_exit_ts'] >= start, t['lot2_exit_ts'] >= start
    row = {'trade_id': int(t['trade_id']), 'contract_expiry': str(t['contract_expiry']), 'direction': t['direction'], 'units': u,
           'entry_ts': t['entry_ts'].isoformat(), 'entry_price': ep, 'signal_ts': str(t['signal_ts']), 'signal_close': float(t['signal_close']),
           'entry_slippage_points': None if pd.isna(t['entry_slippage_points']) else float(t['entry_slippage_points']),
           'sl_price': round(lv.sl_price, 2), 'lot1_target': round(lv.lot1_target, 2), 'lot2_target': round(lv.lot2_target, 2),
           'lot2_target_source': 'flat_pct', 'parent_trade_id': None}
    if not l1_open:                                            # lot 1 booked before the day began: its fields are already in the row
        row.update(lot1_exit_ts=t['lot1_exit_ts'].isoformat(), lot1_exit_price=float(t['lot1_exit_price']),
                   lot1_exit_reason=t['lot1_exit_reason'], lot1_pnl_points=float(t['lot1_pnl_points']), lot1_pnl_rs=float(t['lot1_pnl_rs']))
    st = EngineState(status='in_trade', direction=d, units=u, entry_price=ep, entry_ts=t['entry_ts'].isoformat(),
                     signal_ts=str(t['signal_ts']), signal_close=float(t['signal_close']), contract_token=REF.token,
                     contract_symbol=REF.symbol, contract_expiry=REF.expiry.isoformat(), sl_price=lv.sl_price, lot1_target=lv.lot1_target,
                     lot1_lots=u, lot1_status='open' if l1_open else 'booked',
                     lot1_exit_price=None if l1_open else float(t['lot1_exit_price']), lot2_target=lv.lot2_target,
                     lot2_source='flat_pct', lot2_lots=u, lot2_status='open' if l2_open else 'booked', trade_counter=counter,
                     trade_row=row, last_processed_boundary=wm)
    lots = (u if l1_open else 0) + (u if l2_open else 0)
    return st, lots * (1 if d == 'bullish' else -1), ep


def _units_for(day: date, session: rec.SessionRecord, carried: Optional[EngineState]) -> int:
    entries = session.of(rec.ENTRY)
    if entries:
        return entries[0].data['units']
    return carried.units if carried is not None and carried.units else 1


def replay_day(day: date, sessions: Sequence[rec.SessionRecord], frames: Dict[str, pd.DataFrame], trades: pd.DataFrame,
               cfg: EngineConfig = RECORDED_CONFIG, engine_factory: Optional[Callable[[], PrometheusEngine]] = None,
               setup: Optional[Callable[[FakeHestia], None]] = None, until_hm: str = '23:40') -> Tuple[FakeHestia, List[PrometheusEngine]]:
    """Run one recorded day through the engine on the fake Hestia and return it (call `h.close()` when done). `setup` runs after the
    session starts and before time is advanced, for fault injection."""
    session = next(s for s in sessions if s.day == day)
    ordered = sorted(sessions, key=lambda s: s.day)
    prior = [s for s in ordered if s.day < day and s.bars]
    watermark = prior[-1].bars[-1].data['bar_start'] if prior else None
    made: List[PrometheusEngine] = []

    def factory(kernel, holidays):
        data = rec.LoggedReplayData(kernel, sessions, frames, holidays)
        data.add_logged_contract(ContractSpec(REF, lot_size=LOT_SIZE, tick_size=1.0, freeze_qty_lots=1000, minutes=frames[EXPIRY_KEY]),
                                 EXPIRY_KEY)
        return data
    h = FakeHestia(datetime.combine(day, datetime.min.time()) + timedelta(hours=8, minutes=50), data_factory=factory)
    state, lots, avg = carried_state(day, trades, cfg, watermark)
    units = _units_for(day, session, state if state.status == 'in_trade' else None)

    def make():
        e = (engine_factory or (lambda: PrometheusEngine(cfg)))()
        made.append(e)
        return e
    h.register('prometheus', make, lots_per_unit=2, sizing=SizingConfig(dynamic=False, static_units=units, unit_cap=50))
    h._saved_state['prometheus'] = state.to_json()
    if lots:
        h.seed_position('prometheus', REF, lots, avg)
    h.start_session(day)
    if setup is not None:
        setup(h)
    h.run_until(datetime.combine(day, datetime.strptime(until_hm, '%H:%M').time()))
    return h, made


def check_day(day: date, sessions, frames, trades, **kw) -> DayReport:
    h, made = replay_day(day, sessions, frames, trades, **kw)
    try:
        session = next(s for s in sessions if s.day == day)
        rep = compare(day, live_decisions(session), replay_decisions(h))
        rep.replay_orders = len(h.orders)
        return rep
    finally:
        h.close()


def check_chain(days: Sequence[date], sessions, frames, trades, cfg: EngineConfig = RECORDED_CONFIG, units: Optional[int] = None):
    """Replay consecutive days on ONE fake Hestia: the engine's own saved state and the ledger carry from day to day (only the first
    day is seeded from the trades file), the way the daily cron run does it. Returns (reports, the fake, the engines made); the caller
    closes the fake."""
    from hestia_core.interface import StopReason
    first = days[0]
    ordered = sorted(sessions, key=lambda s: s.day)
    prior = [s for s in ordered if s.day < first and s.bars]
    made: List[PrometheusEngine] = []

    def factory(kernel, holidays):
        data = rec.LoggedReplayData(kernel, sessions, frames, holidays)
        data.add_logged_contract(ContractSpec(REF, lot_size=LOT_SIZE, tick_size=1.0, freeze_qty_lots=1000, minutes=frames[EXPIRY_KEY]),
                                 EXPIRY_KEY)
        return data
    h = FakeHestia(datetime.combine(first, datetime.min.time()) + timedelta(hours=8, minutes=50), data_factory=factory)
    state, lots, avg = carried_state(first, trades, cfg, prior[-1].bars[-1].data['bar_start'] if prior else None)
    first_session = next(s for s in sessions if s.day == first)
    n = units or _units_for(first, first_session, state if state.status == 'in_trade' else None)

    def make():
        e = PrometheusEngine(cfg)
        made.append(e)
        return e
    h.register('prometheus', make, lots_per_unit=2, sizing=SizingConfig(dynamic=False, static_units=n, unit_cap=50))
    h._saved_state['prometheus'] = state.to_json()
    if lots:
        h.seed_position('prometheus', REF, lots, avg)
    reports = []
    for d in days:
        midnight = datetime.combine(d, datetime.min.time())
        h.start_session(d)
        h.run_until(midnight + timedelta(hours=23, minutes=35))            # past the close: the engine itself declines the last boundary's bar
        h.stop_all(StopReason.SESSION_END)
        h.run_until(midnight + timedelta(hours=23, minutes=36))
        session = next(s for s in sessions if s.day == d)
        reports.append(compare(d, live_decisions(session), [x for x in replay_decisions(h) if x.ts.date() == d]))
        h.run_until(midnight + timedelta(hours=32))                        # the night: the next session's start is scheduled from here
    return reports, h, made


# ---------------------------------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------------------------------

def main(argv: Sequence[str]) -> int:
    root = Path(__file__).resolve().parents[1]
    pull = Path(argv[1]) if len(argv) > 1 else root / 'hestia_data' / 'replay_pull'
    sessions = rec.load_sessions(pull)
    frames = rec.load_minute_files(root / 'data_pipeline' / 'data' / 'mcx', 'CRUDEOILM')
    trades = rec.load_trades(pull / 'data' / 'prometheus_trades.csv')
    covered = frames[EXPIRY_KEY]['time_stamp'].max().date()
    print(f'Tier 1 replay: live decisions against the engine on the fake Hestia (price data to {covered}; 2026-09-15 is a known-bad day)')
    tot_live = tot_match = 0
    for s in sorted(sessions, key=lambda x: x.day):
        if s.day == date(2026, 9, 15) or s.day > covered:
            print(f'  {s.day}: skipped ({"known-bad day" if s.day == date(2026, 9, 15) else "no price data"})')
            continue
        rep = check_day(s.day, sessions, frames, trades)
        tot_live += len(rep.live)
        tot_match += rep.matched
        gap = f', fill price gap mean {sum(rep.price_gaps) / len(rep.price_gaps):.2f} max {max(rep.price_gaps):.2f}' if rep.price_gaps else ''
        print(f'  {s.day}: {rep.matched}/{len(rep.live)} live decisions reproduced ({len(rep.replay)} replay){gap}')
        for d in rep.differences:
            print('     difference:', d)
    print(f'  total: {tot_match}/{tot_live}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
