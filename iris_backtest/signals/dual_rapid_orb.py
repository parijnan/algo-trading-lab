"""DUAL_RAPID_ORB — ST_RAPID signals filtered to ORB_75 day direction only."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from signals import st_rapid
from signals.dual_fast_orb import _orb75_day_lookup

SIGNAL_NAME        = 'DUAL_RAPID_ORB'
BAR_PERIOD_MINUTES = 3


def detect(df_1min: pd.DataFrame) -> pd.DataFrame:
    """
    Take ST_RAPID signals only when ORB_75 has already fired that day in the
    same direction. Higher frequency than DUAL_FAST_ORB; shorter entry bar.
    """
    orb_lookup    = _orb75_day_lookup(df_1min)
    rapid_signals = st_rapid.detect(df_1min)

    keep = []
    for _, row in rapid_signals.iterrows():
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
