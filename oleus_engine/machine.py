"""
The Oleus state machine as a pure function of prices: the tick-level logic of `janus_backtest/janus_machine.py` (plain thirds: re-buy, counter
trade and trailing stop all off), copied rather than imported so research edits cannot change the engine. No clock, no I/O, no broker.

Four setups, each at most one trade at a time. A trade has three lots (lot k exits at target k) and ONE stop for all lots still open. The next
thing that can happen to a price moving from `a` to `b` is found by `find_next`; nothing changes until the caller applies the outcome
(`apply_entry`, `apply_target`, `apply_stop`), so an engine can ask Hestia first and change state only on a confirmed fill. After an event the
caller continues the same move from the event's trigger price (`cur`, `just`), which is how a stop that is also another setup's entry level
produces a stop-and-reverse.

Priorities at the same price are the research code's: a stop first, then a target, then an entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

STOP, TARGET, ENTRY = 0, 1, 3           # janus_machine's priorities (2 is the re-buy, which is off)
OPEN, BOOKED, CLOSED = 'open', 'booked', 'closed'


@dataclass(frozen=True)
class Event:
    kind: str                           # 'entry' | 'target' | 'stop'
    key: str                            # setup key
    idx: Optional[int]                  # 0-based lot index, targets only
    trigger: float                      # the price in the move at which it fires
    level: float                        # the level price the research code fills a touch at
    prio: int

    @property
    def fill_key(self) -> tuple:
        return (self.key, self.prio, self.idx)


class Machine:

    def __init__(self, setups: Dict[str, dict]):
        self.setups = setups
        self.trades: Dict[str, Optional[dict]] = {k: None for k in setups}

    # ---- queries ---------------------------------------------------------------------------------------------------------------

    def open_trades(self) -> List[str]:
        return [k for k, t in self.trades.items() if t is not None]

    def open_lot_indices(self, key: str) -> List[int]:
        t = self.trades[key]
        return [] if t is None else [j for j, st in enumerate(t['lots']) if st == OPEN]

    def net_side_lots(self) -> int:
        """Signed number of open lots across the setups (the research code's `net` that orders a bar's path)."""
        return sum(self.setups[k]['side'] * len(self.open_lot_indices(k)) for k in self.trades)

    def entry_ok(self, key: str, price: float) -> bool:
        """An entry filled at or past its first target has nothing left to trade (janus_machine._open_trade refuses it)."""
        s = self.setups[key]
        return (price - s['targets'][0]) * s['side'] < 0

    def _candidates(self, d: int, entries_open: bool) -> list:
        out = []
        for key, tr in self.trades.items():
            s = self.setups[key]
            side = s['side']
            if tr is not None:
                if d == -side and any(x == OPEN for x in tr['lots']):
                    out.append((tr['stop'], STOP, key, None, tr['stop']))
                if d == side:
                    for j, x in enumerate(tr['lots']):
                        if x == OPEN:
                            out.append((s['targets'][j], TARGET, key, j, s['targets'][j]))
            elif d == side and entries_open:
                out.append((s['entry'], ENTRY, key, None, s['entry']))
        return out

    def find_next(self, a: float, b: float, entries_open: bool, cur: Optional[float] = None, just: Optional[float] = None,
                  skipped: Iterable[tuple] = ()) -> Optional[Event]:
        """The next trigger a price moving from `a` to `b` reaches, starting from `cur` (default `a`). `just` is the trigger price of the
        event that was applied last: a trigger at exactly that price may fire again from it (stop-and-reverse). `skipped` holds
        (key, prio, idx) of entries the caller refused for this move."""
        if a == b:
            return None
        d = 1 if b > a else -1
        cur = a if cur is None else cur
        skipped = set(skipped)
        best = None
        for t, pr, key, idx, level in self._candidates(d, entries_open):
            dist = (t - cur) * d
            if (dist > 0 or (dist == 0 and just == t)) and (b - t) * d >= 0 and (key, pr, idx) not in skipped:
                if best is None or (dist, pr) < (best[0], best[1]):
                    best = (dist, pr, t, key, idx, level)
        if best is None:
            return None
        _, pr, t, key, idx, level = best
        return Event({STOP: 'stop', TARGET: 'target', ENTRY: 'entry'}[pr], key, idx, t, level, pr)

    # ---- applying a confirmed outcome ------------------------------------------------------------------------------------------

    def apply_entry(self, key: str, price: float, lots: Tuple[str, str, str] = (OPEN, OPEN, OPEN)) -> None:
        if self.trades[key] is not None:
            raise ValueError(f'{key} already has an open trade')
        s = self.setups[key]
        self.trades[key] = {'entry': price, 'stop': s['stop'], 'lots': list(lots)}

    def apply_target(self, key: str, idx: int) -> List[int]:
        """Book lot `idx`. At the last target the trade ends, and any lot still open is closed with it: the indices of those lots are
        returned (they exit at the same price). Returns [] otherwise."""
        tr = self.trades[key]
        if tr is None or tr['lots'][idx] != OPEN:
            raise ValueError(f'{key} lot {idx + 1} is not open')
        tr['lots'][idx] = BOOKED
        if idx == 2:
            rest = [j for j, x in enumerate(tr['lots']) if x == OPEN]
            self.trades[key] = None
            return rest
        if all(x != OPEN for x in tr['lots']):
            self.trades[key] = None
        return []

    def apply_stop(self, key: str) -> List[int]:
        tr = self.trades[key]
        if tr is None:
            raise ValueError(f'{key} has no open trade')
        rest = [j for j, x in enumerate(tr['lots']) if x == OPEN]
        self.trades[key] = None
        return rest

    def close_all(self) -> Dict[str, List[int]]:
        """Hard exit or session end: every trade ends; returns the open lot indices per setup that held any."""
        out = {k: self.open_lot_indices(k) for k in self.trades if self.trades[k] is not None}
        for k in self.trades:
            self.trades[k] = None
        return out

    # ---- persistence -----------------------------------------------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {k: (None if t is None else {'entry': t['entry'], 'stop': t['stop'], 'lots': list(t['lots'])}) for k, t in self.trades.items()}

    def load(self, data: dict) -> None:
        for k in self.trades:
            t = (data or {}).get(k)
            self.trades[k] = None if t is None else {'entry': t['entry'], 'stop': t['stop'], 'lots': list(t['lots'])}
