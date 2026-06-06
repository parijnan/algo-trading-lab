
import os
import time
import logging
import pandas as pd
from datetime import datetime, timedelta
from io import StringIO
from urllib.request import urlopen
from pyotp import TOTP
from SmartApi import SmartConnect

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
# Constants & Paths
# ---------------------------------------------------------------------------
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
DATA_PIPELINE_DIR = os.path.join(BASE_DIR, "data")
REPO_ROOT    = os.path.dirname(BASE_DIR)
# TEMP DIRECTORY FOR ANGEL ONE DATA
NIFTY_TEMP_DIR = os.path.join(DATA_PIPELINE_DIR, "nifty", "temp")
CRED_FILE    = os.path.join(REPO_ROOT, "data", "user_credentials.csv")

_SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

MARKET_OPEN     = "09:15"
MARKET_CLOSE    = "15:30"
CANDLE_DATE_FMT  = "%Y-%m-%d %H:%M"
OHLCV_HEADERS   = ["time_stamp", "open", "high", "low", "close", "volume"]

# TARGET EXPIRIES
# Each entry may include:
#   "start" : datetime — earliest candle date to fetch (default: 2026-05-11)
#   "strike_lo" / "strike_hi" : int — strike filter bounds (default: 20000 / 26000)
#
# Previously downloaded (already in temp/):
#   05MAY2026  (sell leg for Apr-27 entry)
#   26MAY2026  (buy leg for Apr-27 / May-04 entries; sell leg for May-18 entry)
#
TARGET_EXPIRIES = [
    # Sell leg for Jun-01 entry (need data from Jun-01 onwards only)
    {"str": "09JUN2026", "dir": "2026-06-09", "label": "09-JUN-26",
     "start": datetime(2026, 6, 1, 9, 15)},
    # Buy leg for May-11, May-18, May-25, Jun-01 entries
    # PE wing at 0.05 delta lands ~21,600–22,300 — lower bound must reach 20,000
    {"str": "30JUN2026", "dir": "2026-06-30", "label": "30-JUN-26",
     "start": datetime(2026, 5, 11, 9, 15)},
]

# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
def login():
    creds = pd.read_csv(CRED_FILE).iloc[0]
    obj = SmartConnect(api_key=creds['api_key'])
    totp = TOTP(creds['qr_code']).now()
    obj.generateSession(creds['user_name'], str(creds['password']), totp)
    return obj

# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def fetch_candles(obj, token, from_dt, to_dt, max_retries=5):
    params = {
        "exchange": "NFO",
        "symboltoken": token,
        "interval": "ONE_MINUTE",
        "fromdate": from_dt.strftime(CANDLE_DATE_FMT),
        "todate": to_dt.strftime(CANDLE_DATE_FMT)
    }
    for attempt in range(max_retries):
        try:
            response = obj.getCandleData(params)
            if response.get("status") and response.get("data"):
                df = pd.DataFrame(response["data"], columns=OHLCV_HEADERS)
                df['time_stamp'] = pd.to_datetime(df['time_stamp']).dt.tz_localize(None)
                return df
            return pd.DataFrame()
        except Exception as e:
            err = str(e).lower()
            if 'rate' in err or 'access denied' in err or 'exceeded' in err:
                wait = 60 * (attempt + 1)
                logger.warning(f"Rate limited (token {token}), waiting {wait}s [attempt {attempt+1}/{max_retries}]...")
                time.sleep(wait)
            else:
                logger.error(f"Error fetching {token}: {e}")
                return pd.DataFrame()
    logger.error(f"Max retries exceeded for token {token}")
    return pd.DataFrame()

def main():
    obj = login()
    logger.info("Downloading scrip master...")
    scrip_df = pd.read_json(StringIO(urlopen(_SCRIP_MASTER_URL).read().decode()))
    
    for target in TARGET_EXPIRIES:
        target_expiry_str = target["str"]
        target_expiry_dir = target["dir"]
        target_label      = target["label"]
        
        logger.info(f"=== Processing Expiry: {target_expiry_str} ===")
        
        nifty_df = scrip_df[
            (scrip_df['exch_seg'] == 'NFO') &
            (scrip_df['name'] == 'NIFTY') &
            (scrip_df['expiry'] == target_expiry_str)
        ].copy()
        
        # Strike range: wide enough to capture PE wing (0.05 delta monthly ~21,600–22,300)
        strike_lo = target.get("strike_lo", 20000)
        strike_hi = target.get("strike_hi", 26000)
        nifty_df['strike_val'] = pd.to_numeric(nifty_df['strike']) / 100
        target_contracts = nifty_df[
            (nifty_df['strike_val'] >= strike_lo) &
            (nifty_df['strike_val'] <= strike_hi)
        ].copy()
        
        logger.info(f"Downloading {len(target_contracts)} contracts for {target_expiry_str}...")
        
        out_dir = os.path.join(NIFTY_TEMP_DIR, target_expiry_dir)
        os.makedirs(out_dir, exist_ok=True)
        
        start_dt = target.get("start", datetime(2026, 5, 11, 9, 15))
        end_dt   = datetime.now()
        
        for _, row in target_contracts.iterrows():
            token = row['token']
            strike = int(row['strike_val'])
            otype = row['symbol'][-2:].lower() # CE or PE
            filename = f"{strike}{otype}.csv"
            filepath = os.path.join(out_dir, filename)
            
            # Skip if already exists and has data
            if os.path.exists(filepath) and os.path.getsize(filepath) > 100:
                logger.debug(f"Skipping {filename}, already exists.")
                continue
                
            logger.info(f"Processing {filename} (Token: {token})...")
            
            # Fetch in 2-day chunks to be safe with Angel's 1000 record limit
            all_frames = []
            curr = start_dt
            while curr < end_dt:
                chunk_end = min(curr + timedelta(days=2), end_dt)
                df = fetch_candles(obj, token, curr, chunk_end)
                if not df.empty:
                    all_frames.append(df)
                curr = chunk_end + timedelta(minutes=1)
                time.sleep(1.0)  # Rate limit safety
                
            if all_frames:
                combined = pd.concat(all_frames).drop_duplicates(subset=['time_stamp'])
                combined = combined.sort_values('time_stamp')
                combined = combined.rename(columns={'time_stamp': 'datetime'})
                combined['stock_code'] = 'NIFTY'
                combined['exchange_code'] = 'NFO'
                combined['product_type'] = 'Options'
                combined['expiry_date'] = target_label
                combined['right'] = 'Call' if otype == 'ce' else 'Put'
                combined['strike_price'] = strike
                combined['open_interest'] = 0
                combined['count'] = 0
                
                cols = ['datetime', 'stock_code', 'exchange_code', 'product_type', 'expiry_date', 
                        'right', 'strike_price', 'open', 'high', 'low', 'close', 'volume', 'open_interest', 'count']
                combined = combined[cols]
                
                combined.to_csv(filepath, index=False)
                logger.info(f"Saved {len(combined)} rows to {filepath}")
            else:
                logger.warning(f"No data for {filename}")

if __name__ == "__main__":
    main()
