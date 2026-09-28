"""
Stop level and P&L arithmetic of the Selene position: pure functions, ported from `selene_backtest/parity_backtest_selene.py`'s
`open_pos`/`close_pos` (a single lot, a stop at `ref_price * (1 -+ sl_pct)`, no targets), pinned to that file by
tests/test_selene_engine_levels.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from selene_engine.engine_configs import EngineConfig


def stop_distance(threshold_price: float, cfg: EngineConfig) -> float:
    return threshold_price * cfg.sl_pct / 100


@dataclass(frozen=True)
class Levels:
    sl_price: float


def build_levels(direction: str, threshold_price: float, cfg: EngineConfig) -> Levels:
    """The stop for a position of any size. `threshold_price` is the real fill for an ordinary entry and the recalibration basis
    price for a rollover reopen (the historical-basis method, same as Prometheus's)."""
    sign = 1 if direction == 'bullish' else -1
    return Levels(threshold_price - sign * stop_distance(threshold_price, cfg))


def lot_pnl_points(direction: str, entry: float, exit_price: float) -> float:
    return (exit_price - entry) if direction == 'bullish' else (entry - exit_price)


def margin_per_unit(ltp: Optional[float], lot_size: int, cfg: EngineConfig) -> float:
    """LTP x lot size / divisor x multiplier, the same conservative live formula shape as Prometheus's; the static figure when no
    usable price is available."""
    if not ltp:
        return cfg.fallback_margin_per_unit
    return (ltp * lot_size / cfg.margin_contract_value_divisor) * cfg.margin_sizing_multiplier
