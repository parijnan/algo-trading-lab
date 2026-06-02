import csv
import os
from dataclasses import dataclass, field, fields
from typing import Optional
from configs import STATE_FILE


@dataclass
class IrisState:
    status:       str            = 'idle'        # idle | watching | in_trade
    direction:    Optional[str]  = None          # bullish | bearish
    option_type:  Optional[str]  = None          # ce | pe
    strike:       Optional[int]  = None
    expiry:       Optional[str]  = None          # YYYY-MM-DD
    symbol:       Optional[str]  = None          # full tradingsymbol
    token:        Optional[str]  = None
    entry_price:  Optional[float] = None         # premium paid per unit
    entry_spot:   Optional[float] = None         # Nifty spot at entry
    entry_time:   Optional[str]  = None          # ISO timestamp
    lots:         int            = 0
    last_ltp:     Optional[float] = None         # for restart recovery


def save_state(s: IrisState) -> None:
    tmp = str(STATE_FILE) + '.tmp'
    with open(tmp, 'w', newline='') as f:
        writer = csv.writer(f)
        names  = [fld.name for fld in fields(s)]
        values = [getattr(s, n) for n in names]
        writer.writerow(names)
        writer.writerow(values)
    os.replace(tmp, STATE_FILE)


def load_state() -> IrisState:
    if not STATE_FILE.exists():
        return IrisState()
    with open(STATE_FILE, newline='') as f:
        reader = csv.DictReader(f)
        row    = next(reader, None)
    if row is None:
        return IrisState()
    s = IrisState()
    for fld in fields(s):
        raw = row.get(fld.name)
        if raw in (None, '', 'None'):
            setattr(s, fld.name, None if fld.default is None else fld.default)
        elif fld.type in ('int', 'Optional[int]'):
            try:
                setattr(s, fld.name, int(float(raw)))
            except (ValueError, TypeError):
                setattr(s, fld.name, None)
        elif fld.type in ('float', 'Optional[float]'):
            try:
                setattr(s, fld.name, float(raw))
            except (ValueError, TypeError):
                setattr(s, fld.name, None)
        else:
            setattr(s, fld.name, raw)
    return s
