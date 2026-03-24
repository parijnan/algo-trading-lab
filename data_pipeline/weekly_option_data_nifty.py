import os
import sys
import time
import logging
import urllib.parse
import pandas as pd
from datetime import datetime
from collections import deque
from requests import post
from breeze_connect import BreezeConnect
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from pyotp import TOTP

# Change directory to ensure correct working directory when run as a cronjob
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
STRIKE_STEP       = 50      # Nifty strike interval
STRIKE_RANGE      = 5050    # Range above/below index value to download
RATE_LIMIT_PER_MIN = 100
RATE_LIMIT_PER_DAY = 5000

SLACK_DATA_CHANNEL  = "#data-alerts"
SLACK_ERROR_CHANNEL = "#error-alerts"

# ---------------------------------------------------------------------------
# Paths  (script lives in data_pipeline/, data/ is a sibling directory)
# ---------------------------------------------------------------------------
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(BASE_DIR, "data")
CONFIG_DIR = os.path.join(BASE_DIR, "config")
NIFTY_DIR  = os.path.join(DATA_DIR, "nifty", "options")

# ---------------------------------------------------------------------------
# Load credentials
# ---------------------------------------------------------------------------
user_credentials_df   = pd.read_csv(os.path.join(DATA_DIR, 'user_credentials_icici.csv'))
user_credentials_dict = user_credentials_df.to_dict('list')
apiKey    = user_credentials_dict['apiKey'][0]
secretKey = user_credentials_dict['secretKey'][0]
userName  = user_credentials_dict['userName'][0]
passWord  = user_credentials_dict['passWord'][0]
totpKey   = user_credentials_dict['totpKey'][0]


# ===========================================================================
# Slack messaging
# ===========================================================================

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
    Sliding-window rate limiter enforcing:
      - max calls per minute (100)
      - max calls per day   (5000)
    Call .wait() before every API request.
    Raises SystemExit when the daily limit is reached.
    """

    def __init__(self, per_minute: int = RATE_LIMIT_PER_MIN,
                 per_day: int = RATE_LIMIT_PER_DAY):
        self.per_minute = per_minute
        self.per_day    = per_day
        self._calls_min = deque()
        self._calls_day = deque()

    def _evict(self, window: deque, cutoff: float):
        while window and window[0] < cutoff:
            window.popleft()

    @property
    def daily_count(self):
        self._evict(self._calls_day, time.monotonic() - 86400)
        return len(self._calls_day)

    def wait(self):
        """Block until rate limits allow the next call, or exit if daily limit hit."""
        # Check daily limit first
        self._evict(self._calls_day, time.monotonic() - 86400)
        if len(self._calls_day) >= self.per_day:
            msg = f"Daily API limit of {self.per_day} requests reached. Exiting."
            logger.error(msg)
            slack_bot_sendtext(f"🚨 *Nifty Data Downloader* – {msg}", SLACK_ERROR_CHANNEL)
            sys.exit(1)

        # Enforce per-minute limit
        while True:
            now = time.monotonic()
            self._evict(self._calls_min, now - 60)
            if len(self._calls_min) < self.per_minute:
                break
            sleep_for = max(0.0, self._calls_min[0] + 60 - now)
            logger.debug(f"Rate limit reached – sleeping {sleep_for:.2f}s")
            time.sleep(sleep_for)

        now = time.monotonic()
        self._calls_min.append(now)
        self._calls_day.append(now)


_rate_limiter = RateLimiter()


# ===========================================================================
# Selenium: fetch session ID
# ===========================================================================

def get_session_id(api_key: str, username: str, password: str, totp_key: str) -> str:
    """Launch headless Chrome, log in to ICICI Direct and return the session ID."""
    logger.info("Launching headless Chrome to fetch session ID...")
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')

    browser = webdriver.Chrome(options=options)
    try:
        browser.get("https://api.icicidirect.com/apiuser/login?api_key="
                    + urllib.parse.quote_plus(api_key))
        browser.implicitly_wait(5)

        browser.find_element(By.XPATH,
            '/html/body/form/div[2]/div/div/div[1]/div[2]/div/div[1]/input'
        ).send_keys(username)
        browser.find_element(By.XPATH,
            '/html/body/form/div[2]/div/div/div[1]/div[2]/div/div[3]/div/input'
        ).send_keys(password)
        browser.find_element(By.XPATH,
            '/html/body/form/div[2]/div/div/div[1]/div[2]/div/div[4]/div/input'
        ).click()

        final_button = WebDriverWait(browser, 10).until(
            EC.element_to_be_clickable((By.XPATH,
                '/html/body/form/div[2]/div/div/div[1]/div[2]/div/div[5]/input[1]'))
        )
        browser.execute_script("arguments[0].scrollIntoView(true);", final_button)
        final_button.click()
        time.sleep(2)

        totp_field = browser.find_element("xpath",
            '/html/body/form/div[2]/div/div/div[2]/div/div[2]/div[2]/div[3]/div/div[1]/input')
        totp_field.send_keys(TOTP(totp_key).now())
        browser.find_element("xpath",
            '/html/body/form/div[2]/div/div/div[2]/div/div[2]/div[2]/div[4]/input[1]'
        ).click()
        time.sleep(1)

        session_id = browser.current_url.split('apisession=')[1][:8]
        logger.info(f"Session ID obtained: {session_id}")
        return session_id

    finally:
        browser.quit()   # always release Chrome, even on error


# ===========================================================================
# Data download helpers
# ===========================================================================

def get_nifty_open_value(breeze, expiry_date: str, end_date: str) -> int:
    """
    Fetch the daily candle for Nifty on expiry day and return the opening
    value rounded to the nearest 50.
    """
    while True:
        _rate_limiter.wait()
        try:
            result = breeze.get_historical_data(
                interval="1day",
                from_date=expiry_date,
                to_date=end_date,
                stock_code="NIFTY",
                exchange_code="NSE",
                product_type="cash",
                expiry_date="",
                right="",
                strike_price=""
            )
            open_price = float(result['Success'][0]['open'])
            nf_value   = STRIKE_STEP * round(open_price / STRIKE_STEP)
            logger.info(f"Nifty open on expiry: {open_price} → rounded to {nf_value}")
            return nf_value
        except Exception as e:
            logger.warning(f"Retrying get_nifty_open_value: {e}")
            continue


def download_option(breeze, start_date: str, end_date: str, expiry_date: str,
                    right: str, strike: int, filepath: str):
    """
    Download 1-minute option data for a single strike/right and save to CSV.
    Retries indefinitely on failure (preserving original behaviour).
    Empty files (no data rows) are deleted after saving.
    """
    while True:
        _rate_limiter.wait()
        try:
            result = breeze.get_historical_data(
                interval="1minute",
                from_date=start_date,
                to_date=end_date,
                stock_code="NIFTY",
                exchange_code="NFO",
                product_type="options",
                expiry_date=expiry_date,
                right=right,
                strike_price=str(strike)
            )
            df = pd.DataFrame.from_dict(result['Success'])
            if df.empty:
                logger.debug(f"  No data for {os.path.basename(filepath)} – skipping")
                return
            df.to_csv(filepath, index=False)
            logger.info(f"  Saved {len(df)} rows → {os.path.basename(filepath)}")
            return
        except Exception as e:
            logger.warning(f"  Retrying {os.path.basename(filepath)}: {e}")
            continue


def download_expiry(breeze, contracts_list_df: pd.DataFrame, i: int):
    """
    Download all CE and PE contracts for expiry at index i in contracts_list_df.
    """
    expiry_date = contracts_list_df['expiry_date'][i]
    start_date  = contracts_list_df['start_date'][i]
    end_date    = contracts_list_df['end_date'][i]
    expiry_str  = expiry_date[:10]

    logger.info(f"Processing expiry: {expiry_str}  (data from {start_date[:10]})")

    # Create expiry directory (safe if it already exists)
    expiry_dir = os.path.join(NIFTY_DIR, expiry_str)
    os.makedirs(expiry_dir, exist_ok=True)

    # Get Nifty opening value on expiry day
    nf_value      = get_nifty_open_value(breeze, expiry_date, end_date)
    upper_strikes = nf_value + STRIKE_RANGE
    lower_strikes = nf_value - STRIKE_RANGE

    # Build full list of strikes: ATM first, then above, then below
    strikes_above = list(range(nf_value,          upper_strikes + STRIKE_STEP, STRIKE_STEP))
    strikes_below = list(range(nf_value - STRIKE_STEP, lower_strikes - STRIKE_STEP, -STRIKE_STEP))
    all_strikes   = strikes_above + strikes_below

    total   = len(all_strikes) * 2   # CE + PE per strike
    done    = 0

    for strike in all_strikes:
        for right, suffix in [("call", "ce"), ("put", "pe")]:
            filepath = os.path.join(expiry_dir, f"{strike}{suffix}.csv")
            logger.info(f"  → {strike}{suffix.upper()}")
            download_option(breeze, start_date, end_date,
                            expiry_date, right, strike, filepath)
            done += 1
            logger.info(f"  Progress: {done}/{total} contracts")

    logger.info(f"Expiry {expiry_str} complete – {done} contracts downloaded.")
    slack_bot_sendtext(
        f"Nifty options download complete – expiry {expiry_str} "
        f"({done} contracts, ~{_rate_limiter.daily_count} API calls used today)",
        SLACK_DATA_CHANNEL
    )


# ===========================================================================
# Main
# ===========================================================================

if __name__ == "__main__":

    # --- Fetch session ID via Selenium ---
    try:
        session_id = get_session_id(apiKey, userName, passWord, totpKey)
    except Exception as e:
        logger.error(f"Failed to obtain session ID: {e}")
        slack_bot_sendtext(
            f"🚨 *Nifty Data Downloader* – Selenium login failed: {e}",
            SLACK_ERROR_CHANNEL
        )
        sys.exit(1)

    # Save updated session ID back to credentials file
    user_credentials_dict['sessionID'][0] = session_id
    pd.DataFrame.from_dict(user_credentials_dict).to_csv(
        os.path.join(DATA_DIR, 'user_credentials_icici.csv'), index=False)

    # --- Authenticate with Breeze ---
    try:
        breeze = BreezeConnect(api_key=apiKey)
        breeze.generate_session(api_secret=secretKey, session_token=session_id)
        breeze.get_customer_details(api_session=session_id)
        logger.info("Breeze authentication successful.")
    except Exception as e:
        logger.error(f"Breeze authentication failed: {e}")
        slack_bot_sendtext(
            f"🚨 *Nifty Data Downloader* – Breeze authentication failed: {e}",
            SLACK_ERROR_CHANNEL
        )
        sys.exit(1)

    # --- Load contract list ---
    contracts_list_df = pd.read_csv(os.path.join(CONFIG_DIR, 'options_list_nf.csv'))

    # Identify range of expiries to download:
    # from first un-downloaded row up to (but not including) future expiries
    try:
        df_index_start = int(
            contracts_list_df[contracts_list_df.download_status == False].index[0])
    except IndexError:
        logger.info("No pending contracts to download.")
        sys.exit(0)

    df_index_end = df_index_start
    while (df_index_end < len(contracts_list_df) and
           datetime.strptime(contracts_list_df.iloc[df_index_end].iloc[2][:19],
                             '%Y-%m-%dT%H:%M:%S') < datetime.now()):
        df_index_end += 1

    if df_index_start == df_index_end:
        logger.info("No expired pending contracts to download.")
        sys.exit(0)

    logger.info(f"Downloading {df_index_end - df_index_start} expiry(s) "
                f"[rows {df_index_start} → {df_index_end - 1}]")

    # --- Download each pending expiry ---
    for i in range(df_index_start, df_index_end):
        try:
            download_expiry(breeze, contracts_list_df, i)
        except SystemExit:
            raise   # daily limit hit — propagate
        except Exception as e:
            logger.error(f"Unexpected error for expiry row {i}: {e}")
            slack_bot_sendtext(
                f"🚨 *Nifty Data Downloader* – Unexpected error on expiry row {i}: {e}",
                SLACK_ERROR_CHANNEL
            )
            continue

        # Mark expiry as downloaded and persist immediately
        contracts_list_df.iloc[i, 3] = True
        contracts_list_df.to_csv(os.path.join(CONFIG_DIR, 'options_list_nf.csv'), index=False)
        logger.info(f"Row {i} marked as downloaded in options_list_nf.csv")

    logger.info("=== All done ===")