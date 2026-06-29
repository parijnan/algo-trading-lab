"""
VIX routing helpers.

route(vix) returns the strategy that should be entered at the given VIX reading:
  'artemis'  — VIX < ROUTING_VIX_LOW
  'athena'   — ROUTING_VIX_LOW <= VIX <= ROUTING_VIX_HIGH
  'iris'     — VIX > ROUTING_VIX_HIGH

get_routing_vix(date, vix_df) snaps VIX at VIX_SNAP_TIME on date; returns None
if no candle exists within VIX_SNAP_TOL_MIN minutes of the snap time.
"""

import pandas as pd
from datetime import time as dtime
from configs import (
    ROUTING_VIX_LOW, ROUTING_VIX_HIGH,
    VIX_SNAP_HOUR, VIX_SNAP_MINUTE, VIX_SNAP_TOL_MIN,
)

_SNAP_MINUTES = VIX_SNAP_HOUR * 60 + VIX_SNAP_MINUTE  # 630


def route(vix: float) -> str:
    if vix < ROUTING_VIX_LOW:
        return 'artemis'
    if vix <= ROUTING_VIX_HIGH:
        return 'athena'
    return 'iris'


def get_routing_vix(date, vix_df: pd.DataFrame) -> float | None:
    """
    Snap VIX at 10:30 on date.  Falls back to nearest candle within
    VIX_SNAP_TOL_MIN if the exact candle is missing.
    """
    mask = vix_df['_date'] == date
    day = vix_df[mask]
    if day.empty:
        return None

    exact = day[day['_snap_min'] == 0]
    if not exact.empty:
        return float(exact.iloc[0]['close'])

    near = day[day['_snap_min'] <= VIX_SNAP_TOL_MIN]
    if not near.empty:
        return float(near.sort_values('_snap_min').iloc[0]['close'])

    return None


def prepare_vix_df(vix_path: str) -> pd.DataFrame:
    """
    Load the 1-min VIX CSV and pre-compute helper columns so
    get_routing_vix() lookups are fast.
    """
    df = pd.read_csv(vix_path)
    df['_ts'] = pd.to_datetime(df['time_stamp'], utc=True).dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    df['_date'] = df['_ts'].dt.date
    df['_min'] = df['_ts'].dt.hour * 60 + df['_ts'].dt.minute
    df['_snap_min'] = (df['_min'] - _SNAP_MINUTES).abs()
    return df
