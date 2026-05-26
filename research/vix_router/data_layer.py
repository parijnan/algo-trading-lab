"""
data_layer.py — load and resample VIX / Nifty data for the VIX router research.

All timestamps are returned as tz-naive IST. Never use tz_convert(None) on the raw
1-min CSVs — that converts IST→UTC and silently corrupts intraday resamples.
Use tz_localize(None) to strip the timezone while keeping local IST time.
"""

import os
import sys
import pandas as pd

# Path setup — allow running from any directory
_THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
_DATA_DIR  = os.path.join(_REPO_ROOT, 'data_pipeline', 'data', 'indices')

VIX_1MIN_FILE    = os.path.join(_DATA_DIR, 'india_vix.csv')
NIFTY_1MIN_FILE  = os.path.join(_DATA_DIR, 'nifty.csv')
VIX_DAILY_FILE   = os.path.join(_DATA_DIR, 'india_vix_daily.csv')
NIFTY_DAILY_FILE = os.path.join(_DATA_DIR, 'nifty_daily.csv')

# Add resample.py to path
sys.path.insert(0, os.path.join(_REPO_ROOT, 'research', 'range_detection'))
from resample import resample_daily, resample_intraday  # noqa: E402


def _load_1min_raw(filepath: str) -> pd.DataFrame:
    """Load 1-min CSV; strip timezone (tz_localize keeps IST, tz_convert would shift to UTC)."""
    df = pd.read_csv(filepath)
    df['time_stamp'] = (
        pd.to_datetime(df['time_stamp'], utc=False)
        .dt.tz_localize(None)
    )
    return df.sort_values('time_stamp').reset_index(drop=True)


def load_vix_daily() -> pd.DataFrame:
    """
    Full-history daily VIX from 2019 — resampled from 1-min.
    Returns DataFrame indexed by date (tz-naive), columns: open, high, low, close.
    Uses 15:29 bar close as the daily close (last 1-min bar of the session).
    """
    raw = _load_1min_raw(VIX_1MIN_FILE)
    return resample_daily(raw)


def load_nifty_daily() -> pd.DataFrame:
    """
    Full-history daily Nifty from 2019 — resampled from 1-min.
    Returns DataFrame indexed by date, columns: open, high, low, close.
    """
    raw = _load_1min_raw(NIFTY_1MIN_FILE)
    return resample_daily(raw)


def load_vix_daily_csv() -> pd.DataFrame:
    """
    Load VIX daily from the pre-built CSV (2023-05-23+). Faster but shorter history.
    Returns DataFrame indexed by date.
    """
    df = pd.read_csv(VIX_DAILY_FILE)
    df['time_stamp'] = pd.to_datetime(df['time_stamp'])
    return df.set_index('time_stamp').sort_index()


def load_nifty_daily_csv() -> pd.DataFrame:
    """Load Nifty daily from the pre-built CSV (2023-05-23+)."""
    df = pd.read_csv(NIFTY_DAILY_FILE)
    df['time_stamp'] = pd.to_datetime(df['time_stamp'])
    return df.set_index('time_stamp').sort_index()


def load_combined_daily() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Return (vix_daily, nifty_daily) resampled from 1-min — full history 2019+.
    This is the recommended function for Phase 0–2 validation.
    """
    return load_vix_daily(), load_nifty_daily()
