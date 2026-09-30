"""
Stop/target level and P&L arithmetic of the Typhon position: pure functions, ported from
`typhon_backtest/parity_backtest_typhon.py`'s own `open_pos`/`sl_for`/`target_for` (a single lot,
a stop at `ref_price * (1 -+ sl_pct)` and a target at `ref_price * (1 +- target_pct)`), pinned to
that file by tests/test_typhon_engine_levels.py.

The ONE addition over Selene's/Helios's own levels.py: target_price alongside sl_price. Both are
computed from the SAME `threshold_price` (the real fill for an ordinary entry, or the
recalibration basis price for a rollover reopen -- the historical-basis method, same as
Prometheus's/Selene's/Helios's own) -- so a roll's basis recalibration applies to the target
exactly the same way it already does to the stop, with no separate code path needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from typhon_engine.engine_configs import EngineConfig


def stop_distance(threshold_price: float, cfg: EngineConfig) -> float:
    return threshold_price * cfg.sl_pct / 100


def target_distance(threshold_price: float, cfg: EngineConfig) -> float:
    return threshold_price * cfg.target_pct / 100


@dataclass(frozen=True)
class Levels:
    sl_price: float
    target_price: Optional[float]


def build_levels(direction: str, threshold_price: float, cfg: EngineConfig) -> Levels:
    """The stop and target for a position of any size. `threshold_price` is the real fill for an
    ordinary entry and the recalibration basis price for a rollover reopen."""
    sign = 1 if direction == 'bullish' else -1
    sl = threshold_price - sign * stop_distance(threshold_price, cfg)
    target = threshold_price + sign * target_distance(threshold_price, cfg) if cfg.target_pct else None
    return Levels(sl, target)


def lot_pnl_points(direction: str, entry: float, exit_price: float) -> float:
    return (exit_price - entry) if direction == 'bullish' else (entry - exit_price)


def margin_per_unit(ltp: Optional[float], lot_size: int, cfg: EngineConfig) -> float:
    """LTP x lot size / divisor x multiplier, the same conservative live formula shape as
    Prometheus's/Selene's/Helios's; the static figure when no usable price is available."""
    if not ltp:
        return cfg.fallback_margin_per_unit
    return (ltp * lot_size / cfg.margin_contract_value_divisor) * cfg.margin_sizing_multiplier
