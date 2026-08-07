import os
import time
import logging
import pandas as pd
from datetime import datetime, timedelta, date, time as dtime
from collections import deque
from io import StringIO
from urllib.request import urlopen
from requests import post
from pyotp import TOTP
from SmartApi import SmartConnect

# Change directory to ensure it is operating in the correct directory when run as a cronjob
if os.uname().nodename == 'delos':
    os.chdir('/home/parijnan/scripts/algo-trading-lab/data_pipeline/')

# ---------------------------------------------------------------------------
# Logging setup
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
MARKET_OPEN     = "09:15"
MARKET_CLOSE    = "15:40"   # CAS (3 Aug 2026): derivatives trade till 15:40; index
                            # auction print can land anywhere up to ~15:35
CANDLE_DATE_FMT  = "%Y-%m-%d %H:%M"           # format expected by getCandleData
INDEX_TS_FMT     = "%Y-%m-%d %H:%M:%S"        # base format for index csv (tz appended manually)
OPTIONS_TS_FMT   = "%Y-%m-%dT%H:%M:%S"        # base format for options csv (tz appended manually)
METHOD_TS_FMT    = "%Y-%m-%dT%H:%M:%S%z"      # format returned by getCandleData
OHLCV_HEADERS   = ["time_stamp", "open", "high", "low", "close", "volume"]
INDEX_HEADERS   = ["time_stamp", "open", "high", "low", "close", "volume", "oi"]
OPTIONS_EXCHANGE = "BFO"
CHUNK_DAYS       = 2    # days per API call (375 min * 2 = 750 < 1000 records)

# Rate limit parameters (broker limits: 3/sec, 180/min, 5000/hour)
RATE_LIMIT_PER_SEC  = 2
RATE_LIMIT_PER_MIN  = 180
RATE_LIMIT_PER_HOUR = 5000

# Retry parameters for broker-side rate limit rejections
RATE_LIMIT_BACKOFF_SEC = 60   # seconds to sleep after an "exceeding access rate" error
MAX_RETRIES            = 3    # maximum retry attempts per API call

# Index instruments: (display_name, exchange, symbol_token, filename)
INDEX_INSTRUMENTS = [
    ("Sensex",    "BSE", "99919000", "sensex.csv"),
    ("Nifty",     "NSE", "99926000", "nifty.csv"),
    ("India VIX", "NSE", "99926017", "india_vix.csv"),
]

# Daily index instruments for range detection research
# Fetched with ONE_DAY interval — official close, date-only timestamps
DAILY_INDEX_INSTRUMENTS = [
    ("Nifty",     "NSE", "99926000", "nifty_daily.csv"),
    ("Sensex",    "BSE", "99919000", "sensex_daily.csv"),
    ("India VIX", "NSE", "99926017", "india_vix_daily.csv"),
]
DAILY_HISTORY_YEARS = 3

# ---------------------------------------------------------------------------
# Paths  (script lives in the parent directory of "data")
# ---------------------------------------------------------------------------
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
DATA_DIR     = os.path.join(BASE_DIR, "data")
CONFIG_DIR   = os.path.join(BASE_DIR, "config")
INDICES_DIR  = os.path.join(DATA_DIR, "indices")
OPTIONS_DIR  = os.path.join(DATA_DIR, "sensex")


# ===========================================================================
# Slack messaging
# ===========================================================================

SLACK_DATA_CHANNEL  = "#data-alerts"
SLACK_ERROR_CHANNEL = "#error-alerts"

def slack_bot_sendtext(msg, channel):
    url     = "https://slack.com/api/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {user_credentials_df.iloc[0].loc['slack_token']}",
        "Content-Type":  "application/json"
    }
    payload  = {"channel": channel, "text": msg}
    response = post(url, headers=headers, json=payload, timeout=5)
    return response.json() if "response" in locals() else None


# ===========================================================================
# Rate limiter
# ===========================================================================

class RateLimiter:
    """
    Sliding-window rate limiter enforcing three simultaneous limits:
      - max calls per second  (3)
      - max calls per minute  (180)
      - max calls per hour    (5000)
    Call .wait() before every API request.
    """

    def __init__(self,
                 per_second: int = RATE_LIMIT_PER_SEC,
                 per_minute: int = RATE_LIMIT_PER_MIN,
                 per_hour:   int = RATE_LIMIT_PER_HOUR):
        self.per_second = per_second
        self.per_minute = per_minute
        self.per_hour   = per_hour
        # Sliding windows store monotonic timestamps of recent calls
        self._calls_sec  = deque()
        self._calls_min  = deque()
        self._calls_hour = deque()

    def _evict(self, window: deque, cutoff: float):
        while window and window[0] < cutoff:
            window.popleft()

    def wait(self):
        """Block until all three rate-limit windows allow the next call."""
        while True:
            now = time.monotonic()
            self._evict(self._calls_sec,  now - 1)
            self._evict(self._calls_min,  now - 60)
            self._evict(self._calls_hour, now - 3600)

            if (len(self._calls_sec)  < self.per_second and
                    len(self._calls_min)  < self.per_minute and
                    len(self._calls_hour) < self.per_hour):
                break

            # Sleep just long enough to free one slot in the tightest window
            sleeps = []
            if len(self._calls_sec)  >= self.per_second:
                sleeps.append(self._calls_sec[0]  + 1    - now)
            if len(self._calls_min)  >= self.per_minute:
                sleeps.append(self._calls_min[0]  + 60   - now)
            if len(self._calls_hour) >= self.per_hour:
                sleeps.append(self._calls_hour[0] + 3600 - now)

            sleep_for = max(0.0, min(sleeps))
            logger.debug(f"Rate limit reached – sleeping {sleep_for:.2f}s")
            time.sleep(sleep_for)

        # Record this call across all three windows
        now = time.monotonic()
        self._calls_sec.append(now)
        self._calls_min.append(now)
        self._calls_hour.append(now)


# Module-level singleton shared across all fetch calls
_rate_limiter = RateLimiter()


# ===========================================================================
# Helper utilities
# ===========================================================================

def date_range_chunks(start: datetime, end: datetime, chunk_days: int = CHUNK_DAYS):
    """
    Yield (chunk_start, chunk_end) datetime pairs covering [start, end]
    in steps of chunk_days.
    """
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=chunk_days - 1), end)
        yield current, chunk_end
        current += timedelta(days=chunk_days)


def format_timestamp(ts: pd.Timestamp, base_fmt: str) -> str:
    """
    Format a tz-aware Timestamp using base_fmt and append the UTC offset
    with a colon separator (e.g. +05:30 rather than +0530), since Python's
    %z directive does not include the colon.
    """
    if ts.tzinfo is None:
        ts = ts.tz_localize("Asia/Kolkata")
    offset = ts.strftime("%z")          # e.g. "+0530"
    offset_colon = offset[:-2] + ":" + offset[-2:]   # → "+05:30"
    return ts.strftime(base_fmt) + offset_colon


def fetch_candle_chunk(obj, exchange: str, token: str,
                       from_dt: datetime, to_dt: datetime) -> pd.DataFrame:
    """
    Call getCandleData for a single chunk and return a clean DataFrame.
    Timestamps outside [from_dt, to_dt] are discarded (broker sometimes
    returns random dates when no data exists for the requested range).
    Returns an empty DataFrame on failure or when no valid data is present.
    """
    from_str = f"{from_dt.strftime('%Y-%m-%d')} {MARKET_OPEN}"
    to_str   = f"{to_dt.strftime('%Y-%m-%d')} {MARKET_CLOSE}"

    for attempt in range(MAX_RETRIES + 1):
        _rate_limiter.wait()
        try:
            response = obj.getCandleData({
                "exchange":    exchange,
                "symboltoken": str(token),
                "interval":    "ONE_MINUTE",
                "fromdate":    from_str,
                "todate":      to_str,
            })
        except Exception as e:
            if "exceeding access rate" in str(e) and attempt < MAX_RETRIES:
                logger.warning(
                    f"Rate limited for token {token} [{from_str} → {to_str}] "
                    f"– sleeping {RATE_LIMIT_BACKOFF_SEC}s then retrying "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})"
                )
                time.sleep(RATE_LIMIT_BACKOFF_SEC)
                continue
            logger.warning(f"API error for token {token} [{from_str} → {to_str}]: {e}")
            return pd.DataFrame(columns=OHLCV_HEADERS)
        break

    raw = response.get("data") if isinstance(response, dict) else None
    if not raw:
        logger.debug(f"No data returned for token {token} [{from_str} → {to_str}]")
        return pd.DataFrame(columns=OHLCV_HEADERS)

    df = pd.DataFrame(raw, columns=OHLCV_HEADERS)

    # Parse timestamps returned by the broker (ISO-8601 with T separator)
    df["time_stamp"] = pd.to_datetime(df["time_stamp"], format=METHOD_TS_FMT,
                                      utc=False, errors="coerce")

    # Guard: silently drop rows whose timestamp falls outside the requested window
    window_start = pd.Timestamp(f"{from_dt.strftime('%Y-%m-%d')} {MARKET_OPEN}",
                                tz="Asia/Kolkata")
    window_end   = pd.Timestamp(f"{to_dt.strftime('%Y-%m-%d')} {MARKET_CLOSE}",
                                tz="Asia/Kolkata")
    before = len(df)
    df = df[(df["time_stamp"] >= window_start) & (df["time_stamp"] <= window_end)]
    dropped = before - len(df)
    if dropped:
        logger.debug(f"Dropped {dropped} out-of-window rows for token {token}")

    return df


def fetch_full_range_index(obj, exchange: str, token: str,
                           start: datetime, end: datetime) -> pd.DataFrame:
    """
    Fetch all 1-minute candles for [start, end] in CHUNK_DAYS-sized pieces
    and return a single deduplicated DataFrame sorted by time_stamp.
    Used only for index downloads where we accumulate in memory then save once.
    """
    frames = []

    for chunk_start, chunk_end in date_range_chunks(start, end):
        chunk_df = fetch_candle_chunk(obj, exchange, token, chunk_start, chunk_end)
        if not chunk_df.empty:
            frames.append(chunk_df)

    if not frames:
        return pd.DataFrame(columns=OHLCV_HEADERS)

    combined = pd.concat(frames, ignore_index=True)
    combined["time_stamp"] = pd.to_datetime(combined["time_stamp"], utc=False,
                                            errors="coerce")
    combined.drop_duplicates(subset=["time_stamp"], keep="first", inplace=True)
    combined.sort_values("time_stamp", inplace=True)
    combined.reset_index(drop=True, inplace=True)
    return combined


MAX_FILL_MINUTES = 10   # gaps larger than this are left alone (real halts / pre-open)

# Closing Auction Session (CAS), live on NSE/BSE from 3 Aug 2026: continuous
# trading in the underlying index stops at 15:15, followed by a ~15-minute
# call auction with no 1-minute candles, then a single terminal print (the
# auction-clearing price) lands somewhere in the 15:16-15:35 range. This is a
# daily, structural gap — not a broker glitch — so it's handled separately
# from MAX_FILL_MINUTES-bounded glitch gaps below.
CAS_AUCTION_WINDOW_START = dtime(15, 16)
CAS_AUCTION_WINDOW_END   = dtime(15, 40)
CAS_GAP_MAX_MINUTES      = 20


def fill_missing_candles(df: pd.DataFrame, display_name: str = "") -> tuple:
    """
    Broker sometimes skips 1-minute candles; the next bar's open is then wrong
    because it should equal the close of the last phantom bar.  Pattern:
      - each missing candle OHLC = close of the preceding bar (flat carry-forward)
      - following bar's open should equal that same value

    For each intra-session gap of 2–MAX_FILL_MINUTES minutes:
      1. Insert synthetic flat candles for every missing minute.
      2. Correct the open of the bar that follows the gap.
    Gaps larger than MAX_FILL_MINUTES are left untouched (real halts, pre-open, etc.)
    UNLESS the bar following the gap lands inside the CAS auction window — those
    gaps also get synthetic flat candles inserted, and the terminal (auction
    print) bar is reconstructed to match what the exchange's own chart shows,
    not left as the flat, no-memory candle the raw API returns: open = the
    carried-forward pre-auction close, close = the terminal print (unchanged —
    it's already correct), high/low = max/min of the two so the candle is
    always valid regardless of which direction the print moved
    (verified against live AngelOne pulls and the actual chart display for
    2026-08-06 and 2026-08-07 — see plans/iris-signal-pipeline-hardening.md §5).
    An earlier version of this function deliberately left the terminal bar's
    open untouched, reasoning that forcing it to the pre-auction close would
    produce an invalid candle; that reasoning didn't hold up against what the
    chart actually shows, and is the reason this function looked the way it
    did before 2026-08-07.
    """
    if len(df) < 2:
        return df, 0, 0, []

    df = df.sort_values("time_stamp").reset_index(drop=True)

    synthetic_rows = []
    open_fixes: dict = {}
    cas_terminal_fixes: dict = {}
    cas_gap_count = 0
    cas_auction_moves = []

    for i in range(1, len(df)):
        prev_ts = df.loc[i - 1, "time_stamp"]
        gap_min = (df.loc[i, "time_stamp"] - prev_ts).total_seconds() / 60
        term_time = df.loc[i, "time_stamp"].time()

        is_glitch_gap = 2.0 <= gap_min <= MAX_FILL_MINUTES
        is_cas_gap = (2.0 <= gap_min <= CAS_GAP_MAX_MINUTES and
                      CAS_AUCTION_WINDOW_START <= term_time <= CAS_AUCTION_WINDOW_END)

        if not (is_glitch_gap or is_cas_gap):
            continue

        prev_close = df.loc[i - 1, "close"]
        for m in range(1, int(gap_min)):
            missing_ts = prev_ts + pd.Timedelta(minutes=m)
            row = {col: 0 for col in df.columns}
            row.update({
                "time_stamp": missing_ts,
                "open": prev_close, "high": prev_close,
                "low": prev_close,  "close": prev_close,
                "volume": 0,
            })
            synthetic_rows.append(row)

        if is_glitch_gap:
            open_fixes[i] = prev_close
        else:
            cas_gap_count += 1
            terminal_close = df.loc[i, "close"]
            cas_terminal_fixes[i] = {
                "open": prev_close,
                "high": max(prev_close, terminal_close),
                "low":  min(prev_close, terminal_close),
            }
            cas_auction_moves.append({
                "date":              prev_ts.date(),
                "pre_auction_time":  prev_ts.time(),
                "pre_auction_close": prev_close,
                "terminal_time":     term_time,
                "terminal_close":    terminal_close,
            })

    if not synthetic_rows:
        return df, 0, 0, []

    label = f"[{display_name}] " if display_name else ""
    logger.info(f"{label}Inserting {len(synthetic_rows)} synthetic flat candle(s) "
                f"({cas_gap_count} CAS auction gap(s)) and correcting "
                f"{len(open_fixes)} bar open(s) plus "
                f"{len(cas_terminal_fixes)} CAS terminal-print bar(s).")

    for idx, new_open in open_fixes.items():
        df.at[idx, "open"] = new_open

    for idx, fixes in cas_terminal_fixes.items():
        df.at[idx, "open"] = fixes["open"]
        df.at[idx, "high"] = fixes["high"]
        df.at[idx, "low"]  = fixes["low"]

    result = pd.concat([df, pd.DataFrame(synthetic_rows)], ignore_index=True)
    result.sort_values("time_stamp", inplace=True)
    result.reset_index(drop=True, inplace=True)
    return result, len(synthetic_rows), cas_gap_count, cas_auction_moves


# CAS effective date — Nifty/Sensex spot has no ticks after the auction
# terminal print, but options/derivatives trade on to 15:40 and backtests
# look up the index price at those later timestamps too. Extend each CAS-era
# day's last bar forward with flat carry-forward candles (at the terminal
# print's close, not the pre-auction close) through 15:39. VIX is excluded —
# it keeps ticking natively and needs no extension.
CAS_EFFECTIVE_DATE        = date(2026, 8, 3)
CAS_DAY_END_TARGET        = dtime(15, 39)
CAS_AFFECTED_INDEX_TOKENS = {"99919000", "99926000"}   # Sensex, Nifty spot


def extend_to_day_close(df: pd.DataFrame, display_name: str = "") -> tuple:
    """
    On/after CAS_EFFECTIVE_DATE, extend each day whose last bar lands before
    CAS_DAY_END_TARGET with flat carry-forward candles (OHLC = that day's
    last close) through CAS_DAY_END_TARGET. Days before the CAS effective
    date are left untouched — their market genuinely closed at the old time.
    """
    if df.empty:
        return df, 0

    df = df.sort_values("time_stamp").reset_index(drop=True)
    synthetic_rows = []

    for day, day_df in df.groupby(df["time_stamp"].dt.date):
        if day < CAS_EFFECTIVE_DATE:
            continue
        last_ts = day_df["time_stamp"].iloc[-1]
        if last_ts.time() >= CAS_DAY_END_TARGET:
            continue
        last_close = day_df["close"].iloc[-1]
        cur = last_ts + pd.Timedelta(minutes=1)
        while cur.time() <= CAS_DAY_END_TARGET:
            row = {col: 0 for col in df.columns}
            row.update({
                "time_stamp": cur,
                "open": last_close, "high": last_close,
                "low": last_close,  "close": last_close,
                "volume": 0,
            })
            synthetic_rows.append(row)
            cur += pd.Timedelta(minutes=1)

    if not synthetic_rows:
        return df, 0

    label = f"[{display_name}] " if display_name else ""
    logger.info(f"{label}Appending {len(synthetic_rows)} flat carry-forward "
                f"candle(s) to extend day(s) to {CAS_DAY_END_TARGET} close.")

    result = pd.concat([df, pd.DataFrame(synthetic_rows)], ignore_index=True)
    result.sort_values("time_stamp", inplace=True)
    result.reset_index(drop=True, inplace=True)
    return result, len(synthetic_rows)


CAS_TRACKING_FILE = os.path.join(DATA_DIR, "cas_auction_tracking.csv")


def log_cas_auction_moves(display_name: str, moves: list):
    """
    Append each day's closing-auction move (pre-auction close -> terminal
    print) to a running tracking log. One row per (date, index). Re-running
    over an already-logged day overwrites that row rather than duplicating it.
    """
    if not moves:
        return

    rows = []
    for m in moves:
        move_pts = m["terminal_close"] - m["pre_auction_close"]
        move_pct = move_pts / m["pre_auction_close"] * 100
        rows.append({
            "date":              str(m["date"]),
            "index":             display_name,
            "pre_auction_time":  m["pre_auction_time"],
            "pre_auction_close": m["pre_auction_close"],
            "terminal_time":     m["terminal_time"],
            "terminal_close":    m["terminal_close"],
            "move_pts":          round(move_pts, 2),
            "move_pct":          round(move_pct, 4),
        })
    new_rows = pd.DataFrame(rows)

    if os.path.exists(CAS_TRACKING_FILE):
        existing = pd.read_csv(CAS_TRACKING_FILE)
        combined = pd.concat([existing, new_rows], ignore_index=True)
        combined.drop_duplicates(subset=["date", "index"], keep="last", inplace=True)
    else:
        combined = new_rows

    combined.sort_values(["date", "index"], inplace=True)
    combined.to_csv(CAS_TRACKING_FILE, index=False)
    logger.info(f"[{display_name}] CAS auction move logged for {len(rows)} day(s) "
                f"→ cas_auction_tracking.csv")


GAP_FADE_TRACKING_FILE = os.path.join(DATA_DIR, "cas_gap_fade_tracking.csv")


def log_gap_fade_tracking(display_name: str, df: pd.DataFrame):
    """
    Research-only: pairs each CAS-era day's own open/low/high/close with the
    prior trading day's closing-auction move (from cas_auction_tracking.csv),
    to test whether a large one-directional auction move tends to fade the
    next session. Feeds the CAS manipulation-theory investigation; no bearing
    on any live strategy.
    """
    if df.empty or not os.path.exists(CAS_TRACKING_FILE):
        return

    df = df.copy()
    df["date"] = df["time_stamp"].dt.date
    cas_dates = sorted(d for d in df["date"].unique() if d >= CAS_EFFECTIVE_DATE)
    if not cas_dates:
        return

    auction_log = pd.read_csv(CAS_TRACKING_FILE)
    auction_log = auction_log[auction_log["index"] == display_name].copy()
    if auction_log.empty:
        return
    auction_log["date"] = pd.to_datetime(auction_log["date"]).dt.date

    rows = []
    for d in cas_dates:
        prior = auction_log[auction_log["date"] < d].sort_values("date")
        if prior.empty:
            continue
        prior_row = prior.iloc[-1]

        day_df = df[df["date"] == d].sort_values("time_stamp")
        day_open  = day_df.iloc[0]["open"]
        day_low   = day_df["low"].min()
        day_high  = day_df["high"].max()
        day_close = day_df.iloc[-1]["close"]

        rows.append({
            "date":                   str(d),
            "index":                  display_name,
            "prior_auction_date":     str(prior_row["date"]),
            "prior_auction_move_pct": prior_row["move_pct"],
            "day_open":               day_open,
            "day_low":                day_low,
            "day_high":               day_high,
            "day_close":              day_close,
            "open_to_low_pct":        round((day_low - day_open) / day_open * 100, 4),
            "open_to_close_pct":      round((day_close - day_open) / day_open * 100, 4),
        })

    if not rows:
        return

    new_rows = pd.DataFrame(rows)
    if os.path.exists(GAP_FADE_TRACKING_FILE):
        existing = pd.read_csv(GAP_FADE_TRACKING_FILE)
        combined = pd.concat([existing, new_rows], ignore_index=True)
        combined.drop_duplicates(subset=["date", "index"], keep="last", inplace=True)
    else:
        combined = new_rows

    combined.sort_values(["date", "index"], inplace=True)
    combined.to_csv(GAP_FADE_TRACKING_FILE, index=False)
    logger.info(f"[{display_name}] Gap-fade tracking logged for {len(rows)} day(s) "
                f"→ cas_gap_fade_tracking.csv")


# ===========================================================================
# Generic index updater
# ===========================================================================

def update_index(obj, display_name: str, exchange: str,
                 symbol_token: str, filename: str):
    """
    Append new 1-minute candles to an index CSV file from the last available
    date up to today.  Works for Sensex, Nifty, and India VIX.
    """
    os.makedirs(INDICES_DIR, exist_ok=True)
    filepath = os.path.join(INDICES_DIR, filename)

    if os.path.exists(filepath):
        existing = pd.read_csv(filepath, parse_dates=["time_stamp"])
        existing["time_stamp"] = pd.to_datetime(existing["time_stamp"],
                                                 utc=False, errors="coerce")
        last_ts  = existing["time_stamp"].max()
        start_dt = (last_ts + timedelta(days=1)).replace(
                       hour=0, minute=0, second=0, microsecond=0,
                       tzinfo=None)
    else:
        existing  = pd.DataFrame(columns=INDEX_HEADERS)
        start_dt  = datetime(2024, 7, 1)

    end_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    if start_dt > end_dt:
        logger.info(f"[{display_name}] already up to date.")
        return

    new_data = fetch_full_range_index(obj, exchange, symbol_token, start_dt, end_dt)

    if new_data.empty:
        logger.info(f"[{display_name}] no new data fetched.")
        return

    new_data, gap_fills, cas_gap_fills, cas_auction_moves = fill_missing_candles(new_data, display_name)
    if symbol_token in CAS_AFFECTED_INDEX_TOKENS:
        log_cas_auction_moves(display_name, cas_auction_moves)
    glitch_fills = gap_fills - cas_gap_fills if cas_gap_fills else gap_fills
    if cas_gap_fills and glitch_fills == 0:
        # Expected daily CAS auction gap only — routine, not an anomaly.
        slack_bot_sendtext(
            f"ℹ️ *CAS Gap Fill* | {display_name} ({filename})\n"
            f"{gap_fills} candle(s) filled across {cas_gap_fills} closing-auction "
            f"gap(s). Terminal auction print left untouched.",
            SLACK_DATA_CHANNEL
        )
    elif gap_fills:
        slack_bot_sendtext(
            f"⚠️ *Gap Fill Applied* | {display_name} ({filename})\n"
            f"{gap_fills} missing 1-min candle(s) detected and filled with flat "
            f"carry-forward values ({cas_gap_fills} CAS auction gap(s), "
            f"{glitch_fills} other). Open of the following glitch bar(s) corrected.",
            SLACK_ERROR_CHANNEL
        )

    if symbol_token in CAS_AFFECTED_INDEX_TOKENS:
        new_data, day_end_fills = extend_to_day_close(new_data, display_name)
        if day_end_fills:
            slack_bot_sendtext(
                f"ℹ️ *CAS Day-End Extend* | {display_name} ({filename})\n"
                f"{day_end_fills} flat carry-forward candle(s) appended after the "
                f"auction print to reach {CAS_DAY_END_TARGET} close (index has no "
                f"ticks after the auction; derivatives trade on to 15:40).",
                SLACK_DATA_CHANNEL
            )
        log_gap_fade_tracking(display_name, new_data)

    # Normalise timestamps to the file's format: "2026-03-11 15:29:00+05:30"
    if new_data["time_stamp"].dt.tz is None:
        new_data["time_stamp"] = new_data["time_stamp"].dt.tz_localize("Asia/Kolkata")
    new_data["time_stamp"] = new_data["time_stamp"].apply(
        lambda ts: format_timestamp(ts, INDEX_TS_FMT))

    # Add oi column (always 0 for indices)
    new_data["oi"] = 0
    new_data = new_data[INDEX_HEADERS]

    # Merge with existing data and deduplicate
    if not existing.empty:
        if existing["time_stamp"].dt.tz is None:
            existing["time_stamp"] = existing["time_stamp"].dt.tz_localize("Asia/Kolkata")
        existing["time_stamp"] = existing["time_stamp"].apply(
            lambda ts: format_timestamp(ts, INDEX_TS_FMT))

        combined = pd.concat([existing, new_data], ignore_index=True)
        combined.drop_duplicates(subset=["time_stamp"], keep="first", inplace=True)
        combined.sort_values("time_stamp", inplace=True)
    else:
        combined = new_data

    combined.to_csv(filepath, index=False)
    logger.info(f"[{display_name}] {filename} updated – total rows: {len(combined)}")
    slack_bot_sendtext(
        f"☁️ [{display_name}] {filename} updated – {len(new_data)} new rows added "
        f"(total: {len(combined)})",
        SLACK_DATA_CHANNEL
    )

    # Data integrity check: each trading day should have ~375 minutes (rows).
    # Since CAS (3 Aug 2026), the terminal auction print's exact minute is
    # randomized within the auction window, so the daily count legitimately
    # varies by a few rows either side of 375 — threshold loosened to 370.
    if len(new_data) < 370:
        slack_bot_sendtext(
            f"⚠️ *Data Integrity Warning* | {display_name} ({filename})\n"
            f"Expected at least 370 new rows, but only {len(new_data)} were added.\n"
            f"Please verify if data for any trading day was partial or missing.",
            SLACK_ERROR_CHANNEL
        )


def update_all_indices(obj):
    """Update Sensex, Nifty, and India VIX index CSV files."""
    for display_name, exchange, symbol_token, filename in INDEX_INSTRUMENTS:
        update_index(obj, display_name, exchange, symbol_token, filename)


# ===========================================================================
# Daily index updater (ONE_DAY interval — official close, date-only timestamps)
# ===========================================================================

def update_daily_index(obj, display_name: str, exchange: str,
                       symbol_token: str, filename: str):
    """
    Fetch and append ONE_DAY bars for an index.
    Timestamps stored as date-only strings (YYYY-MM-DD).
    Incremental: first run fetches DAILY_HISTORY_YEARS of history;
    subsequent runs append from last saved date onwards.
    """
    filepath = os.path.join(INDICES_DIR, filename)
    today    = date.today()

    if os.path.exists(filepath):
        existing  = pd.read_csv(filepath)
        last_date = pd.to_datetime(existing["time_stamp"]).max().date()
        if last_date >= today:
            logger.info(f"[{display_name} daily] already up to date.")
            return
        from_date = last_date + timedelta(days=1)
    else:
        from_date = date(today.year - DAILY_HISTORY_YEARS, today.month, today.day)
        existing  = None
        logger.info(f"[{display_name} daily] first run – fetching "
                    f"{DAILY_HISTORY_YEARS}-year history from {from_date}")

    from_str = f"{from_date.strftime('%Y-%m-%d')} {MARKET_OPEN}"
    to_str   = f"{today.strftime('%Y-%m-%d')} {MARKET_CLOSE}"
    logger.info(f"[{display_name} daily] fetching {from_date} → {today}")

    for attempt in range(MAX_RETRIES + 1):
        _rate_limiter.wait()
        try:
            response = obj.getCandleData({
                "exchange":    exchange,
                "symboltoken": symbol_token,
                "interval":    "ONE_DAY",
                "fromdate":    from_str,
                "todate":      to_str,
            })
        except Exception as e:
            if "exceeding access rate" in str(e) and attempt < MAX_RETRIES:
                logger.warning(
                    f"[{display_name} daily] Rate limited "
                    f"– sleeping {RATE_LIMIT_BACKOFF_SEC}s then retrying "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})"
                )
                time.sleep(RATE_LIMIT_BACKOFF_SEC)
                continue
            logger.warning(f"[{display_name} daily] API error: {e}")
            return
        break

    raw = response.get("data") if isinstance(response, dict) else None
    if not raw:
        logger.info(f"[{display_name} daily] no data returned.")
        return

    new_df = pd.DataFrame(raw, columns=["time_stamp", "open", "high", "low", "close", "volume"])
    new_df["time_stamp"] = pd.to_datetime(new_df["time_stamp"]).dt.date.astype(str)
    new_df["oi"] = 0
    for col in ("open", "high", "low", "close", "volume"):
        new_df[col] = pd.to_numeric(new_df[col], errors="coerce")
    new_df = new_df.sort_values("time_stamp").reset_index(drop=True)

    if existing is not None and not existing.empty:
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset="time_stamp", keep="last")
    else:
        combined = new_df

    combined = combined.sort_values("time_stamp").reset_index(drop=True)
    os.makedirs(INDICES_DIR, exist_ok=True)
    combined.to_csv(filepath, index=False)
    logger.info(f"[{display_name} daily] saved {len(combined)} total rows "
                f"(+{len(new_df)} new) → {filename}")


def update_all_daily_indices(obj):
    """Update daily OHLC for Nifty and India VIX."""
    for display_name, exchange, symbol_token, filename in DAILY_INDEX_INSTRUMENTS:
        update_daily_index(obj, display_name, exchange, symbol_token, filename)


# ===========================================================================
# Options downloader
# ===========================================================================

def parse_expiry_from_master(expiry_str: str) -> datetime:
    """Parse broker expiry string like '24SEP2026' into a datetime."""
    return datetime.strptime(expiry_str.strip(), "%d%b%Y")


def get_option_filepath(expiry_date: datetime, strike: int, option_type: str) -> str:
    """
    Build the full path for an option or futures contract CSV file.
    Options: .../sensex/2026-09-24/87000ce.csv
    Futures: .../sensex/2026-09-24/2026-09-24_futures.csv
    """
    expiry_dir  = os.path.join(OPTIONS_DIR, expiry_date.strftime("%Y-%m-%d"))
    os.makedirs(expiry_dir, exist_ok=True)
    if option_type == "futures":
        filename = f"{expiry_date.strftime('%Y-%m-%d')}_futures.csv"
    else:
        filename = f"{strike}{option_type.lower()}.csv"
    return os.path.join(expiry_dir, filename)


def download_option_contract(obj, token: str, strike: int, option_type: str,
                             expiry_date: datetime, start_date: datetime) -> bool:
    """
    Download 1-minute candle data for a single option contract, saving to disk
    after every 2-day chunk so progress is never lost if the run is interrupted.
    Returns True if at least one chunk was saved, False if no data was found.
    """
    filepath = get_option_filepath(expiry_date, strike, option_type)
    data_saved = False

    if os.path.exists(filepath):
        existing = pd.read_csv(filepath, parse_dates=["time_stamp"])
        existing["time_stamp"] = pd.to_datetime(existing["time_stamp"],
                                                 utc=False, errors="coerce")
        last_ts    = existing["time_stamp"].max()
        fetch_from = (last_ts + timedelta(days=1)).replace(
                         hour=0, minute=0, second=0, microsecond=0,
                         tzinfo=None)
    else:
        existing   = pd.DataFrame(columns=OHLCV_HEADERS)
        fetch_from = start_date

    fetch_to = expiry_date   # last trading day

    if fetch_from > fetch_to:
        return True   # file exists and is current — counts as downloaded

    for chunk_start, chunk_end in date_range_chunks(fetch_from, fetch_to):
        chunk_df = fetch_candle_chunk(obj, OPTIONS_EXCHANGE, token,
                                      chunk_start, chunk_end)
        if chunk_df.empty:
            continue

        # Re-read the file each time so we always merge against the latest saved state
        if os.path.exists(filepath):
            on_disk = pd.read_csv(filepath, parse_dates=["time_stamp"])
            on_disk["time_stamp"] = pd.to_datetime(on_disk["time_stamp"],
                                                    utc=False, errors="coerce")
        else:
            on_disk = pd.DataFrame(columns=OHLCV_HEADERS)

        # Normalise new chunk timestamps before merging
        if chunk_df["time_stamp"].dt.tz is None:
            chunk_df["time_stamp"] = chunk_df["time_stamp"].dt.tz_localize("Asia/Kolkata")

        merged = pd.concat([on_disk, chunk_df], ignore_index=True)
        merged["time_stamp"] = pd.to_datetime(merged["time_stamp"],
                                               utc=False, errors="coerce")
        merged.drop_duplicates(subset=["time_stamp"], keep="first", inplace=True)
        merged.sort_values("time_stamp", inplace=True)
        merged.reset_index(drop=True, inplace=True)

        # Format timestamps for saving
        merged["time_stamp"] = merged["time_stamp"].apply(
            lambda ts: format_timestamp(ts, OPTIONS_TS_FMT))

        merged.to_csv(filepath, index=False)
        data_saved = True

    return data_saved


def download_all_options(obj, contracts_df: pd.DataFrame,
                         instruments_df: pd.DataFrame):
    """
    Iterate over all expired contracts in contracts_df, look up their tokens
    from instruments_df, and download option data.

    contracts_df columns : expiry_date (date), start_date (date), download_status (bool)
    instruments_df columns: token, symbol, name, expiry, strike, lotsize,
                            instrumenttype, exch_seg, tick_size
    """
    today = datetime.now().date()

    # Work only on expired contracts not yet fully downloaded
    pending = contracts_df[
        (contracts_df["expiry_date"].dt.date <= today) &
        (~contracts_df["download_status"])
    ].copy()

    if pending.empty:
        logger.info("No pending expired contracts to download.")
        return

    logger.info(f"Found {len(pending)} pending expiry entries.")

    # Pre-process instruments_df once
    inst = instruments_df.copy()
    inst["expiry_parsed"] = inst["expiry"].apply(parse_expiry_from_master)
    inst["strike_actual"] = (inst["strike"] / 100).astype(int)
    inst["option_type"]   = inst.apply(
        lambda row: "futures" if row["instrumenttype"] == "FUTIDX"
                    else row["symbol"][-2:].lower(),   # "ce" or "pe"
        axis=1
    )

    for _, contract in pending.iterrows():
        expiry_date = pd.Timestamp(contract["expiry_date"]).to_pydatetime()
        start_date  = pd.Timestamp(contract["start_date"]).to_pydatetime()

        logger.info(f"Processing expiry: {expiry_date.date()}  "
                    f"(data from {start_date.date()})")

        expiry_contracts = inst[
            inst["expiry_parsed"].dt.date == expiry_date.date()
        ]

        if expiry_contracts.empty:
            logger.warning(f"  No instruments found for expiry {expiry_date.date()}")
            continue

        logger.info(f"  {len(expiry_contracts)} contracts found for this expiry.")

        actual_downloads = 0
        for _, row in expiry_contracts.iterrows():
            token       = str(row["token"])
            strike      = row["strike_actual"]
            option_type = row["option_type"]

            saved = download_option_contract(
                obj, token, strike, option_type,
                expiry_date, start_date
            )
            if saved:
                actual_downloads += 1

        logger.info(f"  {actual_downloads} of {len(expiry_contracts)} contracts had data.")

        # Mark this expiry as fully downloaded
        contracts_df.loc[
            contracts_df["expiry_date"] == contract["expiry_date"],
            "download_status"
        ] = True
        logger.info(f"Expiry {expiry_date.date()} marked complete.")
        slack_bot_sendtext(
            f"☁️ Sensex options download complete – expiry {expiry_date.date()} "
            f"({actual_downloads} of {len(expiry_contracts)} contracts had data)",
            SLACK_DATA_CHANNEL
        )

    # Persist updated statuses back to contracts.csv
    contracts_df.to_csv(os.path.join(CONFIG_DIR, "options_list_sensex.csv"), index=False)
    logger.info("contracts.csv updated with download statuses.")


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    # --- Load data files ---
    contracts_df        = pd.read_csv(os.path.join(CONFIG_DIR, "options_list_sensex.csv"),
                                      parse_dates=["expiry_date", "start_date"])
    user_credentials_df = pd.read_csv(os.path.join(DATA_DIR, "user_credentials_angel.csv"))

    # --- Authentication ---
    try:
        obj  = SmartConnect(api_key=user_credentials_df.iloc[0].loc["api_key"])
        totp = TOTP(user_credentials_df.iloc[0].loc["qr_code"]).now()
        data = obj.generateSession(
                   user_credentials_df.iloc[0].loc["user_name"],
                   str(user_credentials_df.iloc[0].loc["password"]),
                   totp
               )
        logger.info("Authentication successful.")
    except Exception as e:
        logger.error(f"Authentication failed: {e}")
        slack_bot_sendtext(f"🚨 *Data Downloader (AngelOne)* – Authentication failed: {e}",
                           SLACK_ERROR_CHANNEL)
        raise SystemExit(1)

    # --- Refresh instrument master from broker ---
    SCRIP_MASTER_URL    = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    FO_EXCHANGE_SEGMENT = "BFO"
    try:
        logger.info("Refreshing instrument master...")
        scrip_master_df = pd.read_json(StringIO(urlopen(SCRIP_MASTER_URL).read().decode()))
        instruments_df  = scrip_master_df[
            (scrip_master_df["exch_seg"] == FO_EXCHANGE_SEGMENT) &
            (scrip_master_df["name"]     == "SENSEX")
        ]
        instruments_df.to_csv(os.path.join(DATA_DIR, "instrument_master.csv"), index=False)
        logger.info(f"Instrument master saved – {len(instruments_df)} contracts.")
    except Exception as e:
        logger.error(f"Instrument master refresh failed: {e}")
        slack_bot_sendtext(f"🚨 *Data Downloader (AngelOne)* – Instrument master refresh failed: {e}",
                           SLACK_ERROR_CHANNEL)
        raise SystemExit(1)

    # --- Download all index data (1-min) ---
    try:
        update_all_indices(obj)
    except Exception as e:
        logger.error(f"Index data download failed: {e}")
        slack_bot_sendtext(f"🚨 *Data Downloader* – Index data download failed: {e}",
                           SLACK_ERROR_CHANNEL)

    # --- Download daily index data (Nifty + VIX) ---
    try:
        update_all_daily_indices(obj)
    except Exception as e:
        logger.error(f"Daily index data download failed: {e}")
        slack_bot_sendtext(f"🚨 *Data Downloader* – Daily index download failed: {e}",
                           SLACK_ERROR_CHANNEL)

    # --- Download options data ---
    try:
        logger.info("=== Downloading options data ===")
        download_all_options(obj, contracts_df, instruments_df)
    except Exception as e:
        logger.error(f"Options data download failed: {e}")
        slack_bot_sendtext(f"🚨 *Data Downloader (AngelOne)* – Options data download failed: {e}",
                           SLACK_ERROR_CHANNEL)

    # --- Terminate session ---
    try:
        obj.terminateSession(user_credentials_df.iloc[0].loc["user_name"])
        logger.info("Session terminated.")
    except Exception as e:
        logger.warning(f"Session termination failed: {e}")

    logger.info("=== All done ===")