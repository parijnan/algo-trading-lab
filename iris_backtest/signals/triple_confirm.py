"""
TRIPLE_CONFIRM — Three-layer confirmation signal.

Layer 1 (day filter):  ORB_75 has already fired today in direction D.
Layer 2 (regime):      ST_FAST has flipped in direction D within
                       ±TRIPLE_CONFIRM_WINDOW_MINUTES of the entry signal.
Layer 3 (entry):       ST_RAPID flips in direction D → entry trigger.

All three must agree. Entry is at the open of the 1-min bar 3 minutes after
the ST_RAPID signal bar closes (BAR_PERIOD_MINUTES = 3).
"""
import sys
import bisect
from pathlib import Path
from collections import defaultdict
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from signals import st_fast, st_rapid
from signals.dual_fast_orb import _orb75_day_lookup
from configs import TRIPLE_CONFIRM_WINDOW_MINUTES

SIGNAL_NAME        = 'TRIPLE_CONFIRM'
BAR_PERIOD_MINUTES = 3


def detect(df_1min: pd.DataFrame) -> pd.DataFrame:
    orb_lookup    = _orb75_day_lookup(df_1min)
    rapid_signals = st_rapid.detect(df_1min)
    fast_signals  = st_fast.detect(df_1min)

    # Pre-index ST_FAST flips: (date, direction) → sorted list of timestamps
    fast_idx = defaultdict(list)
    for _, row in fast_signals.iterrows():
        fast_idx[(row['timestamp'].date(), row['direction'])].append(row['timestamp'])
    for key in fast_idx:
        fast_idx[key].sort()

    window = pd.Timedelta(minutes=TRIPLE_CONFIRM_WINDOW_MINUTES)
    keep   = []

    for _, row in rapid_signals.iterrows():
        ts        = row['timestamp']
        direction = row['direction']
        date      = ts.date()

        # Layer 1: ORB_75 must have already fired today in the same direction
        if date not in orb_lookup:
            continue
        orb_dir, orb_ts = orb_lookup[date]
        if orb_dir != direction or orb_ts >= ts:
            continue

        # Layer 2: ST_FAST must have flipped in same direction within ±window
        fast_times = fast_idx.get((date, direction), [])
        lo = bisect.bisect_left(fast_times,  ts - window)
        hi = bisect.bisect_right(fast_times, ts + window)
        if hi <= lo:
            continue

        keep.append({'timestamp': ts, 'direction': direction})

    return pd.DataFrame(keep) if keep else pd.DataFrame(columns=['timestamp', 'direction'])
