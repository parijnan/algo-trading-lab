"""
Plan §3 -- Fyers live-polling reliability/accuracy probe, standalone and
explicitly separate from production (§3.2: no changes to
prometheus_production/, no live-feed migration decision made here, just
investigation).

Polls Fyers's regular History API (`/data/history`, confirmed working for
live/current contracts 2026-09-15 -- see plan §2.4a) for the current
CRUDEOILM front-month contract, at the same real cadence and zero-buffer
boundary-fire pattern Prometheus's own production loop and
data_pipeline/mcx_live_downloader.py's existing AB1021 diagnostic probe
both use. Logs every single call attempt (not just the cycle's final
outcome) to a CSV with the same column shape as the existing Angel One
probe log, so a side-by-side comparison is a straight file diff, not a
reformatting exercise.

**Deliberately does NOT also poll Angel One from this script.** A second
Angel One login here, running alongside Prometheus's own live session,
would risk reproducing the exact AB1007 session-collision incident this
plan's §7 exists to fix (2026-09-15, see project_prometheus_production
memory) -- a shared-account second login can silently break the OTHER
session's order-placement capability. The accuracy side of this test
(Fyers vs Angel One) is done as a SEPARATE, offline step afterward,
against Angel One data already collected by Prometheus's own live loop or
the nightly data_downloader_mcx.py run -- never a live duplicate login.

Usage (run from repo root, during real MCX trading hours):
  python research/fyers_mcx_validation/live_poll_probe.py --symbol MCX:CRUDEOILM26OCTFUT

Run for however many real trading days are wanted -- Ctrl-C to stop
(writes are flushed every call, so nothing is lost). Meant to run in a
screen/tmux session (or on Delos, matching Prometheus's own real network
path) for a multi-day, unattended stretch, not a single short invocation.
"""
import argparse
import csv
import json
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CREDS_FILE = REPO_ROOT / 'data' / 'user_credentials.csv'
LOG_DIR = Path(__file__).parent / 'live_poll_logs'
LOG_DIR.mkdir(exist_ok=True)

IST = timezone(timedelta(hours=5, minutes=30))
INNER_RETRY_ATTEMPTS = 3
INNER_RETRY_INTERVAL_SEC = 1
LOOKBACK_MIN = 5   # matches prometheus_functions.fetch_one_minute_window's own 5-min lookback

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')

PROBE_LOG_COLUMNS = [
    'call_ts', 'endpoint', 'symbol', 'attempt', 'success',
    'error_code', 'error_message', 'latency_ms', 'candle_count',
]

_running = True


def _handle_sigint(signum, frame):
    global _running
    print('\nCtrl-C received, finishing current cycle then stopping...')
    _running = False


signal.signal(signal.SIGINT, _handle_sigint)


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


def _log_probe_row(log_file: Path, row: dict):
    write_header = not log_file.exists()
    with open(log_file, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=PROBE_LOG_COLUMNS)
        if write_header:
            w.writeheader()
        w.writerow(row)


def _seconds_until_next_minute() -> float:
    now = datetime.now(IST)
    next_min = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
    return (next_min - now).total_seconds()


def fetch_with_retry(symbol: str, from_dt: datetime, to_dt: datetime, log_file: Path) -> dict | None:
    """Mirrors mcx_live_downloader.py's own inner-retry-burst pattern
    exactly (3 attempts, 1s apart, every attempt logged individually) so
    the resulting probe log is directly comparable to the existing Angel
    One AB1021 investigation's own log shape."""
    for attempt in range(1, INNER_RETRY_ATTEMPTS + 1):
        call_ts = datetime.now(IST)
        t0 = time.monotonic()
        params = {
            'symbol': symbol, 'resolution': '1', 'date_format': 0,
            'range_from': int(from_dt.timestamp()), 'range_to': int(to_dt.timestamp()),
            'cont_flag': 0,
        }
        full_url = f'https://api-t1.fyers.in/data/history?{urllib.parse.urlencode(params)}'
        try:
            req = urllib.request.Request(full_url, headers={'Authorization': _auth_header(), 'User-Agent': _UA})
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode())
            latency_ms = (time.monotonic() - t0) * 1000
            success = body.get('s') == 'ok'
            candles = body.get('candles', []) if success else []
            row = {
                'call_ts': call_ts.isoformat(), 'endpoint': 'fyers_data_history', 'symbol': symbol,
                'attempt': attempt, 'success': success,
                'error_code': None if success else body.get('code'),
                'error_message': None if success else body.get('message'),
                'latency_ms': round(latency_ms, 1), 'candle_count': len(candles),
            }
            _log_probe_row(log_file, row)
            if success:
                print(f'{call_ts:%H:%M:%S} OK  attempt {attempt}/{INNER_RETRY_ATTEMPTS} '
                      f'({len(candles)} candle(s), {latency_ms:.0f}ms)')
                return body
            else:
                print(f'{call_ts:%H:%M:%S} FAIL attempt {attempt}/{INNER_RETRY_ATTEMPTS}: '
                      f'{body.get("code")} {body.get("message")} ({latency_ms:.0f}ms)')
        except urllib.error.HTTPError as e:
            latency_ms = (time.monotonic() - t0) * 1000
            body_text = e.read().decode()
            print(f'{call_ts:%H:%M:%S} HTTPError {e.code} attempt {attempt}/{INNER_RETRY_ATTEMPTS}: '
                  f'{body_text} ({latency_ms:.0f}ms)')
            _log_probe_row(log_file, {
                'call_ts': call_ts.isoformat(), 'endpoint': 'fyers_data_history', 'symbol': symbol,
                'attempt': attempt, 'success': False, 'error_code': f'HTTP{e.code}',
                'error_message': body_text[:500], 'latency_ms': round(latency_ms, 1), 'candle_count': 0,
            })
        except Exception as e:
            latency_ms = (time.monotonic() - t0) * 1000
            print(f'{call_ts:%H:%M:%S} EXCEPTION attempt {attempt}/{INNER_RETRY_ATTEMPTS}: '
                  f'{e} ({latency_ms:.0f}ms)')
            _log_probe_row(log_file, {
                'call_ts': call_ts.isoformat(), 'endpoint': 'fyers_data_history', 'symbol': symbol,
                'attempt': attempt, 'success': False, 'error_code': 'EXCEPTION',
                'error_message': str(e)[:500], 'latency_ms': round(latency_ms, 1), 'candle_count': 0,
            })

        if attempt < INNER_RETRY_ATTEMPTS:
            time.sleep(INNER_RETRY_INTERVAL_SEC)

    print(f'  Inner burst exhausted for [{from_dt:%H:%M} -> {to_dt:%H:%M}]')
    return None


def save_candles(cache_file: Path, symbol: str, body: dict):
    """Append fetched candles to a running cache, dedup on timestamp --
    this is the data the offline accuracy comparison against Angel One
    will use afterward, mirroring the OHLCV schema everywhere else in
    this repo."""
    import pandas as pd
    candles = body.get('candles', [])
    if not candles:
        return
    rows = []
    for c in candles:
        ts = datetime.fromtimestamp(c[0], tz=IST).strftime('%Y-%m-%d %H:%M:%S+05:30')
        rows.append({'time_stamp': ts, 'open': c[1], 'high': c[2], 'low': c[3], 'close': c[4], 'volume': c[5]})
    new_df = pd.DataFrame(rows)
    if cache_file.exists():
        existing = pd.read_csv(cache_file)
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset='time_stamp').sort_values('time_stamp')
    else:
        combined = new_df
    combined.to_csv(cache_file, index=False)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--symbol', required=True, help='Live Fyers contract symbol, e.g. MCX:CRUDEOILM26OCTFUT')
    args = p.parse_args()

    today_str = datetime.now(IST).strftime('%Y%m%d')
    log_file = LOG_DIR / f'fyers_probe_{today_str}.csv'
    cache_file = LOG_DIR / f'fyers_candles_{today_str}.csv'

    print(f'Polling {args.symbol} every minute, zero-buffer boundary fire.')
    print(f'Probe log: {log_file}')
    print(f'Candle cache: {cache_file}')
    print('Ctrl-C to stop cleanly.\n')

    while _running:
        wait = _seconds_until_next_minute()
        time.sleep(wait)
        if not _running:
            break
        boundary = datetime.now(IST).replace(second=0, microsecond=0)
        from_dt = boundary - timedelta(minutes=LOOKBACK_MIN)
        body = fetch_with_retry(args.symbol, from_dt, boundary, log_file)
        if body:
            save_candles(cache_file, args.symbol, body)

    print('Stopped.')


if __name__ == '__main__':
    main()
