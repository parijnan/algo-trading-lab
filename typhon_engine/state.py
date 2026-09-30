"""Typhon's persisted engine state: one flat blob through ctx.save_state/load_state, JSON round-trip. A single lot, no
lot1/lot2 split (unlike Prometheus's) -- target_price is the one addition over Selene's/Helios's own state, since Typhon's
decided config carries one target alongside the stop (plan Step 4, typhon_engine/engine_configs.py's own docstring)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Dict, Optional


@dataclass
class EngineState:
    status: str = 'watching'                                # watching | in_trade
    direction: Optional[str] = None                          # bullish | bearish
    units: Optional[int] = None
    entry_price: Optional[float] = None
    basis_price: Optional[float] = None                       # set only for a rollover reopen; SL/target were computed off this
    entry_ts: Optional[str] = None
    signal_ts: Optional[str] = None
    signal_close: Optional[float] = None

    contract_token: Optional[str] = None
    contract_symbol: Optional[str] = None
    contract_expiry: Optional[str] = None

    sl_price: Optional[float] = None
    target_price: Optional[float] = None
    lots: Optional[int] = None                                 # actual filled lot count (partial-fill aware)

    trade_counter: int = 0
    trade_row: Optional[Dict] = None                           # the in-progress closed-trade row, filled in as it exits
    last_processed_boundary: Optional[str] = None              # ISO ts of the last 15m boundary the engine acted on (or skipped)

    pending: Dict[str, Dict] = field(default_factory=dict)     # request_id -> {purpose, ...context}, persisted BEFORE send
    pending_flip: Optional[Dict] = None
    pending_missed_flip: Optional[Dict] = None

    roll_target: Optional[Dict] = None                         # {token, symbol, expiry, flatten_only} while armed for tonight
    roll_executed_date: Optional[str] = None                   # ISO date the engine last switched contract on

    attempts: Dict[str, int] = field(default_factory=dict)     # "{trade}:{purpose}" -> attempt count, for request ids
    frozen: bool = False                                        # a restart situation the engine could not resolve; needs the operator

    def open_lots(self) -> int:
        return self.lots or 0

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: Optional[str]) -> 'EngineState':
        if not raw:
            return EngineState()
        data = json.loads(raw)
        known = {f.name for f in fields(EngineState)}
        return EngineState(**{k: v for k, v in data.items() if k in known})
