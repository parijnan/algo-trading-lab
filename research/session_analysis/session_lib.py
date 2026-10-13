"""Pure helpers for the session analysis: clock buckets and the per-trade profit attribution."""

import numpy as np
import pandas as pd

import session_configs as cfg


def session_of(hour: int) -> str:
    for name, (a, b) in cfg.SESSIONS.items():
        if a <= hour < b:
            return name
    return 'Other'


def two_way_of(hour: int) -> str:
    s = session_of(hour)
    for name, members in cfg.TWO_WAY.items():
        if s in members:
            return name
    return 'Other'


def attribute_path(bars: pd.DataFrame, entry_px: float, exit_px: float, direction_sign: float, weight: float = 1.0) -> pd.DataFrame:
    """Split one trade's profit (in price points, long-frame sign handled by direction_sign) over the clock buckets it was earned in.

    bars: 1-minute bars from the entry bar to the exit bar INCLUSIVE, columns open/close, index timestamps. The position is held through every bar except the last; the entry is at the
    entry price (open of the first bar), the exit at exit_px (open of the last bar for a flip, inside it for a stop or target). Each bar's close-to-close move goes to that bar's hour; a
    jump of GAP_MIN_MINUTES or more between consecutive bars is booked as the overnight gap (its open-to-close move still goes to the bar). The leg from the last held close to
    exit_px goes to the exit bar's hour. Returns one row per piece: ts, hour, bucket, pts (weighted). Pieces sum to direction_sign * (exit_px - entry_px) * weight by construction when
    bars is one contiguous contract; a roll jump inside the path shows up as a residual the caller reports.
    """
    idx = bars.index
    op, cl = bars['open'].to_numpy(float), bars['close'].to_numpy(float)
    rows = []
    n = len(bars)
    held = n - 1 if n > 1 else 1
    prev_close, prev_ts = entry_px, None
    for j in range(held):
        ts = idx[j]
        if prev_ts is not None and (ts - prev_ts) >= pd.Timedelta(minutes=cfg.GAP_MIN_MINUTES):
            gap = op[j] - prev_close
            rows.append((ts, ts.hour, cfg.OVERNIGHT, direction_sign * gap * weight))
            prev_close = op[j]
        rows.append((ts, ts.hour, session_of(ts.hour), direction_sign * (cl[j] - prev_close) * weight))
        prev_close, prev_ts = cl[j], ts
    last_ts = idx[-1]
    if prev_ts is not None and (last_ts - prev_ts) >= pd.Timedelta(minutes=cfg.GAP_MIN_MINUTES):      # closed on the first bar after a gap: the open's jump is the overnight gap, only the rest is the bar's
        rows.append((last_ts, last_ts.hour, cfg.OVERNIGHT, direction_sign * (op[-1] - prev_close) * weight))
        prev_close = op[-1]
    rows.append((last_ts, last_ts.hour, session_of(last_ts.hour), direction_sign * (exit_px - prev_close) * weight))
    return pd.DataFrame(rows, columns=['ts', 'hour', 'bucket', 'pts'])
