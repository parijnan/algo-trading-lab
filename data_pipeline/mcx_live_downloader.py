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

Third purpose, added 2026-08-28: a parallel WebSocket SNAP_QUOTE (mode 3)
subscription, run as a background thread alongside the REST polling loop —
same session window, own connection-resilience test (backoff/reconnect
pattern ported from websocket_feed.py's SharedFeed, since the WS side of
the AB1021 investigation is a genuinely different question: does a
*persistent* connection degrade the same way repeated REST calls do?).
Logs every tick's best-5 bid/ask levels (SNAP_QUOTE, not the SDK's DEPTH
mode/4 — that's a different, 20-level book with its own 50-token quota;
"5 best bids and asks" is SNAP_QUOTE) to data/mcx_snapquote_log.csv, for
liquidity/slippage analysis. One thing worth flagging: the installed SDK's
own SNAP_QUOTE parser (smartWebSocketV2.py's _parse_data) looks buggy on a
read — it assigns the inner best-5 parser's "sell" list to the outer
parsed_data["best_5_buy_data"] key and vice versa. Live-verified before
trusting it rather than "fixing" a suspected bug blind: the final output's
best_5_buy_data top price IS genuinely below best_5_sell_data's (correct
bid<ask ordering) — so despite how the code reads, don't re-swap these
fields; two things are very likely canceling out internally, and the
labels are correct as delivered.

Usage: python data_pipeline/mcx_live_downloader.py [--max-cycles N]
Intended to be cron-started at/after 15:30 IST; runs until SESSION_END_TIME.
--max-cycles is a smoke-test override, not for normal use.
"""

import argparse
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
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
SNAPQUOTE_LOG_FILE = os.path.join(dl.DATA_DIR, 'mcx_snapquote_log.csv')

MCX_FO = 5
SNAP_QUOTE_MODE = 3
WS_RECONNECT_BACKOFF_SEC = [5, 10, 20, 40, 60]   # matches websocket_feed.py's SharedFeed
WS_MAX_RECONNECT_ATTEMPTS = 5

# Deliberately 0 — fire the fetch exactly at the boundary, no artificial
# delay. Matches how Iris actually runs live: no buffer, no pre-emptive
# safety margin, just fire and let resilient retry/backoff (the inner
# 3-attempt/1s burst + outer pending-recovery queue) absorb whatever
# doesn't succeed on the first try. This is also the point for the AB1021
# investigation right now — testing the limits means polling as close to
# the boundary as possible, not backing off in advance of a problem that
# may not even occur. Iris's own CANDLE_POLL_JITTER_MS experiment (tried
# and reverted per plans/iris-signal-pipeline-hardening.md) went the other
# direction — testing a suspected inter-bot-collision theory by adding
# delay — and was abandoned before yielding a conclusion either way.
CANDLE_CLOSE_BUFFER_SEC = 0

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
    force=True,   # data_downloader_mcx (imported above) calls its own basicConfig()
                  # first, which silently claims the root logger — plain basicConfig()
                  # is a no-op once handlers exist, so without force=True this dedicated
                  # FileHandler never gets attached and the file stays empty all run.
)
logger = logging.getLogger(__name__)


def _mins_since_nse_close(ts: datetime) -> float:
    h, m = NSE_CLOSE_TIME.split(':')
    close_dt = ts.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    return (ts - close_dt).total_seconds() / 60


def _log_probe_row(row: dict):
    write_header = not os.path.exists(PROBE_LOG_FILE)
    pd.DataFrame([row]).to_csv(PROBE_LOG_FILE, mode='a', header=write_header, index=False)


def _sleep_until_next_boundary(buffer_sec: float = CANDLE_CLOSE_BUFFER_SEC) -> datetime:
    """
    Sleep until buffer_sec after the next whole-minute clock boundary and
    return that boundary (the instant the just-finished candle closed —
    e.g. returns 16:02:00 when woken to fetch the 16:01 candle).

    Re-derives the target from the actual current wall-clock time on every
    call rather than sleeping a fixed offset from the previous cycle's
    start — immune to drift. The old approach (`time.sleep(60 - elapsed)`
    at the end of each loop) accumulates whatever the previous cycle's own
    processing time was, so the fetch time slowly walks away from :00
    across a long unattended run instead of staying pinned to it.
    """
    now = datetime.now()
    boundary = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    target = boundary + timedelta(seconds=buffer_sec)
    sleep_for = (target - datetime.now()).total_seconds()
    if sleep_for > 0:
        time.sleep(sleep_for)
    return boundary


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


def _log_snapquote_tick(message: dict):
    """
    Flatten one SNAP_QUOTE tick (best-5 buy/sell levels + LTP context) into
    a single CSV row. Prices arrive as broker-scaled integers (raw/100),
    same convention as SharedFeed._on_data's last_traded_price handling.
    """
    row = {
        'tick_ts': datetime.now(),
        'symbol': SYMBOL,
        'exchange_ts': message.get('exchange_timestamp'),
        'sequence_number': message.get('sequence_number'),
        'ltp': _scale(message.get('last_traded_price')),
        'avg_traded_price': _scale(message.get('average_traded_price')),
        'total_buy_qty': message.get('total_buy_quantity'),
        'total_sell_qty': message.get('total_sell_quantity'),
        'volume_for_day': message.get('volume_trade_for_the_day'),
    }
    bids = message.get('best_5_buy_data') or []
    asks = message.get('best_5_sell_data') or []
    for i in range(5):
        b = bids[i] if i < len(bids) else {}
        a = asks[i] if i < len(asks) else {}
        row[f'bid{i + 1}_price']  = _scale(b.get('price'))
        row[f'bid{i + 1}_qty']    = b.get('quantity')
        row[f'bid{i + 1}_orders'] = b.get('no of orders')
        row[f'ask{i + 1}_price']  = _scale(a.get('price'))
        row[f'ask{i + 1}_qty']    = a.get('quantity')
        row[f'ask{i + 1}_orders'] = a.get('no of orders')

    write_header = not os.path.exists(SNAPQUOTE_LOG_FILE)
    pd.DataFrame([row]).to_csv(SNAPQUOTE_LOG_FILE, mode='a', header=write_header, index=False)


def _scale(raw):
    return None if raw is None else raw / 100.0


class SnapQuoteLogger:
    """
    Background WS thread: subscribes CRUDEOILM in SNAP_QUOTE mode (best-5
    bid/ask), logs every tick to SNAPQUOTE_LOG_FILE, and exercises its own
    connection-resilience test in parallel with the REST poller's. Reconnect
    logic ported directly from websocket_feed.py's SharedFeed
    (_trigger_reconnect / _reconnect_worker) — a dedicated worker thread
    with exponential backoff (WS_RECONNECT_BACKOFF_SEC), not a blocking
    sleep inside the on_error/on_close callback itself.
    """

    def __init__(self, auth_token, api_key, client_code, feed_token, token):
        self._auth_token  = auth_token
        self._api_key     = api_key
        self._client_code = client_code
        self._feed_token  = feed_token
        self._token       = token

        self._sws              = None
        self._ws_thread        = None
        self._reconnect_thread = None
        self._stop_requested   = False
        self._connected        = False
        self._lock              = threading.Lock()
        self.tick_count         = 0
        self.reconnect_count    = 0

    def start(self):
        self._sws = self._build_socket()
        self._ws_thread = threading.Thread(target=self._sws.connect, name='snapquote-ws', daemon=True)
        self._ws_thread.start()

        deadline = time.time() + 10
        while not self.is_connected() and time.time() < deadline:
            time.sleep(0.1)
        if not self.is_connected():
            logger.warning('SnapQuote WS did not connect within 10s of start — '
                            'reconnect worker will keep retrying in the background.')
            self._trigger_reconnect()
        else:
            logger.info('SnapQuote WS connected and subscribed.')

    def stop(self):
        self._stop_requested = True
        if self._ws_thread is None:
            return
        try:
            if self._sws.wsapp and self._sws.wsapp.sock:
                self._sws.wsapp.sock.close()
        except Exception:
            pass
        try:
            self._sws.close_connection()
        except Exception:
            pass
        self._ws_thread.join(timeout=5)

    def is_connected(self):
        with self._lock:
            return self._connected

    def _build_socket(self):
        sws = SmartWebSocketV2(self._auth_token, self._api_key, self._client_code,
                                self._feed_token, max_retry_attempt=0)
        sws.on_open  = self._on_open
        sws.on_data  = self._on_data
        sws.on_error = self._on_error
        sws.on_close = self._on_close
        return sws

    def _on_open(self, wsapp):
        with self._lock:
            self._connected = True
        try:
            self._sws.subscribe('snapquote_probe', SNAP_QUOTE_MODE,
                                 [{'exchangeType': MCX_FO, 'tokens': [self._token]}])
            logger.info(f'SnapQuote WS: subscribed {SYMBOL} (token {self._token}, mode SNAP_QUOTE).')
        except Exception as e:
            logger.warning(f'SnapQuote WS: subscribe call failed: {e}')

    def _on_data(self, wsapp, message):
        self.tick_count += 1
        try:
            _log_snapquote_tick(message)
        except Exception as e:
            logger.warning(f'SnapQuote WS: tick logging failed: {e}')

    def _on_error(self, wsapp, error):
        with self._lock:
            self._connected = False
        logger.warning(f'SnapQuote WS error: {error}')
        if not self._stop_requested:
            self._trigger_reconnect()

    def _on_close(self, wsapp):
        with self._lock:
            self._connected = False
        logger.warning('SnapQuote WS closed.')
        if not self._stop_requested:
            self._trigger_reconnect()

    def _trigger_reconnect(self):
        with self._lock:
            if self._reconnect_thread and self._reconnect_thread.is_alive():
                return
            t = threading.Thread(target=self._reconnect_worker, name='snapquote-ws-reconnect', daemon=True)
            self._reconnect_thread = t
        t.start()

    def _reconnect_worker(self):
        for attempt in range(WS_MAX_RECONNECT_ATTEMPTS):
            delay = WS_RECONNECT_BACKOFF_SEC[min(attempt, len(WS_RECONNECT_BACKOFF_SEC) - 1)]
            logger.warning(f'SnapQuote WS: reconnect attempt {attempt + 1}/{WS_MAX_RECONNECT_ATTEMPTS} in {delay}s.')

            end = time.time() + delay
            while time.time() < end:
                if self._stop_requested:
                    return
                time.sleep(0.5)
            if self._stop_requested:
                return

            try:
                new_sws = self._build_socket()
                new_thread = threading.Thread(target=new_sws.connect, name='snapquote-ws', daemon=True)
                with self._lock:
                    self._sws = new_sws
                    self._ws_thread = new_thread
                new_thread.start()

                deadline = time.time() + 10
                while not self.is_connected() and time.time() < deadline:
                    if self._stop_requested:
                        return
                    time.sleep(0.1)

                if self.is_connected():
                    self.reconnect_count += 1
                    logger.info(f'SnapQuote WS reconnected after {attempt + 1} attempt(s) '
                                f'(total reconnects this run: {self.reconnect_count}).')
                    return
            except Exception as e:
                logger.warning(f'SnapQuote WS: reconnect attempt {attempt + 1} failed: {e}')

        logger.error('SnapQuote WS: reconnect attempts exhausted, giving up. '
                      'REST poller continues independently.')


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

    api_key     = user_credentials_df.iloc[0].loc['api_key']
    client_code = user_credentials_df.iloc[0].loc['user_name']
    obj = SmartConnect(api_key=api_key)
    totp = TOTP(user_credentials_df.iloc[0].loc['qr_code']).now()
    resp = obj.generateSession(client_code, str(user_credentials_df.iloc[0].loc['password']), totp)
    auth_token = resp['data']['jwtToken']
    feed_token = obj.getfeedToken()
    logger.info(f'Authenticated. Polling {SYMBOL} every {POLL_INTERVAL_SEC}s until {SESSION_END_TIME}'
                + (f' (max {args.max_cycles} cycles)' if args.max_cycles else '') + f'. Log file: {_LOG_FILE}')

    instruments_df = pd.read_csv(dl.INSTRUMENT_MASTER_FILE)
    front_month = dl.select_front_month_contracts(instruments_df, [SYMBOL])
    row = front_month.iloc[0]
    token = str(row['token'])
    expiry_date = row['expiry_parsed'].to_pydatetime()
    filepath = dl.get_futures_filepath(SYMBOL, expiry_date)
    logger.info(f'Front-month: {row["symbol"]} (token {token}), writing to {filepath}')

    snapquote_logger = SnapQuoteLogger(auth_token, api_key, client_code, feed_token, token)
    snapquote_logger.start()

    h, m = SESSION_END_TIME.split(':')
    session_end = datetime.now().replace(hour=int(h), minute=int(m), second=0, microsecond=0)

    pending_recovery = []   # list of (from_dt, to_dt) windows that failed and need retry
    total_new_rows = 0
    cycles = 0
    run_start = datetime.now()

    while datetime.now() < session_end:
        # Block here until CANDLE_CLOSE_BUFFER_SEC after the next
        # whole-minute boundary — this is what makes the fetch fire right
        # after each 1-min candle closes (e.g. the 16:01 candle at 16:02),
        # rather than at a drifting offset determined by whenever the
        # script happened to start. Re-derived from wall-clock time every
        # cycle, so no accumulated drift across a long run.
        boundary = _sleep_until_next_boundary()
        if datetime.now() >= session_end:
            break  # boundary sleep carried us past session end — don't fire an extra cycle

        cycle_start = datetime.now()
        fire_delay_ms = (cycle_start - boundary).total_seconds() * 1000
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
        logger.info(f'Cycle {cycles} fired {fire_delay_ms:.0f}ms after the '
                    f'{boundary.strftime("%H:%M:%S")} boundary')
        logger.info(f'Cycle {cycles} done: +{cycle_new_rows} row(s) this cycle, '
                    f'{total_new_rows} total, {len(pending_recovery)} pending recovery')

        if args.max_cycles and cycles >= args.max_cycles:
            logger.info(f'--max-cycles={args.max_cycles} reached, stopping (smoke-test mode).')
            break
        # No sleep here — pacing is entirely owned by _sleep_until_next_boundary()
        # at the top of the loop, which re-derives the target from wall-clock
        # time every cycle rather than accumulating this cycle's own runtime.

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

    logger.info(f'SnapQuote WS: {snapquote_logger.tick_count} tick(s) logged, '
                f'{snapquote_logger.reconnect_count} reconnect(s), '
                f'connected at shutdown: {snapquote_logger.is_connected()}.')
    snapquote_logger.stop()

    try:
        obj.terminateSession(user_credentials_df.iloc[0].loc['user_name'])
        logger.info('Session terminated.')
    except Exception as e:
        logger.warning(f'Session termination warning: {e}')


if __name__ == '__main__':
    main()
