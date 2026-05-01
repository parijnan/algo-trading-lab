
import os
import time
import logging
import pandas as pd
from datetime import datetime, timedelta
from io import StringIO
from urllib.request import urlopen
from requests import post
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
TARGET_EXPIRIES = [
    {"str": "05MAY2026", "dir": "2026-05-05", "label": "05-MAY-26"},
    {"str": "26MAY2026", "dir": "2026-05-26", "label": "26-MAY-26"},
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
def fetch_candles(obj, token, from_dt, to_dt):
    params = {
        "exchange": "NFO",
        "symboltoken": token,
        "interval": "ONE_MINUTE",
        "fromdate": from_dt.strftime(CANDLE_DATE_FMT),
        "todate": to_dt.strftime(CANDLE_DATE_FMT)
    }
    try:
        response = obj.getCandleData(params)
        if response.get("status") and response.get("data"):
            df = pd.DataFrame(response["data"], columns=OHLCV_HEADERS)
            df['time_stamp'] = pd.to_datetime(df['time_stamp']).dt.tz_localize(None)
            return df
    except Exception as e:
        logger.error(f"Error fetching {token}: {e}")
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
        
        # Filter strikes: +/- 1500 points from current spot (~24000)
        nifty_df['strike_val'] = pd.to_numeric(nifty_df['strike']) / 100
        target_contracts = nifty_df[
            (nifty_df['strike_val'] >= 22500) & 
            (nifty_df['strike_val'] <= 25500)
        ].copy()
        
        logger.info(f"Downloading {len(target_contracts)} contracts for {target_expiry_str}...")
        
        out_dir = os.path.join(NIFTY_TEMP_DIR, target_expiry_dir)
        os.makedirs(out_dir, exist_ok=True)
        
        # April 20th to Today
        start_dt = datetime(2026, 4, 20, 9, 15)
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
                time.sleep(0.4) # Rate limit safety
                
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
