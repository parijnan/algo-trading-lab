"""
Parameters of the Prometheus engine. One place, no magic numbers in engine code (repo convention). The values are the LIVE ones in
prometheus_production/prometheus_configs.py as of 2026-09-28 (CRUDEOILM, Phase 3, ST 10 / 2.0, positional 2-lot scale-out); a test
pins them to production so the two cannot drift silently while both exist. Sizing is NOT here: the engine reads it live through
`ctx.sizing()` (static units, or its own dynamic rule), and the hard unit cap is Hestia's.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineConfig:
    instrument: str = 'CRUDEOILM'
    # signal
    st_period: int = 10
    st_multiplier: float = 2.0
    # position shape: 1 unit = 2 lots (lots_per_leg per leg); lot1 books at the first target, lot2 rides to the farther one
    lots_per_leg: int = 1
    sl_pct: float = 2.2
    target1_pct: float = 2.2
    target2_flat_pct: float = 5.0
    target2_source: str = 'flat_pct'
    # timing guards, minutes since the session's actual open
    no_exit_before_buffer_min: float = 1.0
    min_entry_buffer_min: float = 15.0
    # provisional-boundary trading; the margin is measured against the PREVIOUS bar's supertrend (fixed 2026-09-28)
    provisional_enabled: bool = True
    provisional_margin_pct: float = 0.15
    # sizing arithmetic (dynamic sizing and the affordability check): LTP x lot size / divisor x multiplier per unit
    margin_contract_value_divisor: float = 3.0
    margin_sizing_multiplier: float = 4.0
    fallback_margin_per_unit: float = 100000.0
    # the loop
    tick_s_flat: float = 1.0
    tick_s_in_trade: float = 0.5
    retry_cooldown_s: float = 2.0            # before re-sending a failed exit or flip (Hestia has already retried inside the request)
    realert_debounce_s: float = 300.0        # a stuck exit or flip alerts at most this often
    ltp_max_age_s: float = 60.0              # an older price never triggers a stop or a target
    trade_update_sec: float = 20.0           # periodic in-trade P&L update to #trade-updates; production's TRADE_UPDATE_SEC
    running_row_sec: float = 60.0            # per-trade running-log row cadence; production's own 1-minute poll boundary
    # rolling (the rules themselves are hestia_core.roll_policy)
    roll_window_days: int = 5
    basis_tolerance_min: float = 5.0


DEFAULT = EngineConfig()
