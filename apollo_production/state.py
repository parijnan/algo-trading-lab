"""
state.py — Apollo Production State Management
Reads and writes apollo_state.csv — the single source of truth for live trade state.

Public interface:
    state = load_state()     # load from disk, or init fresh if file absent
    save_state(state)        # persist to disk atomically
    init_state()             # return a fresh idle ApolloState

Design:
    - ApolloState is a dataclass — typed fields catch typos at AttributeError,
      not silent None returns
    - Atomic write: write to .tmp file, then os.replace() — a crash mid-write
      never corrupts the live state file
    - One row, always overwritten — not append-only (that's apollo_trades.csv)
    - None values round-trip correctly through CSV via explicit NA handling
"""

import os
import pandas as pd
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from typing import Optional

from configs_live import STATE_FILE

# ---------------------------------------------------------------------------
# ApolloState dataclass
# ---------------------------------------------------------------------------

@dataclass
class ApolloState:
    """
    Complete live trade state. One instance exists at runtime.
    Maps 1:1 to columns in apollo_state.csv.

    status values:
        'idle'     — no active trade, ready to take signals
        'in_trade' — position open, monitoring exits
        'exiting'  — exit orders being placed (guard against duplicate triggers)
    """

    # Session status
    status:               str             = 'idle'

    # Trade direction and structure
    direction:            Optional[str]   = None    # 'bullish' or 'bearish'
    buy_strike:           Optional[int]   = None    # ITM leg strike
    sell_strike:          Optional[int]   = None    # OTM leg strike
    option_type:          Optional[str]   = None    # 'ce' or 'pe'
    expiry:               Optional[str]   = None    # 'YYYY-MM-DD'

    # Instrument tokens for WebSocket subscription and order placement
    buy_token:            Optional[str]   = None
    sell_token:           Optional[str]   = None
    buy_symbol:           Optional[str]   = None    # trading symbol for order params
    sell_symbol:          Optional[str]   = None    # trading symbol for order params

    # Entry prices (actual fills)
    buy_entry:            Optional[float] = None
    sell_entry:           Optional[float] = None

    # Lot count — stored so exit always matches entry lots after restart
    lots:                 int             = 1

    # Derived spread metrics — computed at entry, used for exit checks
    net_debit:            Optional[float] = None    # buy_entry - sell_entry
    max_profit:           Optional[float] = None    # HEDGE_POINTS - net_debit
    profit_target_pts:    Optional[float] = None    # max_profit * PROFIT_TARGET_PCT
    hard_stop_pts:        Optional[float] = None    # HARD_STOP_POINTS (stored for reference)

    # Entry context
    entry_time:           Optional[str]   = None    # ISO datetime string
    entry_spot:           Optional[float] = None
    entry_vix:            Optional[float] = None

    # Time gate
    gate_date:            Optional[str]   = None    # 'YYYY-MM-DD' — Day 1 gate check date
    gate_checked:         bool            = False   # True once gate has been evaluated

    # Peak unrealised P&L — updated tick-by-tick, persisted for restart recovery
    max_unrealised_pl:    float           = 0.0

    # Last known LTPs from WebSocket — carried forward on restart
    last_buy_ltp:         Optional[float] = None
    last_sell_ltp:        Optional[float] = None

    # Timestamp of last state write — useful for diagnosing stale state on restart
    last_updated:         Optional[str]   = None    # ISO datetime string


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def init_state() -> ApolloState:
    """Return a fresh idle ApolloState with all trade fields cleared."""
    return ApolloState()


def load_state() -> ApolloState:
    """
    Load ApolloState from apollo_state.csv.
    If the file does not exist or is empty, returns a fresh idle state.

    On restart with status='in_trade', apollo.py should re-subscribe
    option tokens to the WebSocket feed using buy_token and sell_token.
    """
    if not os.path.exists(STATE_FILE):
        return init_state()

    try:
        df = pd.read_csv(STATE_FILE)
        if df.empty:
            return init_state()

        row = df.iloc[0]
        state = ApolloState()

        for f in fields(state):
            if f.name not in row.index:
                continue   # new field added after last save — use default

            val = row[f.name]

            # Treat pandas NA / NaN / empty string as None
            if pd.isna(val) or val == '':
                setattr(state, f.name, None if f.default is None else f.default)
                continue

            # Cast to the declared type
            origin = f.type
            if origin in (Optional[str], str):
                setattr(state, f.name, str(val))
            elif origin in (Optional[int], int):
                setattr(state, f.name, int(val))
            elif origin in (Optional[float], float):
                setattr(state, f.name, float(val))
            elif origin == bool:
                # CSV round-trips booleans as 'True'/'False' strings
                setattr(state, f.name, str(val).strip().lower() == 'true')
            else:
                setattr(state, f.name, val)

        return state

    except Exception as e:
        # Corrupted state file — return idle rather than crash
        print(f"[state] WARNING: Could not load state file ({e}). Starting idle.")
        return init_state()


def save_state(state: ApolloState) -> None:
    """
    Persist ApolloState to apollo_state.csv atomically.
    Stamps last_updated with the current datetime before writing.

    Atomic write: write to .tmp file first, then os.replace().
    A crash mid-write never leaves a corrupted state file.
    """
    state.last_updated = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    row = {f.name: getattr(state, f.name) for f in fields(state)}
    df  = pd.DataFrame([row])

    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)

    tmp_file = STATE_FILE + '.tmp'
    df.to_csv(tmp_file, index=False)
    os.replace(tmp_file, STATE_FILE)


def clear_trade_fields(state: ApolloState) -> ApolloState:
    """
    Reset all trade-specific fields to None/defaults, set status to 'idle'.
    Call after a trade is fully closed and logged.
    Returns the same state object for convenience.
    """
    state.status            = 'idle'
    state.direction         = None
    state.buy_strike        = None
    state.sell_strike       = None
    state.option_type       = None
    state.expiry            = None
    state.buy_token         = None
    state.sell_token        = None
    state.buy_symbol        = None
    state.sell_symbol       = None
    state.lots              = 1
    state.buy_entry         = None
    state.sell_entry        = None
    state.net_debit         = None
    state.max_profit        = None
    state.profit_target_pts = None
    state.hard_stop_pts     = None
    state.entry_time        = None
    state.entry_spot        = None
    state.entry_vix         = None
    state.gate_date         = None
    state.gate_checked      = False
    state.max_unrealised_pl = 0.0
    state.last_buy_ltp      = None
    state.last_sell_ltp     = None
    return state