"""
data_pipeline/mcx_live_downloader.py — resilient live 1-minute CRUDEOILM
downloader, running from NSE close (15:30) through MCX close.

Two purposes:
  1. Genuinely useful live 1-min data. Writes into the SAME contract file
     data_downloader_mcx.py already maintains (dedup-on-timestamp merge,
     same convention) — this is the "maintained running CSV" idea from
     plans/prometheus-phase2-production.md §1, kept current live rather
     than only overnight.
  2. A diagnostic probe for the ongoing AngelOne AB1021 false-positive
     rate-limit investigation. Logs every single getCandleData call
     attempt (not just the final outcome per cycle) to
     data/ab1021_probe_log.csv, with enough context — timestamp, success/
     failure, error code, minutes since NSE close — to test whether AB1021
     hit-rate differs between NSE-open hours and MCX-only hours. This
     script only runs in the MCX-only window, so its log is one half of
     that comparison (the other half is whatever's already logged from
     NSE-hours polling, e.g. Iris's own candle-fetch history).

Resilience pattern ported from iris_production/iris.py's hardened
candle-fetch retry/backoff (see plans/iris-signal-pipeline-hardening.md):
  - Inner burst: 3 attempts, 1s apart, per fetch — every attempt logged
    individually to the probe log, not just the cycle's final outcome.
  - Outer, non-blocking retry: a cycle whose inner burst is exhausted
    marks that window as pending-recovery and moves on immediately —
    never blocks the next cycle's poll.
  - Missed-candle recovery: every subsequent cycle retries any pending
    windows first, merging recovered candles into history via the same
    dedup-and-sort merge used for the initial fetch (never a raw append).

Deliberately NOT using Iris's adaptive early-shutdown logic (no new trade
possible -> stop polling): for a data-collection tool, uniform coverage of
the whole post-NSE-close window matters more than efficiency, and polling
past MCX's actual close is itself useful data for the investigation
(confirms whether AB1021 rate drops once every exchange is done for the
day). SESSION_END_TIME is a fixed, generous buffer instead.

Usage: python data_pipeline/mcx_live_downloader.py [--max-cycles N]
Intended to be cron-started at/after 15:30 IST; runs until SESSION_END_TIME.
--max-cycles is a smoke-test override, not for normal use.
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
from SmartApi import SmartConnect
from pyotp import TOTP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data_downloader_mcx as dl  # noqa: E402

SYMBOL = 'CRUDEOILM'
POLL_INTERVAL_SEC = 60
INNER_RETRY_ATTEMPTS = 3
INNER_RETRY_INTERVAL_SEC = 1
NSE_CLOSE_TIME = '15:30'
SESSION_END_TIME = '23:55'   # generous buffer past the latest observed MCX close (23:45)

PROBE_LOG_FILE = os.path.join(dl.DATA_DIR, 'ab1021_probe_log.csv')

# Dated log file, matching Leto's logs/leto_YYYYMMDD.log naming convention.
# Written directly under data_pipeline/ — already covered by the existing
# `data_pipeline/*.log` gitignore pattern, no new entry needed. Both a
# console and file handler: this runs unattended for hours, so the file
# handler is the durable record if the invoking session/terminal goes away,
# while the console handler keeps live-tail visibility during the run.
_LOG_FILE = os.path.join(dl.BASE_DIR, f"mcx_live_downloader_{datetime.now().strftime('%Y%m%d')}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(), logging.FileHandler(_LOG_FILE)],
)
logger = logging.getLogger(__name__)


def _mins_since_nse_close(ts: datetime) -> float:
    h, m = NSE_CLOSE_TIME.split(':')
    close_dt = ts.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    return (ts - close_dt).total_seconds() / 60


def _log_probe_row(row: dict):
    write_header = not os.path.exists(PROBE_LOG_FILE)
    pd.DataFrame([row]).to_csv(PROBE_LOG_FILE, mode='a', header=write_header, index=False)


def _fetch_one_minute_window(obj, token: str, from_dt: datetime, to_dt: datetime):
    """
    Single getCandleData call for a narrow window, with the inner
    3-attempt/1s burst. Logs EVERY attempt (not just the final outcome) —
    full retry-level visibility is the point of this tool.
    Returns a DataFrame (possibly empty, on success) or None if the whole
    inner burst is exhausted.
    """
    from_str = from_dt.strftime('%Y-%m-%d %H:%M')
    to_str = to_dt.strftime('%Y-%m-%d %H:%M')

    for attempt in range(1, INNER_RETRY_ATTEMPTS + 1):
        call_ts = datetime.now()
        t0 = time.monotonic()
        try:
            response = obj.getCandleData({
                "exchange": "MCX",
                "symboltoken": token,
                "interval": "ONE_MINUTE",
                "fromdate": from_str,
                "todate": to_str,
            })
            latency_ms = (time.monotonic() - t0) * 1000
            raw = response.get("data") if isinstance(response, dict) else None
            success = raw is not None
            error_code = None if success else (response.get('errorcode') if isinstance(response, dict) else 'UNKNOWN')
            error_message = None if success else (response.get('message') if isinstance(response, dict) else str(response))

            _log_probe_row({
                'call_ts': call_ts, 'endpoint': 'getCandleData', 'symbol': SYMBOL,
                'attempt': attempt, 'success': success, 'error_code': error_code,
                'error_message': error_message,
                'mins_since_nse_close': round(_mins_since_nse_close(call_ts), 2),
                'latency_ms': round(latency_ms, 1),
            })

            if success:
                logger.info(f'Fetch OK [{from_str} -> {to_str}] attempt {attempt}/{INNER_RETRY_ATTEMPTS} '
                            f'({len(raw)} candle(s), {latency_ms:.0f}ms)')
                df = pd.DataFrame(raw, columns=dl.OHLCV_HEADERS)
                df['time_stamp'] = pd.to_datetime(df['time_stamp'], format=dl.METHOD_TS_FMT,
                                                   utc=False, errors='coerce')
                return df
            else:
                logger.warning(f'Fetch failed (JSON error response) [{from_str} -> {to_str}] '
                                f'attempt {attempt}/{INNER_RETRY_ATTEMPTS}: {error_code} — {error_message}')

        except Exception as e:
            latency_ms = (time.monotonic() - t0) * 1000
            msg = str(e)
            # Confirmed (this repo's AB1021 investigation): the rate-limit
            # rejection is the broker's raw response body surfacing as an
            # exception (fails json.loads() inside the SDK), not a
            # normal error-shaped JSON response.
            error_code = 'AB1021' if 'exceeding access rate' in msg else 'EXCEPTION'
            log_fn = logger.warning if error_code == 'AB1021' else logger.error
            log_fn(f'Fetch failed [{from_str} -> {to_str}] attempt {attempt}/{INNER_RETRY_ATTEMPTS}: '
                   f'{error_code} — {msg} ({latency_ms:.0f}ms)')
            _log_probe_row({
                'call_ts': call_ts, 'endpoint': 'getCandleData', 'symbol': SYMBOL,
                'attempt': attempt, 'success': False, 'error_code': error_code,
                'error_message': msg,
                'mins_since_nse_close': round(_mins_since_nse_close(call_ts), 2),
                'latency_ms': round(latency_ms, 1),
            })

        if attempt < INNER_RETRY_ATTEMPTS:
            time.sleep(INNER_RETRY_INTERVAL_SEC)

    logger.error(f'Inner burst exhausted [{from_str} -> {to_str}] — deferring to pending-recovery queue')
    return None  # inner burst exhausted


def _merge_and_save(filepath: str, new_df: pd.DataFrame) -> int:
    if new_df is None or new_df.empty:
        return 0
    on_disk = (pd.read_csv(filepath, parse_dates=['time_stamp'])
               if os.path.exists(filepath) else pd.DataFrame(columns=dl.OHLCV_HEADERS))
    if not on_disk.empty:
        on_disk['time_stamp'] = pd.to_datetime(on_disk['time_stamp'], utc=False, errors='coerce')
    new_df = new_df.copy()
    if new_df['time_stamp'].dt.tz is None:
        new_df['time_stamp'] = new_df['time_stamp'].dt.tz_localize('Asia/Kolkata')
    before = len(on_disk)
    merged = pd.concat([on_disk, new_df], ignore_index=True)
    merged['time_stamp'] = pd.to_datetime(merged['time_stamp'], utc=False, errors='coerce')
    merged.drop_duplicates(subset=['time_stamp'], keep='first', inplace=True)
    merged.sort_values('time_stamp', inplace=True)
    merged.reset_index(drop=True, inplace=True)
    new_rows = len(merged) - before
    save_df = merged.copy()
    save_df['time_stamp'] = save_df['time_stamp'].apply(lambda ts: dl.format_timestamp(ts, dl.OPTIONS_TS_FMT))
    save_df.to_csv(filepath, index=False)
    return new_rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--max-cycles', type=int, default=None,
                         help='Smoke-test override: stop after N cycles instead of running to SESSION_END_TIME.')
    args = parser.parse_args()

    user_credentials_df = pd.read_csv(os.path.join(dl.DATA_DIR, 'user_credentials_angel.csv'))
    dl.user_credentials_df = user_credentials_df

    obj = SmartConnect(api_key=user_credentials_df.iloc[0].loc['api_key'])
    totp = TOTP(user_credentials_df.iloc[0].loc['qr_code']).now()
    obj.generateSession(user_credentials_df.iloc[0].loc['user_name'],
                         str(user_credentials_df.iloc[0].loc['password']), totp)
    logger.info(f'Authenticated. Polling {SYMBOL} every {POLL_INTERVAL_SEC}s until {SESSION_END_TIME}'
                + (f' (max {args.max_cycles} cycles)' if args.max_cycles else '') + f'. Log file: {_LOG_FILE}')

    instruments_df = pd.read_csv(dl.INSTRUMENT_MASTER_FILE)
    front_month = dl.select_front_month_contracts(instruments_df, [SYMBOL])
    row = front_month.iloc[0]
    token = str(row['token'])
    expiry_date = row['expiry_parsed'].to_pydatetime()
    filepath = dl.get_futures_filepath(SYMBOL, expiry_date)
    logger.info(f'Front-month: {row["symbol"]} (token {token}), writing to {filepath}')

    h, m = SESSION_END_TIME.split(':')
    session_end = datetime.now().replace(hour=int(h), minute=int(m), second=0, microsecond=0)

    pending_recovery = []   # list of (from_dt, to_dt) windows that failed and need retry
    total_new_rows = 0
    cycles = 0
    run_start = datetime.now()

    while datetime.now() < session_end:
        cycle_start = datetime.now()
        cycle_new_rows = 0

        # Recover any pending windows first — never abandoned, retried every cycle.
        still_pending = []
        for (win_from, win_to) in pending_recovery:
            recovered = _fetch_one_minute_window(obj, token, win_from, win_to)
            if recovered is not None:
                n = _merge_and_save(filepath, recovered)
                cycle_new_rows += n
                total_new_rows += n
                logger.info(f'Recovered pending window [{win_from} -> {win_to}]: {n} row(s)')
            else:
                still_pending.append((win_from, win_to))
        pending_recovery = still_pending

        # Current cycle's candle — narrow window with a little redundancy.
        win_to = cycle_start
        win_from = cycle_start - timedelta(minutes=5)
        df = _fetch_one_minute_window(obj, token, win_from, win_to)
        if df is not None:
            n = _merge_and_save(filepath, df)
            cycle_new_rows += n
            total_new_rows += n
        else:
            pending_recovery.append((win_from, win_to))

        cycles += 1
        logger.info(f'Cycle {cycles} done: +{cycle_new_rows} row(s) this cycle, '
                    f'{total_new_rows} total, {len(pending_recovery)} pending recovery')

        if args.max_cycles and cycles >= args.max_cycles:
            logger.info(f'--max-cycles={args.max_cycles} reached, stopping (smoke-test mode).')
            break

        elapsed = (datetime.now() - cycle_start).total_seconds()
        time.sleep(max(0, POLL_INTERVAL_SEC - elapsed))

    total_ab1021 = 0
    total_other_errors = 0
    if os.path.exists(PROBE_LOG_FILE):
        log = pd.read_csv(PROBE_LOG_FILE, parse_dates=['call_ts'])
        this_run = log[log['call_ts'] >= run_start]
        total_ab1021 = int((this_run['error_code'] == 'AB1021').sum())
        total_other_errors = int((~this_run['success']).sum()) - total_ab1021

    logger.info(f'Session complete. Cycles: {cycles}. New rows: {total_new_rows}. '
                f'AB1021 hits: {total_ab1021}. Other errors: {total_other_errors}. '
                f'Still pending recovery: {len(pending_recovery)}.')
    if pending_recovery:
        logger.warning(f'{len(pending_recovery)} window(s) never recovered by session end: {pending_recovery}')

    try:
        obj.terminateSession(user_credentials_df.iloc[0].loc['user_name'])
        logger.info('Session terminated.')
    except Exception as e:
        logger.warning(f'Session termination warning: {e}')


if __name__ == '__main__':
    main()
