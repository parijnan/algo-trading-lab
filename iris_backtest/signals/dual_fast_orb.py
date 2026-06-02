"""DUAL_FAST_ORB — ST_FAST signals filtered to ORB_75 day direction only."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from signals import st_fast
from signals.orb import _detect_orb

SIGNAL_NAME        = 'DUAL_FAST_ORB'
BAR_PERIOD_MINUTES = 5


def _orb75_day_lookup(df_1min: pd.DataFrame) -> dict:
    """Returns {date: (direction, fired_timestamp)} for the first ORB_75 signal per day."""
    orb = _detect_orb(df_1min, 75)
    lookup = {}
    for _, row in orb.iterrows():
        date = row['timestamp'].date()
        if date not in lookup:
            lookup[date] = (row['direction'], row['timestamp'])
    return lookup


def detect(df_1min: pd.DataFrame) -> pd.DataFrame:
    """
    Take ST_FAST signals only when ORB_75 has already fired that day in the
    same direction. Aligns the 5-min trend flip with the day's structural bias.
    """
    orb_lookup  = _orb75_day_lookup(df_1min)
    fast_signals = st_fast.detect(df_1min)

    keep = []
    for _, row in fast_signals.iterrows():
        ts        = row['timestamp']
        direction = row['direction']
        date      = ts.date()

        if date not in orb_lookup:
            continue
        orb_dir, orb_ts = orb_lookup[date]
        if orb_dir != direction or orb_ts >= ts:
            continue

        keep.append({'timestamp': ts, 'direction': direction})

    return pd.DataFrame(keep) if keep else pd.DataFrame(columns=['timestamp', 'direction'])
