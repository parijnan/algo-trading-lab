"""
state.py — Athena Production State Management
Reads and writes athena_state.csv — the single source of truth for live trade state.

Adapted for Athena's 6-leg structure (4 calendar legs + 2 safety wings).
"""

import os
import pandas as pd
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Optional

from configs_live import STATE_FILE

# ---------------------------------------------------------------------------
# AthenaState dataclass
# ---------------------------------------------------------------------------

@dataclass
class AthenaState:
    """
    Complete live trade state for Athena (Nifty Double Calendar Condor).
    Maps 1:1 to columns in athena_state.csv.

    status values:
        'idle'     — no active trade, ready to take signals
        'in_trade' — position open, monitoring exits
        'exiting'  — exit orders being placed
    """

    # Session status
    status:               str             = 'idle'
    wings_enabled:        bool            = True    # To remember if wings were taken at entry

    # Expiries
    sell_expiry:          Optional[str]   = None    # 'YYYY-MM-DD'
    buy_expiry:           Optional[str]   = None    # 'YYYY-MM-DD' (monthly)

    # CE Legs
    ce_sell_strike:       Optional[int]   = None
    ce_sell_token:        Optional[str]   = None
    ce_sell_symbol:       Optional[str]   = None
    ce_sell_entry:        Optional[float] = None

    ce_buy_strike:        Optional[int]   = None
    ce_buy_token:         Optional[str]   = None
    ce_buy_symbol:        Optional[str]   = None
    ce_buy_entry:         Optional[float] = None

    ce_wing_strike:       Optional[int]   = None
    ce_wing_token:        Optional[str]   = None
    ce_wing_symbol:       Optional[str]   = None
    ce_wing_entry:        Optional[float] = None

    # PE Legs
    pe_sell_strike:       Optional[int]   = None
    pe_sell_token:        Optional[str]   = None
    pe_sell_symbol:       Optional[str]   = None
    pe_sell_entry:        Optional[float] = None

    pe_buy_strike:        Optional[int]   = None
    pe_buy_token:         Optional[str]   = None
    pe_buy_symbol:        Optional[str]   = None
    pe_buy_entry:         Optional[float] = None

    pe_wing_strike:       Optional[int]   = None
    pe_wing_token:        Optional[str]   = None
    pe_wing_symbol:       Optional[str]   = None
    pe_wing_entry:        Optional[float] = None

    # Order management
    lots:                 int             = 1
    net_debit:            Optional[float] = None

    # Entry context
    entry_time:           Optional[str]   = None    # ISO datetime string
    entry_spot:           Optional[float] = None
    entry_vix:            Optional[float] = None

    # Peak unrealised P&L — persisted for restart recovery
    max_unrealised_pl:    float           = 0.0

    # Last known LTPs (from polling)
    last_spot:            Optional[float] = None
    last_ce_sell_ltp:     Optional[float] = None
    last_ce_buy_ltp:      Optional[float] = None
    last_ce_wing_ltp:     Optional[float] = None
    last_pe_sell_ltp:     Optional[float] = None
    last_pe_buy_ltp:      Optional[float] = None
    last_pe_wing_ltp:     Optional[float] = None

    # Metadata
    last_updated:         Optional[str]   = None    # ISO datetime string


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def init_state() -> AthenaState:
    """Return a fresh idle AthenaState."""
    return AthenaState()


def load_state() -> AthenaState:
    """
    Load AthenaState from athena_state.csv.
    Returns a fresh idle state if file is absent or empty.
    """
    if not os.path.exists(STATE_FILE):
        return init_state()

    try:
        df = pd.read_csv(STATE_FILE)
        if df.empty:
            return init_state()

        row = df.iloc[0]
        state = AthenaState()

        for f in fields(state):
            if f.name not in row.index:
                continue

            val = row[f.name]

            if pd.isna(val) or val == '':
                setattr(state, f.name, None if f.default is None else f.default)
                continue

            origin = f.type
            if origin in (Optional[str], str):
                setattr(state, f.name, str(val))
            elif origin in (Optional[int], int):
                setattr(state, f.name, int(val))
            elif origin in (Optional[float], float):
                setattr(state, f.name, float(val))
            elif origin == bool:
                setattr(state, f.name, str(val).strip().lower() == 'true')
            else:
                setattr(state, f.name, val)

        return state

    except Exception as e:
        print(f"[state] WARNING: Could not load state file ({e}). Starting idle.")
        return init_state()


def save_state(state: AthenaState) -> None:
    """Persist AthenaState to athena_state.csv atomically."""
    state.last_updated = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    row = {f.name: getattr(state, f.name) for f in fields(state)}
    df  = pd.DataFrame([row])
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp_file = STATE_FILE + '.tmp'
    df.to_csv(tmp_file, index=False)
    os.replace(tmp_file, STATE_FILE)


def clear_trade_fields(state: AthenaState) -> AthenaState:
    """Reset all trade fields, preserving wings_enabled default."""
    status_snapshot = 'idle'
    new_state = init_state()
    new_state.status = status_snapshot
    return new_state
