"""
Parameters of the Typhon engine. One place, no magic numbers in engine code (repo convention). The values are the DECIDED
ones in plans/typhon-natgasmini-st-strategy.md as of 2026-09-30 (NATGASMINI, ST 10 / 3.0, real-price/real-roll parity
backtest -- see the plan's Step 4 -- 0.8% stop, 15% target, single lot); a test pins them to that plan so the two cannot
drift silently. Sizing is NOT here: the engine reads it live through ctx.sizing(); registered paper, 1 static unit, same
posture Selene's/Helios's own config comments carry -- real sizing and risk of ruin deferred to a later phase.

Same shape as selene_engine's/helios_engine's own configs (single lot, no scale-out) EXCEPT for target_pct, which neither
of those needed (both decided SL-only, trend-flip-only designs) -- Typhon's own parity calibration found a real, data-backed
edge from adding ONE target (Step 4: profit factor 1.31 vs neighbouring multipliers' ~1.15, and the target itself is a
near-uncapped safety cap in practice, hit on only 0.5% of trades -- trend-flip still does almost all the real work). This
is NOT Prometheus's 2-lot/2-target shape: a single lot with one optional target needs no lot1/lot2 coordination
(no Rule 7, no per-lot partial exits) -- levels.py's build_levels() just gains a second optional field alongside sl_price.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineConfig:
    instrument: str = 'NATGASMINI'
    # signal
    st_period: int = 10
    st_multiplier: float = 3.0
    # position shape: a single lot, no scale-out; ONE target alongside the stop (plan Step 4) -- unlike Selene's/Helios's
    # own SL-only decided designs, Typhon's own calibration found a target genuinely helps here
    sl_pct: float = 0.8
    target_pct: float = 15.0
    lots_per_unit: int = 2                   # 1 unit = 2 lots (owner's decision 2026-09-30: matches Selene's/Helios's margin per unit)
    # timing guards, minutes since the session's actual open
    no_exit_before_buffer_min: float = 1.0
    min_entry_buffer_min: float = 15.0
    # sizing arithmetic (dynamic sizing and the affordability check, if sizing is ever turned on): LTP x lot size / divisor x
    # multiplier per unit -- derived 2026-09-30 from one observed margin quote (plan Step 1: margin ~Rs 14,000/lot when
    # NATGASMINI19OCT26FUT last closed 299.7), same single-point-derivation caveat as Selene's/Helios's own
    margin_contract_value_divisor: float = 1.0
    margin_sizing_multiplier: float = 14000 / (299.7 * 250)   # ~0.1869, ~18.7% of notional
    fallback_margin_per_unit: float = 100000.0
    # the loop
    tick_s_flat: float = 1.0
    tick_s_in_trade: float = 0.5
    retry_cooldown_s: float = 2.0            # before re-sending a failed exit or flip (Hestia has already retried inside the request)
    realert_debounce_s: float = 300.0        # a stuck exit or flip alerts at most this often
    ltp_max_age_s: float = 60.0              # an older price never triggers a stop or target
    trade_update_sec: float = 20.0           # periodic in-trade P&L update to #trade-updates, matching the other engines' cadence
    running_row_sec: float = 60.0            # per-trade running-log row cadence, matching the other engines' own
    # rolling (the rules themselves are hestia_core.roll_policy)
    roll_window_days: int = 5                # NATGASMINI's own tender period, user-confirmed 2026-09-30 (plan Step 1)
    basis_tolerance_min: float = 5.0
    rollover_buffer_min: float = 14.0        # same as Selene's/Helios's, NOT Prometheus's 15: typhon_configs.ROLLOVER_BUFFER_MIN


DEFAULT = EngineConfig()
