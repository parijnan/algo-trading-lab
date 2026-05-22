"""
nifty_daily_index_angel.py — Download official daily Nifty 50 OHLC via AngelOne.

Fetches the NSE cash daily series (token 99926000) using getCandleData with
ONE_DAY interval. AngelOne updates same-day data after market close, making
this the preferred live source for the range detection research module.

Output: data/indices/nifty_daily_angel.csv
  Columns: time_stamp, open, high, low, close, volume, oi
  (Same structure as nifty_daily.csv for direct comparison.)

Behaviour:
  - First run  : fetches ~3 years of history (stays within AngelOne 1000-record limit)
  - Subsequent : incremental update from last saved date onwards

Usage:
    python nifty_daily_index_angel.py
"""

import os
import sys
import time
import logging
import pandas as pd
from datetime import datetime, timedelta, date
from collections import deque
from requests import post
from pyotp import TOTP
from SmartApi import SmartConnect

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
# Paths
# ---------------------------------------------------------------------------
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(BASE_DIR, "data")
INDICES_DIR = os.path.join(DATA_DIR, "indices")
OUTPUT_FILE = os.path.join(INDICES_DIR, "nifty_daily_angel.csv")

SLACK_DATA_CHANNEL  = "#data-alerts"
SLACK_ERROR_CHANNEL = "#error-alerts"

HISTORY_YEARS   = 3
NIFTY_TOKEN     = "99926000"
NIFTY_EXCHANGE  = "NSE"
CANDLE_DATE_FMT = "%Y-%m-%d %H:%M"
MARKET_OPEN     = "09:15"
MARKET_CLOSE    = "15:30"

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
_creds_df   = pd.read_csv(os.path.join(DATA_DIR, "user_credentials_angel.csv"))
_creds      = _creds_df.iloc[0]
slack_token = _creds["slack_token"]


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def _slack(msg: str, channel: str):
    try:
        post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {slack_token}",
                     "Content-Type": "application/json"},
            json={"channel": channel, "text": msg},
            timeout=5,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Rate limiter  (AngelOne: 2/sec, 180/min, 5000/hr)
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, per_sec=2, per_min=180, per_hour=5000):
        self.per_sec  = per_sec
        self.per_min  = per_min
        self.per_hour = per_hour
        self._sec  = deque()
        self._min  = deque()
        self._hour = deque()

    def _evict(self, q, cutoff):
        while q and q[0] < cutoff:
            q.popleft()

    def wait(self):
        while True:
            now = time.monotonic()
            self._evict(self._sec,  now - 1)
            self._evict(self._min,  now - 60)
            self._evict(self._hour, now - 3600)
            if (len(self._sec) < self.per_sec and
                    len(self._min) < self.per_min and
                    len(self._hour) < self.per_hour):
                break
            time.sleep(0.05)
        now = time.monotonic()
        self._sec.append(now); self._min.append(now); self._hour.append(now)


_rate_limiter = RateLimiter()


# ---------------------------------------------------------------------------
# Fetch daily candles via AngelOne
# ---------------------------------------------------------------------------

def fetch_daily_nifty(obj, from_date: date, to_date: date) -> pd.DataFrame:
    from_str = f"{from_date.strftime('%Y-%m-%d')} {MARKET_OPEN}"
    to_str   = f"{to_date.strftime('%Y-%m-%d')} {MARKET_CLOSE}"

    logger.info(f"Fetching daily Nifty: {from_date} → {to_date}")
    _rate_limiter.wait()

    response = obj.getCandleData({
        "exchange":    NIFTY_EXCHANGE,
        "symboltoken": NIFTY_TOKEN,
        "interval":    "ONE_DAY",
        "fromdate":    from_str,
        "todate":      to_str,
    })

    raw = response.get("data") if isinstance(response, dict) else None
    if not raw:
        raise ValueError(f"Empty response from AngelOne: {response}")

    df = pd.DataFrame(raw, columns=["time_stamp", "open", "high", "low", "close", "volume"])
    logger.info(f"Received {len(df)} rows")

    # Normalise timestamp to date-only string
    df["time_stamp"] = pd.to_datetime(df["time_stamp"]).dt.date.astype(str)

    df["oi"] = 0
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.sort_values("time_stamp").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Incremental save
# ---------------------------------------------------------------------------

def update_nifty_daily(obj):
    today = date.today()

    if os.path.exists(OUTPUT_FILE):
        existing  = pd.read_csv(OUTPUT_FILE)
        last_date = pd.to_datetime(existing["time_stamp"]).max().date()

        if last_date >= today:
            logger.info(f"Already up to date (last row: {last_date}). Nothing to do.")
            return 0

        from_date = last_date + timedelta(days=1)
        logger.info(f"Incremental update: {from_date} → {today}")
    else:
        from_date = date(today.year - HISTORY_YEARS, today.month, today.day)
        existing  = None
        logger.info(f"First run: fetching {HISTORY_YEARS}-year history from {from_date}")

    new_df = fetch_daily_nifty(obj, from_date, today)

    if new_df.empty:
        logger.info("No new rows returned.")
        return 0

    if existing is not None and not existing.empty:
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset="time_stamp", keep="last")
    else:
        combined = new_df

    combined = combined.sort_values("time_stamp").reset_index(drop=True)
    os.makedirs(INDICES_DIR, exist_ok=True)
    combined.to_csv(OUTPUT_FILE, index=False)
    new_count = len(new_df)
    logger.info(f"Saved {len(combined)} total rows → {OUTPUT_FILE}  (+{new_count} new)")
    return new_count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        obj  = SmartConnect(api_key=_creds["api_key"])
        totp = TOTP(_creds["qr_code"]).now()
        obj.generateSession(_creds["user_name"], str(_creds["password"]), totp)
        logger.info("AngelOne authentication successful.")
    except Exception as e:
        logger.error(f"Authentication failed: {e}")
        _slack(f"🚨 *Nifty Daily (Angel)* – Auth failed: {e}", SLACK_ERROR_CHANNEL)
        sys.exit(1)

    try:
        new_rows = update_nifty_daily(obj)
        if new_rows > 0:
            _slack(
                f"✅ *Nifty Daily Index (Angel)* – {new_rows} new rows saved to `nifty_daily_angel.csv`",
                SLACK_DATA_CHANNEL,
            )
        logger.info("Done.")
    except Exception as e:
        logger.error(f"Download failed: {e}")
        _slack(f"🚨 *Nifty Daily (Angel)* – Download failed: {e}", SLACK_ERROR_CHANNEL)
        sys.exit(1)
    finally:
        try:
            obj.terminateSession(_creds["user_name"])
        except Exception:
            pass
