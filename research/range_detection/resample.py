"""
resample.py — shared data loading and resampling for range detection scripts.

Supports:
  - 'daily'  : reads nifty_daily.csv directly
  - integer N: resamples 1-min nifty.csv to N-minute bars (day-anchored)
"""

import os
import pandas as pd

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT       = os.path.dirname(os.path.dirname(BASE_DIR))
NIFTY_1MIN_FILE = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'indices', 'nifty.csv')
NIFTY_DAILY_FILE = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'indices', 'nifty_daily.csv')

MARKET_OPEN  = '09:15'
MARKET_CLOSE = '15:30'


def timeframe_label(timeframe) -> str:
    if timeframe == 'daily':
        return 'daily'
    return f'{timeframe}min'


def load_daily() -> pd.DataFrame:
    df = pd.read_csv(NIFTY_DAILY_FILE)
    df['time_stamp'] = pd.to_datetime(df['time_stamp'])
    df = df.set_index('time_stamp').sort_index()
    df = df[df['close'].notna() & (df['close'] > 0)].copy()
    return df


def _load_1min() -> pd.DataFrame:
    df = pd.read_csv(NIFTY_1MIN_FILE)
    df['time_stamp'] = pd.to_datetime(df['time_stamp'], utc=False).dt.tz_localize(None)
    return df.sort_values('time_stamp').reset_index(drop=True)


def resample_intraday(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Day-anchored resample from 1-min data to N-minute bars."""
    open_time  = pd.Timestamp(MARKET_OPEN).time()
    close_time = pd.Timestamp(MARKET_CLOSE).time()
    candles = []

    for date, day_df in df.groupby(df['time_stamp'].dt.date):
        day_df = day_df[
            (day_df['time_stamp'].dt.time >= open_time) &
            (day_df['time_stamp'].dt.time <= close_time)
        ].copy()
        if day_df.empty:
            continue

        session_start = pd.Timestamp(f'{date} {MARKET_OPEN}')
        session_end   = pd.Timestamp(f'{date} {MARKET_CLOSE}')
        anchor = session_start

        while anchor <= session_end:
            window_end = min(
                anchor + pd.Timedelta(minutes=minutes) - pd.Timedelta(minutes=1),
                session_end,
            )
            window = day_df[
                (day_df['time_stamp'] >= anchor) &
                (day_df['time_stamp'] <= window_end)
            ]
            if not window.empty:
                candles.append({
                    'time_stamp': anchor,
                    'open':   window['open'].iloc[0],
                    'high':   window['high'].max(),
                    'low':    window['low'].min(),
                    'close':  window['close'].iloc[-1],
                    'volume': window['volume'].sum(),
                })
            anchor += pd.Timedelta(minutes=minutes)

    result = pd.DataFrame(candles).dropna(subset=['open', 'high', 'low', 'close'])
    return result.set_index('time_stamp').sort_index()


def load_data(timeframe) -> pd.DataFrame:
    """
    Load and return OHLC data for the given timeframe.
    timeframe: 'daily' or int (minutes)
    Returns DataFrame indexed by timestamp.
    """
    if timeframe == 'daily':
        return load_daily()
    raw = _load_1min()
    return resample_intraday(raw, int(timeframe))
