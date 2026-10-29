"""Oleus's persisted engine state: one JSON blob through ctx.save_state/load_state. The day's levels are stored in it so a restart rebuilds
exactly the same machine (levels are fixed before the open and never change in a session)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Dict, List, Optional


@dataclass
class EngineState:
    session_date: Optional[str] = None                        # the session this state belongs to; a new date resets the day's fields
    contract_token: Optional[str] = None
    contract_symbol: Optional[str] = None
    contract_expiry: Optional[str] = None
    levels: Optional[Dict[str, float]] = None                 # the day's Camarilla levels
    prev_session: Optional[str] = None                        # the date whose high, low and close made them
    no_trade_reason: Optional[str] = None                     # set when today cannot be traded (no usable previous session)

    machine: Dict[str, Optional[dict]] = field(default_factory=dict)       # Machine.to_dict()
    trades: Dict[str, dict] = field(default_factory=dict)     # setup key -> bookkeeping of the open trade (see engine._new_trade)
    trade_counter: int = 0

    prev_price: Optional[float] = None                        # where the last scan left the price
    just: Optional[float] = None                              # trigger price of the event applied last, while its move is being continued
    pending: Dict[str, Dict] = field(default_factory=dict)    # request_id -> context, persisted BEFORE send
    attempts: Dict[str, int] = field(default_factory=dict)    # "{trade}:{purpose}" -> attempt count, for request ids
    last_minute_checked: Optional[str] = None                 # the last completed minute the missed-touch net looked at
    late_catches: int = 0                                     # stops/targets seen only by the minute-bar net (QC number)
    missed_entries: int = 0                                   # entry touches skipped for a stale price or a refused entry
    frozen: bool = False                                      # a restart situation the engine could not resolve; needs the operator

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: Optional[str]) -> 'EngineState':
        if not raw:
            return EngineState()
        data = json.loads(raw)
        known = {f.name for f in fields(EngineState)}
        return EngineState(**{k: v for k, v in data.items() if k in known})

    def new_day(self, session_date: str) -> None:
        """Reset everything that belongs to one session (positions are flat overnight); the counters of trade ids and request attempts stay."""
        keep = dict(trade_counter=self.trade_counter, attempts=self.attempts)
        self.__dict__.update(EngineState(session_date=session_date, **keep).__dict__)
