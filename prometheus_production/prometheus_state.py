import csv
import os
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Optional
from prometheus_configs import STATE_FILE


@dataclass
class PrometheusState:
    status:              str             = 'idle'   # idle | watching | in_trade
    direction:           Optional[str]   = None      # bullish | bearish
    units:               Optional[int]   = None      # persisted verbatim, not recomputed (§4)
    entry_price:         Optional[float] = None
    recalibration_basis_price: Optional[float] = None   # §8: SL/target basis price for a rolled
                                                          # position's reopen leg, distinct from
                                                          # entry_price (the real fill) -- SL/target
                                                          # LEVELS are computed off this once, at
                                                          # reopen time, then persist as ordinary
                                                          # absolute levels; P&L always uses entry_price.
                                                          # None for a never-rolled trade.
    entry_ts:            Optional[str]   = None       # ISO timestamp
    signal_ts:           Optional[str]   = None       # ISO timestamp — bar whose close triggered the flip
    signal_close:        Optional[float] = None
    contract_expiry:     Optional[str]   = None       # YYYY-MM-DD
    symbol:              Optional[str]   = None       # full tradingsymbol
    token:               Optional[str]   = None
    sl_price:            Optional[float] = None       # persisted verbatim, not recomputed (§4)
    lot1_target:         Optional[float] = None       # persisted verbatim, not recomputed (§4)
    lot1_lots:           Optional[int]   = None        # actual filled lot count for this leg (partial-fill aware, §2)
    lot1_status:         Optional[str]   = None        # open | booked | stopped | eod | flip | never_opened
    lot1_exit_price:     Optional[float] = None
    lot1_exit_ts:        Optional[str]   = None
    lot1_exit_reason:    Optional[str]   = None
    lot2_target:         Optional[float] = None       # persisted verbatim, not recomputed (§4)
    lot2_target_source:  Optional[str]   = None        # pivot level name | flat_pct | no_pivot_fallback
    lot2_lots:           Optional[int]   = None        # actual filled lot count for this leg (partial-fill aware, §2)
    lot2_status:         Optional[str]   = None        # open | booked | stopped | eod | flip | never_opened
    lot2_exit_price:     Optional[float] = None
    lot2_exit_ts:        Optional[str]   = None
    lot2_exit_reason:    Optional[str]   = None
    last_known_ltp:      Optional[float] = None       # for restart recovery
    last_updated:        Optional[str]   = None        # stamped in save_state()


_FLOAT_FIELDS = {
    'entry_price', 'recalibration_basis_price', 'signal_close', 'sl_price', 'lot1_target',
    'lot1_exit_price', 'lot2_target', 'lot2_exit_price', 'last_known_ltp',
}
_INT_FIELDS = {'units', 'lot1_lots', 'lot2_lots'}


def save_state(s: PrometheusState) -> None:
    s.last_updated = datetime.now().isoformat()
    tmp = str(STATE_FILE) + '.tmp'
    with open(tmp, 'w', newline='') as f:
        writer = csv.writer(f)
        names  = [fld.name for fld in fields(s)]
        values = [getattr(s, n) for n in names]
        writer.writerow(names)
        writer.writerow(values)
    os.replace(tmp, STATE_FILE)


def load_state() -> PrometheusState:
    if not STATE_FILE.exists():
        return PrometheusState()
    with open(STATE_FILE, newline='') as f:
        reader = csv.DictReader(f)
        row    = next(reader, None)
    if row is None:
        return PrometheusState()
    s = PrometheusState()
    for fld in fields(s):
        raw = row.get(fld.name)
        if raw in (None, '', 'None'):
            setattr(s, fld.name, None if fld.default is None else fld.default)
        elif fld.name in _INT_FIELDS:
            try:
                setattr(s, fld.name, int(float(raw)))
            except (ValueError, TypeError):
                setattr(s, fld.name, None)
        elif fld.name in _FLOAT_FIELDS:
            try:
                setattr(s, fld.name, float(raw))
            except (ValueError, TypeError):
                setattr(s, fld.name, None)
        else:
            setattr(s, fld.name, raw)
    return s
