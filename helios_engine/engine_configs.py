"""
Parameters of the Helios engine. One place, no magic numbers in engine code (repo convention). The values are the DECIDED ones
in plans/helios-goldpetal-st-strategy.md as of 2026-09-29 (GOLDPETAL, ST 10 / 3.5, walk-forward-validated 1.6% stop, no target,
1 unit = 20 lots); a test pins them to that plan so the two cannot drift silently. Sizing is NOT here: the engine reads it live
through ctx.sizing(); deployed paper, 1 static unit, alongside Selene (user, 2026-09-29) -- real sizing and risk of ruin deferred
to a later phase, same posture Selene's own config comment carries.

Same shape as selene_engine/engine_configs.py (single position, no scale-out, no targets -- the walk-forward validation landed
Helios on exactly this design too, plan §4g) EXCEPT for lots_per_unit, which Selene never needed (its own unit is 1 lot).
Prometheus's own multi-lot pattern (lots_per_leg, prometheus_engine/engine_configs.py) does NOT fold the lot count into its
margin formula -- its divisor/multiplier were calibrated directly against a real observed PER-UNIT (2-lot) margin figure.
Helios's own margin figure (plan §4g/§2: LTP 14,893, margin ~1,380) was reported for ONE LOT, the natural quantity a broker
screen quotes -- so lots_per_unit is instead an explicit factor in margin_per_unit() (helios_engine/levels.py), keeping the
config directly checkable against a live 1-lot margin quote rather than baking the 20x into an otherwise-opaque multiplier.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineConfig:
    instrument: str = 'GOLDPETAL'
    # signal
    st_period: int = 10
    st_multiplier: float = 3.5
    # position shape: a single lot-group, no scale-out, no targets; 1 unit = 20 lots (plan §4c)
    sl_pct: float = 1.6
    lots_per_unit: int = 20
    # timing guards, minutes since the session's actual open
    no_exit_before_buffer_min: float = 1.0
    min_entry_buffer_min: float = 15.0
    # sizing arithmetic (dynamic sizing and the affordability check, if sizing is ever turned on): LTP x lot size / divisor x
    # multiplier PER LOT, then x lots_per_unit for the per-unit figure margin_per_unit() actually returns -- see this file's
    # own docstring for why the lot-count factor lives here rather than being baked into the divisor/multiplier themselves.
    margin_contract_value_divisor: float = 1.0
    margin_sizing_multiplier: float = 1380 / 14893   # ~0.0927, derived 2026-09-29 from one observed margin quote (plan §2)
    fallback_margin_per_unit: float = 100000.0
    # the loop
    tick_s_flat: float = 1.0
    tick_s_in_trade: float = 0.5
    retry_cooldown_s: float = 2.0            # before re-sending a failed exit or flip (Hestia has already retried inside the request)
    realert_debounce_s: float = 300.0        # a stuck exit or flip alerts at most this often
    ltp_max_age_s: float = 60.0              # an older price never triggers a stop
    trade_update_sec: float = 20.0           # periodic in-trade P&L update to #trade-updates, matching Prometheus's/Selene's cadence
    running_row_sec: float = 60.0            # per-trade running-log row cadence, matching Prometheus's/Selene's
    # rolling (the rules themselves are hestia_core.roll_policy)
    roll_window_days: int = 5                # GOLDPETAL's own tender period, user-confirmed 2026-09-29 (plan §2)
    basis_tolerance_min: float = 5.0
    rollover_buffer_min: float = 14.0        # same as Selene's, NOT Prometheus's 15: helios_configs.ROLLOVER_BUFFER_MIN


DEFAULT = EngineConfig()
