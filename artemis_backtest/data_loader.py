"""
data_loader.py — Artemis Backtest Data Loader

Handles schema differences between Nifty and Sensex option files,
and between the two Sensex data sources (with/without OI column).

All returned DataFrames use a consistent internal schema:
  time_stamp (tz-naive IST datetime), open, high, low, close, volume, oi

Index files use the same schema minus oi.
"""

import os
import logging
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Index data
# ---------------------------------------------------------------------------

def load_index_data(filepath: str) -> pd.DataFrame:
    """
    Load a 1-min index CSV (sensex.csv, nifty.csv, or india_vix.csv).

    Timestamp format: '2024-07-19 09:20:00+05:30'
    Strips timezone to produce tz-naive IST timestamps.
    Returns DataFrame indexed by time_stamp, sorted ascending.
    Columns: open, high, low, close, volume, oi
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Index file not found: {filepath}")

    df = pd.read_csv(filepath, parse_dates=['time_stamp'])
    df['time_stamp'] = pd.to_datetime(
        df['time_stamp'], utc=False).dt.tz_localize(None)
    df = df.sort_values('time_stamp').reset_index(drop=True)
    df = df.set_index('time_stamp')
    return df


def load_vix_daily(vix_filepath: str) -> pd.DataFrame:
    """
    Aggregate 1-min VIX data to daily.
    Returns DataFrame with columns: date (datetime.date), vix_open.
    vix_open = first value of the day (observable before market open).
    """
    df = load_index_data(vix_filepath)
    df = df.reset_index()
    df['date'] = df['time_stamp'].dt.date
    daily = (
        df.groupby('date')['close']
        .first()
        .reset_index()
        .rename(columns={'close': 'vix_open'})
    )
    return daily


# ---------------------------------------------------------------------------
# Option data
# ---------------------------------------------------------------------------

def load_option_data(instrument: str, options_base_path: str,
                     expiry_date: pd.Timestamp,
                     strike: int, option_type: str) -> pd.DataFrame:
    """
    Load 1-min option data for a given instrument, expiry, strike, and type.

    instrument:      'nifty' or 'sensex'
    options_base_path: root options directory (NIFTY_OPTIONS_PATH or SENSEX_OPTIONS_PATH)
    expiry_date:     the expiry date (used to find the subdirectory)
    strike:          integer strike price
    option_type:     'ce' or 'pe'

    Returns normalised DataFrame with columns:
        time_stamp (tz-naive, index), open, high, low, close, volume, oi
    Returns empty DataFrame if file not found.

    Nifty schema:
        datetime, stock_code, exchange_code, product_type, expiry_date,
        right, strike_price, open, high, low, close, volume, open_interest, count
        Timestamp: 'YYYY-MM-DD HH:MM:SS' (no timezone)

    Sensex schema A (older, no OI):
        time_stamp, open, high, low, close, volume
        Timestamp: 'YYYY-MM-DDTHH:MM:SS+05:30'

    Sensex schema B (newer, with OI):
        time_stamp, open, high, low, close, volume, oi
        Timestamp: 'YYYY-MM-DDTHH:MM:SS+05:30'
    """
    expiry_str = expiry_date.strftime('%Y-%m-%d')
    filename   = f"{strike}{option_type}.csv"
    filepath   = os.path.join(options_base_path, expiry_str, filename)

    if not os.path.exists(filepath):
        logger.debug(f"Option file not found: {filepath}")
        return pd.DataFrame()

    if instrument == 'nifty':
        return _load_nifty_option(filepath)
    else:
        return _load_sensex_option(filepath)


def _load_nifty_option(filepath: str) -> pd.DataFrame:
    """
    Load and normalise a Nifty option CSV.
    Renames 'datetime' → 'time_stamp', 'open_interest' → 'oi'.
    No timezone stripping needed (timestamps are already tz-naive).
    """
    df = pd.read_csv(filepath, parse_dates=['datetime'])
    df = df.rename(columns={
        'datetime':       'time_stamp',
        'open_interest':  'oi',
    })
    df['time_stamp'] = pd.to_datetime(df['time_stamp'], utc=False)
    df = df[['time_stamp', 'open', 'high', 'low', 'close', 'volume', 'oi']]
    df = df.sort_values('time_stamp').reset_index(drop=True)
    df = df.drop_duplicates(subset='time_stamp', keep='first')
    df = df.set_index('time_stamp')
    return df


def _load_sensex_option(filepath: str) -> pd.DataFrame:
    """
    Load and normalise a Sensex option CSV.
    Detects schema A (no oi column) vs schema B (with oi column) automatically.
    Strips '+05:30' timezone to produce tz-naive IST timestamps.
    """
    # Peek at header to detect schema variant
    with open(filepath, 'r') as f:
        header = f.readline().strip().split(',')
    has_oi = 'oi' in header

    df = pd.read_csv(filepath, parse_dates=['time_stamp'])
    df['time_stamp'] = pd.to_datetime(
        df['time_stamp'], utc=False).dt.tz_localize(None)

    if not has_oi:
        df['oi'] = float('nan')

    df = df[['time_stamp', 'open', 'high', 'low', 'close', 'volume', 'oi']]
    df = df.sort_values('time_stamp').reset_index(drop=True)
    df = df.set_index('time_stamp')
    return df


# ---------------------------------------------------------------------------
# Price lookup helpers
# ---------------------------------------------------------------------------

def get_price(option_df: pd.DataFrame, timestamp: pd.Timestamp,
              col: str = 'close') -> float:
    """
    Get option price at an exact timestamp.
    Falls back to the last available price before the timestamp if not found.
    Returns None if no data is available at or before the timestamp.
    Handles duplicate timestamps (can occur in Nifty ICICI Breeze data) by
    taking the first matching row.
    """
    if option_df.empty:
        return None
    if timestamp in option_df.index:
        val = option_df.loc[timestamp, col]
        # loc returns a Series when duplicate timestamps exist — take first value
        if isinstance(val, pd.Series):
            val = val.iloc[0]
        return float(val) if pd.notna(val) else None
    prior = option_df[option_df.index < timestamp]
    if not prior.empty:
        val = prior[col].iloc[-1]
        return float(val) if pd.notna(val) else None
    return None


def get_index_price(index_df: pd.DataFrame, timestamp: pd.Timestamp,
                    col: str = 'close') -> float:
    """
    Get index price at an exact timestamp from a timestamp-indexed DataFrame.
    Falls back to last available before timestamp.
    Returns None if not found.
    Handles duplicate timestamps by taking the first matching row.
    """
    if index_df.empty:
        return None
    if timestamp in index_df.index:
        val = index_df.loc[timestamp, col]
        if isinstance(val, pd.Series):
            val = val.iloc[0]
        return float(val) if pd.notna(val) else None
    prior = index_df[index_df.index < timestamp]
    if not prior.empty:
        val = prior[col].iloc[-1]
        return float(val) if pd.notna(val) else None
    return None


def get_next_open(option_df: pd.DataFrame,
                  after_ts: pd.Timestamp) -> tuple:
    """
    Get the open price of the first candle strictly after after_ts.
    Returns (timestamp, open_price) or (None, None) if not found.
    """
    if option_df.empty:
        return None, None
    future = option_df[option_df.index > after_ts]
    if future.empty:
        return None, None
    ts    = future.index[0]
    price = future['open'].iloc[0]
    return ts, float(price) if pd.notna(price) else None


def get_index_next_open(index_df: pd.DataFrame,
                        after_ts: pd.Timestamp) -> tuple:
    """
    Get the open price of the first index candle strictly after after_ts.
    Returns (timestamp, open_price) or (None, None) if not found.
    """
    if index_df.empty:
        return None, None
    future = index_df[index_df.index > after_ts]
    if future.empty:
        return None, None
    ts    = future.index[0]
    price = future['open'].iloc[0]
    return ts, float(price) if pd.notna(price) else None


def scan_strikes_for_premium(
        instrument: str,
        options_base_path: str,
        expiry_date: pd.Timestamp,
        option_type: str,
        start_strike: int,
        strike_interval: int,
        direction: int,
        target_premium: float,
        ref_timestamp: pd.Timestamp,
        max_steps: int = 200) -> tuple:
    """
    Scan OTM strikes from start_strike in direction (+1 for CE, -1 for PE),
    loading the close price at ref_timestamp for each strike.

    Stops at the first strike whose LTP is <= target_premium and returns it.
    This is correct because OTM premiums decrease monotonically away from ATM,
    so the first strike below the target is always the closest from below.
    No fixed scan range needed — works correctly in high-VIX environments
    where the target strike may be far OTM.

    max_steps is a safety cap only — should never be reached in practice.
    Returns (strike, close_price) or (None, None) if not found.
    """
    for step in range(max_steps):
        strike = start_strike + direction * step * strike_interval
        df     = load_option_data(
            instrument, options_base_path, expiry_date, strike, option_type)
        if df.empty:
            continue
        price = get_price(df, ref_timestamp, col='close')
        if price is None or price <= 0:
            continue
        if price <= target_premium:
            return strike, price

    return None, None