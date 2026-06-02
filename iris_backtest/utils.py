import sys
import pandas as pd
import numpy as np
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT / 'apollo_production'))
from technical_indicators import SupertrendIndicator

from configs import NIFTY_1MIN_FILE, NIFTY_DAILY_FILE, SENSEX_1MIN_FILE, HORIZONS


def load_nifty_1min() -> pd.DataFrame:
    df = pd.read_csv(NIFTY_1MIN_FILE, parse_dates=['time_stamp'])
    df['time_stamp'] = pd.to_datetime(df['time_stamp']).dt.tz_localize(None)
    df = df.set_index('time_stamp').sort_index()
    df = df[(df['close'].notna()) & (df['close'] > 0)]
    return df.between_time('09:15', '15:29')


def load_sensex_1min() -> pd.DataFrame:
    df = pd.read_csv(SENSEX_1MIN_FILE, parse_dates=['time_stamp'])
    df['time_stamp'] = pd.to_datetime(df['time_stamp']).dt.tz_localize(None)
    df = df.set_index('time_stamp').sort_index()
    df = df[(df['close'].notna()) & (df['close'] > 0)]
    return df.between_time('09:15', '15:29')


def load_nifty_daily() -> pd.DataFrame:
    df = pd.read_csv(NIFTY_DAILY_FILE, parse_dates=['time_stamp'])
    df['time_stamp'] = pd.to_datetime(df['time_stamp'])
    df = df.set_index('time_stamp').sort_index()
    return df[(df['close'].notna()) & (df['close'] > 0)]


def resample_ohlcv(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Resample per trading day, anchored to each day's first bar (09:15)."""
    dfs = []
    for _, day in df.groupby(df.index.date):
        resampled = day.resample(freq, origin=day.index[0]).agg(
            open=('open', 'first'),
            high=('high', 'max'),
            low=('low', 'min'),
            close=('close', 'last'),
            volume=('volume', 'sum'),
        ).dropna(subset=['close'])
        dfs.append(resampled)
    return pd.concat(dfs) if dfs else pd.DataFrame()


def compute_st(df: pd.DataFrame, period: int, multiplier: float) -> pd.DataFrame:
    """
    Add supertrend, trend (bool), trend_flip (bool) to df.
    df must have lowercase ohlcv columns and a DatetimeIndex.
    """
    df_up = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'})
    result = SupertrendIndicator(period=period, multiplier=multiplier).calculate(df_up)
    result = result.rename(columns={
        'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close',
        'Supertrend': 'supertrend',
    })
    result['trend'] = (result['close'] > result['supertrend']).astype(object)
    result.loc[result['supertrend'].isna(), 'trend'] = pd.NA
    result['trend_flip'] = result['trend'] != result['trend'].shift(1)
    result.loc[result['supertrend'].isna(), 'trend_flip'] = False
    return result


def intraday_kbar_range(df: pd.DataFrame, k: int) -> pd.DataFrame:
    """
    For each bar in df (resampled, DatetimeIndex), compute the rolling K-bar
    range from PREVIOUS bars within the same trading day.
    Returns df with 'range_high' and 'range_low' columns (NaN for first K bars
    of each day — no signal possible until enough intraday history exists).
    """
    dfs = []
    for _, day in df.groupby(df.index.date):
        d = day.copy()
        d['range_high'] = d['high'].rolling(k, min_periods=k).max().shift(1)
        d['range_low']  = d['low'].rolling(k, min_periods=k).min().shift(1)
        dfs.append(d)
    return pd.concat(dfs) if dfs else df.assign(range_high=np.nan, range_low=np.nan)


def compute_excursions(df_1min: pd.DataFrame, signals: pd.DataFrame,
                       bar_period_minutes: int,
                       horizons: list = None) -> pd.DataFrame:
    """
    For each signal fire (timestamp = bar open time, direction = bullish/bearish):
      - Entry price = open of the 1-min bar immediately after the signal bar closes
        (i.e., at signal_timestamp + bar_period_minutes)
      - For each horizon N: compute MFE, MAE, close_at_N (in index points, signed
        so that positive = in the trade's favour regardless of direction)
      - Horizons that extend past the end of the trading day are recorded as NaN

    MFE = max favorable excursion (always positive; zero means price never moved for you)
    MAE = max adverse excursion   (always positive; zero means price never moved against you)
    close_N = net points at bar N  (positive = profitable, negative = loss)
    """
    if horizons is None:
        horizons = HORIZONS
    if signals.empty:
        return pd.DataFrame()

    idx = df_1min.index
    records = []

    for _, sig in signals.iterrows():
        signal_ts = sig['timestamp']
        direction = sig['direction']
        entry_ts  = signal_ts + pd.Timedelta(minutes=bar_period_minutes)

        entry_pos = idx.searchsorted(entry_ts)
        if entry_pos >= len(idx):
            continue
        # Allow up to 2-min slip for minor data gaps; skip larger holes
        if idx[entry_pos] > entry_ts + pd.Timedelta(minutes=2):
            continue

        entry_price = df_1min['open'].iloc[entry_pos]
        trade_date  = idx[entry_pos].date()

        # Last bar of the trading day (capped at 15:29)
        day_last_pos = idx.searchsorted(
            pd.Timestamp(f'{trade_date} 15:30:00'), side='right') - 1

        record = {
            'signal_ts':   signal_ts,
            'entry_ts':    idx[entry_pos],
            'direction':   direction,
            'entry_price': entry_price,
        }

        for N in horizons:
            end_pos = entry_pos + N
            if end_pos > day_last_pos + 1:
                record[f'mfe_{N}']   = np.nan
                record[f'mae_{N}']   = np.nan
                record[f'close_{N}'] = np.nan
                continue

            window = df_1min.iloc[entry_pos:end_pos]

            if direction == 'bullish':
                mfe     = window['high'].max() - entry_price
                mae     = entry_price - window['low'].min()
                close_n = window['close'].iloc[-1] - entry_price
            else:
                mfe     = entry_price - window['low'].min()
                mae     = window['high'].max() - entry_price
                close_n = entry_price - window['close'].iloc[-1]

            record[f'mfe_{N}']   = round(mfe, 2)
            record[f'mae_{N}']   = round(mae, 2)
            record[f'close_{N}'] = round(close_n, 2)

        records.append(record)

    return pd.DataFrame(records)
