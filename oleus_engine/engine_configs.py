"""
Parameters of the Oleus engine (Janus "plain thirds" on CRUDEOILM, paper first). One place, no magic numbers in engine code (repo
convention). Every rule value mirrors `janus_backtest/janus_configs.py` as of 2026-10-29 (control levels = the previous session's own
high, low and close; touch entries; three equal lots with lot k exiting at target k; one stop for all open lots; no trailing stop, no
re-buy, no counter trade, no entry limit per setup); tests/test_oleus_engine_configs.py pins the two so they cannot drift silently.
Sizing is NOT here: the engine reads it live through `ctx.sizing()`; one unit is one full set of three lots (`lots_per_unit = 3`), and
the hard unit cap is Hestia's.
"""

from dataclasses import dataclass, field
from typing import Dict, Tuple

# The four setups in absolute prices (janus_configs.ABS_SETUPS without the counter trade, which is off). side +1 long, -1 short.
# The entry is a touch of the level approached from the side named by the setup: a long enters as price rises through its level, a short
# as price falls through it. Lot 1 targets T1, lot 2 T2, lot 3 T3; one stop for all open lots.
SETUPS: Dict[str, dict] = {
    'F_L': {'side': 1, 'family': 'fade', 'entry': 'S3', 'stop': 'S4', 'targets': ('R1', 'R2', 'R3')},
    'F_S': {'side': -1, 'family': 'fade', 'entry': 'R3', 'stop': 'R4', 'targets': ('S1', 'S2', 'S3')},
    'B_L': {'side': 1, 'family': 'breakout', 'entry': 'R4', 'stop': 'R3', 'targets': ('R5', 'R6', 'R7')},
    'B_S': {'side': -1, 'family': 'breakout', 'entry': 'S4', 'stop': 'S3', 'targets': ('S5', 'S6', 'S7')},
}


@dataclass(frozen=True)
class EngineConfig:
    instrument: str = 'CRUDEOILM'
    # Oleus does not use the Supertrend. These are Prometheus's settings, passed only so the two engines share one cached 15-minute series.
    st_period: int = 10
    st_multiplier: float = 2.5
    roll_window_days: int = 5                 # rolls follow hestia_core.roll_policy, like the other engines
    # Camarilla levels from the previous session's high H, low L, close C, range R = H - L
    camarilla_factor: float = 1.1
    level_divisors: Tuple[Tuple[int, float], ...] = ((1, 12.0), (2, 6.0), (3, 4.0), (4, 2.0))   # R1..R4 = C + R * factor / divisor
    r6_factor: float = 1.168
    r7_factor: float = 1.168
    # the previous session must be at most this many calendar days back and have at least this many 1-minute bars, else no trading today
    max_prev_session_gap_days: int = 5
    min_bars_per_session: int = 120
    # position shape: 1 unit = three lots (lots_per_target per target), lot k exits at target k
    lots_per_target: int = 1
    # time rules, minutes (the session's first bar and last bar come from Hestia: first bar = session open, last bar = close - 1 min)
    at_open_minutes: int = 1                  # no entry in the first minute
    entry_cutoff_min: int = 60                # no entry in the last 60 minutes (measured from the last bar)
    hard_exit_min: int = 15                   # everything open is closed from this many minutes before the last bar
    # prices: an older tick never triggers anything (entries are stricter: a touch is the whole signal)
    ltp_max_age_s: float = 30.0
    ltp_max_age_entry_s: float = 10.0
    # the loop
    tick_s_flat: float = 1.0
    tick_s_in_trade: float = 0.5
    retry_cooldown_s: float = 2.0
    realert_debounce_s: float = 300.0
    trade_update_sec: float = 20.0
    running_row_sec: float = 60.0
    # sizing arithmetic (affordability check): LTP x lot size / divisor x multiplier per unit. Multiplier 4 is the risk-taking choice of
    # plans/oleus-janus-engine.md D2 (6 is the conservative one); paper runs on 4 and the live figure is the owner's call.
    margin_contract_value_divisor: float = 3.0
    margin_sizing_multiplier: float = 4.0
    fallback_margin_per_unit: float = 100000.0


DEFAULT = EngineConfig()

# Extra columns of oleus_trades.csv beyond the shared Prometheus format (interface.TRADE_RECORD_COLUMNS): the third lot, and the setup.
TRADE_EXTRA_COLUMNS = ('setup', 'lot3_target', 'lot3_exit_ts', 'lot3_exit_price', 'lot3_exit_reason', 'lot3_pnl_points', 'lot3_pnl_rs')
