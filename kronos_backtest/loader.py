"""
loader.py — Kronos data loading

Adapted from athena_backtest's loader: same 1-min Nifty option layout
(data_pipeline/data/nifty/options/<YYYY-MM-DD>/<strike><ce|pe>.csv), same index
and VIX series. What is new here is the monthly universe: which contracts exist,
and how far back each one's data actually reaches.
"""

import os
import csv
import logging
from datetime import datetime

import pandas as pd

from configs import (
    NIFTY_INDEX_FILE, VIX_INDEX_FILE, NIFTY_OPTIONS_PATH,
    CONTRACT_LIST_FILE, HOLIDAYS_FILE, OUTPUT_DIR,
    BACKTEST_START_DATE, BACKTEST_END_DATE,
    REQUIRE_DOWNLOAD_STATUS, MAX_PRICE_STALENESS_MINUTES,
)
from expiry_rules import identify_monthly_expiries

logger = logging.getLogger(__name__)

DATA_START_CACHE = os.path.join(OUTPUT_DIR, "contract_data_start.csv")


# ---------------------------------------------------------------------------
# Holidays
# ---------------------------------------------------------------------------

def load_holidays() -> set:
    """
    Exchange holidays as a set of datetime.date.

    The dtype matters: expiry_rules tests `d not in holidays` with d a
    datetime.date. If this set held strings or Timestamps the test would never
    fire and every calendar rule would silently degrade to weekday arithmetic.
    """
    df = pd.read_csv(HOLIDAYS_FILE, parse_dates=['date'])
    holidays = set(pd.to_datetime(df['date']).dt.date)
    assert all(isinstance(d, type(next(iter(holidays)))) for d in holidays)
    logger.info(f"  Holidays        : {len(holidays)} "
                f"({min(holidays)} -> {max(holidays)})")
    return holidays


# ---------------------------------------------------------------------------
# Index series
# ---------------------------------------------------------------------------

def load_index_data():
    """1-min Nifty spot and India VIX, tz-stripped and indexed by timestamp."""
    frames = {}
    for name, path in (('nifty', NIFTY_INDEX_FILE), ('vix', VIX_INDEX_FILE)):
        df = pd.read_csv(path, parse_dates=['time_stamp'])
        df['time_stamp'] = pd.to_datetime(df['time_stamp'], utc=False).dt.tz_localize(None)
        if BACKTEST_START_DATE:
            df = df[df['time_stamp'] >= pd.Timestamp(BACKTEST_START_DATE)]
        if BACKTEST_END_DATE:
            df = df[df['time_stamp'] <= pd.Timestamp(BACKTEST_END_DATE)]
        frames[name] = df.set_index('time_stamp').sort_index()
        logger.info(f"  1-min {name:<10}: {len(frames[name]):,} rows "
                    f"({frames[name].index.min().date()} -> {frames[name].index.max().date()})")
    return frames['nifty'], frames['vix']


# ---------------------------------------------------------------------------
# Contract universe
# ---------------------------------------------------------------------------

def load_contract_list() -> pd.DataFrame:
    """Every Nifty expiry in the pipeline's contract list, weeklies included."""
    df = pd.read_csv(CONTRACT_LIST_FILE)
    df['expiry_date'] = pd.to_datetime(df['expiry_date'], utc=True).dt.tz_convert(
        'Asia/Kolkata').dt.tz_localize(None).dt.date
    df['download_status'] = df['download_status'].astype(str).str.strip().str.lower() == 'true'
    return df.sort_values('expiry_date').reset_index(drop=True)


def expiry_dir(expiry) -> str:
    return os.path.join(NIFTY_OPTIONS_PATH, expiry.strftime('%Y-%m-%d'))


def _first_timestamp_in_file(path: str):
    """Timestamp on the first data row, read without parsing the whole file."""
    try:
        with open(path, 'r', newline='') as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            row = next(reader, None)
        if not header or not row:
            return None
        raw = row[0]
        # Nifty files are tz-naive 'YYYY-MM-DD HH:MM:SS'; be tolerant anyway.
        ts = pd.to_datetime(raw, errors='coerce')
        if pd.isna(ts):
            return None
        if ts.tzinfo is not None:
            ts = ts.tz_convert('Asia/Kolkata').tz_localize(None)
        return ts.to_pydatetime()
    except (OSError, StopIteration, csv.Error):
        return None


def contract_data_start(expiry):
    """
    Earliest timestamp present anywhere in a contract's option files.

    Strikes are listed progressively as spot moves, so no single strike's first
    bar represents the contract. The minimum across all files does.
    Returns (first_timestamp, n_files); (None, 0) if the directory is missing.
    """
    d = expiry_dir(expiry)
    if not os.path.isdir(d):
        return None, 0
    earliest, n = None, 0
    with os.scandir(d) as it:
        for entry in it:
            if not entry.is_file() or not entry.name.endswith('.csv'):
                continue
            n += 1
            ts = _first_timestamp_in_file(entry.path)
            if ts is not None and (earliest is None or ts < earliest):
                earliest = ts
    return earliest, n


def _load_data_start_cache() -> dict:
    if not os.path.exists(DATA_START_CACHE):
        return {}
    cache = {}
    with open(DATA_START_CACHE, 'r', newline='') as fh:
        for row in csv.DictReader(fh):
            ts = row['data_start']
            cache[row['expiry_date']] = (
                datetime.fromisoformat(ts) if ts else None,
                int(row['n_files']),
            )
    return cache


def _save_data_start_cache(cache: dict) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(DATA_START_CACHE, 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['expiry_date', 'data_start', 'n_files'])
        for k in sorted(cache):
            ts, n = cache[k]
            w.writerow([k, ts.isoformat() if ts else '', n])


def load_monthly_universe(holidays: set, use_cache: bool = True) -> tuple:
    """
    The Kronos contract universe.

    Monthlies are identified by calendar rule over every expiry in the contract
    list — the last expiry of each calendar month — and only then filtered for
    usability. Identification and usability are kept strictly separate: a
    lead-time filter applied at identification would drop ~30 genuine monthlies
    that merely have short history, most of them in 2020 (§3 of the plan).

    Returns (universe_df, warnings). Columns:
      expiry_date, expiry_weekday, download_status, dir_exists, n_files,
      data_start, lead_days
    """
    contracts = load_contract_list()
    monthlies, warnings = identify_monthly_expiries(list(contracts['expiry_date']))

    status = dict(zip(contracts['expiry_date'], contracts['download_status']))
    cache = _load_data_start_cache() if use_cache else {}
    dirty = False

    rows = []
    for expiry in monthlies:
        key = expiry.strftime('%Y-%m-%d')
        if key not in cache:
            cache[key] = contract_data_start(expiry)
            dirty = True
        data_start, n_files = cache[key]
        rows.append({
            'expiry_date':     expiry,
            'expiry_weekday':  expiry.strftime('%A'),
            'download_status': status.get(expiry, False),
            'dir_exists':      os.path.isdir(expiry_dir(expiry)),
            'n_files':         n_files,
            'data_start':      data_start,
            'lead_days':       (expiry - data_start.date()).days if data_start else None,
        })

    if dirty:
        _save_data_start_cache(cache)

    df = pd.DataFrame(rows)
    if REQUIRE_DOWNLOAD_STATUS:
        dropped = df[~df['download_status']]
        for _, r in dropped.iterrows():
            warnings.append(f"{r['expiry_date']}: download_status False — excluded")
        df = df[df['download_status']]

    dropped = df[(~df['dir_exists']) | (df['n_files'] == 0)]
    for _, r in dropped.iterrows():
        warnings.append(f"{r['expiry_date']}: no option files on disk — excluded")
    df = df[df['dir_exists'] & (df['n_files'] > 0)]

    return df.sort_values('expiry_date').reset_index(drop=True), warnings


def usable_at_dte(universe: pd.DataFrame, dte: int) -> pd.DataFrame:
    """
    Contracts whose data reaches back far enough to enter at the given DTE.
    Usability is a data-coverage property, never part of identification.
    """
    return universe[universe['lead_days'].notna() & (universe['lead_days'] >= dte)]


# ---------------------------------------------------------------------------
# Option series
# ---------------------------------------------------------------------------

def load_option_data(expiry, strike: int, option_type: str) -> pd.DataFrame:
    """1-min bars for one leg. Empty DataFrame if the file does not exist."""
    path = os.path.join(expiry_dir(expiry), f"{strike}{option_type}.csv")
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path, parse_dates=['datetime'])
    df['datetime'] = pd.to_datetime(df['datetime'])
    return df


def get_option_price_with_age(option_df: pd.DataFrame, timestamp: pd.Timestamp,
                              price_col: str = 'open') -> tuple:
    """
    (price, age_in_minutes) at timestamp, falling back to the last close before it.

    Age is what makes the fallback safe to use. These files are trade-derived:
    a minute with no trade produces no bar, so a far-OTM strike can fall back to
    a print from days earlier. That price is not tradeable and the IV backed out
    of it is meaningless. Callers must bound the age — see get_option_price.
    Returns (None, None) when there is no prior print at all.
    """
    if option_df.empty:
        return None, None
    row = option_df[option_df['datetime'] == timestamp]
    if not row.empty:
        val = row[price_col].iloc[0]
        return (float(val), 0.0) if pd.notna(val) else (None, None)
    prior = option_df[option_df['datetime'] < timestamp]
    if prior.empty:
        return None, None
    last = prior.iloc[-1]
    age = (timestamp - last['datetime']).total_seconds() / 60.0
    return float(last['close']), age


def get_option_price(option_df: pd.DataFrame, timestamp: pd.Timestamp,
                     price_col: str = 'open',
                     max_staleness_minutes: float = -1) -> float:
    """
    Price at timestamp, rejecting fallbacks older than the staleness bound.
    Pass max_staleness_minutes=None to disable the bound (measurement only).
    """
    if max_staleness_minutes == -1:
        max_staleness_minutes = MAX_PRICE_STALENESS_MINUTES
    price, age = get_option_price_with_age(option_df, timestamp, price_col)
    if price is None:
        return None
    if max_staleness_minutes is not None and age > max_staleness_minutes:
        return None
    return price


def get_1min_value(indexed_df: pd.DataFrame, timestamp: pd.Timestamp,
                   col: str = 'close') -> float:
    """Value from a timestamp-indexed 1-min frame, falling back to the last prior."""
    if timestamp in indexed_df.index:
        val = indexed_df.loc[timestamp, col]
        return float(val) if pd.notna(val) else None
    prior = indexed_df[indexed_df.index < timestamp]
    if not prior.empty:
        return float(prior[col].iloc[-1])
    return None
