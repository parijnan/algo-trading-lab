"""
supertrend.py — Apollo Production Supertrend Manager
Handles seeding and incremental updates for 15-min and 75-min Supertrend.

Public interface:
    st = SupertrendManager()
    st.seed(smart_connect_obj)              # call once at session start
    trend_15, flip_15, trend_75, flip_75 = st.update(candle)
    st.get_cache()                          # returns 15-min DataFrame with ST values
    st.get_current_trend_75()              # current 75-min trend without update
    st.get_last_completed_flip()           # most recent today-flip for restart recovery

Seeding strategy:
    Always fetches 600 candles (60-day window) from the API at startup.
    This takes ~2 seconds and guarantees correct Wilder's smoothing warmup
    regardless of how long the strategy has been dormant.

    supertrend_cache.csv is written after every update() for intra-session
    restart recovery — restoring self._df_15 avoids re-seeding on restart.
    The ST cache never contains incomplete candles because update() only
    receives fully closed candles from _fetch_latest_candle().

Design:
    - 75-min candles derived by grouping every 5 x 15-min candles anchored at 09:15
    - Full recompute on every update() — Wilder's smoothing is path-dependent
    - supertrend_cache.csv: ST-computed cache for intra-session restart recovery
"""

import os
import sys
import pandas as pd
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from technical_indicators import SupertrendIndicator
from logger_setup import get_logger

from configs_live import (
    ST_15MIN_PERIOD, ST_15MIN_MULTIPLIER,
    ST_75MIN_PERIOD, ST_75MIN_MULTIPLIER,
    TF_LOW, TF_HIGH,
    ST_HISTORY_CANDLES,
    NIFTY_INDEX_TOKEN,
    ST_CACHE_FILE,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_MARKET_OPEN     = "09:15"
_CANDLE_INTERVAL = "FIFTEEN_MINUTE"
_EXCHANGE        = "NSE"

_75MIN_CLOSE_TIMES = {"10:30", "11:45", "13:00", "14:15", "15:30"}


class SupertrendManager:
    """
    Manages 15-min and 75-min Supertrend computation for Apollo live execution.
    """

    def __init__(self):
        self._df_15  = None
        self._df_75  = None
        self._seeded = False

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def seed(self, smart_connect_obj):
        """
        Seed the Supertrend history from the Angel One historical candle API.
        Fetches 600 15-min candles (60-day window), computes ST on both
        timeframes, saves ST cache for intra-session restart recovery.
        Call once at session start after login.
        """
        logger.info("Seeding Supertrend history...")

        raw_candles = self._fetch_candles(smart_connect_obj)
        logger.info(f"Seeding with {len(raw_candles)} completed 15-min candles.")

        self._df_15 = self._compute_15min_st(raw_candles)
        self._df_75 = self._compute_75min_st(self._df_15)
        self._save_st_cache()
        self._seeded = True

        logger.debug("Last 3 rows of 15-min ST cache after seed:")
        for _, row in self._df_15.tail(3).iterrows():
            logger.debug(
                f"  {row['time_stamp']}  close={row['close']:.2f}  "
                f"ST={row['Supertrend']:.2f}  trend={row['trend']}  "
                f"flip={row['trend_flip']}")

        if self._df_75 is not None and not self._df_75.empty:
            logger.debug("Last 3 rows of 75-min ST cache after seed:")
            for _, row in self._df_75.tail(3).iterrows():
                logger.debug(
                    f"  {row['time_stamp']}  close={row['close']:.2f}  "
                    f"ST={row['Supertrend']:.2f}  trend={row['trend']}  "
                    f"flip={row['trend_flip']}")

        logger.info(
            f"Seed complete. "
            f"15-min: {len(self._df_15)} candles, "
            f"current trend: {self._df_15.iloc[-1]['trend']} "
            f"(ST={self._df_15.iloc[-1]['Supertrend']:.2f}). "
            f"75-min: {len(self._df_75) if self._df_75 is not None else 0} candles, "
            f"current trend: {self.get_current_trend_75()}.")

    def update(self, candle):
        """
        Incorporate a newly closed 15-min candle and recompute Supertrend.
        Returns (trend_15, flip_15, trend_75, flip_75).
        """
        if not self._seeded:
            raise RuntimeError("SupertrendManager.seed() must be called before update().")

        ts    = pd.Timestamp(candle['time_stamp'])
        close = float(candle['close'])
        logger.debug(
            f"ST update — candle {ts}  "
            f"O={candle['open']:.2f} H={candle['high']:.2f} "
            f"L={candle['low']:.2f} C={close:.2f}")

        new_row = pd.DataFrame([{
            'time_stamp': ts,
            'open':       float(candle['open']),
            'high':       float(candle['high']),
            'low':        float(candle['low']),
            'close':      close,
            'volume':     float(candle.get('volume', 0)),
        }])

        base_cols = ['time_stamp', 'open', 'high', 'low', 'close', 'volume']
        df_base   = self._df_15[base_cols].copy()
        df_base   = pd.concat([df_base, new_row], ignore_index=True)

        self._df_15 = self._compute_15min_st(df_base)

        last_15  = self._df_15.iloc[-1]
        trend_15 = bool(last_15['trend'])
        flip_15  = bool(last_15['trend_flip'])

        logger.debug(
            f"15-min result — ST={last_15['Supertrend']:.2f}  "
            f"trend={trend_15}  flip={flip_15}")

        candle_time_str = ts.strftime('%H:%M')
        trend_75 = None
        flip_75  = False

        if candle_time_str in _75MIN_CLOSE_TIMES:
            logger.debug(f"75-min candle closes at {candle_time_str} — recomputing 75-min ST.")
            self._df_75 = self._compute_75min_st(self._df_15)
            if not self._df_75.empty:
                last_75  = self._df_75.iloc[-1]
                trend_75 = bool(last_75['trend']) if pd.notna(last_75['trend']) else None
                flip_75  = bool(last_75['trend_flip'])
                logger.debug(
                    f"75-min result — ST={last_75['Supertrend']:.2f}  "
                    f"trend={trend_75}  flip={flip_75}")
        else:
            if self._df_75 is not None and not self._df_75.empty:
                last_75  = self._df_75.iloc[-1]
                trend_75 = bool(last_75['trend']) if pd.notna(last_75['trend']) else None
                flip_75  = False
            logger.debug(f"75-min trend carried forward: {trend_75}")

        logger.info(
            f"Candle {ts}  close={close:.2f}  "
            f"trend_15={trend_15} flip_15={flip_15}  "
            f"trend_75={trend_75} flip_75={flip_75}")

        self._save_st_cache()
        return trend_15, flip_15, trend_75, flip_75

    def get_cache(self):
        """Return the current 15-min DataFrame with Supertrend values."""
        return self._df_15.copy() if self._df_15 is not None else pd.DataFrame()

    def get_current_trend_75(self):
        """Return current 75-min trend. True=bullish, False=bearish, None=warmup."""
        if self._df_75 is None or self._df_75.empty:
            return None
        last = self._df_75.iloc[-1]
        return bool(last['trend']) if pd.notna(last['trend']) else None

    def get_75min_cache(self):
        """Return the current 75-min DataFrame with Supertrend values."""
        return self._df_75.copy() if self._df_75 is not None else pd.DataFrame()

    def get_last_completed_flip(self):
        """
        Return the most recent candle row where trend_flip is True and
        time_stamp is today. Used for missed flip recovery at startup.
        """
        if self._df_15 is None or self._df_15.empty:
            return None
        today = datetime.now().date()
        today_flips = self._df_15[
            (self._df_15['time_stamp'].dt.date == today) &
            (self._df_15['trend_flip'] == True)
        ]
        if today_flips.empty:
            return None
        flip_row = today_flips.iloc[-1]
        logger.debug(
            f"Last completed flip today: {flip_row['time_stamp']}  "
            f"trend={flip_row['trend']}")
        return flip_row

    # -----------------------------------------------------------------------
    # Candle fetching
    # -----------------------------------------------------------------------

    def _fetch_candles(self, smart_connect_obj) -> pd.DataFrame:
        """
        Fetch the last ST_HISTORY_CANDLES 15-min candles from Angel One.
        Uses a 60-day window to ensure correct Wilder's smoothing warmup.
        Drops the incomplete current candle (still forming).
        """
        now       = datetime.now()
        from_date = now - timedelta(days=60)

        params = {
            "exchange":    _EXCHANGE,
            "symboltoken": NIFTY_INDEX_TOKEN,
            "interval":    _CANDLE_INTERVAL,
            "fromdate":    from_date.strftime('%Y-%m-%d %H:%M'),
            "todate":      now.strftime('%Y-%m-%d %H:%M'),
        }

        logger.debug(
            f"Fetching candle history: {params['fromdate']} to {params['todate']}")

        response = smart_connect_obj.getCandleData(params)
        raw      = response['data']

        df = pd.DataFrame(
            raw,
            columns=['time_stamp', 'open', 'high', 'low', 'close', 'volume']
        )

        df['time_stamp'] = df['time_stamp'].str.slice(0, 19).str.replace('T', ' ')
        df['time_stamp'] = pd.to_datetime(df['time_stamp'])

        market_open = pd.Timestamp(_MARKET_OPEN).time()
        df = df[df['time_stamp'].dt.time >= market_open].copy()

        # Drop incomplete current candle
        current_minute   = now.minute
        minutes_into_bar = current_minute % 15
        current_bar_open = now.replace(
            minute=current_minute - minutes_into_bar,
            second=0, microsecond=0)
        before = len(df)
        df = df[df['time_stamp'] < current_bar_open].copy()
        after  = len(df)

        if before != after:
            logger.debug(
                f"Dropped {before - after} incomplete candle(s) "
                f"(current bar open: {current_bar_open}).")

        if len(df) > ST_HISTORY_CANDLES:
            df = df.iloc[-ST_HISTORY_CANDLES:]

        df = df.sort_values('time_stamp').reset_index(drop=True)

        logger.debug(
            f"Candle range after filtering: "
            f"{df['time_stamp'].iloc[0]} to {df['time_stamp'].iloc[-1]}")

        return df

    # -----------------------------------------------------------------------
    # Supertrend computation
    # -----------------------------------------------------------------------

    def _compute_15min_st(self, df):
        """Compute 15-min Supertrend. Adds Supertrend, trend, trend_flip columns."""
        df_st = df.rename(columns={
            'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'})

        indicator = SupertrendIndicator(
            period=ST_15MIN_PERIOD, multiplier=ST_15MIN_MULTIPLIER)
        df_st = indicator.calculate(df_st)

        df_st = df_st.rename(columns={
            'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close'})

        df_st['trend']      = (df_st['close'] > df_st['Supertrend']).astype(object)
        df_st['trend_flip'] = (df_st['trend'] != df_st['trend'].shift(1))

        warmup = df_st['Supertrend'].isna()
        df_st.loc[warmup, 'trend']      = pd.NA
        df_st.loc[warmup, 'trend_flip'] = False

        return df_st.reset_index(drop=True)

    def _compute_75min_st(self, df_15):
        """Derive 75-min candles from 15-min DataFrame and compute Supertrend."""
        candles_75 = []

        for date, day_df in df_15.groupby(df_15['time_stamp'].dt.date):
            market_open = pd.Timestamp(f"{date} {_MARKET_OPEN}")
            anchor      = market_open

            while anchor.strftime('%H:%M') in _75MIN_CLOSE_TIMES or anchor == market_open:
                window_end = anchor + timedelta(minutes=TF_HIGH) - timedelta(minutes=1)
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
            'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'})

        indicator = SupertrendIndicator(
            period=ST_75MIN_PERIOD, multiplier=ST_75MIN_MULTIPLIER)
        df_st = indicator.calculate(df_st)

        df_st = df_st.rename(columns={
            'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close'})

        df_st['trend']      = (df_st['close'] > df_st['Supertrend']).astype(object)
        df_st['trend_flip'] = (df_st['trend'] != df_st['trend'].shift(1))

        warmup = df_st['Supertrend'].isna()
        df_st.loc[warmup, 'trend']      = pd.NA
        df_st.loc[warmup, 'trend_flip'] = False

        logger.debug(
            f"75-min ST computed: {len(df_st)} candles. "
            f"Last: {df_st.iloc[-1]['time_stamp']}  "
            f"close={df_st.iloc[-1]['close']:.2f}  "
            f"ST={df_st.iloc[-1]['Supertrend']:.2f}  "
            f"trend={df_st.iloc[-1]['trend']}")

        return df_st.reset_index(drop=True)

    # -----------------------------------------------------------------------
    # ST cache persistence (intra-session restart recovery)
    # -----------------------------------------------------------------------

    def _save_st_cache(self):
        """
        Persist 15-min ST cache to disk for intra-session restart recovery.
        Trims to ST_HISTORY_CANDLES before saving.
        Only contains fully closed candles — update() never receives forming candles.
        """
        if self._df_15 is not None:
            os.makedirs(os.path.dirname(ST_CACHE_FILE), exist_ok=True)
            df_save = self._df_15
            if len(df_save) > ST_HISTORY_CANDLES:
                df_save = df_save.iloc[-ST_HISTORY_CANDLES:]
            df_save.to_csv(ST_CACHE_FILE, index=False)