"""
supertrend.py — Apollo Production Supertrend Manager
Handles seeding and incremental updates for 15-min and 75-min Supertrend.

Public interface:
    st = SupertrendManager()
    st.seed(smart_connect_obj, holidays)    # call once at session start
    trend_15, flip_15, trend_75, flip_75 = st.update(candle)
    st.update_candle_cache(smart_connect_obj) # call at logout to persist today's candles
    st.get_cache()                          # returns 15-min DataFrame with ST values
    st.get_current_trend_75()              # current 75-min trend without update
    st.get_last_completed_flip()           # most recent today-flip for restart recovery

Seeding strategy:
    1. If nifty_15min_cache.csv exists and is current (last candle reconciles
       with today's trading calendar accounting for holidays/weekends):
       load from cache — fast, accurate, no warmup issue
    2. If cache is stale/absent: fetch 600 candles from API (60-day window),
       rebuild cache — handles first run and long absences
    At logout: append today's candles to cache, trim to ST_HISTORY_CANDLES,
    save. No ST computation — raw OHLCV only.

Design:
    - Incomplete current candle always dropped — only fully closed candles used
    - 75-min candles derived by grouping every 5 x 15-min candles anchored at 09:15
    - Full recompute on every update() — Wilder's smoothing is path-dependent
    - supertrend_cache.csv: ST-computed cache for intra-session restart recovery
    - nifty_15min_cache.csv: raw OHLCV cache for next-day seed (gitignored)
"""

import os
import sys
import pandas as pd
from datetime import datetime, timedelta, date
from time import sleep

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
    NIFTY_15MIN_CACHE_FILE,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_MARKET_OPEN     = "09:15"
_CANDLE_INTERVAL = "FIFTEEN_MINUTE"
_EXCHANGE        = "NSE"

_75MIN_CLOSE_TIMES = {"10:30", "11:45", "13:00", "14:15", "15:30"}

# Number of recent candles to fetch for reconciliation check at startup
_RECONCILE_CANDLES = 5


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

    def seed(self, smart_connect_obj, holidays: set):
        """
        Seed the Supertrend history at session start.

        Strategy:
          1. Try to load and reconcile from nifty_15min_cache.csv
          2. If reconciliation fails (stale/absent cache), fetch full 600
             candles from API and rebuild cache
          3. Compute 15-min and 75-min ST on the final candle set
          4. Save ST cache for intra-session restart recovery

        Parameters
        ----------
        smart_connect_obj : SmartConnect
            Authenticated SmartConnect instance.
        holidays : set
            Set of date objects for market holidays. Used for reconciliation.
        """
        logger.info("Seeding Supertrend history...")

        raw_candles = self._load_or_fetch_candles(smart_connect_obj, holidays)
        logger.info(f"Seeding with {len(raw_candles)} completed 15-min candles.")

        self._df_15 = self._compute_15min_st(raw_candles)
        self._df_75 = self._compute_75min_st(self._df_15)
        self._save_st_cache()
        self._seeded = True

        # Log last 3 rows of 15-min cache
        logger.debug("Last 3 rows of 15-min ST cache after seed:")
        for _, row in self._df_15.tail(3).iterrows():
            logger.debug(
                f"  {row['time_stamp']}  close={row['close']:.2f}  "
                f"ST={row['Supertrend']:.2f}  trend={row['trend']}  "
                f"flip={row['trend_flip']}")

        # Log last 3 rows of 75-min cache
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

    def update_candle_cache(self, smart_connect_obj):
        """
        Called at logout. Appends today's completed 15-min candles to
        nifty_15min_cache.csv and trims to ST_HISTORY_CANDLES rows.

        Raw OHLCV only — no ST computation. This file is used for fast
        seeding on next startup, avoiding the 60-day API fetch and
        Wilder's smoothing warmup problem.
        """
        logger.info("Updating 15-min candle cache for next session...")
        if self._df_15 is None or not self._seeded:
            logger.info("Candle cache not updated — no seeded data available.")
            return
        try:
            # Today's completed candles are already in self._df_15
            # Load existing raw cache if it exists
            base_cols = ['time_stamp', 'open', 'high', 'low', 'close', 'volume']

            if os.path.exists(NIFTY_15MIN_CACHE_FILE):
                existing = pd.read_csv(
                    NIFTY_15MIN_CACHE_FILE, parse_dates=['time_stamp'])
            else:
                existing = pd.DataFrame(columns=base_cols)

            # Get today's candles from the ST cache (already computed)
            today = datetime.now().date()
            today_candles = self._df_15[
                self._df_15['time_stamp'].dt.date == today
            ][base_cols].copy()

            # Drop incomplete current candle — script may have been stopped
            # mid-candle (Ctrl+C or crash), leaving a partially formed candle
            # in self._df_15 from the last _fetch_latest_candle() call
            today_candles = self._drop_incomplete(today_candles)

            # Drop any today rows already in existing (avoid duplicates)
            existing = existing[
                existing['time_stamp'].dt.date != today
            ].copy()

            # Append and trim
            combined = pd.concat([existing, today_candles], ignore_index=True)
            combined = combined.sort_values('time_stamp').reset_index(drop=True)
            if len(combined) > ST_HISTORY_CANDLES:
                combined = combined.iloc[-ST_HISTORY_CANDLES:]

            os.makedirs(os.path.dirname(NIFTY_15MIN_CACHE_FILE), exist_ok=True)
            combined.to_csv(NIFTY_15MIN_CACHE_FILE, index=False)
            logger.info(
                f"Candle cache updated: {len(combined)} rows saved to "
                f"{NIFTY_15MIN_CACHE_FILE}.")

        except Exception as e:
            logger.error(f"Failed to update candle cache: {e}")

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
    # Candle loading — cache-first strategy
    # -----------------------------------------------------------------------

    def _load_or_fetch_candles(self, smart_connect_obj, holidays: set) -> pd.DataFrame:
        """
        Load candles for seeding using cache-first strategy.

        1. Try to reconcile nifty_15min_cache.csv with recent API data
        2. If reconciliation passes: merge cache with new candles since last save
        3. If reconciliation fails: fetch full 600-candle history from API
        """
        if os.path.exists(NIFTY_15MIN_CACHE_FILE):
            cache = pd.read_csv(NIFTY_15MIN_CACHE_FILE, parse_dates=['time_stamp'])
            cache = cache.sort_values('time_stamp').reset_index(drop=True)
            logger.info(
                f"Candle cache found: {len(cache)} rows, "
                f"last: {cache.iloc[-1]['time_stamp']}")

            # Fetch recent candles for reconciliation
            recent = self._fetch_recent_candles(smart_connect_obj, _RECONCILE_CANDLES)

            if recent is not None and not recent.empty:
                if self._reconcile(cache, recent, holidays):
                    # Merge: keep cache, append any new candles not already in cache
                    last_cached_ts = cache.iloc[-1]['time_stamp']
                    new_candles    = recent[recent['time_stamp'] > last_cached_ts]

                    if not new_candles.empty:
                        base_cols = ['time_stamp', 'open', 'high', 'low', 'close', 'volume']
                        combined  = pd.concat(
                            [cache[base_cols], new_candles[base_cols]],
                            ignore_index=True)
                        combined = combined.sort_values('time_stamp').reset_index(drop=True)
                        logger.info(
                            f"Reconciliation passed. Appended {len(new_candles)} new "
                            f"candle(s). Total: {len(combined)}.")
                    else:
                        combined = cache
                        logger.info("Reconciliation passed. Cache is current.")

                    # Drop incomplete current candle and trim
                    combined = self._drop_incomplete(combined)
                    if len(combined) > ST_HISTORY_CANDLES:
                        combined = combined.iloc[-ST_HISTORY_CANDLES:]
                    return combined.reset_index(drop=True)
                else:
                    logger.warning(
                        "Reconciliation failed — cache is stale. "
                        "Falling back to full API fetch.")
            else:
                logger.warning("Could not fetch recent candles for reconciliation. "
                               "Falling back to full API fetch.")
        else:
            logger.info("No candle cache found. Fetching full history from API.")

        # Sleep 1s between the reconciliation fetch (if it happened) and the
        # full fetch to respect the 3/sec getCandleData rate limit
        sleep(1)
        return self._fetch_full_candles(smart_connect_obj)

    def _reconcile(self, cache: pd.DataFrame, recent: pd.DataFrame,
                   holidays: set) -> bool:
        """
        Check whether the most recent completed candle in `recent` can be
        connected to the tail of `cache` accounting for weekends and holidays.

        Reconciliation passes if:
          - The most recent candle in `recent` is already in `cache` (cache
            is fully current), OR
          - The gap between cache tail and recent candles contains only
            weekends and market holidays (expected absence)

        Reconciliation fails if there is an unexplained gap — implying the
        cache is too stale to be trusted.
        """
        if cache.empty or recent.empty:
            return False

        last_cache_ts  = pd.Timestamp(cache.iloc[-1]['time_stamp'])
        first_recent_ts = pd.Timestamp(recent.iloc[0]['time_stamp'])

        # If the first recent candle is already in or before the cache tail
        # the cache is current or overlapping — passes
        if first_recent_ts <= last_cache_ts:
            logger.debug(
                f"Reconciliation: recent candles overlap cache tail. "
                f"Cache tail: {last_cache_ts}  Recent start: {first_recent_ts}")
            return True

        # Otherwise check if the gap between cache tail and first recent candle
        # is fully explained by weekends and holidays
        gap_start = last_cache_ts.date() + timedelta(days=1)
        gap_end   = first_recent_ts.date()

        current = gap_start
        while current < gap_end:
            if current.weekday() < 5 and current not in holidays:
                # Found a trading day in the gap — unexplained absence
                logger.debug(
                    f"Reconciliation failed: unexplained trading day in gap: {current}. "
                    f"Cache tail: {last_cache_ts}  Recent start: {first_recent_ts}")
                return False
            current += timedelta(days=1)

        logger.debug(
            f"Reconciliation passed: gap from {last_cache_ts.date()} to "
            f"{first_recent_ts.date()} fully explained by weekends/holidays.")
        return True

    def _fetch_recent_candles(self, smart_connect_obj, n_candles: int):
        """
        Fetch the last n_candles 15-min candles from the API for reconciliation.
        Returns cleaned DataFrame or None on failure.
        """
        try:
            now       = datetime.now()
            from_date = now - timedelta(days=3)   # 3 days covers weekends + 1 trading day
            params = {
                "exchange":    _EXCHANGE,
                "symboltoken": NIFTY_INDEX_TOKEN,
                "interval":    _CANDLE_INTERVAL,
                "fromdate":    from_date.strftime('%Y-%m-%d %H:%M'),
                "todate":      now.strftime('%Y-%m-%d %H:%M'),
            }
            response = smart_connect_obj.getCandleData(params)
            df = self._parse_candle_response(response)
            df = self._drop_incomplete(df)
            if len(df) > n_candles:
                df = df.iloc[-n_candles:]
            logger.debug(f"Fetched {len(df)} recent candles for reconciliation.")
            return df
        except Exception as e:
            logger.error(f"Failed to fetch recent candles: {e}")
            return None

    def _fetch_full_candles(self, smart_connect_obj) -> pd.DataFrame:
        """
        Fetch full 60-day history (up to ST_HISTORY_CANDLES candles) from API.
        Used on first run or after reconciliation failure.
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

        logger.debug(f"Fetching full candle history: {params['fromdate']} to {params['todate']}")

        response = smart_connect_obj.getCandleData(params)
        df = self._parse_candle_response(response)
        df = self._drop_incomplete(df)

        if len(df) > ST_HISTORY_CANDLES:
            df = df.iloc[-ST_HISTORY_CANDLES:]

        logger.info(
            f"Full fetch complete: {len(df)} candles "
            f"({df['time_stamp'].iloc[0]} to {df['time_stamp'].iloc[-1]})")

        # Save as new cache
        os.makedirs(os.path.dirname(NIFTY_15MIN_CACHE_FILE), exist_ok=True)
        df.to_csv(NIFTY_15MIN_CACHE_FILE, index=False)
        logger.info(f"New candle cache saved: {NIFTY_15MIN_CACHE_FILE}")

        return df.reset_index(drop=True)

    def _parse_candle_response(self, response) -> pd.DataFrame:
        """Parse raw getCandleData response into a clean OHLCV DataFrame."""
        raw = response['data']
        df  = pd.DataFrame(
            raw,
            columns=['time_stamp', 'open', 'high', 'low', 'close', 'volume']
        )
        df['time_stamp'] = df['time_stamp'].str.slice(0, 19).str.replace('T', ' ')
        df['time_stamp'] = pd.to_datetime(df['time_stamp'])

        market_open = pd.Timestamp(_MARKET_OPEN).time()
        df = df[df['time_stamp'].dt.time >= market_open].copy()
        return df.sort_values('time_stamp').reset_index(drop=True)

    def _drop_incomplete(self, df: pd.DataFrame) -> pd.DataFrame:
        """Drop the incomplete current candle (still forming) from a DataFrame."""
        now              = datetime.now()
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
    # Cache persistence
    # -----------------------------------------------------------------------

    def _save_st_cache(self):
        """Persist 15-min ST cache to disk for intra-session restart recovery."""
        if self._df_15 is not None:
            os.makedirs(os.path.dirname(ST_CACHE_FILE), exist_ok=True)
            # Trim to ST_HISTORY_CANDLES — ST cache can grow unbounded
            # as update() appends candles throughout the session
            df_save = self._df_15
            if len(df_save) > ST_HISTORY_CANDLES:
                df_save = df_save.iloc[-ST_HISTORY_CANDLES:]
            df_save.to_csv(ST_CACHE_FILE, index=False)