"""
fyers_st_probe.py -- standalone, Fyers-sourced parallel ST_15 builder for
CRUDEOILM. Plan §3.4/§3.5 (plans/fyers-mcx-data-integration.md): mirrors
Prometheus's own seed -> live-poll -> cache -> 15m-boundary -> ST pipeline
exactly, but sourced entirely from Fyers's regular History API instead of
Angel One's getCandleData -- the same call class both Prometheus's live
polling and the historical downloaders use, per the user's own framing of
why this matters (2026-09-15).

**Read-only, diagnostic. No orders, no Slack, no trading logic, no writes
to anything Prometheus itself owns.** Its entire purpose is producing a
directly-comparable ST_15 series and a reliability log (latency, success/
failure, error codes) alongside Prometheus's own real one, for the AB1021-
equivalent investigation (plan §3.1) and the eventual fallback-cascade
decision (§3.5) -- neither decided nor built here.

Reuses Prometheus's own pure, source-agnostic utility functions directly
(not reimplemented -- a numerically-identical ST series requires the exact
same compute_st, not a parallel copy that could silently drift):
  - resolve_effective_contract() -- same tender-margin-roll contract choice
    Prometheus itself uses, so this probe always tracks the SAME contract.
  - compute_st() / _resample_1m_to_Nmin() / _find_Nmin_gaps()
  - _resolve_closing_time() (the DST-aware MCX close time)
  - mcx_evening_only_today() (deferred-start holiday detection)
None of these touch the broker, place orders, or write to any file
Prometheus owns -- confirmed by reading their own implementations before
reuse (see plan §3.4/§3.5's own notes). Everything that DOES touch state
(today-cache, the 15m series, the probe log) uses this script's own
separate files under fyers_st_probe/data/ and fyers_st_probe/logs/ --
never TODAY_1M_CACHE_FILE or any other file prometheus_production/ reads.

Auth: performs a fresh headless Fyers login at startup via
data_pipeline/fyers_auth.py (plan §2.3) -- Fyers's access_token expires at
midnight IST regardless of issue time, so a 9:00 daily cron can't rely on
a token obtained any earlier. Requires fyers_client_id/fyers_pin/
fyers_totp_key in data/user_credentials.csv (from enabling External 2FA
TOTP on the Fyers account) -- see fyers_auth.py's own docstring. If that
login fails, this script logs the failure and exits rather than running
on a stale/expired token.

Usage:
  python fyers_st_probe/fyers_st_probe.py
Runs from startup (deferring to EVENING_SESSION_OPEN_TIME on a morning-
closed MCX holiday, exactly like Prometheus) through CLOSING_TIME, then
exits cleanly. Meant for cron `00 9 * * 1-5`, matching Prometheus's own
schedule -- see plan §3.4 for the cron entry itself (not added without
separate explicit approval, per this repo's standing Delos protocol).
"""
import csv
import json
import logging
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, date
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'prometheus_production'))
sys.path.insert(0, str(REPO_ROOT / 'data_pipeline'))

from prometheus_functions import (   # noqa: E402 -- see module docstring for why these are reused, not reimplemented
    resolve_effective_contract, mcx_evening_only_today, compute_st,
    _resample_1m_to_Nmin, _find_Nmin_gaps, _safe_concat,
)
from prometheus_configs import (   # noqa: E402
    _resolve_closing_time, SESSION_START_TIME, EVENING_SESSION_OPEN_TIME,
    EVENING_SESSION_WAKE_BUFFER_MIN, ST_PERIOD, ST_MULTIPLIER, SEED_DAYS,
    DEFERRED_BAR_CUTOFF_MIN,
)
from fyers_auth import ensure_fresh_token   # noqa: E402

# ---------------------------------------------------------------------------
# Config -- this script's own paths, never Prometheus's
# ---------------------------------------------------------------------------
SYMBOL = 'CRUDEOILM'
DATA_DIR = Path(__file__).parent / 'data'
LOG_DIR = Path(__file__).parent / 'logs'
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

TODAY_1M_CACHE_FILE = DATA_DIR / 'fyers_today_1m.csv'
SEED_PAST_CACHE_FILE = DATA_DIR / 'fyers_seed_past.csv'
SERIES_15M_FILE = DATA_DIR / 'fyers_15m_series.csv'
PROBE_LOG_FILE = DATA_DIR / 'fyers_fetch_probe_log.csv'
CREDS_FILE = REPO_ROOT / 'data' / 'user_credentials.csv'

OHLCV_HEADERS = ['time_stamp', 'open', 'high', 'low', 'close', 'volume']
LOOKBACK_MIN = 5             # matches prometheus_functions.fetch_one_minute_window's own lookback
INNER_RETRY_ATTEMPTS = 3     # matches mcx_live_downloader.py's own AB1021 probe pattern
INNER_RETRY_INTERVAL_SEC = 1
SEED_LOOKBACK_CALENDAR_DAYS = SEED_DAYS + 10   # buffer over trading-day count for weekends/holidays

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')

# ---------------------------------------------------------------------------
# Logging -- own named logger, own daily file, mirrors prometheus_logger_setup.py's
# own format exactly for easy side-by-side reading.
# ---------------------------------------------------------------------------
def _get_logger() -> logging.Logger:
    log_file = LOG_DIR / f'fyers_st_probe_{date.today():%Y%m%d}.log'
    logger = logging.getLogger('fyers_st_probe')
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


logger = _get_logger()

_running = True


def _handle_sigterm(signum, frame):
    global _running
    logger.warning(f'Signal {signum} received -- stopping after current cycle.')
    _running = False


signal.signal(signal.SIGINT, _handle_sigterm)
signal.signal(signal.SIGTERM, _handle_sigterm)


# ---------------------------------------------------------------------------
# Auth / low-level HTTP -- same pattern as data_downloader_fyers_mcx.py
# ---------------------------------------------------------------------------
def _creds() -> dict:
    with open(CREDS_FILE, newline='') as f:
        return next(csv.DictReader(f))


def _auth_header() -> str:
    creds = _creds()
    app_id = creds.get('fyers_app_id')
    token = creds.get('fyers_access_token')
    if not app_id or not token:
        raise RuntimeError(f'No fyers_app_id/fyers_access_token in {CREDS_FILE}.')
    return f'{app_id}:{token}'


def fetch_history(symbol: str, from_dt: datetime, to_dt: datetime,
                  endpoint_label: str = 'live') -> pd.DataFrame | None:
    """Inner-retry-burst fetch against the regular (non-expired) History
    API, logging every attempt to PROBE_LOG_FILE -- same shape as
    mcx_live_downloader.py's own AB1021 probe log, so a side-by-side
    comparison against the existing Angel One investigation is a plain
    file operation. Returns None only on a genuine, fully-exhausted
    failure (caller's job to decide what "no new data yet" vs "real
    failure" means, matching seed_st15's own distinction)."""
    for attempt in range(1, INNER_RETRY_ATTEMPTS + 1):
        call_ts = datetime.now()
        t0 = time.monotonic()
        params = {
            'symbol': symbol, 'resolution': '1', 'date_format': 0,
            'range_from': int(from_dt.timestamp()), 'range_to': int(to_dt.timestamp()),
            'cont_flag': 0,
        }
        full_url = f'https://api-t1.fyers.in/data/history?{urllib.parse.urlencode(params)}'
        success, error_code, error_message, candle_count = False, None, None, 0
        try:
            req = urllib.request.Request(full_url, headers={'Authorization': _auth_header(), 'User-Agent': _UA})
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode())
            latency_ms = (time.monotonic() - t0) * 1000
            success = body.get('s') == 'ok'
            candles = body.get('candles', []) if success else []
            candle_count = len(candles)
            if not success:
                error_code, error_message = body.get('code'), body.get('message')
        except urllib.error.HTTPError as e:
            latency_ms = (time.monotonic() - t0) * 1000
            error_code, error_message = f'HTTP{e.code}', e.read().decode()[:300]
            candles = []
        except Exception as e:
            latency_ms = (time.monotonic() - t0) * 1000
            error_code, error_message = 'EXCEPTION', str(e)[:300]
            candles = []

        write_header = not PROBE_LOG_FILE.exists()
        with open(PROBE_LOG_FILE, 'a', newline='') as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(['call_ts', 'endpoint', 'symbol', 'attempt', 'success',
                           'error_code', 'error_message', 'latency_ms', 'candle_count'])
            w.writerow([call_ts.isoformat(), endpoint_label, symbol, attempt, success,
                       error_code, error_message, round(latency_ms, 1), candle_count])

        if success:
            logger.debug(f'Fetch OK [{from_dt:%H:%M} -> {to_dt:%H:%M}] attempt {attempt}/'
                        f'{INNER_RETRY_ATTEMPTS} ({candle_count} candle(s), {latency_ms:.0f}ms)')
            if not candles:
                return pd.DataFrame(columns=OHLCV_HEADERS)
            df = pd.DataFrame(candles, columns=['epoch', 'open', 'high', 'low', 'close', 'volume'])
            df['time_stamp'] = pd.to_datetime(df['epoch'], unit='s', utc=True).dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
            return df[OHLCV_HEADERS].sort_values('time_stamp').reset_index(drop=True)
        else:
            logger.warning(f'Fetch failed [{from_dt:%H:%M} -> {to_dt:%H:%M}] attempt {attempt}/'
                          f'{INNER_RETRY_ATTEMPTS}: {error_code} -- {error_message} ({latency_ms:.0f}ms)')

        if attempt < INNER_RETRY_ATTEMPTS:
            time.sleep(INNER_RETRY_INTERVAL_SEC)

    logger.error(f'Inner burst exhausted [{from_dt:%H:%M} -> {to_dt:%H:%M}] -- deferring to pending-recovery queue')
    return None


# ---------------------------------------------------------------------------
# Contract resolution -- reuses Prometheus's own choice, translated to
# Fyers's own symbol format (found 2026-09-15, plan §2.4a: month+2-digit-
# year, no day -- "CRUDEOILM26OCTFUT", not Angel One's "CRUDEOILM19OCT26FUT").
# ---------------------------------------------------------------------------
def resolve_fyers_contract() -> dict:
    contract = resolve_effective_contract(SYMBOL, today=date.today())
    expiry = contract['expiry_date']
    fyers_symbol = f'MCX:{SYMBOL}{expiry:%y%b}FUT'.upper()
    logger.info(f'Effective contract: {contract["symbol"]} (Angel One) -> {fyers_symbol} (Fyers), '
               f'expiry {expiry:%Y-%m-%d}')
    return {'angel_symbol': contract['symbol'], 'fyers_symbol': fyers_symbol, 'expiry_date': expiry}


# ---------------------------------------------------------------------------
# Own independent today-cache (own file, own token-scoping by fyers_symbol
# instead of Angel One's numeric token) -- same merge-dedup shape as
# prometheus_functions._merge_and_save / read_today_cache, but never
# touching TODAY_1M_CACHE_FILE (Prometheus's own file).
# ---------------------------------------------------------------------------
def _merge_and_save(filepath: Path, new_df: pd.DataFrame) -> int:
    """Mirrors prometheus_functions._merge_and_save's own discipline exactly
    (tz-naive normalization before concat, dedup keep='first') so any
    divergence from Prometheus's own cache behavior is deliberate, not
    incidental -- these CSVs exist specifically to be diffed against
    Prometheus's own for the accuracy investigation."""
    if new_df is None or new_df.empty:
        return 0
    on_disk = (pd.read_csv(filepath, parse_dates=['time_stamp'])
              if filepath.exists() else pd.DataFrame(columns=OHLCV_HEADERS + ['fyers_symbol']))
    if not on_disk.empty:
        on_disk['time_stamp'] = pd.to_datetime(on_disk['time_stamp'], utc=False, errors='coerce')
        if on_disk['time_stamp'].dt.tz is not None:
            on_disk['time_stamp'] = on_disk['time_stamp'].dt.tz_localize(None)
    new_df = new_df.copy()
    if new_df['time_stamp'].dt.tz is not None:
        new_df['time_stamp'] = new_df['time_stamp'].dt.tz_localize(None)
    before = len(on_disk)
    merged = _safe_concat([on_disk, new_df], ignore_index=True)
    merged['time_stamp'] = pd.to_datetime(merged['time_stamp'], utc=False, errors='coerce')
    merged.drop_duplicates(subset=['time_stamp'], keep='first', inplace=True)
    merged.sort_values('time_stamp', inplace=True)
    merged.reset_index(drop=True, inplace=True)
    merged.to_csv(filepath, index=False)
    return len(merged) - before


def read_today_cache(fyers_symbol: str) -> pd.DataFrame:
    """Date- AND symbol-filtered on every read, same defense-in-depth
    reasoning as Prometheus's own read_today_cache (a leftover row from a
    prior day or a rollover can't silently corrupt today's seed)."""
    if not TODAY_1M_CACHE_FILE.exists():
        return pd.DataFrame(columns=OHLCV_HEADERS)
    df = pd.read_csv(TODAY_1M_CACHE_FILE, parse_dates=['time_stamp'])
    if df.empty:
        return df
    today = date.today()
    todays_rows = df[(df['time_stamp'].dt.date == today) & (df['fyers_symbol'] == fyers_symbol)]
    return todays_rows[OHLCV_HEADERS].sort_values('time_stamp').reset_index(drop=True)


def save_today_cache(fyers_symbol: str, new_df: pd.DataFrame) -> int:
    if new_df.empty:
        return 0
    tagged = new_df.copy()
    tagged['fyers_symbol'] = fyers_symbol
    return _merge_and_save(TODAY_1M_CACHE_FILE, tagged)


# ---------------------------------------------------------------------------
# Seed-past cache -- the user's own point (2026-09-15): the current
# contract is still live, so it will NEVER appear in the expired-only
# staging backfill (data_pipeline/data/mcx_fyers/). Fetch its own recent
# history directly via the regular History API instead, cached locally so
# a same-day restart doesn't re-fetch SEED_DAYS worth of history every time.
# ---------------------------------------------------------------------------
def get_seed_past(fyers_symbol: str, now: datetime) -> pd.DataFrame:
    if SEED_PAST_CACHE_FILE.exists():
        cached = pd.read_csv(SEED_PAST_CACHE_FILE, parse_dates=['time_stamp'])
        if not cached.empty and (cached.get('fyers_symbol') == fyers_symbol).all():
            logger.info(f'Seed-past cache already covers {fyers_symbol} '
                       f'({len(cached)} rows, {cached["time_stamp"].min()} -> {cached["time_stamp"].max()})')
            return cached[OHLCV_HEADERS]
        logger.info('Seed-past cache is for a different contract (or empty) -- refetching.')

    seed_from = now - timedelta(days=SEED_LOOKBACK_CALENDAR_DAYS)
    logger.info(f'Fetching seed history for {fyers_symbol}: {seed_from:%Y-%m-%d} -> {now:%Y-%m-%d} ...')
    df = fetch_history(fyers_symbol, seed_from, now, endpoint_label='seed')
    if df is None or df.empty:
        logger.error(f'Seed-past fetch failed or returned nothing for {fyers_symbol}.')
        return pd.DataFrame(columns=OHLCV_HEADERS)

    tagged = df.copy()
    tagged['fyers_symbol'] = fyers_symbol
    tagged.to_csv(SEED_PAST_CACHE_FILE, index=False)
    logger.info(f'Seed-past cache saved: {len(df)} rows.')
    return df


# ---------------------------------------------------------------------------
# Seeding -- mirrors seed_st15()'s own shape exactly (past + today, gap
# check, compute_st), just against Fyers-sourced data throughout.
# ---------------------------------------------------------------------------
def seed(contract: dict, now: datetime) -> pd.DataFrame:
    fyers_symbol = contract['fyers_symbol']
    raw_1m_past = get_seed_past(fyers_symbol, now)

    cached_today = read_today_cache(fyers_symbol)
    session_start = pd.Timestamp(f'{now.date()} {SESSION_START_TIME}')
    gap_from = (cached_today['time_stamp'].max() + timedelta(minutes=1)
               if not cached_today.empty else session_start)
    if gap_from < now:
        gap_df = fetch_history(fyers_symbol, gap_from, now, endpoint_label='seed_gap')
        if gap_df is not None and not gap_df.empty:
            save_today_cache(fyers_symbol, gap_df)
            cached_today = (_safe_concat([cached_today, gap_df], ignore_index=True)
                            .drop_duplicates(subset=['time_stamp'], keep='last')
                            .sort_values('time_stamp').reset_index(drop=True))
        elif gap_df is None:
            if cached_today.empty:
                logger.error('seed: no cached today data and live gap-fetch failed -- cannot seed.')
                return pd.DataFrame()
            logger.warning(f'seed: live gap-fetch failed, proceeding with cached data only '
                          f'(through {cached_today["time_stamp"].max()}).')
        else:
            logger.info(f'seed: live gap-fetch [{gap_from} -> {now}] found nothing new yet.')

    raw_1m = (_safe_concat([raw_1m_past, cached_today], ignore_index=True)
             .sort_values('time_stamp').reset_index(drop=True))
    if raw_1m.empty:
        logger.error('seed: no 1-min history available after seed-past + cache + live fetch.')
        return pd.DataFrame()

    df_15m_raw = _resample_1m_to_Nmin(raw_1m, 15, now)
    if df_15m_raw.empty:
        logger.error('seed: resample produced no 15-min bars.')
        return pd.DataFrame()

    gaps = _find_Nmin_gaps(df_15m_raw, 15)
    if gaps:
        logger.error(f'seed: gap(s) in reconstructed 15m series, refusing to seed: {gaps}')
        return pd.DataFrame()

    return compute_st(df_15m_raw, ST_PERIOD, ST_MULTIPLIER)


# ---------------------------------------------------------------------------
# 15m boundary handling -- same deferred-wait-then-build-from-what's-on-
# hand pattern as prometheus.py's own §12, plus the same pre-open guard
# built into Prometheus itself earlier today (2026-09-15 fix) so a
# deferred-start day's structurally-empty first boundary doesn't fire a
# false gap alert here either.
# ---------------------------------------------------------------------------
def build_15m_bar(df_1m: pd.DataFrame, df_15m: pd.DataFrame, boundary: datetime,
                  session_open_today: datetime) -> tuple:
    window_start = boundary - timedelta(minutes=15)
    if window_start < session_open_today:
        logger.info(f'{window_start:%H:%M}-{boundary:%H:%M} window precedes today\'s session open '
                   f'-- not a gap, skipping quietly.')
        return df_15m, None

    window = df_1m[(df_1m['time_stamp'] >= window_start) & (df_1m['time_stamp'] < boundary)]
    if window.empty:
        logger.error(f'No 1-min bars for {window_start:%H:%M}-{boundary:%H:%M} -- 15m bar SKIPPED.')
        return df_15m, None
    if len(window) < 8:
        logger.warning(f'Only {len(window)}/15 1-min bars for {window_start:%H:%M}-{boundary:%H:%M} '
                      f'-- building from incomplete data.')

    new_bar = pd.DataFrame([{
        'time_stamp': window_start, 'open': window['open'].iloc[0],
        'high': window['high'].max(), 'low': window['low'].min(),
        'close': window['close'].iloc[-1], 'volume': window['volume'].sum(),
    }])
    combined = _safe_concat([df_15m, new_bar], ignore_index=True)
    combined = combined.drop_duplicates(subset=['time_stamp'], keep='last').sort_values('time_stamp')
    updated = compute_st(combined.reset_index(drop=True), ST_PERIOD, ST_MULTIPLIER)
    row = updated[updated['time_stamp'] == window_start]
    return updated, (row.iloc[-1] if not row.empty and not pd.isna(row.iloc[-1]['trend']) else None)


def persist_15m_series(df_15m: pd.DataFrame) -> None:
    try:
        df_15m.to_csv(SERIES_15M_FILE, index=False)
    except Exception as e:
        logger.warning(f'persist_15m_series: failed to write: {e}')


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    logger.info('=' * 70)
    logger.info(f'fyers_st_probe starting -- {SYMBOL}, read-only, no trading logic.')
    logger.info('=' * 70)

    try:
        ensure_fresh_token()
        logger.info('Fyers headless login OK -- fresh access_token in user_credentials.csv.')
    except Exception as e:
        logger.critical(f'Fyers headless login failed -- cannot start. {e}')
        sys.exit(1)

    evening_only, reason = mcx_evening_only_today()
    session_open_time_today = EVENING_SESSION_OPEN_TIME if evening_only else SESSION_START_TIME
    if evening_only:
        wait_sec = max(0.0, (pd.Timestamp(f'{date.today()} {EVENING_SESSION_OPEN_TIME}')
                             - pd.Timestamp.now()).total_seconds() - EVENING_SESSION_WAKE_BUFFER_MIN * 60)
        logger.info(f'MCX morning session closed today ({reason}) -- deferring start until '
                   f'{EVENING_SESSION_OPEN_TIME} ({wait_sec / 60:.0f} min).')
        if wait_sec > 0:
            time.sleep(wait_sec)

    closing_time = _resolve_closing_time()
    logger.info(f'Session window: {session_open_time_today} -> {closing_time}')

    contract = resolve_fyers_contract()
    fyers_symbol = contract['fyers_symbol']
    session_open_today = pd.Timestamp(f'{date.today()} {session_open_time_today}')

    now = datetime.now()
    df_15m = seed(contract, now)
    if df_15m.empty:
        logger.critical('Initial seed failed -- cannot start. Exiting.')
        sys.exit(1)
    last = df_15m.iloc[-1]
    trend_str = ('bullish' if bool(last['trend']) else 'bearish') if not pd.isna(last['trend']) else 'warmup'
    logger.info(f'Seeded: {len(df_15m)} 15-min bars | trend={trend_str} ST={last.get("supertrend", float("nan")):.2f}')

    df_1m_today = read_today_cache(fyers_symbol)
    pending_recovery = []
    pending_15m_boundary = None
    pending_15m_deadline = None

    logger.info('Setup complete -- polling loop starting.')

    while _running:
        closing_ts = pd.Timestamp(f'{date.today()} {closing_time}')
        if datetime.now() >= closing_ts:
            logger.info(f'Session end ({closing_time}) reached -- stopping.')
            break

        # sleep to the next minute boundary (same zero-buffer fire pattern
        # as live_poll_probe.py / mcx_live_downloader.py's own probe), then
        # act -- no `continue` here, or the loop only ever sleeps and never
        # actually fetches anything.
        now = datetime.now()
        next_boundary_dt = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        sleep_sec = (next_boundary_dt - datetime.now()).total_seconds()
        if sleep_sec > 0:
            time.sleep(min(sleep_sec, 60))
        if not _running:
            break
        if datetime.now() >= closing_ts:
            logger.info(f'Session end ({closing_time}) reached -- stopping.')
            break
        boundary = datetime.now().replace(second=0, microsecond=0)

        # retry any pending windows first (non-blocking outer queue), same
        # shape as mcx_live_downloader.py's own pattern
        still_pending = []
        for (win_from, win_to) in pending_recovery:
            recovered = fetch_history(fyers_symbol, win_from, win_to, endpoint_label='recovery')
            if recovered is not None and not recovered.empty:
                save_today_cache(fyers_symbol, recovered)
                df_1m_today = (_safe_concat([df_1m_today, recovered], ignore_index=True)
                              .drop_duplicates(subset=['time_stamp'], keep='last')
                              .sort_values('time_stamp').reset_index(drop=True))
                logger.info(f'Recovered pending window [{win_from} -> {win_to}]')
            elif recovered is None:
                still_pending.append((win_from, win_to))
        pending_recovery = still_pending

        # regular per-minute poll
        win_to = boundary
        win_from = win_to - timedelta(minutes=LOOKBACK_MIN)
        df = fetch_history(fyers_symbol, win_from, win_to, endpoint_label='live')
        if df is not None:
            if not df.empty:
                save_today_cache(fyers_symbol, df)
                df_1m_today = (_safe_concat([df_1m_today, df], ignore_index=True)
                              .drop_duplicates(subset=['time_stamp'], keep='last')
                              .sort_values('time_stamp').reset_index(drop=True))
        else:
            pending_recovery.append((win_from, win_to))

        # 15m boundary
        if boundary.minute % 15 == 0 and pending_15m_boundary is None:
            pending_15m_boundary = boundary
            pending_15m_deadline = boundary + timedelta(minutes=DEFERRED_BAR_CUTOFF_MIN)

        if pending_15m_boundary is not None:
            pb = pending_15m_boundary
            window_start = pb - timedelta(minutes=15)
            window = df_1m_today[(df_1m_today['time_stamp'] >= window_start) & (df_1m_today['time_stamp'] < pb)]
            complete = len(window) >= 15
            past_cutoff = datetime.now() >= pending_15m_deadline
            pre_open = window_start < session_open_today
            if complete or past_cutoff or pre_open:
                df_15m, bar = build_15m_bar(df_1m_today, df_15m, pb, session_open_today)
                persist_15m_series(df_15m)
                if bar is not None:
                    flip = 'FLIP -> ' + ('bullish' if bool(bar['trend']) else 'bearish') if bool(bar['trend_flip']) else 'no flip'
                    logger.info(f'15m bar {window_start:%H:%M} -- ST={bar["supertrend"]:.2f} '
                               f'close={bar["close"]:.2f}  {flip}')
                pending_15m_boundary = None
                pending_15m_deadline = None

    logger.info('Session terminated.')


if __name__ == '__main__':
    main()
