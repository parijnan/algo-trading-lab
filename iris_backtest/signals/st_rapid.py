"""ST_RAPID — Dual supertrend 3m entry + 9m regime."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from configs import ST_RAPID_ENTRY_TF, ST_RAPID_REGIME_TF, ST_RAPID_PERIOD, ST_RAPID_MULTIPLIER
from utils import resample_ohlcv, compute_st

SIGNAL_NAME        = 'ST_RAPID'
BAR_PERIOD_MINUTES = 3


def detect(df_1min: pd.DataFrame) -> pd.DataFrame:
    """
    Fires when the 3-min supertrend flips AND the new direction agrees with
    the most-recently-closed 9-min supertrend.
    """
    df_entry  = compute_st(resample_ohlcv(df_1min, ST_RAPID_ENTRY_TF),
                           ST_RAPID_PERIOD, ST_RAPID_MULTIPLIER)
    df_regime = compute_st(resample_ohlcv(df_1min, ST_RAPID_REGIME_TF),
                           ST_RAPID_PERIOD, ST_RAPID_MULTIPLIER)

    flips = df_entry[df_entry['trend_flip'] & df_entry['trend'].notna()].reset_index()
    if flips.empty:
        return pd.DataFrame(columns=['timestamp', 'direction'])

    regime_ts = df_regime[df_regime['trend'].notna()][['trend']].rename(
        columns={'trend': 'trend_regime'}).reset_index()

    merged = pd.merge_asof(flips, regime_ts, on='time_stamp', direction='backward')
    aligned = merged.dropna(subset=['trend_regime'])
    aligned = aligned[aligned['trend'] == aligned['trend_regime']]

    return (
        aligned
        .assign(direction=aligned['trend'].map({True: 'bullish', False: 'bearish'}))
        .rename(columns={'time_stamp': 'timestamp'})
        [['timestamp', 'direction']]
        .reset_index(drop=True)
    )
