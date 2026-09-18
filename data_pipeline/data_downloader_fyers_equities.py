"""
data_pipeline/data_downloader_fyers_equities.py

Fyers-sourced Nifty/Sensex/India VIX index + Nifty/Sensex options downloader
(plan §7.2, plans/fyers-mcx-data-integration.md). Built 2026-09-17/18 after:
(a) a real AB1007 incident traced to data_downloader_angelone.py sharing
Prometheus's own AngelOne account (now permanently avoided -- this script
never touches AngelOne at all), and (b) a real silent-failure incident the
SAME night the AngelOne-merge fix first ran live: AngelOne's live
getCandleData returned clean-but-empty responses for ~376/382 Sensex option
contracts under sustained load, with no exception and no warning logged,
and the code marked the expiry complete anyway -- unretried, silently
incomplete. Confirmed directly that the missing data was still genuinely
available (both re-fetched live from AngelOne moments later AND recovered
in full from Fyers's expired-contract endpoint), which is what prompted
this rewrite: Fyers serves an EXPIRED contract's entire lifetime in one
bulk call (validated directly against the exact contract that failed --
366 Sensex contracts resolved, 4,044 real candles for one near-the-money
strike in a single request), not AngelOne's day-by-day live polling that
this whole incident class stems from.

**Runs on the LOCAL machine only, MANUALLY (weekly-ish, no cron)** -- same
as data_downloader_icicidirect.py, which this script's Nifty-options
coverage supersedes (ICICI retired from the crontab, not deleted -- kept
as a manual fallback). Fyers headless daily re-auth is confirmed blocked by
SEBI-driven platform policy (plan §2.3) -- there is no way to make this
unattended, so it isn't scheduled at all; the manual browser-OAuth refresh
this whole project already uses for the MCX-side Fyers work is the only
path, every time this runs.

**Output schema matches data_downloader_angelone.py's own files exactly**
(data/indices/{nifty,sensex,india_vix}{,_daily}.csv, data/sensex/<expiry>/
<strike><ce|pe>.csv) and data_downloader_icicidirect.py's own Nifty options
layout (data/nifty/options/<expiry>/<strike><ce|pe>.csv, config/
options_list_nf.csv) -- so nothing downstream needs to change to consume
this instead.

**No CAS gap-fill logic needed, unlike data_downloader_angelone.py's own
fill_missing_candles/extend_to_day_close.** Confirmed directly: Fyers's own
index feed already returns a flat carry-forward candle through the
15:16-15:27 auction window and the real terminal print at 15:28, with zero
missing timestamps (375 candles for a full CAS-era day, matching the
pre-CAS baseline count exactly) -- whatever synthesizes AngelOne's own gap
doesn't apply to Fyers's feed, so porting that logic here would be
solving a problem that doesn't exist on this data source.

**A real lesson from the AngelOne incident is built in from the start**:
an options-cycle's own download is only marked complete if the fraction of
contracts that actually returned data clears MIN_HIT_RATE. A suspiciously
low hit rate (the AngelOne incident's own 6/382 = 1.6%) is left pending
and logged loudly instead of silently marked done and never revisited.

Usage (run from repo root, local machine only):
  python data_pipeline/data_downloader_fyers_equities.py --indices
  python data_pipeline/data_downloader_fyers_equities.py --options
  python data_pipeline/data_downloader_fyers_equities.py --all
Requires a fresh data/user_credentials.csv fyers_access_token -- same
manual OAuth flow as data_downloader_fyers_mcx.py; refresh first if a run
fails with "token has expired".
"""
import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data_downloader_fyers_mcx import (  # noqa: E402
    _get, get_expired_historical_data, FyersAuthExpiredError, FyersRateLimitError,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
                     datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / 'data'
CONFIG_DIR = BASE_DIR / 'config'
INDICES_DIR = DATA_DIR / 'indices'
SENSEX_DIR = DATA_DIR / 'sensex'
NIFTY_OPTIONS_DIR = DATA_DIR / 'nifty' / 'options'

INDEX_TS_FMT = '%Y-%m-%d %H:%M:%S'
OPTIONS_TS_FMT = '%Y-%m-%dT%H:%M:%S'
OHLCV_HEADERS = ['time_stamp', 'open', 'high', 'low', 'close', 'volume']
INDEX_HEADERS = ['time_stamp', 'open', 'high', 'low', 'close', 'volume', 'oi']

# (display_name, Fyers symbol, 1-min filename, daily filename)
INDEX_INSTRUMENTS = [
    ('Nifty', 'NSE:NIFTY50-INDEX', 'nifty.csv', 'nifty_daily.csv'),
    ('Sensex', 'BSE:SENSEX-INDEX', 'sensex.csv', 'sensex_daily.csv'),
    ('India VIX', 'NSE:INDIAVIX-INDEX', 'india_vix.csv', 'india_vix_daily.csv'),
]

# (display_name, underlying name, symbol-master URL + exchange prefix,
# options staging dir, tracking config file) -- the anchor symbol itself is
# resolved dynamically (resolve_options_anchor below), NOT hardcoded: Get
# Expiry Dates rejects a bare underlying like "BSE:SENSEX" (confirmed
# directly, same finding as data_downloader_fyers_mcx.py's own
# resolve_anchor_symbol -- "any real, currently-tradeable contract" is
# required, not the underlying name itself).
OPTIONS_INSTRUMENTS = [
    ('Sensex', 'SENSEX', 'BSE_FO', SENSEX_DIR, CONFIG_DIR / 'options_list_sensex.csv'),
    ('Nifty', 'NIFTY', 'NSE_FO', NIFTY_OPTIONS_DIR, CONFIG_DIR / 'options_list_nf.csv'),
]

HIST_CHUNK_DAYS = 90        # regular History API, same conservative margin as the expired-data endpoint
EXPIRY_DATES_CHUNK_DAYS = 365
MIN_HIT_RATE = 0.5           # below this fraction of contracts-with-data, an expiry is left pending, not
                              # marked complete -- see module docstring: the AngelOne incident this
                              # script exists to avoid had a 1.6% hit rate marked "done" and never retried.


# ---------------------------------------------------------------------------
# Regular (non-expired) History API -- for indices, which never expire
# ---------------------------------------------------------------------------
def fetch_regular_history(symbol: str, resolution: str, range_from: str, range_to: str) -> pd.DataFrame:
    from_dt = datetime.strptime(range_from, '%Y-%m-%d')
    to_dt = datetime.strptime(range_to, '%Y-%m-%d')
    frames = []
    chunk_start = from_dt
    while chunk_start <= to_dt:
        chunk_end = min(chunk_start + timedelta(days=HIST_CHUNK_DAYS - 1), to_dt)
        r = _get('https://api-t1.fyers.in/data/history', {
            'symbol': symbol, 'resolution': resolution, 'date_format': '1',
            'range_from': chunk_start.strftime('%Y-%m-%d'), 'range_to': chunk_end.strftime('%Y-%m-%d'),
            'cont_flag': '1',
        })
        if r.get('s') == 'ok':
            frames.append(pd.DataFrame(r.get('candles', []),
                                        columns=['epoch', 'open', 'high', 'low', 'close', 'volume']))
        elif r.get('s') != 'no_data':
            logger.error(f'History fetch failed for {symbol} [{chunk_start.date()} -> {chunk_end.date()}]: {r}')
        chunk_start = chunk_end + timedelta(days=1)
    if not frames:
        return pd.DataFrame(columns=OHLCV_HEADERS)
    df = pd.concat(frames, ignore_index=True)
    df['time_stamp'] = pd.to_datetime(df['epoch'], unit='s', utc=True).dt.tz_convert('Asia/Kolkata')
    return df[['time_stamp', 'open', 'high', 'low', 'close', 'volume']].drop_duplicates(
        subset='time_stamp').sort_values('time_stamp').reset_index(drop=True)


def _format_ts(ts: pd.Timestamp, fmt: str) -> str:
    if ts.tzinfo is None:
        ts = ts.tz_localize('Asia/Kolkata')
    offset = ts.strftime('%z')
    return ts.strftime(fmt) + offset[:-2] + ':' + offset[-2:]


# ---------------------------------------------------------------------------
# Index updaters (1-min + daily) -- output schema matches
# data_downloader_angelone.py's own update_index/update_all_daily_indices
# ---------------------------------------------------------------------------
def update_index_1min(display_name: str, symbol: str, filename: str) -> None:
    INDICES_DIR.mkdir(parents=True, exist_ok=True)
    filepath = INDICES_DIR / filename

    if filepath.exists():
        existing = pd.read_csv(filepath, parse_dates=['time_stamp'])
        existing['time_stamp'] = pd.to_datetime(existing['time_stamp'], utc=False, errors='coerce')
        last_ts = existing['time_stamp'].max()
        start_dt = (last_ts + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    else:
        existing = pd.DataFrame(columns=INDEX_HEADERS)
        start_dt = datetime(2024, 7, 1)

    end_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if start_dt > end_dt:
        logger.info(f'[{display_name}] already up to date.')
        return

    new_data = fetch_regular_history(symbol, '1', start_dt.strftime('%Y-%m-%d'), end_dt.strftime('%Y-%m-%d'))
    if new_data.empty:
        logger.info(f'[{display_name}] no new data fetched.')
        return

    new_data['time_stamp'] = new_data['time_stamp'].apply(lambda ts: _format_ts(ts, INDEX_TS_FMT))
    new_data['oi'] = 0
    new_data = new_data[INDEX_HEADERS]

    if not existing.empty:
        if existing['time_stamp'].dt.tz is None:
            existing['time_stamp'] = existing['time_stamp'].dt.tz_localize('Asia/Kolkata')
        existing['time_stamp'] = existing['time_stamp'].apply(lambda ts: _format_ts(ts, INDEX_TS_FMT))
        combined = pd.concat([existing, new_data], ignore_index=True)
        combined.drop_duplicates(subset=['time_stamp'], keep='first', inplace=True)
        combined.sort_values('time_stamp', inplace=True)
    else:
        combined = new_data

    combined.to_csv(filepath, index=False)
    logger.info(f'[{display_name}] {filename} updated -- {len(new_data)} new row(s), total {len(combined)}.')


def update_index_daily(display_name: str, symbol: str, filename: str) -> None:
    INDICES_DIR.mkdir(parents=True, exist_ok=True)
    filepath = INDICES_DIR / filename
    today = datetime.now().date()

    if filepath.exists():
        existing = pd.read_csv(filepath)
        last_date = pd.to_datetime(existing['time_stamp']).max().date()
        if last_date >= today:
            logger.info(f'[{display_name} daily] already up to date.')
            return
        from_date = last_date + timedelta(days=1)
    else:
        existing = None
        from_date = today - timedelta(days=3 * 365)

    new_df = fetch_regular_history(symbol, 'D', from_date.strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d'))
    if new_df.empty:
        logger.info(f'[{display_name} daily] no data returned.')
        return

    new_df['time_stamp'] = new_df['time_stamp'].dt.date.astype(str)
    new_df['oi'] = 0
    for col in ('open', 'high', 'low', 'close', 'volume'):
        new_df[col] = pd.to_numeric(new_df[col], errors='coerce')

    if existing is not None and not existing.empty:
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset='time_stamp', keep='last')
    else:
        combined = new_df
    combined = combined.sort_values('time_stamp').reset_index(drop=True)
    combined.to_csv(filepath, index=False)
    logger.info(f'[{display_name} daily] saved {len(combined)} total rows (+{len(new_df)} new) -> {filename}')


# ---------------------------------------------------------------------------
# Options: expired-contract workflow, generalized from
# prometheus_backtest/phase3_fyers/options_iv/fyers_options_api.py for
# equity-index (NFO/BFO) underlyings instead of MCX commodities.
# ---------------------------------------------------------------------------
_SYMBOL_MASTER_CACHE: dict = {}


def _fetch_symbol_master(master_name: str) -> dict:
    """master_name e.g. 'BSE_FO', 'NSE_FO' -- cached per process run, these
    are large (multi-MB) files with no need to refetch per instrument."""
    if master_name in _SYMBOL_MASTER_CACHE:
        return _SYMBOL_MASTER_CACHE[master_name]
    url = f'https://public.fyers.in/sym_details/{master_name}_sym_master.json'
    req = urllib.request.Request(url, headers={'User-Agent': _UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        master = json.loads(resp.read().decode())
    _SYMBOL_MASTER_CACHE[master_name] = master
    return master


def resolve_options_anchor(underlying: str, master_name: str) -> str:
    """Any real, currently-tradeable OPTION contract for `underlying` --
    Get Expiry Dates resolves the underlying internally regardless of which
    specific contract is passed (confirmed directly, same finding as
    data_downloader_fyers_mcx.py's own resolve_anchor_symbol). Picks the
    soonest-expiring live option, same not-yet-expired logic as that
    function for the same reason (a cached master can be briefly stale)."""
    master = _fetch_symbol_master(master_name)
    now_epoch = datetime.now().timestamp()
    candidates = [
        rec for rec in master.values()
        if rec.get('underSym') == underlying and rec.get('tradeStatus') == 1
        and rec.get('optType') in ('CE', 'PE')
    ]
    if not candidates:
        raise RuntimeError(f'No active option contract found for {underlying!r} in {master_name}.')
    not_yet_expired = [rec for rec in candidates if int(rec['expiryDate']) > now_epoch]
    pool = not_yet_expired or candidates
    nearest = min(pool, key=lambda rec: int(rec['expiryDate']))
    return nearest['symTicker']


def get_expiry_dates_paired(anchor_symbol: str, range_from: str, range_to: str) -> tuple:
    start = datetime.strptime(range_from, '%Y-%m-%d')
    end = datetime.strptime(range_to, '%Y-%m-%d')
    all_fut, all_opt = set(), set()
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=EXPIRY_DATES_CHUNK_DAYS - 1), end)
        r = _get('https://api-t1.fyers.in/data/history/fno/expired/expiry-dates', {
            'symbol': anchor_symbol, 'range_from': chunk_start.strftime('%Y-%m-%d'),
            'range_to': chunk_end.strftime('%Y-%m-%d'), 'date_format': 1,
        })
        if r.get('s') == 'ok':
            ed = r.get('data', {}).get('expiry_dates', {})
            all_fut.update(ed.get('futures', []))
            all_opt.update(ed.get('options', []))
        chunk_start = chunk_end + timedelta(days=1)
    return sorted(all_fut), sorted(all_opt)


def get_expired_option_contracts(anchor_symbol: str, options_expiry_date: str) -> list:
    r = _get('https://api-t1.fyers.in/data/history/fno/expired/underlying-symbols', {
        'symbol': anchor_symbol, 'expiry_date': options_expiry_date,
    })
    if r.get('s') != 'ok':
        return []
    return r.get('data', {}).get('contracts', {}).get('options', [])


def _strike_and_type(symbol: str) -> tuple:
    r"""
    'BSE:SENSEX2691776000CE' -> (76000, 'ce'); 'NSE:NIFTY26SEP1500CE' -> (1500, 'ce').

    Verified directly against the live NSE/BSE F&O symbol masters' own
    structured strikePrice field (2026-09-18), not guessed: weekly-format
    symbols (SENSEX2691776000CE-style -- year + 3-digit week code + a
    fixed-width 5-digit strike) are exactly the last 5 digits before CE/PE,
    confirmed with 0 mismatches across all 3,156 live SENSEX and all
    non-monthly NIFTY contracts checked. But ~3.5% of NIFTY contracts use a
    SEPARATE monthly format (NIFTY26SEPStrikeCE -- a 3-letter month code,
    MCX's own convention) where the strike is variable-width and can be
    SHORTER than 5 digits (e.g. strike 1500) -- the fixed-width rule
    misparses these (its last-5-chars capture lands on the month code's own
    letters, e.g. 'P1500' instead of '01500'). Handled by trying the
    fixed-width numeric rule first, falling back to the TRAILING contiguous
    run of digits (re.search(r'(\d+)$', body)) -- not "every digit anywhere
    in the string", which was tried and rejected: it wrongly sweeps in the
    symbol's own leading year digits too (e.g. 'NIFTY26SEP1500' collecting
    both the '26' and the '1500' into one run, misparsing the strike as
    61500). A trailing-run match stops at the month code's own letters, so
    only the genuine strike digits are captured.
    """
    suffix = symbol[-2:].lower()
    body = symbol[:-2]
    last5 = body[-5:]
    if last5.isdigit():
        return int(last5), suffix
    m = re.search(r'(\d+)$', body)
    return int(m.group(1)) if m else 0, suffix


def get_options_filepath(options_dir: Path, expiry_date: str, strike: int, option_type: str) -> Path:
    expiry_dir = options_dir / expiry_date
    expiry_dir.mkdir(parents=True, exist_ok=True)
    return expiry_dir / f'{strike}{option_type}.csv'


def download_options_for_symbol(display_name: str, underlying: str, master_name: str,
                                options_dir: Path, tracking_file: Path, dry_run: bool = False) -> None:
    if not tracking_file.exists():
        logger.warning(f'[{display_name}] no tracking file at {tracking_file}, skipping.')
        return

    contracts_df = pd.read_csv(tracking_file, parse_dates=['expiry_date', 'start_date'])
    today = datetime.now().date()
    pending = contracts_df[
        (contracts_df['expiry_date'].dt.date <= today) & (~contracts_df['download_status'])
    ].copy()
    if pending.empty:
        logger.info(f'[{display_name}] no pending expired option contracts.')
        return
    logger.info(f'[{display_name}] {len(pending)} pending expiry entries.')

    anchor_symbol = resolve_options_anchor(underlying, master_name)
    logger.info(f'[{display_name}] anchor: {anchor_symbol}')

    for _, row in pending.iterrows():
        expiry_ts = row['expiry_date']
        expiry_str = expiry_ts.strftime('%Y-%m-%d')
        start_str = row['start_date'].strftime('%Y-%m-%d')

        symbols = get_expired_option_contracts(anchor_symbol, expiry_str)
        if not symbols:
            logger.warning(f'[{display_name}] {expiry_str}: no contracts resolved from Fyers '
                           f'(genuinely no options history back this far, or a real expiry-date '
                           f'mismatch) -- leaving pending.')
            continue
        logger.info(f'[{display_name}] {expiry_str}: {len(symbols)} contracts found.')

        saved = 0
        for symbol in symbols:
            strike, option_type = _strike_and_type(symbol)
            out_path = get_options_filepath(options_dir, expiry_str, strike, option_type)
            if out_path.exists():
                saved += 1
                continue
            try:
                df = get_expired_historical_data(symbol, start_str, expiry_str)
            except (FyersAuthExpiredError, FyersRateLimitError) as e:
                logger.error(f'{type(e).__name__}: {e} -- stopping this instrument, resume later.')
                return
            if df.empty:
                continue
            if not dry_run:
                df.to_csv(out_path, index=False)
            saved += 1

        hit_rate = saved / len(symbols)
        if hit_rate < MIN_HIT_RATE:
            logger.error(f'[{display_name}] {expiry_str}: only {saved}/{len(symbols)} contracts '
                         f'({hit_rate*100:.1f}%) had data -- below the {MIN_HIT_RATE*100:.0f}% '
                         f'sanity floor. NOT marking complete, left pending for the next run. '
                         f'(This exact silent-failure shape is why this floor exists -- see module '
                         f'docstring.)')
            continue

        logger.info(f'[{display_name}] {expiry_str} complete -- {saved}/{len(symbols)} contracts had data.')
        if not dry_run:
            contracts_df.loc[contracts_df['expiry_date'] == expiry_ts, 'download_status'] = True

    if not dry_run:
        contracts_df.to_csv(tracking_file, index=False)
        logger.info(f'[{display_name}] {tracking_file.name} updated.')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_indices():
    for display_name, symbol, fname_1m, fname_daily in INDEX_INSTRUMENTS:
        update_index_1min(display_name, symbol, fname_1m)
        update_index_daily(display_name, symbol, fname_daily)


def run_options(dry_run: bool = False):
    for display_name, underlying, master_name, options_dir, tracking_file in OPTIONS_INSTRUMENTS:
        download_options_for_symbol(display_name, underlying, master_name, options_dir, tracking_file,
                                    dry_run=dry_run)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--indices', action='store_true', help='Update Nifty/Sensex/India VIX index data')
    p.add_argument('--options', action='store_true', help='Download pending Nifty/Sensex expired options')
    p.add_argument('--all', action='store_true', help='Run both')
    p.add_argument('--dry-run', action='store_true', help='Options only: fetch and log, do not write files')
    args = p.parse_args()

    if not (args.indices or args.options or args.all):
        p.error('specify --indices, --options, or --all')

    try:
        if args.indices or args.all:
            logger.info('=== Updating indices ===')
            run_indices()
        if args.options or args.all:
            logger.info('=== Downloading options ===')
            run_options(dry_run=args.dry_run)
    except FyersAuthExpiredError as e:
        logger.error(f'{e}')
        logger.error('Fyers token expired -- refresh via the manual OAuth flow, then re-run '
                     '(everything already downloaded is skipped, this resumes cleanly).')
        raise SystemExit(1)

    logger.info('=== All done ===')


if __name__ == '__main__':
    main()
