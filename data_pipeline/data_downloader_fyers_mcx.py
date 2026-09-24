"""
Fyers-sourced MCX historical backfill downloader (plan §2,
plans/fyers-mcx-data-integration.md) -- deep historical 1-min data for
any enabled MCX underlying (data_pipeline/config/mcx_underlyings.csv),
going back further than Angel One safely allows (§0's own rationale:
Angel One's getCandleData silently mislabels pre-front-month history
under the wrong contract's token, so data_downloader_mcx.py deliberately
never backfills into the past).

Originally built (2026-09-15) for CRUDEOILM/CRUDEOIL only, with a
hardcoded per-instrument anchor-contract dict. Generalized (2026-09-16) to
every enabled underlying in mcx_underlyings.csv (energy, base metals,
precious metals) by resolving each instrument's anchor contract
dynamically from Fyers's own public MCX symbol master
(https://public.fyers.in/sym_details/MCX_COM_sym_master.json, first
referenced in plan §1.3) instead of hand-maintaining a stale symbol string
per instrument -- see resolve_anchor_symbol() below.

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
  python data_pipeline/data_downloader_fyers_mcx.py --instrument ALL --months-back 6
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
UNDERLYINGS_FILE = Path(__file__).parent / 'config' / 'mcx_underlyings.csv'

OHLCV_HEADERS = ['time_stamp', 'open', 'high', 'low', 'close', 'volume']   # matches data_downloader_mcx.py exactly
IST = timezone(timedelta(hours=5, minutes=30))

# Fyers's own public MCX symbol master -- covers every underlying, not just
# the two this script originally hardcoded. Used only to resolve one "any
# real, currently-tradeable contract" anchor symbol per underlying (what
# Get Expiry Dates needs to resolve the underlying internally); no auth
# required, cached to disk since it's ~18MB (2026-09-16).
SYMBOL_MASTER_URL = 'https://public.fyers.in/sym_details/MCX_COM_sym_master.json'
SYMBOL_MASTER_CACHE = Path(__file__).parent / 'data' / 'mcx_com_sym_master.json'
SYMBOL_MASTER_MAX_AGE_HOURS = 12   # contract listings don't change intraday; avoid refetching 18MB every invocation

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


class FyersAuthExpiredError(RuntimeError):
    """Fyers rejected a request because the access_token is invalid/expired
    (confirmed empirically, 2026-09-16: code -16, 'Could not authenticate the
    user'). Not resumable within this process -- needs a fresh manual OAuth
    token (plan §2.1/§2.5's flow). Deliberately distinct from a generic
    per-instrument failure so main() can stop the whole batch immediately
    instead of burning through every remaining instrument with the same
    doomed call."""


class FyersRateLimitError(RuntimeError):
    """Fyers rejected a request as rate-limited (HTTP 429, or an equivalent
    error code/message in a 200 JSON body). Documented ceilings are generous
    (10/sec, 200/min, 100,000/day, plan §0) and this script's own
    RateLimiter already sits under them (5/sec, 100/min) -- a real hit here
    is unexpected, but caught explicitly rather than left to masquerade as
    a per-chunk data error."""


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


def _check_fatal(body: dict, context: str) -> None:
    """Raises FyersAuthExpiredError/FyersRateLimitError for the two known
    unrecoverable-within-this-process conditions; leaves every other error
    shape (a genuine per-chunk/per-contract issue, e.g. no_data) for the
    caller's own existing per-call error handling."""
    if not isinstance(body, dict) or body.get('s') != 'error':
        return
    code = body.get('code')
    message = str(body.get('message', '')).lower()
    if code == -16 or 'authenticate' in message or 'invalid token' in message or 'token' in message and 'expired' in message:
        raise FyersAuthExpiredError(f'{context}: {body}')
    if code == 429 or 'rate limit' in message or 'request limit' in message or 'too many request' in message:
        raise FyersRateLimitError(f'{context}: {body}')


def _get(url: str, params: dict) -> dict:
    _rate_limiter.wait()
    full_url = f'{url}?{urllib.parse.urlencode(params)}'
    req = urllib.request.Request(full_url, headers={'Authorization': _auth_header(), 'User-Agent': _UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise FyersRateLimitError(f'HTTP 429 (rate limited) for {full_url}')
        raw = e.read().decode()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            logger.error(f'HTTP {e.code} for {full_url}: {raw}')
            raise
    _check_fatal(body, full_url)
    return body


# ---------------------------------------------------------------------------
# Underlying list + anchor-symbol resolution
# ---------------------------------------------------------------------------
def load_enabled_underlyings() -> list:
    with open(UNDERLYINGS_FILE, newline='') as f:
        return [r['name'] for r in csv.DictReader(f) if r['enabled'] == 'True']


def _fetch_symbol_master() -> dict:
    """Downloads (or reuses a fresh disk-cached copy of) Fyers's full MCX
    symbol master. No auth needed -- it's a public file -- but still needs a
    real User-Agent or Cloudflare silently blocks it (same gotcha found for
    the OAuth token exchange, plan §2.1's addendum #2)."""
    if SYMBOL_MASTER_CACHE.exists():
        age_hours = (time.time() - SYMBOL_MASTER_CACHE.stat().st_mtime) / 3600
        if age_hours < SYMBOL_MASTER_MAX_AGE_HOURS:
            with open(SYMBOL_MASTER_CACHE) as f:
                return json.load(f)

    logger.info(f'Fetching Fyers MCX symbol master ({SYMBOL_MASTER_URL}) ...')
    req = urllib.request.Request(SYMBOL_MASTER_URL, headers={'User-Agent': _UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    SYMBOL_MASTER_CACHE.parent.mkdir(parents=True, exist_ok=True)
    SYMBOL_MASTER_CACHE.write_bytes(raw)
    return json.loads(raw.decode())


def resolve_anchor_symbol(instrument: str) -> str:
    """Any real, currently-tradeable contract for `instrument` -- Get Expiry
    Dates resolves the underlying internally regardless of which specific
    contract is passed (plan §1.3). Picks the soonest-expiring contract that
    hasn't ALREADY expired from Fyers's own symbol master, rather than a
    hardcoded, staleness-prone string per instrument -- a contract expiring
    within days is a poor anchor since the cache backing `tradeStatus` can
    itself be up to SYMBOL_MASTER_MAX_AGE_HOURS stale, and an
    already-expired anchor would make Get Expiry Dates fail in a way that
    looks like an auth/API error rather than a stale-anchor one."""
    master = _fetch_symbol_master()
    now_epoch = time.time()
    candidates = [
        rec for rec in master.values()
        if rec.get('underSym') == instrument and rec.get('tradeStatus') == 1
        and rec.get('optType') == 'XX'   # 'XX' = futures; 'CE'/'PE' = options on the same underlying -- exclude
    ]
    if not candidates:
        raise RuntimeError(
            f'No active Fyers contract found for underlying {instrument!r} in the symbol master '
            f'({SYMBOL_MASTER_URL}) -- check the name matches mcx_underlyings.csv exactly.'
        )
    not_yet_expired = [rec for rec in candidates if int(rec['expiryDate']) > now_epoch]
    pool = not_yet_expired or candidates
    nearest = min(pool, key=lambda rec: int(rec['expiryDate']))
    return nearest['symTicker']


# ---------------------------------------------------------------------------
# Fyers expired-contract workflow (plan §1.3's validated findings baked in)
# ---------------------------------------------------------------------------
EXPIRY_DATES_CHUNK_DAYS = 365   # documented limit is 366 days per request; margin of 1

def get_expiry_dates(anchor_symbol: str, range_from: str, range_to: str) -> list:
    """2026-09-15 finding: Get Expiry Dates itself has an undocumented-until-
    you-hit-it 366-day range cap ({'code':-50,'data':{'range_to':'Date range
    cannot exceed 366 days'}}) -- a multi-year backfill request needs
    chunking here too, not just on the historical-data call. Chunked
    unconditionally so this never silently truncates a wide request."""
    start = datetime.strptime(range_from, '%Y-%m-%d')
    end = datetime.strptime(range_to, '%Y-%m-%d')
    all_expiries = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=EXPIRY_DATES_CHUNK_DAYS - 1), end)
        r = _get('https://api-t1.fyers.in/data/history/fno/expired/expiry-dates', {
            'symbol': anchor_symbol, 'range_from': chunk_start.strftime('%Y-%m-%d'),
            'range_to': chunk_end.strftime('%Y-%m-%d'), 'date_format': 1,
        })
        if r.get('s') == 'ok':
            all_expiries.extend(r.get('data', {}).get('expiry_dates', {}).get('futures', []))
        else:
            logger.error(f'Get Expiry Dates failed for [{chunk_start.date()} -> {chunk_end.date()}]: {r}')
        chunk_start = chunk_end + timedelta(days=1)
    return sorted(set(all_expiries))


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
def backfill_instrument(instrument: str, months_back: int, dry_run: bool, extend_days: int = 0) -> None:
    anchor = resolve_anchor_symbol(instrument)
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
        if existing.exists() and not extend_days:
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
        hist_from = (expiry_dt - timedelta(days=extend_days or 60)).strftime('%Y-%m-%d')
        hist_to = expiry_date

        # --extend-history-days (2026-09-24, SILVERMIC/Selene): the 60-day window above is
        # tuned for crude's ~1-month real listing life and leaves whole months uncovered
        # for a contract that lists 3-4 months ahead (SILVERMIC: Feb/Apr/Jun/Aug/Nov expiries
        # leave September and December with no data at all, and every early-roll week without
        # the next contract). For a staging file that already exists, fetch only the part
        # BEFORE what it already holds and merge, never re-download what's there.
        prior = None
        if existing.exists():
            prior = pd.read_csv(existing)
            first_have = pd.to_datetime(prior['time_stamp']).min().date()
            hist_to = (first_have - timedelta(days=1)).strftime('%Y-%m-%d')
            if hist_to < hist_from:
                logger.info(f'{instrument} {expiry_date}: already covers {hist_from} onward, nothing to extend.')
                continue

        logger.info(f'{instrument} {expiry_date}: fetching {contract_symbol}, {hist_from} -> {hist_to} ...')
        df = get_expired_historical_data(contract_symbol, hist_from, hist_to)
        if df.empty:
            logger.warning(f'{instrument} {expiry_date}: no candles returned for {contract_symbol} -- skipping.')
            continue

        if prior is not None:
            df = pd.concat([df, prior], ignore_index=True).drop_duplicates(subset='time_stamp') \
                   .sort_values('time_stamp').reset_index(drop=True)
        save_contract(instrument, expiry_date, df, dry_run)


def main():
    enabled = load_enabled_underlyings()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--instrument', required=True, choices=enabled + ['ALL'],
                    help="A single underlying, or ALL to backfill every enabled underlying "
                         "in data_pipeline/config/mcx_underlyings.csv")
    p.add_argument('--months-back', type=int, default=6,
                    help='How many months of expired contracts to backfill (default: 6)')
    p.add_argument('--extend-history-days', type=int, default=0,
                    help='Fetch each contract from this many days before expiry (default: off = 60-day window, '
                         'skip existing files). When set, existing staging files are extended backward, not skipped.')
    p.add_argument('--dry-run', action='store_true', help='Fetch and log, but do not write any files')
    args = p.parse_args()

    instruments = enabled if args.instrument == 'ALL' else [args.instrument]

    logger.info(f'Staging output directory: {STAGING_DIR} (NOT the live data_pipeline/data/mcx/ tree)')
    logger.info(f'Instruments: {instruments}')
    failed = []
    for i, instrument in enumerate(instruments):
        try:
            backfill_instrument(instrument, args.months_back, args.dry_run, args.extend_history_days)
        except (FyersAuthExpiredError, FyersRateLimitError) as e:
            remaining = instruments[i:]
            failed.extend(remaining)
            logger.error(f'{type(e).__name__}: {e}')
            logger.error(
                f'Stopping the run now -- not resumable within this process. '
                f'{len(remaining)} instrument(s) not yet attempted: {remaining}. '
                f'Every contract already written this run (or in a prior run) is skipped '
                f'automatically on re-run (see backfill_instrument\'s existing.exists() check), so '
                f'just re-run the same --instrument ALL command later (with a fresh access_token, '
                f'plan §2.1/§2.5\'s manual OAuth flow, if this was a token-expiry stop) to pick up '
                f'exactly where this run left off.'
            )
            break
        except Exception:
            logger.exception(f'{instrument}: backfill failed, continuing with remaining instruments.')
            failed.append(instrument)

    if failed:
        logger.error(f'Done, with {len(failed)} instrument(s) not completed: {failed}')
        sys.exit(1)
    else:
        logger.info('Done.')


if __name__ == '__main__':
    main()
