"""
Live tracker for NSE's Closing Auction Session (CAS) Market Watch feed.

NSE publishes the auction's evolving state (indicative equilibrium price,
imbalance quantities, final matched price/quantity once the auction closes)
via a public JSON endpoint behind its Market Watch page
(https://www.nseindia.com/market-data/closing-auction-session). This script
polls that endpoint through the auction window and logs one row per
(poll, symbol) so the auction's evolution can be reconstructed after the
fact, not just its final snapshot.

Endpoint discovered by reading the page's own JS bundle (no NSE API docs
exist for this) — see WATCH_URL below. Session/cookie handling reuses the
warm-up technique already proven in ../../swing-trading-lab/data_pipeline/
bhavcopy/nse_session.py (Akamai sets cookies on a homepage hit even though
that request itself 403s; a follow-up hit to a real page completes the set).
Duplicated here in miniature rather than cross-imported, since delos only
deploys this repo, not swing-trading-lab.

Research-only / observational. Does not feed any live strategy.
"""

import os
import time as time_module
import logging
from datetime import datetime, date, time as dtime

import requests
import pandas as pd

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

if os.uname().nodename == 'delos':
    os.chdir('/home/parijnan/scripts/algo-trading-lab/data_pipeline/')

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT      = os.path.dirname(BASE_DIR)
HOLIDAYS_FILE  = os.path.join(REPO_ROOT, "data", "holidays.csv")
OUTPUT_DIR     = os.path.join(BASE_DIR, "data", "cas_market_watch")

# ---------------------------------------------------------------------------
# Auction window
# ---------------------------------------------------------------------------
WATCH_START      = dtime(15, 14)   # 1 min before continuous trading ends
WATCH_END        = dtime(15, 36)   # 1 min past the latest observed confirmation deadline (15:35)
POLL_INTERVAL_SEC = 15

WATCH_URL = "https://www.nseindia.com/api/NextApi/apiClient/casApi?functionName=getCASData"

# Nested per-symbol order-book depth — excluded from the flat CSV on purpose,
# everything else in the API response is kept.
ROW_FIELDS = [
    "symbol", "series", "refrencePrice", "prevClose", "upperBand", "lowerBand",
    "IEP", "change", "perChange", "finalPrice", "finalValue", "finalQuantity",
    "iiqAtEP", "iiqAtMO", "atoBuyQuantity", "atoSellQuantity",
    "totalBuyQuantity", "totalSellQuantity", "totTradedQty",
    "lastTradedPrice", "avgTrdPrice", "openPrice", "highPrice", "lowPrice",
    "lastUpdateTime",
]

_SESSION_HEADERS = {
    "Connection": "keep-alive",
    "Cache-Control": "max-age=0",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.9",
}
_WARMUP_URLS = ("https://www.nseindia.com", "https://www.nseindia.com/option-chain")


def new_nse_session() -> requests.Session:
    """A requests.Session pre-loaded with valid NSE/Akamai cookies (no Selenium needed)."""
    session = requests.Session()
    session.headers.update(_SESSION_HEADERS)
    session.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    for url in _WARMUP_URLS:
        session.get(url, timeout=10)
    session.headers["Accept"] = "application/json, text/plain, */*"
    session.headers["Referer"] = "https://www.nseindia.com/market-data/closing-auction-session"
    return session


def is_trading_day(today: date) -> bool:
    if today.weekday() >= 5:
        return False
    if os.path.exists(HOLIDAYS_FILE):
        holidays_df = pd.read_csv(HOLIDAYS_FILE, parse_dates=["date"])
        holidays = set(pd.to_datetime(holidays_df["date"]).dt.date)
        if today in holidays:
            return False
    else:
        logger.warning("holidays.csv not found — proceeding without a holiday check.")
    return True


def fetch_cas_snapshot(session: requests.Session) -> dict | None:
    try:
        r = session.get(WATCH_URL, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"CAS fetch failed: {e}")
        return None


def append_snapshot(filepath: str, poll_ts: datetime, payload: dict):
    status = payload.get("status")
    status_msg = payload.get("statusMsg")
    rows = []
    for row in payload.get("data", []):
        record = {"poll_timestamp": poll_ts.isoformat(sep=" "),
                   "status": status, "statusMsg": status_msg}
        for field in ROW_FIELDS:
            record[field] = row.get(field)
        rows.append(record)

    if not rows:
        return

    df = pd.DataFrame(rows)
    write_header = not os.path.exists(filepath)
    df.to_csv(filepath, mode="a", header=write_header, index=False)


def run():
    today = datetime.now().date()
    if not is_trading_day(today):
        logger.info(f"{today} is not a trading day. Exiting.")
        return

    now = datetime.now().time()
    if now > WATCH_END:
        logger.info(f"Already past the CAS window ({WATCH_END}). Exiting.")
        return
    if now < WATCH_START:
        wait_sec = (datetime.combine(today, WATCH_START) - datetime.now()).total_seconds()
        logger.info(f"Waiting {wait_sec:.0f}s for CAS window to start at {WATCH_START}.")
        time_module.sleep(max(0, wait_sec))

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(OUTPUT_DIR, f"{today.isoformat()}.csv")

    session = new_nse_session()
    logger.info(f"Session warmed up. Polling {WATCH_URL} every {POLL_INTERVAL_SEC}s until {WATCH_END}.")

    poll_count = 0
    while datetime.now().time() <= WATCH_END:
        poll_ts = datetime.now()
        payload = fetch_cas_snapshot(session)
        if payload is None:
            # Akamai cookies can expire mid-session; re-warm and retry once.
            logger.info("Re-warming NSE session after fetch failure.")
            session = new_nse_session()
            payload = fetch_cas_snapshot(session)

        if payload is not None:
            append_snapshot(filepath, poll_ts, payload)
            poll_count += 1
            if poll_count % 10 == 0:
                logger.info(f"{poll_count} polls logged "
                            f"(status={payload.get('status')}, symbols={len(payload.get('data', []))}).")

        time_module.sleep(POLL_INTERVAL_SEC)

    logger.info(f"CAS window ended. {poll_count} total polls logged to {filepath}.")


if __name__ == "__main__":
    run()
