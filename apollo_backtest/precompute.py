"""
precompute.py — Apollo Backtest Precomputation
Reads raw 1-min Nifty index and VIX data, resamples to 15-min and 75-min,
computes Supertrend on both timeframes, and saves intermediate files.

Run this once (or whenever new data is added) before running backtest.py.
Output files are read by backtest.py — no recomputation needed on every run.
"""

import os
import sys
import logging
import pandas as pd

# Ensure the project root is on the path so configs and indicators import cleanly
sys.path.insert(0, os.path.dirname(__file__))

from configs_debit import (
    NIFTY_INDEX_FILE, VIX_INDEX_FILE,
    NIFTY_15MIN_FILE, NIFTY_75MIN_FILE, VIX_DAILY_FILE,
    PRECOMPUTED_DIR,
    ST_15MIN_PERIOD, ST_15MIN_MULTIPLIER,
    ST_75MIN_PERIOD, ST_75MIN_MULTIPLIER,
    TF_LOW, TF_HIGH,
    BACKTEST_START_DATE, BACKTEST_END_DATE
)
from technical_indicators import SupertrendIndicator

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MARKET_OPEN  = "09:15"
MARKET_CLOSE = "15:30"

# Resample aggregation rules for OHLCV
RESAMPLE_OHLCV = {
    'open':   'first',
    'high':   'max',
    'low':    'min',
    'close':  'last',
    'volume': 'sum',
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_index(filepath: str, label: str) -> pd.DataFrame:
    """
    Load a 1-min index CSV (nifty.csv or india_vix.csv).
    Handles the +05:30 timezone suffix in the time_stamp column.
    Returns a DataFrame indexed by timezone-naive IST timestamps.
    """
    logger.info(f"Loading {label} from {filepath}")
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"{label} file not found: {filepath}")

    df = pd.read_csv(filepath, parse_dates=['time_stamp'])

    # Strip timezone info — all data is IST, we don't need tz-awareness internally
    df['time_stamp'] = pd.to_datetime(df['time_stamp'], utc=False).dt.tz_localize(None)

    # Keep only market hours
    df = df[
        (df['time_stamp'].dt.time >= pd.Timestamp(MARKET_OPEN).time()) &
        (df['time_stamp'].dt.time <= pd.Timestamp(MARKET_CLOSE).time())
    ].copy()

    # Apply backtest date range filter
    if BACKTEST_START_DATE:
        df = df[df['time_stamp'] >= pd.Timestamp(BACKTEST_START_DATE)]
    if BACKTEST_END_DATE:
        df = df[df['time_stamp'] <= pd.Timestamp(BACKTEST_END_DATE)]

    df = df.sort_values('time_stamp').reset_index(drop=True)
    logger.info(f"  Loaded {len(df):,} rows ({df['time_stamp'].min().date()} "
                f"to {df['time_stamp'].max().date()})")
    return df


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------

def resample_ohlcv(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """
    Resample a 1-min OHLCV DataFrame to the given timeframe in minutes.
    Uses manual day-by-day anchored grouping so candles always start at 09:15
    regardless of the interval. This avoids pandas resample drifting on
    non-clock-aligned intervals like 75 minutes.

    For a 375-minute session with 75-min candles, this guarantees exactly:
      09:15, 10:30, 11:45, 13:00, 14:15
    """
    market_open_time  = pd.Timestamp(MARKET_OPEN).time()
    market_close_time = pd.Timestamp(MARKET_CLOSE).time()

    candles = []

    for date, day_df in df.groupby(df['time_stamp'].dt.date):
        # Filter strictly to market hours for this day
        day_df = day_df[
            (day_df['time_stamp'].dt.time >= market_open_time) &
            (day_df['time_stamp'].dt.time <= market_close_time)
        ].copy()

        if day_df.empty:
            continue

        # Build anchor timestamps for this day starting at 09:15
        session_start = pd.Timestamp(f"{date} {MARKET_OPEN}")
        session_end   = pd.Timestamp(f"{date} {MARKET_CLOSE}")

        anchor = session_start
        while anchor <= session_end:
            window_end = anchor + pd.Timedelta(minutes=minutes) - pd.Timedelta(minutes=1)
            window_end = min(window_end, session_end)

            window = day_df[
                (day_df['time_stamp'] >= anchor) &
                (day_df['time_stamp'] <= window_end)
            ]

            if not window.empty:
                candle = {
                    'time_stamp': anchor,
                    'open':       window['open'].iloc[0],
                    'high':       window['high'].max(),
                    'low':        window['low'].min(),
                    'close':      window['close'].iloc[-1],
                    'volume':     window['volume'].sum(),
                }
                candles.append(candle)

            anchor += pd.Timedelta(minutes=minutes)

    resampled = pd.DataFrame(candles)
    resampled = resampled.dropna(subset=['open', 'high', 'low', 'close'])
    resampled = resampled.reset_index(drop=True)
    return resampled


# ---------------------------------------------------------------------------
# Supertrend calculation
# ---------------------------------------------------------------------------

def compute_supertrend(df: pd.DataFrame, period: int, multiplier: float,
                       label: str) -> pd.DataFrame:
    """
    Compute Supertrend on a resampled OHLCV DataFrame.
    Renames columns to match SupertrendIndicator expectations (High/Low/Close),
    runs the calculation, then renames back.
    Adds 'supertrend', 'trend' (True=bullish, False=bearish), and
    'trend_signal' ('bullish'/'bearish'/None) columns.
    """
    logger.info(f"  Computing Supertrend ({period}, {multiplier}) on {label} data...")

    # SupertrendIndicator expects capitalised column names
    df_st = df.rename(columns={
        'open':  'Open',
        'high':  'High',
        'low':   'Low',
        'close': 'Close',
    })

    indicator = SupertrendIndicator(period=period, multiplier=multiplier)
    df_st = indicator.calculate(df_st)

    # Rename back
    df_st = df_st.rename(columns={
        'Open':  'open',
        'High':  'high',
        'Low':   'low',
        'Close': 'close',
    })

    # Derive boolean trend direction from Supertrend value
    # Supertrend = LowerBand when bullish (price above), UpperBand when bearish
    # We detect direction by comparing close to supertrend value
    df_st['trend'] = df_st['close'] > df_st['Supertrend']

    # Detect flips — where trend changes from previous candle
    df_st['trend_flip'] = df_st['trend'] != df_st['trend'].shift(1)

    # Human-readable signal
    df_st['trend_signal'] = df_st['trend'].map({True: 'bullish', False: 'bearish'})

    # Mark NaN for the warmup period where Supertrend is not yet valid
    warmup_mask = df_st['Supertrend'].isna()
    df_st['trend']        = df_st['trend'].astype(object)
    df_st.loc[warmup_mask, 'trend']        = pd.NA
    df_st.loc[warmup_mask, 'trend_flip']   = False
    df_st.loc[warmup_mask, 'trend_signal'] = None

    logger.info(f"  Done. {len(df_st):,} candles computed.")
    return df_st


# ---------------------------------------------------------------------------
# VIX daily aggregation
# ---------------------------------------------------------------------------

def compute_vix_daily(vix_df: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate 1-min VIX data to daily.
    Uses the opening VIX value of the day as the regime indicator —
    this is what would be observable before market open for trade decisions.
    Also stores daily high/low/close for reference.
    """
    logger.info("Computing daily VIX...")
    vix_df = vix_df.set_index('time_stamp')
    daily = vix_df['close'].resample('D').agg(
        vix_open='first',
        vix_high='max',
        vix_low='min',
        vix_close='last'
    ).dropna()
    daily = daily.reset_index()
    daily = daily.rename(columns={'time_stamp': 'date'})
    daily['date'] = daily['date'].dt.date
    logger.info(f"  {len(daily):,} trading days of VIX data computed.")
    return daily


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logger.info("=== Apollo Precompute starting ===")

    # Create output directory
    os.makedirs(PRECOMPUTED_DIR, exist_ok=True)
    os.makedirs(os.path.join(PRECOMPUTED_DIR, "trade_logs"), exist_ok=True)

    # --- Load raw 1-min data ---
    nifty_1min = load_index(NIFTY_INDEX_FILE, "Nifty 1-min")
    vix_1min   = load_index(VIX_INDEX_FILE,   "India VIX 1-min")

    # --- Resample Nifty ---
    logger.info(f"Resampling Nifty to {TF_LOW}-min...")
    nifty_15min = resample_ohlcv(nifty_1min, TF_LOW)
    logger.info(f"  {len(nifty_15min):,} candles")

    logger.info(f"Resampling Nifty to {TF_HIGH}-min...")
    nifty_75min = resample_ohlcv(nifty_1min, TF_HIGH)
    logger.info(f"  {len(nifty_75min):,} candles")

    # --- Compute Supertrend ---
    nifty_15min = compute_supertrend(
        nifty_15min, ST_15MIN_PERIOD, ST_15MIN_MULTIPLIER, f"{TF_LOW}-min")
    nifty_75min = compute_supertrend(
        nifty_75min, ST_75MIN_PERIOD, ST_75MIN_MULTIPLIER, f"{TF_HIGH}-min")

    # --- Compute daily VIX ---
    vix_daily = compute_vix_daily(vix_1min)

    # --- Save intermediate files ---
    logger.info(f"Saving {NIFTY_15MIN_FILE}...")
    nifty_15min.to_csv(NIFTY_15MIN_FILE, index=False)

    logger.info(f"Saving {NIFTY_75MIN_FILE}...")
    nifty_75min.to_csv(NIFTY_75MIN_FILE, index=False)

    logger.info(f"Saving {VIX_DAILY_FILE}...")
    vix_daily.to_csv(VIX_DAILY_FILE, index=False)

    # --- Sanity check output ---
    logger.info("=== Precompute complete. Summary: ===")
    logger.info(f"  15-min candles : {len(nifty_15min):,} "
                f"({nifty_15min['time_stamp'].min()} → {nifty_15min['time_stamp'].max()})")
    logger.info(f"  75-min candles : {len(nifty_75min):,} "
                f"({nifty_75min['time_stamp'].min()} → {nifty_75min['time_stamp'].max()})")
    logger.info(f"  VIX daily rows : {len(vix_daily):,} "
                f"({vix_daily['date'].min()} → {vix_daily['date'].max()})")
    logger.info(f"  Bullish 75-min candles : "
                f"{nifty_75min['trend'].sum():,} / {nifty_75min['trend'].notna().sum():,}")
    bearish_count = (nifty_75min['trend'].dropna() == False).sum()
    logger.info(f"  Bearish 75-min candles : "
                f"{bearish_count:,} / "
                f"{nifty_75min['trend'].notna().sum():,}")
    logger.info(f"  Days with VIX > 16     : "
                f"{(vix_daily['vix_open'] > 16).sum()} / {len(vix_daily)}")
    logger.info("=== Done ===")


if __name__ == "__main__":
    main()