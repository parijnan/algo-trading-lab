"""
The engine's own decision state, persisted through `ctx.save_state` as one JSON string. What Hestia's ledger holds (what actually
filled and is held) is the truth about the position; this holds what the engine believes and decided: the levels, the lot bookkeeping,
the trade row being built, the roll and flip intentions, and the requests it has sent and is waiting on.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Dict, Optional


@dataclass
class EngineState:
    status: str = 'watching'                 # watching | in_trade
    direction: Optional[str] = None          # bullish | bearish
    units: Optional[int] = None
    entry_price: Optional[float] = None      # the real fill price
    basis_price: Optional[float] = None      # a rollover reopen's recalibration basis
    entry_ts: Optional[str] = None
    signal_ts: Optional[str] = None
    signal_close: Optional[float] = None
    contract_token: Optional[str] = None
    contract_symbol: Optional[str] = None
    contract_expiry: Optional[str] = None
    sl_price: Optional[float] = None
    lot1_target: Optional[float] = None
    lot1_lots: Optional[int] = None
    lot1_status: Optional[str] = None        # open | booked | never_opened
    lot1_exit_price: Optional[float] = None
    lot2_target: Optional[float] = None
    lot2_source: Optional[str] = None
    lot2_lots: Optional[int] = None
    lot2_status: Optional[str] = None
    lot2_exit_price: Optional[float] = None
    trade_counter: int = 0
    trade_row: Dict[str, object] = field(default_factory=dict)          # the closed-trade record being built
    last_processed_boundary: Optional[str] = None
    pending: Dict[str, dict] = field(default_factory=dict)              # request_id -> what it was for
    pending_flip: Optional[dict] = None                                 # a Rule 7 flip still to be resolved
    pending_missed_flip: Optional[dict] = None
    roll_target: Optional[dict] = None       # {'token','symbol','expiry'} of tonight's roll, once armed
    roll_executed_date: Optional[str] = None
    attempts: Dict[str, int] = field(default_factory=dict)              # purpose+trade -> attempts used, for request ids
    frozen: bool = False                     # refused to trade after an inconsistency that needs the operator

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: Optional[str]) -> 'EngineState':
        if not raw:
            return EngineState()
        data = json.loads(raw)
        known = {f.name for f in fields(EngineState)}
        return EngineState(**{k: v for k, v in data.items() if k in known})

    def open_lots(self) -> int:
        return ((self.lot1_lots or 0) if self.lot1_status == 'open' else 0) + ((self.lot2_lots or 0) if self.lot2_status == 'open' else 0)
