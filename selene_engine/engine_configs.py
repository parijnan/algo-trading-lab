"""
Parameters of the Selene engine. One place, no magic numbers in engine code (repo convention). The values are the DECIDED ones in
selene_backtest/selene_configs.py as of 2026-09-29 (SILVERMIC, ST 10 / 2.5, single position, 3.0% stop, no targets); a test pins them
to that file so the two cannot drift silently. Sizing is NOT here: the engine reads it live through ctx.sizing(); sizing itself is on
hold pending the user's review (project_selene.md) — Selene is registered paper, static units, until that review.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineConfig:
    instrument: str = 'SILVERMIC'
    # signal
    st_period: int = 10
    st_multiplier: float = 2.5
    # position shape: a single lot, no scale-out, no targets
    sl_pct: float = 3.0
    # provisional-boundary trading (plans/hestia-provisional-all-engines.md): act on a tick-built bar when the candle window is STILL incomplete at the
    # boundary after Angel One's retries and the Fyers rescue (a net below the net). ON from 2026-10-08 (user's decision). The margin is the typical size of
    # the last minute's wobble for SILVERMIC: the mean high-low range of the final traded minute of a bar over history, as a percent of price, rounded up to
    # 0.01% (plan section 7). The tick close must clear the PREVIOUS bar's supertrend by more than this to act. provisional_shadow (evaluate and log, never
    # act) only has an effect when provisional_enabled is False.
    provisional_enabled: bool = True
    provisional_shadow: bool = False
    provisional_margin_pct: float = 0.05
    # timing guards, minutes since the session's actual open
    no_exit_before_buffer_min: float = 1.0
    min_entry_buffer_min: float = 15.0
    # sizing arithmetic (dynamic sizing and the affordability check, if sizing is ever turned on): LTP x lot size / divisor x
    # multiplier per unit
    margin_contract_value_divisor: float = 8.0
    margin_sizing_multiplier: float = 4.0
    fallback_margin_per_unit: float = 100000.0
    # the loop
    tick_s_flat: float = 1.0
    tick_s_in_trade: float = 0.5
    retry_cooldown_s: float = 2.0            # before re-sending a failed exit or flip (Hestia has already retried inside the request)
    realert_debounce_s: float = 300.0        # a stuck exit or flip alerts at most this often
    ltp_max_age_s: float = 60.0              # an older price never triggers a stop
    trade_update_sec: float = 20.0           # periodic in-trade P&L update to #trade-updates, matching Prometheus's own cadence
    running_row_sec: float = 60.0            # per-trade running-log row cadence, matching Prometheus's own
    # rolling (the rules themselves are hestia_core.roll_policy)
    roll_window_days: int = 5
    basis_tolerance_min: float = 5.0
    rollover_buffer_min: float = 14.0        # NOT Prometheus's 15: selene_configs.ROLLOVER_BUFFER_MIN


DEFAULT = EngineConfig()
