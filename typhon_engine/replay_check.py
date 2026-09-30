"""
P7.2 (Typhon's own): the Typhon engine replayed on the fake Hestia over REAL NATGASMINI 1-minute data, compared trade for
trade against `typhon_backtest/parity_backtest_typhon.py`'s own deterministic output at the DECIDED config (mult 3.0,
SL 0.8%, target 15%, single lot) -- `data_sweep/parity_decided_trades.csv`/`parity_decided_legs.csv`.

Unlike Prometheus's P5.4 (recorded live logs as the oracle, tolerances for tick-level timing), Typhon has never traded: the
oracle here is the backtest's own simulation, computed from the SAME 1-minute bars and the SAME Supertrend formula the
engine itself uses (`hestia_core.indicators.compute_st`, `hestia_core.roll_policy`'s rules). Exact agreement is the gate --
but "exact" is agreement with what the harness's OWN price feed can actually show the engine, not with the backtest's own
idealized intrabar fill. `hestia_core.replay.ReplayData._price_at` only ever exposes a bar's open (first 30s of its
minute) then its close (the rest) to a live-style poller -- never the true intrabar high/low `scan_exit()` uses -- so for
a SL/target exit, `predict_live_exit()` (below) computes what the harness will really show and that prediction is what
gets compared against the replay, not the oracle's raw exit_ts/price. Verified exact against every case found 2026-09-30,
including a genuine cascade (a stop the harness's coarser feed misses survives to the next trend-flip instead). Entries
and trend-flip exits ARE compared against the oracle's own raw values, unapproximated -- both are 15-minute-boundary
events with no intrabar-fill question at all.

**Scope, same as Selene's/Helios's own P7.2 (`selene_engine/replay_check.py`).** The parity backtest blends Fyers
(preferred) and AngelOne; Hestia's live data path is AngelOne-only (`data_pipeline/data/mcx/NATGASMINI/`). The two sources
coincide only from `typhon_configs.ANGELONE_OWN_FROM` (2026-09-02) onward -- before that the backtest traded on data the
live engine will never see, so a replay there proves nothing about production. This tool always starts at that date.

    python -m typhon_engine.replay_check [end_date]        # default: typhon_configs.PARITY_END_EXTENDED
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'typhon_backtest'))
import typhon_configs as bconfig                                    # noqa: E402
import typhon_data_loader as loader                                 # noqa: E402

from hestia_core.fake import ContractSpec, FakeHestia                # noqa: E402
from hestia_core.interface import ContractRef, SizingConfig, StopReason  # noqa: E402
from typhon_engine.engine import TyphonEngine                        # noqa: E402
from typhon_engine.engine_configs import DEFAULT                     # noqa: E402
from typhon_engine.levels import build_levels                        # noqa: E402
from typhon_engine.state import EngineState                          # noqa: E402

REPO = Path(__file__).resolve().parents[1]
MCX_DIR = REPO / 'data_pipeline' / 'data' / 'mcx' / 'NATGASMINI'
LOT_SIZE = 250
TICK_SIZE = 0.10
FREEZE_QTY_LOTS = 240
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
    return ContractRef('NATGASMINI', str(token), symbol, exp)


def load_contracts(seg_start: str, seg_end: str) -> Dict[str, ContractSpec]:
    """Every NATGASMINI contract, keyed by expiry date (ISO), with the SAME blended (Fyers-preferred, AngelOne-filled)
    1-minute history the parity backtest itself used (`parity_backtest_typhon.load_contracts`), not a raw read of the
    pipeline's AngelOne-only files.

    This matters: the pipeline's own per-contract file starts only from when AngelOne began tracking that contract
    (`ANGELONE_OWN_FROM`, 2026-09-02 for the contracts this window uses) -- feeding the engine ONLY that file starves its
    Supertrend seed of the 18 days of history before it (`DataSpec.seed_days`), so it warms up from a much shorter series
    than the backtest's own 18-calendar-day trailing window and the two decision engines can disagree on early flips for
    reasons that are a seed-data difference, not an engine-logic difference. Reusing the backtest's own blended loader
    keeps the price series -- and so the Supertrend series -- identical between the oracle and the engine, isolating the
    comparison to what P7.2 actually exists to check: does the engine's decision logic agree with the backtest's.
    Live Hestia does not have this gap: `LiveData._backfill_if_needed` fetches the missing days from the broker itself;
    this replay tool has no broker to call, so it borrows the backtest's own pre-fetched history instead."""
    from parity_backtest_typhon import load_contracts as _blended_load
    master = pd.read_csv(REPO / 'data_pipeline' / 'data' / 'mcx_instrument_master.csv')
    by_expiry_row = {row['expiry']: row for _, row in master[master.name == 'NATGASMINI'].iterrows()}
    blended = _blended_load(bconfig.SYMBOL, seg_start, seg_end)
    out = {}
    for expiry, contract in blended.items():                      # expiry is a date.Contract.expiry, per parity_backtest_typhon.Contract
        exp = expiry if hasattr(expiry, 'year') and not hasattr(expiry, 'hour') else expiry.date()
        df = pd.DataFrame({'time_stamp': contract.idx, 'open': contract.o, 'high': contract.h, 'low': contract.l,
                           'close': contract.c, 'volume': contract.v})
        symbol = f'NATGASMINI{exp.strftime("%d%b%y").upper()}FUT'
        token = None
        for exp_str, row in by_expiry_row.items():
            if datetime.strptime(exp_str, '%d%b%Y').date() == exp:
                token = row['token']
                break
        if token is None:
            # mcx_instrument_master.csv is a live snapshot of currently-listed contracts, not a
            # historical record -- an already-expired contract (e.g. NATGASMINI's own Sep-2026
            # expiry, gone by the time this replay runs in Sep-2026) has no row left even though
            # its price file is still on disk. token is pure internal ledger bookkeeping here
            # (FakeHestia/the engine never re-look it up against master.csv -- confirmed: nothing
            # in hestia_core/fake.py or roll_policy.py reads that file), so a stable synthetic
            # token is safe and keeps the contract's real data in the replay instead of silently
            # dropping it.
            token = f'SYN{exp.isoformat()}'
        ref = ContractRef('NATGASMINI', str(token), symbol, exp)
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
    an older leg (e.g. a pre-window forced-roll artifact) is not reconstructable and is not needed -- only the surviving leg's own
    entry matters for the engine's levels. Unlike Selene's/Helios's own seed (SL only), Typhon's `build_levels()` also returns a
    target -- both must be seeded, or a genuinely-open replayed position would silently run with no target level at all."""
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
    st = EngineState(status='in_trade', direction=direction, units=1, entry_price=entry_px, entry_ts=str(leg['entry_ts']),
                     contract_token=ref.token, contract_symbol=ref.symbol, contract_expiry=ref.expiry.isoformat(),
                     sl_price=lv.sl_price, target_price=lv.target_price, lots=DEFAULT.lots_per_unit, trade_counter=0,
                     trade_row={'trade_id': 0, 'entry_price': entry_px, 'units': 1, 'direction': direction})
    h._saved_state['typhon'] = st.to_json()
    h.seed_position('typhon', ref, DEFAULT.lots_per_unit if direction == 'bullish' else -DEFAULT.lots_per_unit, entry_px)
    print(f'seeded a carried {direction} position on {ref.symbol}, entry {entry_px}')


def predict_live_exit(frame: Optional[pd.DataFrame], direction: str, sl_px: float, target_px: Optional[float],
                      start_ts, end_ts=None) -> Optional[Tuple[datetime, float, str]]:
    """Mirrors `hestia_core.replay.ReplayData._price_at` exactly: a bar's OPEN for the first 30s of its own minute,
    then its CLOSE for the rest -- never the true intrabar high/low the oracle backtest's own `scan_exit()` uses.
    A live-style poller genuinely cannot see a wick that touches a level and retreats before the bar closes; it only
    reacts once the level is crossed by a value it can actually observe. Verified exact (timestamp AND fill price,
    not just "close enough") against every residual difference found in the first replay pass, 2026-09-30 -- this
    is not a tolerance fudge, it is what the harness's own price feed will actually produce, and the real gate this
    tool checks is whether the engine matches THIS prediction, not the oracle's own idealized intrabar fill.

    Also suppresses the FIRST bar of any new session the position is held into (a gap of > 60 minutes since the
    previous bar, a safe proxy for a session boundary given real intra-session gaps are always far smaller): both
    the engine (`_past_first_minute_guard`, checked against `session_open`, not the position's own entry_ts) and
    the oracle backtest itself refuse to act in a session's own first `no_exit_before_buffer_min` (1.0) minutes.
    Found 2026-09-30: a position entered late one evening and held into the next morning's open showed a spurious
    60s residual until this was added -- the guard is per-session, not "N minutes after this position's entry"."""
    if frame is None:
        return None
    win = frame[frame.index >= start_ts]
    if end_ts is not None:
        win = win[win.index < end_ts]
    prev_ts = None
    for ts, row in win.iterrows():
        if prev_ts is not None and (ts - prev_ts).total_seconds() > 3600:
            prev_ts = ts
            continue
        prev_ts = ts
        o, cl = float(row['open']), float(row['close'])
        if (direction == 'bearish' and o >= sl_px) or (direction == 'bullish' and o <= sl_px):
            return ts.to_pydatetime(), o, 'stop_loss'
        if (direction == 'bearish' and cl >= sl_px) or (direction == 'bullish' and cl <= sl_px):
            return (ts + pd.Timedelta(seconds=30)).to_pydatetime(), cl, 'stop_loss'
        if target_px is not None:
            if (direction == 'bearish' and o <= target_px) or (direction == 'bullish' and o >= target_px):
                return ts.to_pydatetime(), o, 'target'
            if (direction == 'bearish' and cl <= target_px) or (direction == 'bullish' and cl >= target_px):
                return (ts + pd.Timedelta(seconds=30)).to_pydatetime(), cl, 'target'
    return None


def replay(end: date, verbose: bool = True) -> ReplayReport:
    contracts = load_contracts(bconfig.DATA_START, end.isoformat())
    days = [d for d in trading_days(REPLAY_START, end)]
    trades, legs = parity_window(REPO / 'typhon_backtest' / 'data_sweep' / 'parity_decided_trades.csv',
                                 REPO / 'typhon_backtest' / 'data_sweep' / 'parity_decided_legs.csv', REPLAY_START, end)
    made: List[TyphonEngine] = []

    def make():
        e = TyphonEngine(DEFAULT)
        made.append(e)
        return e
    h = FakeHestia(datetime.combine(days[0], datetime.min.time()) + timedelta(hours=8, minutes=50))
    # `contracts` spans the full multi-year archive (needed as a lookup table for seed_from_carried_leg, below), but
    # ONLY the ones near the replay window get registered with FakeHestia. Live Hestia's own DataPort only ever knows
    # the 2-3 currently-listed contracts; roll_policy.effective_from_days_left() (shared production code) relies on
    # that -- it sorts every REGISTERED contract by expiry and treats the earliest two as "front"/"next"
    # (count_trading_days_inclusive clamps any already-past expiry to 0, so it can't tell "yesterday's contract" from
    # one that expired three years ago). Registering the entire archive silently made the very first flat session
    # after the seeded position closed pick a 2023 contract as "front" -- found and fixed 2026-09-30 via a direct
    # replay of the first week, which showed the engine going dead silent (frozen on a dead 2023 contract, ST value
    # never moving) right after its first real exit.
    # >= REPLAY_START, not some earlier buffer: effective_from_days_left() always treats the two nearest-expiry
    # REGISTERED contracts as front/next, so an already-expired contract in the registered set (even one that
    # expired only weeks before the window) still wrongly displaces the real front contract. A carried-over
    # position's own (possibly earlier) contract doesn't need registering here -- seed_from_carried_leg looks it up
    # in the full `contracts` dict directly, and by the time the engine goes flat and needs effective_from_days_left
    # for a FRESH pick, only genuinely current contracts should be in the running.
    for spec in contracts.values():
        if REPLAY_START <= spec.ref.expiry <= end + timedelta(days=60):
            h.add_contract(spec)
    h.register('typhon', make, lots_per_unit=DEFAULT.lots_per_unit, sizing=SizingConfig(dynamic=False, static_units=1, unit_cap=50))
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
    contract_frames = {exp: spec.minutes.set_index('time_stamp') for exp, spec in contracts.items()}
    legs_by_trade = {tid: g for tid, g in legs.groupby('trade_id')}
    trades_sorted = trades.sort_values('entry_ts').reset_index(drop=True)
    live_decisions = []
    for i, t in trades_sorted.iterrows():
        if win_start <= t.entry_ts < win_end:
            live_decisions.append(Decision(t.entry_ts.to_pydatetime(), 'entry', t.direction, price=float(t.entry_px)))
        if not (win_start <= t.exit_ts < win_end):
            continue
        tlegs = legs_by_trade.get(t.trade_id)
        next_entry_ts = trades_sorted.iloc[i + 1].entry_ts if i + 1 < len(trades_sorted) else None
        # Single-leg trades (the common case -- no roll happened mid-trade) get the real prediction: what would a
        # live-style poller of this same 1-minute data actually have seen, given this trade's own sl_px/target_px.
        # A predicted breach that never arrives before the NEXT trade's own entry means the position legitimately
        # rode past where the oracle's intrabar fill closed it and got taken out by the next trend-flip instead --
        # a real, predictable cascade (verified 2026-09-30 against the 09-25/09-28 case), not a bug.
        if tlegs is not None and len(tlegs) == 1:
            leg = tlegs.iloc[0]
            frame = contract_frames.get(str(pd.Timestamp(leg['contract']).date()))
            guard_start = leg['entry_ts'] + pd.Timedelta(minutes=DEFAULT.no_exit_before_buffer_min)
            tgt = None if pd.isna(leg['target_px']) else float(leg['target_px'])
            pred = predict_live_exit(frame, leg['direction'], float(leg['sl_px']), tgt, guard_start, end_ts=next_entry_ts)
            if pred is not None:
                ts, px, reason = pred
                live_decisions.append(Decision(ts, 'exit', reason=reason, price=px))
                continue
            if next_entry_ts is not None:
                nxt = trades_sorted.iloc[i + 1]
                live_decisions.append(Decision(next_entry_ts.to_pydatetime(), 'exit', reason='trend_flip',
                                               price=float(nxt.entry_px)))
                continue
        # A multi-leg (rolled) trade, or the very last trade with nothing to bound the search against: fall back to
        # the oracle's own raw exit as-is (the old, looser comparison) rather than guessing.
        live_decisions.append(Decision(t.exit_ts.to_pydatetime(), 'exit', reason=t.last_reason, price=None))
    live_decisions.sort(key=lambda d: d.ts)
    # KNOWN, PROVEN benign residual (not a bug): `parity_backtest_typhon.simulate()` only appends a leg to its output
    # in close_pos() -- a position still open when the requested data window ends (`legs.attrs['open_at_end']`,
    # confirmed True at PARITY_END_EXTENDED 2026-09-30) never gets a row written, so it is silently absent from
    # both parity_decided_trades.csv and parity_decided_legs.csv even though the oracle's own state machine DID
    # open it. Verified 2026-09-30 by tracing simulate()'s own logic directly: at the real window's last entry
    # signal (09-29 18:15 ST_15 flip to bearish), the oracle's state machine opens the exact same bearish position
    # the replay engine does, at the exact same 18:30 boundary -- it just never reaches a close_pos() call before
    # data runs out, so no CSV row exists to compare against. A single trailing "replay entry has no live
    # counterpart" at the very end of the window is this artifact, not a decision-logic disagreement -- re-running
    # against a later `end` (once more data exists) would show it as a normal, ordinary, matching entry.
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
        # exit `live_decisions` are now predictions of what the harness's own open/close price sampling will
        # produce (see predict_live_exit), not the oracle's raw intrabar fill -- so they should agree with the
        # replay almost exactly, same tolerance as entries plus a little slack for the engine's own tick cadence
        # (observed consistently landing ~0.25s after the bar boundary the prediction names).
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
