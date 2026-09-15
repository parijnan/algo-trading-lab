"""
Fyers-sourced MCX historical backfill downloader (plan §2,
plans/fyers-mcx-data-integration.md) -- deep historical 1-min data for
CRUDEOILM and CRUDEOIL, going back further than Angel One safely allows
(§0's own rationale: Angel One's getCandleData silently mislabels
pre-front-month history under the wrong contract's token, so
data_downloader_mcx.py deliberately never backfills into the past).

**Writes to a SEPARATE staging tree, data_pipeline/data/mcx_fyers/, NOT the
live data_pipeline/data/mcx/ tree Prometheus and the backtest pipeline
actually read.** Per plan §2.4's "store separately until confidence is
higher" option -- §1's validation passed, but actually merging Fyers data
into the same files live production depends on is a distinct, deliberate,
higher-stakes step (especially given §1.3's found zero-volume-placeholder-
bar convention difference, not yet handled by load_futures_1min()) that
deserves its own explicit go-ahead, not something to fold silently into
building the downloader itself.

Workflow (confirmed directly against Fyers's docs + live symbol master,
2026-09-15 -- see plan §1.3/§2.1 for the full research trail):
  1. Get Expiry Dates  -- GET .../data/history/fno/expired/expiry-dates
     `symbol` must be a real, currently-tradeable contract for the
     underlying (a bare "MCX:CRUDEOILM" is rejected) -- any live contract
     works, Fyers resolves the underlying internally.
  2. Get Expired Contracts -- GET .../data/history/fno/expired/underlying-symbols
  3. Get Expired F&O Data -- GET .../data/history/fno/expired/historical-data
     Up to 100 days per request for 1-min resolution; chunked here anyway
     since Fyers's data floor for MCX is 2022 and older contracts, or a
     deliberately wide range, could exceed that.

Auth: uses the access_token already in data/user_credentials.csv (from the
one-time manual OAuth flow, plan §2.1). The headless daily-login script
(plan §2.3) is NOT built yet -- blocked on enabling External 2FA TOTP on
the account (plan §2.5) -- so this script is currently a manually-invoked
backfill tool, not yet a cron job. Re-run the manual OAuth flow to refresh
the token once it expires (~midnight IST daily, per the token's own `exp`
claim).

Usage (run from repo root):
  python data_pipeline/data_downloader_fyers_mcx.py --instrument CRUDEOILM --months-back 6
  python data_pipeline/data_downloader_fyers_mcx.py --instrument CRUDEOIL --months-back 6
  python data_pipeline/data_downloader_fyers_mcx.py --instrument CRUDEOILM --months-back 6 --dry-run
"""
import argparse
import csv
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGING_DIR = Path(__file__).parent / 'data' / 'mcx_fyers'
CREDS_FILE = REPO_ROOT / 'data' / 'user_credentials.csv'

OHLCV_HEADERS = ['time_stamp', 'open', 'high', 'low', 'close', 'volume']   # matches data_downloader_mcx.py exactly
IST = timezone(timedelta(hours=5, minutes=30))

# Current live front-month contract per underlying, as of 2026-09-15 --
# used only as the "any real contract" symbol Get Expiry Dates needs to
# resolve the underlying. Update if this script is still in use once these
# have rolled -- picking any OTHER still-valid contract for the same
# underlying works identically, this isn't a hardcoded data dependency.
FRONT_MONTH_ANCHOR = {
    'CRUDEOILM': 'MCX:CRUDEOILM26OCTFUT',
    'CRUDEOIL': 'MCX:CRUDEOIL26OCTFUT',
}

HIST_DATA_CHUNK_DAYS = 90   # under the documented 100-day-per-request limit, with margin

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
                     datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rate limiting -- Fyers: 10/sec, 200/min (Standard plan). Same sliding-
# window pattern as data_downloader_mcx.py's own RateLimiter, sized well
# under Fyers's documented ceiling since headroom costs nothing here.
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, per_second: int = 5, per_minute: int = 100):
        self.per_second = per_second
        self.per_minute = per_minute
        self._calls_sec = deque()
        self._calls_min = deque()

    def _evict(self, window: deque, cutoff: float):
        while window and window[0] < cutoff:
            window.popleft()

    def wait(self):
        now = time.time()
        self._evict(self._calls_sec, now - 1)
        self._evict(self._calls_min, now - 60)
        if len(self._calls_sec) >= self.per_second:
            time.sleep(max(0.0, 1 - (now - self._calls_sec[0])))
        if len(self._calls_min) >= self.per_minute:
            time.sleep(max(0.0, 60 - (now - self._calls_min[0])))
        now = time.time()
        self._calls_sec.append(now)
        self._calls_min.append(now)


_rate_limiter = RateLimiter()


# ---------------------------------------------------------------------------
# Auth / low-level HTTP
# ---------------------------------------------------------------------------
def _creds() -> dict:
    with open(CREDS_FILE, newline='') as f:
        return next(csv.DictReader(f))


def _auth_header() -> str:
    creds = _creds()
    app_id = creds.get('fyers_app_id')
    token = creds.get('fyers_access_token')
    if not app_id or not token:
        raise RuntimeError(
            f'No fyers_app_id/fyers_access_token in {CREDS_FILE} -- run the manual OAuth flow '
            f'first (plan §2.1/§2.5). The headless daily-login script (§2.3) is not built yet.'
        )
    return f'{app_id}:{token}'


def _get(url: str, params: dict) -> dict:
    _rate_limiter.wait()
    full_url = f'{url}?{urllib.parse.urlencode(params)}'
    req = urllib.request.Request(full_url, headers={'Authorization': _auth_header(), 'User-Agent': _UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            logger.error(f'HTTP {e.code} for {full_url}: {body}')
            raise


# ---------------------------------------------------------------------------
# Fyers expired-contract workflow (plan §1.3's validated findings baked in)
# ---------------------------------------------------------------------------
def get_expiry_dates(anchor_symbol: str, range_from: str, range_to: str) -> list:
    r = _get('https://api-t1.fyers.in/data/history/fno/expired/expiry-dates', {
        'symbol': anchor_symbol, 'range_from': range_from, 'range_to': range_to, 'date_format': 1,
    })
    if r.get('s') != 'ok':
        logger.error(f'Get Expiry Dates failed: {r}')
        return []
    return r.get('data', {}).get('expiry_dates', {}).get('futures', [])


def get_expired_contract_symbol(anchor_symbol: str, expiry_date: str) -> str | None:
    r = _get('https://api-t1.fyers.in/data/history/fno/expired/underlying-symbols', {
        'symbol': anchor_symbol, 'expiry_date': expiry_date,
    })
    if r.get('s') != 'ok':
        logger.error(f'Get Expired Contracts failed for {expiry_date}: {r}')
        return None
    contracts = r.get('data', {}).get('contracts', {}).get('futures', [])
    if len(contracts) != 1:
        logger.warning(f'Expected 1 futures contract for expiry={expiry_date}, got {contracts}')
    return contracts[0] if contracts else None


def get_expired_historical_data(contract_symbol: str, range_from: str, range_to: str) -> pd.DataFrame:
    """Chunked at HIST_DATA_CHUNK_DAYS, well under the documented 100-day
    limit. Real MCX contract listing windows have been ~1 month in every
    case checked so far, so this rarely needs more than one chunk -- but a
    deliberately wide range_from/range_to (e.g. re-backfilling far history)
    could exceed 100 days, so chunk unconditionally rather than assume."""
    start = datetime.strptime(range_from, '%Y-%m-%d')
    end = datetime.strptime(range_to, '%Y-%m-%d')
    all_rows = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=HIST_DATA_CHUNK_DAYS - 1), end)
        r = _get('https://api-t1.fyers.in/data/history/fno/expired/historical-data', {
            'symbol': contract_symbol, 'resolution': '1', 'date_format': 1,
            'range_from': chunk_start.strftime('%Y-%m-%d'), 'range_to': chunk_end.strftime('%Y-%m-%d'),
        })
        if r.get('s') == 'ok':
            all_rows.extend(r.get('candles', []))
        elif r.get('s') == 'no_data':
            pass   # documented possible response, not an error -- just nothing in this chunk
        else:
            logger.error(f'Get Expired F&O Data failed for {contract_symbol} '
                        f'[{chunk_start.date()} -> {chunk_end.date()}]: {r}')
        chunk_start = chunk_end + timedelta(days=1)

    if not all_rows:
        return pd.DataFrame(columns=OHLCV_HEADERS)

    df = pd.DataFrame(all_rows, columns=['epoch', 'open', 'high', 'low', 'close', 'volume'])
    df['time_stamp'] = df['epoch'].apply(
        lambda e: datetime.fromtimestamp(e, tz=IST).strftime('%Y-%m-%d %H:%M:%S+05:30'))
    return df[OHLCV_HEADERS].drop_duplicates(subset='time_stamp').sort_values('time_stamp').reset_index(drop=True)


# ---------------------------------------------------------------------------
# Storage -- staging tree, mirrors data_downloader_mcx.py's own naming
# (<expiry:%Y-%m-%d>_futures.csv) so a later merge into the live tree is a
# straight file-level operation, not a reformatting job.
# ---------------------------------------------------------------------------
def get_staging_filepath(instrument: str, expiry_date: str) -> Path:
    d = STAGING_DIR / instrument
    d.mkdir(parents=True, exist_ok=True)
    return d / f'{expiry_date}_futures.csv'


def save_contract(instrument: str, expiry_date: str, df: pd.DataFrame, dry_run: bool) -> int:
    filepath = get_staging_filepath(instrument, expiry_date)
    if dry_run:
        logger.info(f'  [dry-run] would write {len(df)} rows to {filepath}')
        return len(df)
    df.to_csv(filepath, index=False)
    logger.info(f'  wrote {len(df)} rows -> {filepath}')
    return len(df)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def backfill_instrument(instrument: str, months_back: int, dry_run: bool) -> None:
    anchor = FRONT_MONTH_ANCHOR[instrument]
    today = datetime.now(IST).date()
    # 2026-09-15 finding: Get Expiry Dates rejects range_to == today (or later)
    # -- {'code':-50,'data':{'range_to':'range_to cannot be current date or a
    # future date'}}. Use yesterday as the effective "now" for this bound.
    range_from = (today - timedelta(days=months_back * 31)).strftime('%Y-%m-%d')
    range_to = (today - timedelta(days=1)).strftime('%Y-%m-%d')

    logger.info(f'=== {instrument}: resolving expiries {range_from} -> {range_to} (anchor {anchor}) ===')
    expiries = get_expiry_dates(anchor, range_from, range_to)
    logger.info(f'{instrument}: {len(expiries)} expired futures contract(s) found: {expiries}')

    for expiry_date in expiries:
        existing = get_staging_filepath(instrument, expiry_date)
        if existing.exists():
            logger.info(f'{instrument} {expiry_date}: staging file already exists, skipping '
                        f'({existing}). Delete it first to force a re-fetch.')
            continue

        contract_symbol = get_expired_contract_symbol(anchor, expiry_date)
        if not contract_symbol:
            logger.error(f'{instrument} {expiry_date}: could not resolve a contract symbol, skipping.')
            continue

        # Fetch the contract's real listing window: from ~2 months before
        # expiry (generous; actual real windows seen so far are ~1 month)
        # through the expiry date itself. Fyers returns no_data / empty
        # gracefully outside the real window, so an over-wide guess costs
        # a slightly larger request, not incorrect data.
        expiry_dt = datetime.strptime(expiry_date, '%Y-%m-%d')
        hist_from = (expiry_dt - timedelta(days=60)).strftime('%Y-%m-%d')
        hist_to = expiry_date

        logger.info(f'{instrument} {expiry_date}: fetching {contract_symbol}, {hist_from} -> {hist_to} ...')
        df = get_expired_historical_data(contract_symbol, hist_from, hist_to)
        if df.empty:
            logger.warning(f'{instrument} {expiry_date}: no candles returned for {contract_symbol} -- skipping.')
            continue

        save_contract(instrument, expiry_date, df, dry_run)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--instrument', required=True, choices=['CRUDEOILM', 'CRUDEOIL'])
    p.add_argument('--months-back', type=int, default=6,
                    help='How many months of expired contracts to backfill (default: 6)')
    p.add_argument('--dry-run', action='store_true', help='Fetch and log, but do not write any files')
    args = p.parse_args()

    logger.info(f'Staging output directory: {STAGING_DIR} (NOT the live data_pipeline/data/mcx/ tree)')
    backfill_instrument(args.instrument, args.months_back, args.dry_run)
    logger.info('Done.')


if __name__ == '__main__':
    main()
