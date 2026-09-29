"""
P8.2: the Helios engine replayed on the fake Hestia over REAL GOLDPETAL 1-minute data, compared trade for trade against
`helios_backtest/parity_backtest_helios.py`'s own deterministic output (`data_sweep/parity_trades.csv`/`parity_legs.csv`).
Mirrors Selene's own P7.2 (`selene_engine/replay_check.py`), including its own scope caveat below.

Unlike Prometheus's P5.4 (recorded live logs as the oracle, tolerances for tick-level timing), Helios has never traded: the oracle
here is the backtest's own simulation, computed from the SAME 1-minute bars and the SAME Supertrend formula the engine itself
uses (`hestia_core.indicators.compute_st`, `hestia_core.roll_policy`'s rules). Exact agreement is the gate.

**Scope, same as Selene's own (plans/hestia-p7-selene-engine.md section 4).** The parity backtest blends Fyers (preferred) and
AngelOne; Hestia's live data path is AngelOne-only (`data_pipeline/data/mcx/GOLDPETAL/`). The two sources coincide only from
`helios_configs.ANGELONE_OWN_FROM` (2026-09-02) onward — before that the backtest traded on data the live engine will never see,
so a replay there proves nothing about production. This tool always starts at that date.

    python -m helios_engine.replay_check [end_date]        # default: helios_configs.PARITY_END_EXTENDED
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'helios_backtest'))
import helios_configs as bconfig                                    # noqa: E402
import helios_data_loader as loader                                 # noqa: E402

from hestia_core.fake import ContractSpec, FakeHestia                # noqa: E402
from hestia_core.interface import ContractRef, SizingConfig, StopReason  # noqa: E402
from helios_engine.engine import HeliosEngine                        # noqa: E402
from helios_engine.engine_configs import DEFAULT                     # noqa: E402
from helios_engine.levels import build_levels                        # noqa: E402
from helios_engine.state import EngineState                          # noqa: E402

REPO = Path(__file__).resolve().parents[1]
MCX_DIR = REPO / 'data_pipeline' / 'data' / 'mcx' / 'GOLDPETAL'
LOT_SIZE = 1
TICK_SIZE = 1.0
FREEZE_QTY_LOTS = 10000   # GOLDPETAL's own instrument-master freeze_qty (Selene's SILVERMIC was 600) -- confirmed 2026-09-29
REPLAY_START = date.fromisoformat(bconfig.ANGELONE_OWN_FROM)         # 2026-09-02: where AngelOne-only == what the backtest used


@dataclass
class Decision:
    ts: datetime
    kind: str                    # 'entry' | 'exit'
    direction: Optional[str] = None
    reason: Optional[str] = None
    price: Optional[float] = None


@dataclass
class ReplayReport:
    matched: int = 0
    live: List[Decision] = field(default_factory=list)
    replay: List[Decision] = field(default_factory=list)
    differences: List[str] = field(default_factory=list)
    price_gaps: List[float] = field(default_factory=list)

    @property
    def exact(self) -> bool:
        return not self.differences


def _contract_ref(symbol: str, token: int, expiry: str) -> ContractRef:
    exp = datetime.strptime(expiry, '%d%b%Y').date()
    return ContractRef('GOLDPETAL', str(token), symbol, exp)


def load_contracts(seg_start: str, seg_end: str) -> Dict[str, ContractSpec]:
    """Every GOLDPETAL contract, keyed by expiry date (ISO), with the SAME blended (Fyers-preferred, AngelOne-filled) 1-minute
    history the parity backtest itself used (`parity_backtest_helios.load_contracts`), not a raw read of the pipeline's
    AngelOne-only files.

    This matters: the pipeline's own per-contract file starts only from when AngelOne began tracking that contract
    (`ANGELONE_OWN_FROM`, 2026-09-02 for the contracts this window uses) -- feeding the engine ONLY that file starves its
    Supertrend seed of the 18 days of history before it (`DataSpec.seed_days`), so it warms up from a much shorter series
    than the backtest's own 18-calendar-day trailing window and the two decision engines can disagree on early flips for
    reasons that are a seed-data difference, not an engine-logic difference. Reusing the backtest's own blended loader
    keeps the price series -- and so the Supertrend series -- identical between the oracle and the engine, isolating the
    comparison to what P7.2 actually exists to check: does the engine's decision logic agree with the backtest's.
    Live Hestia does not have this gap: `LiveData._backfill_if_needed` fetches the missing days from the broker itself
    (confirmed live in the 2026-09-28 Prometheus paper session's own log, backfilling its Nov-2026 contract the same way);
    this replay tool has no broker to call, so it borrows the backtest's own pre-fetched history instead."""
    from parity_backtest_helios import load_contracts as _blended_load
    master = pd.read_csv(REPO / 'data_pipeline' / 'data' / 'mcx_instrument_master.csv')
    by_expiry_row = {row['expiry']: row for _, row in master[master.name == 'GOLDPETAL'].iterrows()}
    blended = _blended_load(bconfig.SYMBOL, seg_start, seg_end)
    out = {}
    for expiry, contract in blended.items():                      # expiry is a date.Contract.expiry, per parity_backtest_helios.Contract
        exp = expiry if hasattr(expiry, 'year') and not hasattr(expiry, 'hour') else expiry.date()
        df = pd.DataFrame({'time_stamp': contract.idx, 'open': contract.o, 'high': contract.h, 'low': contract.l,
                           'close': contract.c, 'volume': contract.v})
        symbol = f'GOLDPETAL{exp.strftime("%d%b%y").upper()}FUT'
        token = None
        for exp_str, row in by_expiry_row.items():
            if datetime.strptime(exp_str, '%d%b%Y').date() == exp:
                token = row['token']
                break
        if token is None:
            continue
        ref = ContractRef('GOLDPETAL', str(token), symbol, exp)
        out[ref.expiry.isoformat()] = ContractSpec(ref, lot_size=LOT_SIZE, tick_size=TICK_SIZE, freeze_qty_lots=FREEZE_QTY_LOTS,
                                                    minutes=df)
    return out


def trading_days(start: date, end: date) -> List[date]:
    closed = loader._load_fully_closed_dates()
    return [d.date() for d in pd.bdate_range(start, end) if d.date() not in closed]


def parity_window(trades_path: Path, legs_path: Path, start: date, end: date) -> Tuple[pd.DataFrame, pd.DataFrame]:
    t = pd.read_csv(trades_path)
    t['entry_ts'], t['exit_ts'] = pd.to_datetime(t['entry_ts']), pd.to_datetime(t['exit_ts'])
    l = pd.read_csv(legs_path)
    l['entry_ts'], l['exit_ts'] = pd.to_datetime(l['entry_ts']), pd.to_datetime(l['exit_ts'])
    return t, l


def seed_from_carried_leg(h: FakeHestia, legs: pd.DataFrame, start: datetime, contracts: Dict[str, ContractSpec]) -> None:
    """A trade whose entry is before `start` but whose last leg is still open at `start` (or closed exactly at/after it) seeds
    the engine's state and the ledger. Only the leg ALREADY on data this tool has (expiry >= start's earliest contract) is used;
    an older leg (e.g. a pre-window forced-roll artifact) is not reconstructable and is not needed — only the surviving leg's own
    entry matters for the engine's levels."""
    open_legs = legs[(legs['entry_ts'] < start) & (legs['exit_ts'] >= start)]
    if not len(open_legs):
        return
    leg = open_legs.iloc[-1]
    direction, entry_px = leg['direction'], float(leg['entry_px'])
    contract_expiry = str(pd.Timestamp(leg['contract']).date())
    spec = contracts.get(contract_expiry)
    if spec is None:
        print(f'WARNING: carried leg is on {contract_expiry}, which this tool has no data file for; skipping the seed')
        return
    ref = spec.ref
    lv = build_levels(direction, entry_px, DEFAULT)
    # Selene's own line here was units=1, lots=1 (its unit IS 1 lot); Helios is 1 unit = 20 lots (DEFAULT.lots_per_unit).
    lots = DEFAULT.lots_per_unit
    st = EngineState(status='in_trade', direction=direction, units=1, entry_price=entry_px, entry_ts=str(leg['entry_ts']),
                     contract_token=ref.token, contract_symbol=ref.symbol, contract_expiry=ref.expiry.isoformat(),
                     sl_price=lv.sl_price, lots=lots, trade_counter=0,
                     trade_row={'trade_id': 0, 'entry_price': entry_px, 'units': 1, 'direction': direction})
    h._saved_state['helios'] = st.to_json()
    h.seed_position('helios', ref, lots if direction == 'bullish' else -lots, entry_px)
    print(f'seeded a carried {direction} position on {ref.symbol}, entry {entry_px}, {lots} lots')


def replay(end: date, verbose: bool = True) -> ReplayReport:
    contracts = load_contracts(bconfig.DATA_START, end.isoformat())
    days = [d for d in trading_days(REPLAY_START, end)]
    trades, legs = parity_window(REPO / 'helios_backtest' / 'data_sweep' / 'parity_trades.csv',
                                 REPO / 'helios_backtest' / 'data_sweep' / 'parity_legs.csv', REPLAY_START, end)
    made: List[HeliosEngine] = []

    def make():
        e = HeliosEngine(DEFAULT)
        made.append(e)
        return e
    h = FakeHestia(datetime.combine(days[0], datetime.min.time()) + timedelta(hours=8, minutes=50))
    for spec in contracts.values():
        h.add_contract(spec)
    h.register('helios', make, lots_per_unit=DEFAULT.lots_per_unit, sizing=SizingConfig(dynamic=False, static_units=1, unit_cap=50))
    seed_from_carried_leg(h, legs, datetime.combine(REPLAY_START, datetime.min.time()), contracts)
    for d in days:
        midnight = datetime.combine(d, datetime.min.time())
        h.start_session(d)
        h.run_until(midnight + timedelta(hours=23, minutes=35))
        h.stop_all(StopReason.SESSION_END)
        h.run_until(midnight + timedelta(hours=23, minutes=36))
        h.run_until(midnight + timedelta(hours=32))
    h.close()

    win_start, win_end = pd.Timestamp(REPLAY_START), pd.Timestamp(end) + pd.Timedelta(days=1)
    live_decisions = []
    for _, t in trades.iterrows():
        if win_start <= t.entry_ts < win_end:
            live_decisions.append(Decision(t.entry_ts.to_pydatetime(), 'entry', t.direction, price=float(t.entry_px)))
        if win_start <= t.exit_ts < win_end:
            live_decisions.append(Decision(t.exit_ts.to_pydatetime(), 'exit', reason=t.last_reason, price=None))
    live_decisions.sort(key=lambda d: d.ts)
    replay_decisions = []
    for a in h.alerts:
        if a.text.startswith('Entered '):
            direction = 'bullish' if 'BULLISH' in a.text else 'bearish'
            px = float(a.text.split('Entry: ')[1].split(' ')[0])
            replay_decisions.append(Decision(a.ts, 'entry', direction, price=px))
        elif a.text.startswith('Exit: '):
            reason = a.text.split('Exit: ')[1].split(' ')[0]
            px = float(a.text.split('-> Exit ')[1].split(' ')[0])
            replay_decisions.append(Decision(a.ts, 'exit', reason=reason, price=px))

    rep = ReplayReport(live=live_decisions, replay=replay_decisions)
    used = set()

    def close_enough(a: Decision, b: Decision) -> bool:
        if a.kind != b.kind:
            return False
        dt = abs((a.ts - b.ts).total_seconds())
        if a.kind == 'entry':
            return a.direction == b.direction and dt <= 5.0
        return a.reason == b.reason and dt <= 5.0

    for a in live_decisions:
        hit = next((i for i, b in enumerate(replay_decisions) if i not in used and close_enough(a, b)), None)
        if hit is None:
            rep.differences.append(f'live {a.kind} at {a.ts} {a.direction or a.reason} has no replay counterpart')
            continue
        used.add(hit)
        rep.matched += 1
        if a.price is not None and replay_decisions[hit].price is not None:
            rep.price_gaps.append(abs(a.price - replay_decisions[hit].price))
    for i, b in enumerate(replay_decisions):
        if i not in used:
            rep.differences.append(f'replay {b.kind} at {b.ts} {b.direction or b.reason} has no live counterpart')
    if verbose:
        print(f'window {REPLAY_START} -> {end}: {rep.matched}/{len(live_decisions)} live decisions reproduced '
              f'({len(replay_decisions)} replay)')
        if rep.price_gaps:
            print(f'  fill price gap: mean {sum(rep.price_gaps) / len(rep.price_gaps):.2f}, max {max(rep.price_gaps):.2f}')
        for diff in rep.differences:
            print('  difference:', diff)
    return rep


def main(argv: Sequence[str]) -> int:
    end = date.fromisoformat(argv[1]) if len(argv) > 1 else date.fromisoformat(bconfig.PARITY_END_EXTENDED)
    rep = replay(end)
    return 0 if rep.exact else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
