"""Camarilla levels and the previous session's high, low and close. Pure arithmetic, no data access; a copy of the research code
(`janus_backtest/janus_levels.py`, `janus_data.py`), pinned to it by tests/test_oleus_engine_levels.py."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, Optional, Sequence, Tuple

from oleus_engine.engine_configs import SETUPS, EngineConfig


def camarilla_levels(high: float, low: float, close: float, cfg: EngineConfig) -> Dict[str, float]:
    """R1..R7, S1..S7 plus 'C' (previous close) and 'R' (previous range)."""
    if not (high >= low > 0):
        raise ValueError(f'bad previous-session range: high={high} low={low}')
    rng = high - low
    out = {'C': close, 'R': rng}
    for k, div in cfg.level_divisors:
        step = rng * cfg.camarilla_factor / div
        out[f'R{k}'] = close + step
        out[f'S{k}'] = close - step
    r5 = (high / low) * close
    out['R5'] = r5
    out['S5'] = close - (r5 - close)
    r6 = r5 + cfg.r6_factor * (r5 - out['R4'])
    out['R6'] = r6
    out['S6'] = close - (r6 - close)
    r7 = r6 + cfg.r7_factor * (r6 - r5)
    out['R7'] = r7
    out['S7'] = close - (r7 - close)
    return out


def previous_session(minute_bars_for_day, session_date: date, cfg: EngineConfig) -> Optional[Tuple[date, float, float, float]]:
    """(date, high, low, close) of the previous session, or None when there is none usable. `minute_bars_for_day(d)` returns that
    calendar day's completed one-minute bars of the traded contract, oldest first. Same rule as janus_data.load_sessions: the previous
    session is the latest earlier date the contract has any bars on (not the latest with enough of them); it must be within
    `max_prev_session_gap_days` and have `min_bars_per_session` bars, otherwise today is not traded."""
    for back in range(1, cfg.max_prev_session_gap_days + 1):
        d = session_date - timedelta(days=back)
        bars = minute_bars_for_day(d)
        if not bars:
            continue
        if len(bars) < cfg.min_bars_per_session:
            return None
        return d, max(b.high for b in bars), min(b.low for b in bars), bars[-1].close
    return None


def build_setups(levels: Dict[str, float]) -> Dict[str, dict]:
    out = {}
    for key, spec in SETUPS.items():
        e, x = levels[spec['entry']], levels[spec['stop']]
        out[key] = {'key': key, 'side': spec['side'], 'family': spec['family'], 'entry': e, 'stop': x,
                    'targets': [levels[t] for t in spec['targets']], 'risk': abs(e - x)}
    return out


def margin_per_unit(ltp: Optional[float], lot_size: int, cfg: EngineConfig) -> float:
    """LTP x lot size / divisor x multiplier, the shape of Prometheus's own formula with no separate lots factor (plans/oleus-janus-engine.md
    section 1): the multiplier carries the three lots of a unit (4 is the risk-taking choice, 6 the conservative one). The static figure
    when no usable price is available."""
    if not ltp:
        return cfg.fallback_margin_per_unit
    return (ltp * lot_size / cfg.margin_contract_value_divisor) * cfg.margin_sizing_multiplier
