"""
supertrend.py — Apollo Production Supertrend Manager
Handles seeding and incremental updates for 15-min and 75-min Supertrend.

Public interface:
    st = SupertrendManager()
    st.seed(smart_connect_obj)              # call once at session start
    trend_15, flip_15, trend_75, flip_75 = st.update(candle)  # after each 15-min close
    st.get_cache()                          # returns 15-min DataFrame with ST values

Design:
    - Seeds from Angel One historical candle API (last ST_HISTORY_CANDLES 15-min candles)
    - 75-min candles derived by grouping every 5 × 15-min candles anchored at 09:15
    - Full recompute on every update() — never incremental — because Wilder's smoothing
      (ewm alpha=1/period, adjust=False) is path-dependent. Incremental computation
      diverges from chart values over time.
    - Cache persisted to supertrend_cache.csv after every update for restart recovery
    - Reuses SupertrendIndicator from technical_indicators.py (same class as backtest)
"""

import os
import sys
import pandas as pd
from datetime import datetime, timedelta

# technical_indicators.py lives in the same directory (apollo_production/)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from technical_indicators import SupertrendIndicator

from configs_live import (
    ST_15MIN_PERIOD, ST_15MIN_MULTIPLIER,
    ST_75MIN_PERIOD, ST_75MIN_MULTIPLIER,
    TF_LOW, TF_HIGH,
    ST_HISTORY_CANDLES,
    NIFTY_INDEX_TOKEN,
    ST_CACHE_FILE,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_MARKET_OPEN  = "09:15"
_CANDLE_INTERVAL = "FIFTEEN_MINUTE"
_EXCHANGE     = "NSE"

# 75-min candle close times (minutes since midnight) — fixed for a 09:15 open
# Candle 1: 09:15–10:30  closes at 10:30 (630 min)
# Candle 2: 10:30–11:45  closes at 11:45 (705 min)
# Candle 3: 11:45–13:00  closes at 13:00 (780 min)
# Candle 4: 13:00–14:15  closes at 14:15 (855 min)
# Candle 5: 14:15–15:30  closes at 15:30 (930 min)
_75MIN_CLOSE_TIMES = {"10:30", "11:45", "13:00", "14:15", "15:30"}


class SupertrendManager:
    """
    Manages 15-min and 75-min Supertrend computation for Apollo live execution.
    """

    def __init__(self):
        self._df_15  = None   # 15-min candle cache with ST values
        self._df_75  = None   # 75-min candle cache with ST values
        self._seeded = False

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def seed(self, smart_connect_obj):
        """
        Seed the Supertrend history from Angel One historical candle API.
        Call once at session start after login.

        Fetches the last ST_HISTORY_CANDLES 15-min candles, computes
        15-min and 75-min Supertrend on the full series, and persists
        the cache to disk.

        Parameters
        ----------
        smart_connect_obj : SmartConnect
            Authenticated SmartConnect instance.
        """
        raw_candles = self._fetch_candles(smart_connect_obj)
        self._df_15 = self._compute_15min_st(raw_candles)
        self._df_75 = self._compute_75min_st(self._df_15)
        self._save_cache()
        self._seeded = True

    def update(self, candle):
        """
        Incorporate a newly closed 15-min candle and recompute Supertrend.

        Parameters
        ----------
        candle : dict
            Keys: 'time_stamp' (pd.Timestamp), 'open', 'high', 'low',
                  'close', 'volume' (all numeric).

        Returns
        -------
        trend_15 : bool
            True = bullish, False = bearish on 15-min timeframe.
        flip_15 : bool
            True if trend_15 changed from the previous candle.
        trend_75 : bool or None
            True = bullish, False = bearish on 75-min timeframe.
            None if 75-min ST is not yet valid (warmup period).
        flip_75 : bool
            True if trend_75 changed from the previous candle.
        """
        if not self._seeded:
            raise RuntimeError("SupertrendManager.seed() must be called before update().")

        # Append new 15-min candle
        new_row = pd.DataFrame([{
            'time_stamp': candle['time_stamp'],
            'open':       float(candle['open']),
            'high':       float(candle['high']),
            'low':        float(candle['low']),
            'close':      float(candle['close']),
            'volume':     float(candle.get('volume', 0)),
        }])

        # Drop the ST columns before concat — they'll be recomputed
        base_cols = ['time_stamp', 'open', 'high', 'low', 'close', 'volume']
        df_base   = self._df_15[base_cols].copy()
        df_base   = pd.concat([df_base, new_row], ignore_index=True)

        # Recompute full 15-min ST series
        self._df_15 = self._compute_15min_st(df_base)

        # Extract 15-min trend for the new (last) candle
        last_15    = self._df_15.iloc[-1]
        prev_15    = self._df_15.iloc[-2]
        trend_15   = bool(last_15['trend'])
        flip_15    = bool(last_15['trend_flip'])

        # Check if this candle closes a 75-min period
        candle_time_str = pd.Timestamp(candle['time_stamp']).strftime('%H:%M')
        trend_75  = None
        flip_75   = False

        if candle_time_str in _75MIN_CLOSE_TIMES:
            # Recompute 75-min ST with the new 75-min candle included
            self._df_75 = self._compute_75min_st(self._df_15)
            if not self._df_75.empty:
                last_75  = self._df_75.iloc[-1]
                trend_75 = bool(last_75['trend']) if pd.notna(last_75['trend']) else None
                flip_75  = bool(last_75['trend_flip'])
        else:
            # No new 75-min candle — carry forward last known 75-min trend
            if self._df_75 is not None and not self._df_75.empty:
                last_75  = self._df_75.iloc[-1]
                trend_75 = bool(last_75['trend']) if pd.notna(last_75['trend']) else None
                flip_75  = False   # no flip if no new 75-min candle

        self._save_cache()
        return trend_15, flip_15, trend_75, flip_75

    def get_cache(self):
        """Return the current 15-min DataFrame with Supertrend values."""
        return self._df_15.copy() if self._df_15 is not None else pd.DataFrame()

    def get_current_trend_75(self):
        """
        Return the current 75-min trend without triggering a candle update.
        Useful for checking regime at session start after seeding.

        Returns True (bullish), False (bearish), or None (warmup/not ready).
        """
        if self._df_75 is None or self._df_75.empty:
            return None
        last = self._df_75.iloc[-1]
        return bool(last['trend']) if pd.notna(last['trend']) else None

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _fetch_candles(self, smart_connect_obj):
        """
        Fetch the last ST_HISTORY_CANDLES 15-min candles from Angel One.
        Uses a 12-calendar-day lookback window to ensure enough trading days
        are covered regardless of weekends and holidays.

        Returns a cleaned DataFrame with columns:
            time_stamp, open, high, low, close, volume
        """
        now       = datetime.now()
        from_date = now - timedelta(days=12)

        from_str  = from_date.strftime('%Y-%m-%d %H:%M')
        to_str    = now.strftime('%Y-%m-%d %H:%M')

        params = {
            "exchange":    _EXCHANGE,
            "symboltoken": NIFTY_INDEX_TOKEN,
            "interval":    _CANDLE_INTERVAL,
            "fromdate":    from_str,
            "todate":      to_str,
        }

        response = smart_connect_obj.getCandleData(params)
        raw      = response['data']

        df = pd.DataFrame(
            raw,
            columns=['time_stamp', 'open', 'high', 'low', 'close', 'volume']
        )

        # Clean timestamp — API returns ISO format with T separator
        df['time_stamp'] = df['time_stamp'].str.slice(0, 19).str.replace('T', ' ')
        df['time_stamp'] = pd.to_datetime(df['time_stamp'])

        # Filter to market hours only
        market_open  = pd.Timestamp(_MARKET_OPEN).time()
        df = df[df['time_stamp'].dt.time >= market_open].copy()

        # Trim to last ST_HISTORY_CANDLES candles
        if len(df) > ST_HISTORY_CANDLES:
            df = df.iloc[-ST_HISTORY_CANDLES:]

        df = df.sort_values('time_stamp').reset_index(drop=True)
        return df

    def _compute_15min_st(self, df):
        """
        Compute 15-min Supertrend on a candle DataFrame.
        Adds columns: Supertrend, trend (bool), trend_flip (bool).
        """
        df_st = df.rename(columns={
            'open':  'Open',
            'high':  'High',
            'low':   'Low',
            'close': 'Close',
        })

        indicator = SupertrendIndicator(
            period=ST_15MIN_PERIOD,
            multiplier=ST_15MIN_MULTIPLIER
        )
        df_st = indicator.calculate(df_st)

        df_st = df_st.rename(columns={
            'Open':  'open',
            'High':  'high',
            'Low':   'low',
            'Close': 'close',
        })

        df_st['trend']      = df_st['close'] > df_st['Supertrend']
        df_st['trend_flip'] = df_st['trend'] != df_st['trend'].shift(1)

        # Mark warmup period as NA
        warmup = df_st['Supertrend'].isna()
        df_st.loc[warmup, 'trend']      = pd.NA
        df_st.loc[warmup, 'trend_flip'] = False

        return df_st.reset_index(drop=True)

    def _compute_75min_st(self, df_15):
        """
        Derive 75-min candles from the 15-min DataFrame and compute Supertrend.
        Groups every 5 × 15-min candles anchored at 09:15 each day.
        Exactly mirrors the precompute.py resample logic.
        """
        candles_75 = []

        for date, day_df in df_15.groupby(df_15['time_stamp'].dt.date):
            market_open = pd.Timestamp(f"{date} {_MARKET_OPEN}")
            anchor      = market_open

            while anchor.strftime('%H:%M') in _75MIN_CLOSE_TIMES or anchor == market_open:
                window_end = anchor + timedelta(minutes=TF_HIGH) - timedelta(minutes=TF_LOW)

                window = day_df[
                    (day_df['time_stamp'] >= anchor) &
                    (day_df['time_stamp'] <= window_end)
                ]

                if not window.empty:
                    candles_75.append({
                        'time_stamp': anchor,
                        'open':       window['open'].iloc[0],
                        'high':       window['high'].max(),
                        'low':        window['low'].min(),
                        'close':      window['close'].iloc[-1],
                        'volume':     window['volume'].sum(),
                    })

                anchor += timedelta(minutes=TF_HIGH)
                if anchor > pd.Timestamp(f"{date} 15:30"):
                    break

        if not candles_75:
            return pd.DataFrame()

        df_75 = pd.DataFrame(candles_75).reset_index(drop=True)
        df_75 = df_75.dropna(subset=['open', 'high', 'low', 'close'])

        df_st = df_75.rename(columns={
            'open':  'Open',
            'high':  'High',
            'low':   'Low',
            'close': 'Close',
        })

        indicator = SupertrendIndicator(
            period=ST_75MIN_PERIOD,
            multiplier=ST_75MIN_MULTIPLIER
        )
        df_st = indicator.calculate(df_st)

        df_st = df_st.rename(columns={
            'Open':  'open',
            'High':  'high',
            'Low':   'low',
            'Close': 'close',
        })

        df_st['trend']      = df_st['close'] > df_st['Supertrend']
        df_st['trend_flip'] = df_st['trend'] != df_st['trend'].shift(1)

        warmup = df_st['Supertrend'].isna()
        df_st.loc[warmup, 'trend']      = pd.NA
        df_st.loc[warmup, 'trend_flip'] = False

        return df_st.reset_index(drop=True)

    def _save_cache(self):
        """Persist 15-min cache to disk for restart recovery."""
        if self._df_15 is not None:
            os.makedirs(os.path.dirname(ST_CACHE_FILE), exist_ok=True)
            self._df_15.to_csv(ST_CACHE_FILE, index=False)