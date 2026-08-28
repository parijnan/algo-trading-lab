import os
import time
import logging
import pandas as pd
from datetime import datetime, timedelta, date
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
# MCX session hours vary by commodity group (agri closes earlier than bullion/
# energy/metals, and non-agri shifts 23:30 <-> 23:55 with US daylight saving).
# One wide window covers all of them; requesting past a commodity's actual
# close just yields no extra candles, same as the equities downloader's
# out-of-window filtering below.
MARKET_OPEN  = "09:00"
MARKET_CLOSE = "23:30"

CANDLE_DATE_FMT = "%Y-%m-%d %H:%M"        # format expected by getCandleData
OPTIONS_TS_FMT  = "%Y-%m-%dT%H:%M:%S"     # base format for saved csv (tz appended manually)
METHOD_TS_FMT   = "%Y-%m-%dT%H:%M:%S%z"   # format returned by getCandleData
OHLCV_HEADERS   = ["time_stamp", "open", "high", "low", "close", "volume"]

MCX_EXCHANGE = "MCX"
CHUNK_DAYS   = 2     # days per API call (matches equities downloader's sizing)

# SmartAPI does not serve historical data for expired F&O contracts, so
# forward-testing data collection starts from whatever history the current
# (unexpired) front-month contract has since its own listing date. MCX
# typically lists a new contract several months ahead of its expiry, so this
# lookback is generous rather than tightly measured; chunks with no data
# before the true listing date simply come back empty at no extra cost beyond
# the wasted call.
LOOKBACK_DAYS = 200

# Rate limit parameters (broker limits: 3/sec, 180/min, 5000/hour)
RATE_LIMIT_PER_SEC  = 2
RATE_LIMIT_PER_MIN  = 180
RATE_LIMIT_PER_HOUR = 5000

# Retry parameters for broker-side rate limit rejections
RATE_LIMIT_BACKOFF_SEC = 60
MAX_RETRIES            = 3

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# ---------------------------------------------------------------------------
# Paths  (script lives in the parent directory of "data")
# ---------------------------------------------------------------------------
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(BASE_DIR, "data")
CONFIG_DIR = os.path.join(BASE_DIR, "config")
MCX_DIR    = os.path.join(DATA_DIR, "mcx")

UNDERLYINGS_FILE = os.path.join(CONFIG_DIR, "mcx_underlyings.csv")
TRACKING_FILE    = os.path.join(DATA_DIR, "mcx_contract_tracking.csv")
INSTRUMENT_MASTER_FILE = os.path.join(DATA_DIR, "mcx_instrument_master.csv")


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
        self._calls_sec  = deque()
        self._calls_min  = deque()
        self._calls_hour = deque()

    def _evict(self, window: deque, cutoff: float):
        while window and window[0] < cutoff:
            window.popleft()

    def wait(self):
        while True:
            now = time.monotonic()
            self._evict(self._calls_sec,  now - 1)
            self._evict(self._calls_min,  now - 60)
            self._evict(self._calls_hour, now - 3600)

            if (len(self._calls_sec)  < self.per_second and
                    len(self._calls_min)  < self.per_minute and
                    len(self._calls_hour) < self.per_hour):
                break

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

        now = time.monotonic()
        self._calls_sec.append(now)
        self._calls_min.append(now)
        self._calls_hour.append(now)


_rate_limiter = RateLimiter()


# ===========================================================================
# Helper utilities
# ===========================================================================

def date_range_chunks(start: datetime, end: datetime, chunk_days: int = CHUNK_DAYS):
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=chunk_days - 1), end)
        yield current, chunk_end
        current += timedelta(days=chunk_days)


def format_timestamp(ts: pd.Timestamp, base_fmt: str) -> str:
    if ts.tzinfo is None:
        ts = ts.tz_localize("Asia/Kolkata")
    offset = ts.strftime("%z")
    offset_colon = offset[:-2] + ":" + offset[-2:]
    return ts.strftime(base_fmt) + offset_colon


def parse_expiry_from_master(expiry_str: str) -> datetime:
    """Parse broker expiry string like '31AUG2026' into a datetime."""
    return datetime.strptime(expiry_str.strip(), "%d%b%Y")


def fetch_candle_chunk(obj, token: str, from_dt: datetime, to_dt: datetime) -> pd.DataFrame:
    """
    Call getCandleData for a single chunk and return a clean DataFrame.
    Timestamps outside [from_dt, to_dt] are discarded (broker sometimes
    returns stray dates when no data exists for the requested range).
    """
    from_str = f"{from_dt.strftime('%Y-%m-%d')} {MARKET_OPEN}"
    to_str   = f"{to_dt.strftime('%Y-%m-%d')} {MARKET_CLOSE}"

    for attempt in range(MAX_RETRIES + 1):
        _rate_limiter.wait()
        try:
            response = obj.getCandleData({
                "exchange":    MCX_EXCHANGE,
                "symboltoken": str(token),
                "interval":    "ONE_MINUTE",
                "fromdate":    from_str,
                "todate":      to_str,
            })
        except Exception as e:
            if "exceeding access rate" in str(e) and attempt < MAX_RETRIES:
                logger.warning(
                    f"Rate limited for token {token} [{from_str} -> {to_str}] "
                    f"- sleeping {RATE_LIMIT_BACKOFF_SEC}s then retrying "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})"
                )
                time.sleep(RATE_LIMIT_BACKOFF_SEC)
                continue
            logger.warning(f"API error for token {token} [{from_str} -> {to_str}]: {e}")
            return pd.DataFrame(columns=OHLCV_HEADERS)
        break

    raw = response.get("data") if isinstance(response, dict) else None
    if not raw:
        return pd.DataFrame(columns=OHLCV_HEADERS)

    df = pd.DataFrame(raw, columns=OHLCV_HEADERS)
    df["time_stamp"] = pd.to_datetime(df["time_stamp"], format=METHOD_TS_FMT,
                                      utc=False, errors="coerce")

    window_start = pd.Timestamp(f"{from_dt.strftime('%Y-%m-%d')} {MARKET_OPEN}",
                                tz="Asia/Kolkata")
    window_end   = pd.Timestamp(f"{to_dt.strftime('%Y-%m-%d')} {MARKET_CLOSE}",
                                tz="Asia/Kolkata")
    df = df[(df["time_stamp"] >= window_start) & (df["time_stamp"] <= window_end)]

    return df


# ===========================================================================
# Front-month selection
# ===========================================================================

def load_enabled_underlyings() -> list:
    df = pd.read_csv(UNDERLYINGS_FILE)
    return df[df["enabled"] == True]["name"].tolist()  # noqa: E712


def select_front_month_contracts(instruments_df: pd.DataFrame, names: list) -> pd.DataFrame:
    """
    For each requested underlying name, pick the contract with the nearest
    expiry that has not yet passed. Returns one row per underlying.
    """
    inst = instruments_df[instruments_df["name"].isin(names)].copy()
    inst["expiry_parsed"] = inst["expiry"].apply(parse_expiry_from_master)

    today = pd.Timestamp(date.today())
    inst = inst[inst["expiry_parsed"] >= today]

    inst.sort_values(["name", "expiry_parsed"], inplace=True)
    front_month = inst.groupby("name", as_index=False).first()
    return front_month


# ===========================================================================
# Contract-roll tracking
# ===========================================================================

def load_tracking() -> pd.DataFrame:
    if os.path.exists(TRACKING_FILE):
        return pd.read_csv(TRACKING_FILE)
    return pd.DataFrame(columns=["name", "token", "symbol", "expiry_date"])


def save_tracking(df: pd.DataFrame):
    df.to_csv(TRACKING_FILE, index=False)


def detect_rolls(front_month: pd.DataFrame, prior_tracking: pd.DataFrame) -> list:
    """Return list of (name, old_symbol, new_symbol) for underlyings whose
    tracked front-month contract changed since the last run."""
    rolls = []
    prior_by_name = prior_tracking.set_index("name") if not prior_tracking.empty else None
    for _, row in front_month.iterrows():
        name = row["name"]
        if prior_by_name is not None and name in prior_by_name.index:
            old_symbol = prior_by_name.loc[name, "symbol"]
            if old_symbol != row["symbol"]:
                rolls.append((name, old_symbol, row["symbol"]))
    return rolls


# ===========================================================================
# Futures downloader
# ===========================================================================

def get_futures_filepath(name: str, expiry_date: datetime) -> str:
    contract_dir = os.path.join(MCX_DIR, name)
    os.makedirs(contract_dir, exist_ok=True)
    filename = f"{expiry_date.strftime('%Y-%m-%d')}_futures.csv"
    return os.path.join(contract_dir, filename)


def download_futures_contract(obj, name: str, token: str, expiry_date: datetime) -> int:
    """
    Download 1-minute candle data for a single front-month futures contract,
    saving to disk after every chunk so progress is never lost mid-run.
    Returns the number of new rows added.
    """
    filepath = get_futures_filepath(name, expiry_date)

    if os.path.exists(filepath):
        existing = pd.read_csv(filepath, parse_dates=["time_stamp"])
        existing["time_stamp"] = pd.to_datetime(existing["time_stamp"],
                                                 utc=False, errors="coerce")
        last_ts = existing["time_stamp"].max()
        # Re-request from the START of last_ts's own day, not the day after.
        # fetch_candle_chunk() only supports day-granularity requests (its
        # from_str/to_str always use MARKET_OPEN/MARKET_CLOSE, ignoring any
        # time-of-day on the datetime passed in) — so if last_ts's day was
        # only partially downloaded (e.g. this function last ran mid-session,
        # or a run was interrupted), jumping to day+1 permanently skips the
        # rest of that day with no re-fetch ever attempted again. Re-fetching
        # last_ts's whole day is redundant for the already-saved portion, but
        # harmless: the merge below dedupes on time_stamp, so only genuinely
        # missing rows from that day actually get added. Confirmed live
        # 2026-08-28: a mid-session gap-fill on 2026-08-27 left this function
        # (before this fix) unable to backfill the rest of that day — the
        # next attempt jumped straight to 2026-08-28 and the broker rejected
        # the from-date as being in the future.
        fetch_from = last_ts.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    else:
        fetch_from = datetime.now() - timedelta(days=LOOKBACK_DAYS)

    fetch_to = min(expiry_date, datetime.now())

    if fetch_from > fetch_to:
        return 0

    total_new = 0
    for chunk_start, chunk_end in date_range_chunks(fetch_from, fetch_to):
        chunk_df = fetch_candle_chunk(obj, token, chunk_start, chunk_end)
        if chunk_df.empty:
            continue

        if os.path.exists(filepath):
            on_disk = pd.read_csv(filepath, parse_dates=["time_stamp"])
            on_disk["time_stamp"] = pd.to_datetime(on_disk["time_stamp"],
                                                    utc=False, errors="coerce")
        else:
            on_disk = pd.DataFrame(columns=OHLCV_HEADERS)

        if chunk_df["time_stamp"].dt.tz is None:
            chunk_df["time_stamp"] = chunk_df["time_stamp"].dt.tz_localize("Asia/Kolkata")

        before = len(on_disk)
        merged = chunk_df.copy() if on_disk.empty else pd.concat([on_disk, chunk_df], ignore_index=True)
        merged["time_stamp"] = pd.to_datetime(merged["time_stamp"],
                                               utc=False, errors="coerce")
        merged.drop_duplicates(subset=["time_stamp"], keep="first", inplace=True)
        merged.sort_values("time_stamp", inplace=True)
        merged.reset_index(drop=True, inplace=True)
        total_new += len(merged) - before

        save_df = merged.copy()
        save_df["time_stamp"] = save_df["time_stamp"].apply(
            lambda ts: format_timestamp(ts, OPTIONS_TS_FMT))
        save_df.to_csv(filepath, index=False)

    return total_new


def download_all_front_month_futures(obj, front_month: pd.DataFrame) -> dict:
    """Download/update every front-month contract. Returns {name: new_rows}."""
    results = {}
    for _, row in front_month.iterrows():
        name        = row["name"]
        token       = str(row["token"])
        expiry_date = row["expiry_parsed"].to_pydatetime()

        logger.info(f"[{name}] {row['symbol']} (expiry {expiry_date.date()}) - updating...")
        new_rows = download_futures_contract(obj, name, token, expiry_date)
        results[name] = new_rows
        logger.info(f"[{name}] {new_rows} new row(s) added.")

    return results


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    os.makedirs(MCX_DIR, exist_ok=True)
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
        slack_bot_sendtext(f"🚨 *Data Downloader (MCX)* – Authentication failed: {e}",
                           SLACK_ERROR_CHANNEL)
        raise SystemExit(1)

    # --- Refresh instrument master and select front-month contracts ---
    try:
        logger.info("Refreshing MCX instrument master...")
        scrip_master_df = pd.read_json(StringIO(urlopen(SCRIP_MASTER_URL).read().decode()))
        mcx_futures_df = scrip_master_df[
            (scrip_master_df["exch_seg"] == MCX_EXCHANGE) &
            (scrip_master_df["instrumenttype"] == "FUTCOM")
        ]
        mcx_futures_df.to_csv(INSTRUMENT_MASTER_FILE, index=False)

        names = load_enabled_underlyings()
        front_month = select_front_month_contracts(mcx_futures_df, names)

        missing = set(names) - set(front_month["name"])
        if missing:
            logger.warning(f"No live contract found for: {sorted(missing)}")

        logger.info(f"Front-month contracts selected for {len(front_month)} underlying(s).")
    except Exception as e:
        logger.error(f"Instrument master refresh failed: {e}")
        slack_bot_sendtext(f"🚨 *Data Downloader (MCX)* – Instrument master refresh failed: {e}",
                           SLACK_ERROR_CHANNEL)
        raise SystemExit(1)

    # --- Detect contract rolls since last run ---
    prior_tracking = load_tracking()
    rolls = detect_rolls(front_month, prior_tracking)
    if rolls:
        roll_lines = "\n".join(f"  {name}: {old} -> {new}" for name, old, new in rolls)
        logger.info(f"Contract roll(s) detected:\n{roll_lines}")
        slack_bot_sendtext(
            f"🔄 *MCX Contract Roll* – {len(rolls)} underlying(s) rolled to a new "
            f"front-month contract:\n{roll_lines}",
            SLACK_DATA_CHANNEL
        )

    # --- Download all front-month futures ---
    try:
        results = download_all_front_month_futures(obj, front_month)
        total_new = sum(results.values())
        logger.info(f"MCX futures update complete – {total_new} total new row(s) "
                    f"across {len(results)} contract(s).")
        slack_bot_sendtext(
            f"☁️ *MCX Futures Update* – {total_new} new row(s) across "
            f"{len(results)} front-month contract(s).",
            SLACK_DATA_CHANNEL
        )
    except Exception as e:
        logger.error(f"MCX futures download failed: {e}")
        slack_bot_sendtext(f"🚨 *Data Downloader (MCX)* – Futures download failed: {e}",
                           SLACK_ERROR_CHANNEL)

    # --- Persist tracking state ---
    save_tracking(front_month[["name", "token", "symbol", "expiry"]].rename(
        columns={"expiry": "expiry_date"}))

    # --- Terminate session ---
    try:
        obj.terminateSession(user_credentials_df.iloc[0].loc["user_name"])
        logger.info("Session terminated.")
    except Exception as e:
        logger.warning(f"Session termination failed: {e}")

    logger.info("=== All done ===")
