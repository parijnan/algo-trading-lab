"""
nifty_daily_index.py — Download official daily Nifty 50 OHLC from ICICI Breeze.

The 1-min nifty.csv carries the last traded price, not the official NSE close
(which is a float-adjusted, market-cap-weighted average of constituent stocks).
This script fetches the NSE/cash daily series from ICICI Breeze, which carries
the correct official closing price.

Output: data/indices/nifty_daily.csv
  Columns: time_stamp, open, high, low, close, volume, oi
  (Same structure as nifty.csv for drop-in compatibility with research tools.)

Behaviour:
  - First run  : fetches ~3 years of history (Breeze rolling window limit)
  - Subsequent : incremental update from last saved date onwards
  - No data loss: existing rows are never overwritten; new rows appended only

Usage:
    python nifty_daily_index.py
"""

import os
import sys
import time
import logging
import urllib.parse
import pandas as pd
from datetime import datetime, timedelta, timezone
from requests import post
from breeze_connect import BreezeConnect
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from pyotp import TOTP

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
OUTPUT_FILE = os.path.join(INDICES_DIR, "nifty_daily.csv")

SLACK_DATA_CHANNEL  = "#data-alerts"
SLACK_ERROR_CHANNEL = "#error-alerts"

# Breeze 3-year rolling window
HISTORY_YEARS = 3

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
_creds_df   = pd.read_csv(os.path.join(DATA_DIR, "user_credentials_icici.csv"))
_creds      = _creds_df.to_dict("list")
apiKey      = _creds["apiKey"][0]
secretKey   = _creds["secretKey"][0]
userName    = _creds["userName"][0]
passWord    = _creds["passWord"][0]
totpKey     = _creds["totpKey"][0]
slack_token = _creds["slack_token"][0]


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
# Selenium auth  (identical pattern to weekly_option_data_nifty.py)
# ---------------------------------------------------------------------------

def get_session_id() -> str:
    logger.info("Launching headless Chrome to fetch Breeze session ID...")
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")

    browser = webdriver.Chrome(options=opts)
    try:
        browser.get("https://api.icicidirect.com/apiuser/login?api_key="
                    + urllib.parse.quote_plus(apiKey))
        browser.implicitly_wait(5)

        browser.find_element(By.XPATH,
            "/html/body/form/div[2]/div/div/div[1]/div[2]/div/div[1]/input"
        ).send_keys(userName)
        browser.find_element(By.XPATH,
            "/html/body/form/div[2]/div/div/div[1]/div[2]/div/div[3]/div/input"
        ).send_keys(passWord)
        browser.find_element(By.XPATH,
            "/html/body/form/div[2]/div/div/div[1]/div[2]/div/div[4]/div/input"
        ).click()

        WebDriverWait(browser, 10).until(
            EC.element_to_be_clickable((By.XPATH,
                "/html/body/form/div[2]/div/div/div[1]/div[2]/div/div[5]/input[1]"))
        ).click()
        time.sleep(2)

        browser.find_element("xpath",
            "/html/body/form/div[2]/div/div/div[2]/div/div[2]/div[2]/div[3]/div/div[1]/input"
        ).send_keys(TOTP(totpKey).now())
        browser.find_element("xpath",
            "/html/body/form/div[2]/div/div/div[2]/div/div[2]/div[2]/div[4]/input[1]"
        ).click()
        time.sleep(1)

        session_id = browser.current_url.split("apisession=")[1][:8]
        logger.info(f"Session ID: {session_id}")
        return session_id
    finally:
        browser.quit()


# ---------------------------------------------------------------------------
# Fetch daily Nifty index data from Breeze
# ---------------------------------------------------------------------------

def _breeze_date(dt: datetime) -> str:
    """Format datetime as Breeze API from_date/to_date string."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fetch_daily_nifty(breeze, from_dt: datetime, to_dt: datetime) -> pd.DataFrame:
    """
    Fetch daily Nifty 50 OHLC from ICICI Breeze for the given date range.
    Returns a DataFrame with columns: time_stamp, open, high, low, close, volume, oi
    """
    logger.info(f"Fetching daily Nifty: {from_dt.date()} → {to_dt.date()}")
    result = breeze.get_historical_data(
        interval="1day",
        from_date=_breeze_date(from_dt),
        to_date=_breeze_date(to_dt),
        stock_code="NIFTY",
        exchange_code="NSE",
        product_type="cash",
        expiry_date="",
        right="",
        strike_price="",
    )

    if not result or "Success" not in result or not result["Success"]:
        raise ValueError(f"Empty response from Breeze: {result}")

    df = pd.DataFrame(result["Success"])
    logger.info(f"Received {len(df)} rows")

    # Normalise datetime → time_stamp in IST
    IST = timezone(timedelta(hours=5, minutes=30))
    def _to_ist(ts_str):
        # Breeze returns e.g. "2023-05-22T00:00:00.000Z"
        dt = pd.to_datetime(ts_str, utc=True).tz_convert(IST)
        return dt

    df["time_stamp"] = df["datetime"].apply(_to_ist)

    # Keep only columns we need; add oi=0 (index has no OI)
    df = df[["time_stamp", "open", "high", "low", "close", "volume"]].copy()
    df["oi"] = 0

    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.sort_values("time_stamp").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Incremental save
# ---------------------------------------------------------------------------

def update_nifty_daily(breeze):
    ist_now = datetime.now(tz=timezone(timedelta(hours=5, minutes=30)))
    today   = ist_now.date()

    if os.path.exists(OUTPUT_FILE):
        existing = pd.read_csv(OUTPUT_FILE)
        existing["time_stamp"] = pd.to_datetime(existing["time_stamp"], utc=True)
        last_date = existing["time_stamp"].max().date()

        if last_date >= today:
            logger.info(f"Already up to date (last row: {last_date}). Nothing to do.")
            return 0

        from_dt = datetime(last_date.year, last_date.month, last_date.day,
                           tzinfo=timezone.utc) + timedelta(days=1)
        logger.info(f"Incremental update: {last_date + timedelta(days=1)} → {today}")
    else:
        # First run — fetch full 3-year history
        from_dt = datetime(today.year - HISTORY_YEARS, today.month, today.day,
                           tzinfo=timezone.utc)
        existing = None
        logger.info(f"First run: fetching {HISTORY_YEARS}-year history from {from_dt.date()}")

    to_dt = datetime(today.year, today.month, today.day, 23, 59, 59,
                     tzinfo=timezone.utc)

    new_df = fetch_daily_nifty(breeze, from_dt, to_dt)

    if new_df.empty:
        logger.info("No new rows returned.")
        return 0

    if existing is not None and not existing.empty:
        combined = pd.concat([existing, new_df], ignore_index=True)
        # Drop any duplicates (same date appearing in both)
        combined["_date"] = pd.to_datetime(combined["time_stamp"]).dt.date
        combined = combined.drop_duplicates(subset="_date", keep="last").drop(columns="_date")
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
    # --- Auth ---
    try:
        session_id = get_session_id()
    except Exception as e:
        logger.error(f"Selenium login failed: {e}")
        _slack(f"🚨 *Nifty Daily Downloader* – Selenium login failed: {e}", SLACK_ERROR_CHANNEL)
        sys.exit(1)

    # Persist updated session ID
    _creds["sessionID"][0] = session_id
    pd.DataFrame.from_dict(_creds).to_csv(
        os.path.join(DATA_DIR, "user_credentials_icici.csv"), index=False)

    # --- Breeze session ---
    try:
        breeze = BreezeConnect(api_key=apiKey)
        breeze.generate_session(api_secret=secretKey, session_token=session_id)
        breeze.get_customer_details(api_session=session_id)
        logger.info("Breeze authentication successful.")
    except Exception as e:
        logger.error(f"Breeze auth failed: {e}")
        _slack(f"🚨 *Nifty Daily Downloader* – Breeze auth failed: {e}", SLACK_ERROR_CHANNEL)
        sys.exit(1)

    # --- Fetch & save ---
    try:
        new_rows = update_nifty_daily(breeze)
        if new_rows > 0:
            _slack(
                f"✅ *Nifty Daily Index* – {new_rows} new rows saved to `nifty_daily.csv`",
                SLACK_DATA_CHANNEL,
            )
        logger.info("Done.")
    except Exception as e:
        logger.error(f"Download failed: {e}")
        _slack(f"🚨 *Nifty Daily Downloader* – Download failed: {e}", SLACK_ERROR_CHANNEL)
        sys.exit(1)
