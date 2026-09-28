"""
Stop and target levels and the P&L arithmetic of the Prometheus position: pure functions, ported from prometheus_functions.py
(`resolve_thresholds`, `resolve_target2`) and prometheus.py (`_finalize_new_position`, `_lot_pnl` shapes), pinned to production by
tests/test_prometheus_engine_levels.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from prometheus_engine.engine_configs import EngineConfig


def resolve_thresholds(threshold_price: float, cfg: EngineConfig) -> Tuple[float, Optional[float]]:
    """(lot1 distance, stop distance) as percentages of the price the levels are computed off."""
    return threshold_price * cfg.target1_pct / 100, (threshold_price * cfg.sl_pct / 100 if cfg.sl_pct is not None else None)


def resolve_target2(threshold_price: float, direction: str, cfg: EngineConfig) -> Tuple[float, str]:
    dist = threshold_price * cfg.target2_flat_pct / 100
    return (threshold_price + dist if direction == 'bullish' else threshold_price - dist), cfg.target2_source


@dataclass(frozen=True)
class Levels:
    sl_price: Optional[float]
    lot1_target: Optional[float]
    lot2_target: float
    lot2_source: str
    lot1_lots: int
    lot2_lots: int


def build_levels(direction: str, threshold_price: float, filled_lots: int, units: int, cfg: EngineConfig,
                 lot2_only: bool = False) -> Levels:
    """Levels for a position of `filled_lots` lots. `threshold_price` is the real fill for an ordinary entry and the recalibration
    basis price for a rollover reopen; `lot2_only` is a rollover reopen of just the far-target lot."""
    sign = 1 if direction == 'bullish' else -1
    lot1_distance, sl_distance = resolve_thresholds(threshold_price, cfg)
    sl = threshold_price - sign * sl_distance if sl_distance is not None else None
    lot2_target, source = resolve_target2(threshold_price, direction, cfg)
    if lot2_only:
        return Levels(sl, None, lot2_target, source, 0, filled_lots)
    lot1_lots = min(filled_lots, units * cfg.lots_per_leg)
    return Levels(sl, threshold_price + sign * lot1_distance, lot2_target, source, lot1_lots, max(0, filled_lots - lot1_lots))


def lot_pnl_points(direction: str, entry: float, exit_price: float) -> float:
    return (exit_price - entry) if direction == 'bullish' else (entry - exit_price)


def margin_per_unit(ltp: Optional[float], lot_size: int, cfg: EngineConfig) -> float:
    """LTP x lot size / 3 x 4, the user's conservative live formula; the static figure when no usable price is available."""
    if not ltp:
        return cfg.fallback_margin_per_unit
    return (ltp * lot_size / cfg.margin_contract_value_divisor) * cfg.margin_sizing_multiplier
