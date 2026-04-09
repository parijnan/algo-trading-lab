"""
precompute_phase2.py — Apollo Phase 2 Triple-Timeframe Precomputation
Reads raw 1-min Nifty index and VIX data, resamples to 5-min, 15-min and
75-min, computes Supertrend on all three timeframes, and saves intermediate
files with the _phase2 suffix.

Run this once (or whenever new data is added) before running
backtest_debit_phase2.py. Output files are read by the backtest — no
recomputation needed on every run unless ST parameters change.

technical_indicators.py is shared with Phase 1 — do not copy or duplicate it.
"""

import os
import sys
import logging
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))

from configs_debit_phase2 import (
    NIFTY_INDEX_FILE, VIX_INDEX_FILE,
    NIFTY_5MIN_FILE, NIFTY_15MIN_FILE, NIFTY_75MIN_FILE, VIX_DAILY_FILE,
    PRECOMPUTED_DIR,
    ST_5MIN_PERIOD,  ST_5MIN_MULTIPLIER,
    ST_15MIN_PERIOD, ST_15MIN_MULTIPLIER,
    ST_75MIN_PERIOD, ST_75MIN_MULTIPLIER,
    TF_LOW, TF_MID, TF_HIGH,
    BACKTEST_START_DATE, BACKTEST_END_DATE,
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


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_index(filepath: str, label: str) -> pd.DataFrame:
    """
    Load a 1-min index CSV (nifty.csv or india_vix.csv).
    Handles the +05:30 timezone suffix in the time_stamp column.
    Returns a DataFrame with timezone-naive IST timestamps.
    """
    logger.info(f"Loading {label} from {filepath}")
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"{label} file not found: {filepath}")

    df = pd.read_csv(filepath, parse_dates=['time_stamp'])
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
    regardless of the interval. Avoids pandas resample drifting on
    non-clock-aligned intervals like 75 minutes.

    For a 375-minute session:
      5-min:  09:15, 09:20, 09:25 … 15:25, 15:30  (75 candles)
      15-min: 09:15, 09:30, 09:45 … 15:15, 15:30  (25 candles)
      75-min: 09:15, 10:30, 11:45, 13:00, 14:15   (5 candles)
    """
    market_open_time  = pd.Timestamp(MARKET_OPEN).time()
    market_close_time = pd.Timestamp(MARKET_CLOSE).time()

    candles = []

    for date, day_df in df.groupby(df['time_stamp'].dt.date):
        day_df = day_df[
            (day_df['time_stamp'].dt.time >= market_open_time) &
            (day_df['time_stamp'].dt.time <= market_close_time)
        ].copy()

        if day_df.empty:
            continue

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
    Adds 'supertrend', 'trend' (True=bullish, False=bearish),
    'trend_flip', and 'trend_signal' columns.
    """
    logger.info(f"  Computing Supertrend ({period}, {multiplier}) on {label} data...")

    df_st = df.rename(columns={
        'open':  'Open',
        'high':  'High',
        'low':   'Low',
        'close': 'Close',
    })

    indicator = SupertrendIndicator(period=period, multiplier=multiplier)
    df_st = indicator.calculate(df_st)

    df_st = df_st.rename(columns={
        'Open':  'open',
        'High':  'high',
        'Low':   'low',
        'Close': 'close',
    })

    df_st['trend']       = df_st['close'] > df_st['Supertrend']
    df_st['trend_flip']  = df_st['trend'] != df_st['trend'].shift(1)
    df_st['trend_signal'] = df_st['trend'].map({True: 'bullish', False: 'bearish'})

    # Mark warmup period as NA
    warmup_mask = df_st['Supertrend'].isna()
    df_st['trend']       = df_st['trend'].astype(object)
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
    Aggregate 1-min VIX data to daily open/high/low/close.
    Uses opening VIX as the regime indicator — observable before market open.
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
    logger.info("=== Apollo Phase 2 Precompute starting ===")

    os.makedirs(PRECOMPUTED_DIR, exist_ok=True)
    os.makedirs(os.path.join(PRECOMPUTED_DIR, "trade_logs_phase2"), exist_ok=True)

    # --- Load raw 1-min data ---
    nifty_1min = load_index(NIFTY_INDEX_FILE, "Nifty 1-min")
    vix_1min   = load_index(VIX_INDEX_FILE,   "India VIX 1-min")

    # --- Resample Nifty to all three timeframes ---
    logger.info(f"Resampling Nifty to {TF_LOW}-min (entry/exit trigger)...")
    nifty_5min = resample_ohlcv(nifty_1min, TF_LOW)
    logger.info(f"  {len(nifty_5min):,} candles")

    logger.info(f"Resampling Nifty to {TF_MID}-min (context/alignment)...")
    nifty_15min = resample_ohlcv(nifty_1min, TF_MID)
    logger.info(f"  {len(nifty_15min):,} candles")

    logger.info(f"Resampling Nifty to {TF_HIGH}-min (regime)...")
    nifty_75min = resample_ohlcv(nifty_1min, TF_HIGH)
    logger.info(f"  {len(nifty_75min):,} candles")

    # --- Compute Supertrend on all three timeframes ---
    nifty_5min  = compute_supertrend(
        nifty_5min,  ST_5MIN_PERIOD,  ST_5MIN_MULTIPLIER,  f"{TF_LOW}-min")
    nifty_15min = compute_supertrend(
        nifty_15min, ST_15MIN_PERIOD, ST_15MIN_MULTIPLIER, f"{TF_MID}-min")
    nifty_75min = compute_supertrend(
        nifty_75min, ST_75MIN_PERIOD, ST_75MIN_MULTIPLIER, f"{TF_HIGH}-min")

    # --- Compute daily VIX ---
    vix_daily = compute_vix_daily(vix_1min)

    # --- Save all output files ---
    logger.info(f"Saving {NIFTY_5MIN_FILE}...")
    nifty_5min.to_csv(NIFTY_5MIN_FILE, index=False)

    logger.info(f"Saving {NIFTY_15MIN_FILE}...")
    nifty_15min.to_csv(NIFTY_15MIN_FILE, index=False)

    logger.info(f"Saving {NIFTY_75MIN_FILE}...")
    nifty_75min.to_csv(NIFTY_75MIN_FILE, index=False)

    logger.info(f"Saving {VIX_DAILY_FILE}...")
    vix_daily.to_csv(VIX_DAILY_FILE, index=False)

    # --- Sanity check output ---
    logger.info("=== Precompute complete. Summary: ===")
    logger.info(f"  5-min  candles : {len(nifty_5min):,} "
                f"({nifty_5min['time_stamp'].min()} → {nifty_5min['time_stamp'].max()})")
    logger.info(f"  15-min candles : {len(nifty_15min):,} "
                f"({nifty_15min['time_stamp'].min()} → {nifty_15min['time_stamp'].max()})")
    logger.info(f"  75-min candles : {len(nifty_75min):,} "
                f"({nifty_75min['time_stamp'].min()} → {nifty_75min['time_stamp'].max()})")
    logger.info(f"  VIX daily rows : {len(vix_daily):,} "
                f"({vix_daily['date'].min()} → {vix_daily['date'].max()})")

    bullish_5  = nifty_5min['trend'].sum()
    bearish_5  = (nifty_5min['trend'].dropna() == False).sum()
    valid_5    = nifty_5min['trend'].notna().sum()
    logger.info(f"  Bullish 5-min  : {bullish_5:,} / {valid_5:,}")
    logger.info(f"  Bearish 5-min  : {bearish_5:,} / {valid_5:,}")
    logger.info(f"  Days with VIX > 16 : "
                f"{(vix_daily['vix_open'] > 16).sum()} / {len(vix_daily)}")
    logger.info("=== Done ===")


if __name__ == "__main__":
    main()