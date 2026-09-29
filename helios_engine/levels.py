"""
Stop level and P&L arithmetic of the Helios position: pure functions, ported from `helios_backtest/parity_backtest_helios.py`'s
`open_pos`/`close_pos` (a single lot-group, a stop at `ref_price * (1 -+ sl_pct)`, no targets), pinned to that file by
tests/test_helios_engine_levels.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from helios_engine.engine_configs import EngineConfig


def stop_distance(threshold_price: float, cfg: EngineConfig) -> float:
    return threshold_price * cfg.sl_pct / 100


@dataclass(frozen=True)
class Levels:
    sl_price: float


def build_levels(direction: str, threshold_price: float, cfg: EngineConfig) -> Levels:
    """The stop for a position of any size. `threshold_price` is the real fill for an ordinary entry and the recalibration basis
    price for a rollover reopen (the historical-basis method, same as Prometheus's/Selene's)."""
    sign = 1 if direction == 'bullish' else -1
    return Levels(threshold_price - sign * stop_distance(threshold_price, cfg))


def lot_pnl_points(direction: str, entry: float, exit_price: float) -> float:
    return (exit_price - entry) if direction == 'bullish' else (entry - exit_price)


def margin_per_unit(ltp: Optional[float], lot_size: int, cfg: EngineConfig) -> float:
    """(LTP x lot size / divisor x multiplier) x lots_per_unit -- the per-LOT figure (the shape Prometheus's/Selene's own
    formula uses) scaled up by Helios's own 20-lots-per-unit shape. See engine_configs.py's own docstring for why the lot
    count is an explicit factor here rather than folded into the divisor/multiplier the way Prometheus's is. The static
    figure is returned when no usable price is available."""
    if not ltp:
        return cfg.fallback_margin_per_unit
    per_lot = (ltp * lot_size / cfg.margin_contract_value_divisor) * cfg.margin_sizing_multiplier
    return per_lot * cfg.lots_per_unit
