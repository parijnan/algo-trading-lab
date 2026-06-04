"""
Iris — Nifty directional scalping strategy.
ST_FAST (5m+15m supertrend), ITM-150 long call/put, nearest weekly expiry.

Lifecycle:
  Start  → arm watchdog (status=watching)
  Signal → enter long option (status=in_trade)
  Exit   → disarm or keep watching; exit triggers: profit target, stop loss,
            trend flip, time cutoff
  Stop   → graceful teardown

Paper mode is ON by default (PAPER_MODE=True in configs.py).
Set PAPER_MODE=False only after paper-trading parity is confirmed.
"""
import os
import sys
import csv
import signal
import time
import pandas as pd
from datetime import datetime, timedelta, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from configs import (
    PAPER_MODE, DATA_DIR, FLAG_PATH, PID_FILE, STATE_FILE, CREDS_FILE, HOLIDAYS_FILE,
    REPO_ROOT, LOT_COUNT, LOT_SIZE, NIFTY_TOKEN, VIX_TOKEN,
    ST_PERIOD, ST_MULTIPLIER, ENTRY_TF_MIN, REGIME_TF_MIN,
    PROFIT_TARGET_PCT, STOP_LOSS_PCT, MAX_HOLD_MIN, EXIT_BY_TIME,
    MARKET_OPEN, TRADE_UPDATE_SEC, INDEX_EXCHANGE, FO_EXCHANGE,
    SKIP_ENTRY_WINDOWS, MIN_ENTRY_TIME, MAX_ENTRY_TIME,
)
from state import IrisState, save_state, load_state
from logger_setup import get_logger
from functions import (
    seed_st, compute_st, fetch_candles, _candles_to_df,
    select_expiry, select_strike_and_token,
    place_order, verify_fill,
    check_no_active_strategies,
)

sys.path.insert(0, str(REPO_ROOT))
from websocket_feed import SharedFeed, EXCHANGE_NSE_CM
try:
    from leto_config import (SLACK_TRADEBOT_CHANNEL, SLACK_TRADE_ALERTS,
                              SLACK_TRADE_UPDATES, SLACK_ERRORS_CHANNEL)
except ImportError:
    SLACK_TRADEBOT_CHANNEL = None
    SLACK_TRADE_ALERTS     = None
    SLACK_TRADE_UPDATES    = None
    SLACK_ERRORS_CHANNEL   = None

logger = get_logger('iris')

def _load_slack_token() -> str:
    try:
        creds = pd.read_csv(REPO_ROOT / 'data/user_credentials.csv')
        return str(creds.iloc[0]['slack_token'])
    except Exception:
        return ''

_SLACK_TOKEN = _load_slack_token()


# ---------------------------------------------------------------------------
# Slack helper (fire-and-forget, non-blocking)
# ---------------------------------------------------------------------------

def _slack(msg: str, channel=None) -> None:
    if channel is None:
        channel = SLACK_TRADE_ALERTS
    if not channel or not _SLACK_TOKEN:
        return
    try:
        from slack_sdk import WebClient
        import threading
        def _send():
            try:
                WebClient(token=_SLACK_TOKEN).chat_postMessage(
                    channel=channel, text=msg)
            except Exception:
                pass
        threading.Thread(target=_send, daemon=True).start()
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Main Iris class
# ---------------------------------------------------------------------------

class Iris:
    def __init__(self, obj, auth_token: str, api_key: str,
                 client_code: str, instrument_df: pd.DataFrame):
        self.obj            = obj
        self.auth_token     = auth_token
        self._api_key       = api_key
        self._client_code   = client_code
        self.instrument_df  = instrument_df
        self.state         = load_state()
        self.feed          = None
        self._shutdown     = False

        # Live ST state — seeded in _setup()
        self._df_5m        = None   # 5-min bar history with supertrend
        self._df_15m       = None   # 15-min bar history with supertrend
        self._regime_trend = None   # most recent 15-min trend (True=bull, False=bear)

        # Exit signal handler
        signal.signal(signal.SIGINT,  self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info('Shutdown signal received.')
        self._shutdown = True

    # -----------------------------------------------------------------------
    # Setup / teardown
    # -----------------------------------------------------------------------

    def _setup(self) -> bool:
        logger.info(f'Iris starting  [PAPER_MODE={PAPER_MODE}]')
        _slack(f'{"[PAPER] " if PAPER_MODE else ""}⚡ *Iris* starting — '
               f'ST_FAST ITM-150, LOT_COUNT={LOT_COUNT}',
               SLACK_TRADEBOT_CHANNEL)

        # Seed signal
        now = datetime.now()
        self._df_5m, self._df_15m = seed_st(self.obj, now)
        if self._df_5m is None or self._df_5m.empty:
            logger.error('ST_FAST seed failed — cannot start.')
            return False

        # Prime regime trend from 15-min seed
        valid_15m = self._df_15m[self._df_15m['trend'].notna()]
        if not valid_15m.empty:
            self._regime_trend = bool(valid_15m['trend'].iloc[-1])
            logger.info(f'Initial 15-min regime: '
                        f'{"bullish" if self._regime_trend else "bearish"}')
        else:
            logger.warning('15-min ST warmup not complete — regime undefined.')

        # WebSocket feed (Nifty index LTP only; option token added on entry)
        feed_token = self.obj.getfeedToken()
        self.feed  = SharedFeed()
        self.feed.start(
            auth_token     = self.auth_token,
            api_key        = self._api_key,
            client_code    = self._client_code,
            feed_token     = feed_token,
            startup_tokens = [(EXCHANGE_NSE_CM, NIFTY_TOKEN)],
            alert_callback = lambda m: (_slack(f'⚠️ *Iris*: {m}', SLACK_ERRORS_CHANNEL),
                                        logger.warning(m)),
        )

        # If restarting mid-trade, restore in-trade state
        if self.state.status == 'in_trade' and self.state.token:
            logger.info('Resuming in-trade state.')
            self.feed.subscribe_options([self.state.token])

        self.state.status = 'watching'
        save_state(self.state)
        logger.info('Setup complete — watchdog armed.')
        return True

    def _teardown(self) -> None:
        if self.state.status == 'in_trade':
            logger.warning('Teardown with open position — exiting trade.')
            self._execute_exit('shutdown')

        if self.feed:
            try:
                self.feed.stop()
            except Exception:
                pass

        self.state.status = 'idle'
        save_state(self.state)
        logger.info('Iris stopped.')
        _slack(f'{"[PAPER] " if PAPER_MODE else ""}⏹ *Iris* stopped.',
               SLACK_TRADEBOT_CHANNEL)

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    def run(self) -> None:
        if not self._setup():
            return

        next_5m_close  = self._next_bar_close(datetime.now(), ENTRY_TF_MIN)
        last_update_ts = time.time()

        while not self._shutdown and FLAG_PATH.exists():
            now = datetime.now()

            # ── In-trade: tight exit loop ───────────────────────────────
            if self.state.status == 'in_trade':
                self._check_exit_conditions(now)

                # Periodic Slack update
                if time.time() - last_update_ts >= TRADE_UPDATE_SEC:
                    self._send_trade_update()
                    last_update_ts = time.time()

            # ── 5-min bar close: update ST, check entry/flip ────────────
            if now >= next_5m_close:
                candle = self._fetch_candle(next_5m_close, ENTRY_TF_MIN)
                if candle:
                    flip, direction = self._update_5m_st(candle)

                    # Update 15-min regime at each 15-min boundary
                    if next_5m_close.minute % REGIME_TF_MIN == 0:
                        self._update_15m_regime(next_5m_close)

                    if self.state.status == 'watching' and flip and direction:
                        if self._regime_aligned(direction):
                            if self._before_min_entry_time(now):
                                logger.info(f'Signal {direction} skipped — before MIN_ENTRY_TIME')
                            elif self._in_skip_window(now):
                                logger.info(f'Signal {direction} skipped — in skip window')
                            elif self._after_max_entry_time(next_5m_close):
                                logger.info(f'Signal {direction} skipped — after MAX_ENTRY_TIME')
                            else:
                                self._execute_entry(direction, now)

                    elif self.state.status == 'in_trade' and flip:
                        # Trend flip against open trade = exit
                        if direction and direction != self.state.direction:
                            self._execute_exit('trend_flip')

                next_5m_close += timedelta(minutes=ENTRY_TF_MIN)

            sleep_secs = 0.5 if self.state.status == 'in_trade' else 1.0
            time.sleep(sleep_secs)

        self._teardown()

    # -----------------------------------------------------------------------
    # Signal
    # -----------------------------------------------------------------------

    def _next_bar_close(self, now: datetime, tf_min: int) -> datetime:
        remainder = now.minute % tf_min
        to_add    = tf_min - remainder if remainder != 0 else tf_min
        return now.replace(second=0, microsecond=0) + timedelta(minutes=to_add)

    def _fetch_candle(self, close_ts: datetime, tf_min: int) -> dict | None:
        expected_open = close_ts - timedelta(minutes=tf_min)
        interval      = 'FIVE_MINUTE' if tf_min == 5 else 'FIFTEEN_MINUTE'
        fetch_from    = close_ts - timedelta(minutes=tf_min * 3)

        raw = fetch_candles(self.obj, NIFTY_TOKEN, interval, fetch_from, close_ts)
        if not raw:
            return None

        candles = _candles_to_df(raw)
        for _, row in candles.iterrows():
            if row['time_stamp'] == expected_open:
                return row.to_dict()
            if row['time_stamp'] == close_ts:
                d = row.to_dict()
                d['time_stamp'] = expected_open
                return d
        logger.debug(f'Expected candle {expected_open} not found in API response.')
        return None

    def _update_5m_st(self, candle: dict) -> tuple[bool, str | None]:
        """
        Append new 5-min candle to df_5m, recompute ST, detect flip.
        Returns (flip_occurred, new_direction).
        """
        new_row = pd.DataFrame([{
            'time_stamp': candle['time_stamp'],
            'open':  candle['open'],  'high': candle['high'],
            'low':   candle['low'],   'close': candle['close'],
            'volume': candle.get('volume', 0),
        }])
        combined    = pd.concat([self._df_5m, new_row], ignore_index=True)
        self._df_5m = compute_st(combined, ST_PERIOD, ST_MULTIPLIER)

        last = self._df_5m.iloc[-1]
        bar_ts = candle['time_stamp'].strftime('%H:%M')

        if pd.isna(last['trend']) or not last['trend_flip']:
            if not pd.isna(last['supertrend']):
                trend_str = 'bullish' if bool(last['trend']) else 'bearish'
                logger.info(f'Bar {bar_ts} — close={last["close"]:.2f}  '
                            f'ST={last["supertrend"]:.2f}  trend={trend_str}  no flip')
            return False, None

        direction = 'bullish' if bool(last['trend']) else 'bearish'
        logger.info(f'Bar {bar_ts} — close={last["close"]:.2f}  '
                    f'ST={last["supertrend"]:.2f}  FLIP → {direction}')
        return True, direction

    def _update_15m_regime(self, close_ts: datetime) -> None:
        candle = self._fetch_candle(close_ts, REGIME_TF_MIN)
        if not candle:
            return
        new_row = pd.DataFrame([{
            'time_stamp': candle['time_stamp'],
            'open': candle['open'], 'high': candle['high'],
            'low':  candle['low'],  'close': candle['close'],
            'volume': candle.get('volume', 0),
        }])
        combined     = pd.concat([self._df_15m, new_row], ignore_index=True)
        self._df_15m = compute_st(combined, ST_PERIOD, ST_MULTIPLIER)
        last = self._df_15m.iloc[-1]
        if not pd.isna(last['trend']):
            prev = self._regime_trend
            self._regime_trend = bool(last['trend'])
            if prev != self._regime_trend:
                logger.info(f'15-min regime flipped → '
                            f'{"bullish" if self._regime_trend else "bearish"}')

    def _before_min_entry_time(self, ts) -> bool:
        from datetime import datetime as _dt
        min_t = _dt.strptime(MIN_ENTRY_TIME, '%H:%M').time()
        return ts.time() < min_t

    def _after_max_entry_time(self, ts) -> bool:
        from datetime import datetime as _dt
        max_t = _dt.strptime(MAX_ENTRY_TIME, '%H:%M').time()
        return ts.time() > max_t

    def _in_skip_window(self, ts) -> bool:
        from datetime import datetime as _dt
        for start_str, end_str in SKIP_ENTRY_WINDOWS:
            start = _dt.strptime(start_str, '%H:%M').time()
            end   = _dt.strptime(end_str,   '%H:%M').time()
            if start <= ts.time() < end:
                return True
        return False

    def _regime_aligned(self, direction: str) -> bool:
        if self._regime_trend is None:
            logger.info('Regime undefined — skipping signal.')
            return False
        aligned = (direction == 'bullish') == self._regime_trend
        if not aligned:
            regime_str = 'bullish' if self._regime_trend else 'bearish'
            logger.info(f'Signal {direction} against 15-min regime ({regime_str}) — skipped.')
        return aligned

    # -----------------------------------------------------------------------
    # Entry
    # -----------------------------------------------------------------------

    def _execute_entry(self, direction: str, now: datetime) -> None:
        spot = self.feed.get_ltp(NIFTY_TOKEN)
        if not spot:
            logger.error('Nifty LTP unavailable — cannot enter.')
            return

        today  = now.date()
        expiry = select_expiry(self.instrument_df, today)
        if not expiry:
            logger.error('No valid expiry found — cannot enter.')
            return

        strike, option_type, symbol, token = select_strike_and_token(
            self.instrument_df, spot, direction, expiry)
        if not symbol:
            return

        order_id = place_order(self.obj, 'BUY', symbol, token,
                               LOT_COUNT, PAPER_MODE)
        if not order_id:
            logger.error('Entry order failed.')
            return

        fill_price = verify_fill(self.obj, order_id, symbol, LOT_COUNT, PAPER_MODE)
        if fill_price is None and not PAPER_MODE:
            logger.error('Fill verification failed — position state unknown.')
            return

        # In paper mode use current LTP as proxy entry price
        if fill_price is None:
            fill_price = self.feed.get_ltp(token) or 0.0

        # Subscribe to option LTP for monitoring (paper and live both need it)
        self.feed.subscribe_options([token])

        self.state.status      = 'in_trade'
        self.state.direction   = direction
        self.state.option_type = option_type
        self.state.strike      = strike
        self.state.expiry      = expiry.isoformat()
        self.state.symbol      = symbol
        self.state.token       = token
        self.state.entry_price = fill_price
        self.state.entry_spot  = spot
        self.state.entry_time  = now.isoformat()
        self.state.lots        = LOT_COUNT
        save_state(self.state)

        msg = (f'{"[PAPER] " if PAPER_MODE else ""}⚡ *Iris*: Entered {direction.upper()}\n'
               f'Nifty: {spot:.0f} | {strike}{option_type.upper()} {expiry}\n'
               f'Entry premium: {fill_price:.1f} pts | Lots: {LOT_COUNT}\n'
               f'Stop: -{STOP_LOSS_PCT*100:.0f}%  Target: +{PROFIT_TARGET_PCT*100:.0f}%  '
               f'Exit by: {EXIT_BY_TIME}')
        logger.info(msg.replace('\n', '  '))
        _slack(msg)

    # -----------------------------------------------------------------------
    # Exit
    # -----------------------------------------------------------------------

    def _check_exit_conditions(self, now: datetime) -> None:
        if self.state.status != 'in_trade':
            return

        ltp = self.feed.get_ltp(self.state.token or NIFTY_TOKEN)
        if ltp:
            self.state.last_ltp = ltp
            save_state(self.state)

        entry = self.state.entry_price or 0
        if entry <= 0 or not ltp:
            return

        pnl_pct = (ltp - entry) / entry

        if pnl_pct >= PROFIT_TARGET_PCT:
            self._execute_exit(f'profit_target ({pnl_pct:+.1%})')
            return

        if pnl_pct <= -STOP_LOSS_PCT:
            self._execute_exit(f'stop_loss ({pnl_pct:+.1%})')
            return

        # Per-trade max hold
        if self.state.entry_time:
            entry_dt = datetime.fromisoformat(self.state.entry_time)
            if (now - entry_dt).total_seconds() >= MAX_HOLD_MIN * 60:
                self._execute_exit(f'max_hold ({MAX_HOLD_MIN}m)')
                return

        exit_time = datetime.strptime(EXIT_BY_TIME, '%H:%M').time()
        if now.time() >= exit_time:
            self._execute_exit(f'time_cutoff ({EXIT_BY_TIME})')

    def _execute_exit(self, reason: str) -> None:
        if self.state.status != 'in_trade':
            return

        symbol = self.state.symbol
        token  = self.state.token
        lots   = self.state.lots
        entry  = self.state.entry_price or 0

        order_id  = place_order(self.obj, 'SELL', symbol, token, lots, PAPER_MODE)
        fill_price = verify_fill(self.obj, order_id, symbol, lots, PAPER_MODE)
        if fill_price is None:
            fill_price = self.feed.get_ltp(token) or entry

        pnl_pts = fill_price - entry                    # option points per unit
        pnl_rs  = pnl_pts * lots * LOT_SIZE            # total rupees

        self.feed.unsubscribe_options([token])

        self.state.status = 'watching'
        save_state(self.state)

        msg = (f'{"[PAPER] " if PAPER_MODE else ""}✅ *Iris*: Exited — {reason}\n'
               f'{self.state.strike}{(self.state.option_type or "").upper()} | '
               f'Entry {entry:.1f} → Exit {fill_price:.1f} | '
               f'P&L: {pnl_pts:+.1f} pts  ₹{pnl_rs:+,.0f}')
        logger.info(msg.replace('\n', '  '))
        _slack(msg)

        # Clear trade fields on state
        self.state.direction = self.state.option_type = self.state.strike = None
        self.state.expiry = self.state.symbol = self.state.token = None
        self.state.entry_price = self.state.entry_spot = self.state.entry_time = None
        self.state.lots = 0
        save_state(self.state)

    # -----------------------------------------------------------------------
    # Status update
    # -----------------------------------------------------------------------

    def _send_trade_update(self) -> None:
        ltp   = self.feed.get_ltp(self.state.token or '') or 0
        entry = self.state.entry_price or 0
        if entry > 0:
            pnl_pct = (ltp - entry) / entry
            pnl_rs  = (ltp - entry) * (self.state.lots or 0) * LOT_SIZE
            msg = (f'{"[PAPER] " if PAPER_MODE else ""}📊 *Iris* update: '
                   f'{self.state.symbol}  LTP={ltp:.1f}  '
                   f'P&L={pnl_pct:+.1%}  ₹{pnl_rs:+,.0f}')
            logger.info(f'Update: {self.state.symbol}  LTP={ltp:.1f}  '
                        f'P&L={pnl_pct:+.1%}  ₹{pnl_rs:+,.0f}')
            _slack(msg, SLACK_TRADE_UPDATES)


# ---------------------------------------------------------------------------
# Standalone login and entry point
# ---------------------------------------------------------------------------

def _login() -> tuple:
    """Login to Angel One. Returns (obj, auth_token, api_key, client_code)."""
    import pyotp
    from SmartApi import SmartConnect

    creds       = pd.read_csv(CREDS_FILE)
    row         = creds.iloc[0]
    api_key     = str(row['api_key'])
    client_code = str(row['client_id'])

    obj  = SmartConnect(api_key=api_key)
    totp = pyotp.TOTP(str(row['totp_token'])).now()
    resp = obj.generateSession(client_code, str(row['password']), totp)
    if not resp.get('status'):
        raise RuntimeError(f'Angel One login failed: {resp}')

    auth_token = resp['data']['jwtToken']
    logger.info(f'Logged in as {client_code}')
    return obj, auth_token, api_key, client_code


def _load_instrument_df() -> pd.DataFrame:
    """Download Nifty scrip master from the public Angel One URL."""
    from urllib.request import urlopen
    from io import StringIO
    SCRIP_MASTER_URL = ('https://margincalculator.angelbroking.com/'
                        'OpenAPI_File/files/OpenAPIScripMaster.json')
    logger.info('Downloading Nifty scrip master...')
    df = pd.read_json(StringIO(urlopen(SCRIP_MASTER_URL).read().decode()))
    df.columns = [c.lower() for c in df.columns]
    nifty = df[(df['exch_seg'] == 'NFO') & (df['name'] == 'NIFTY')].copy()
    nifty['strike'] = pd.to_numeric(nifty.get('strike', 0), errors='coerce')
    logger.info(f'Scrip master: {len(nifty):,} Nifty option rows')
    return nifty


def main():
    DATA_DIR.mkdir(exist_ok=True)

    # Guardian check
    ok, reason = check_no_active_strategies()
    if not ok:
        print(f'ERROR: Cannot start Iris — {reason}')
        print('Iris requires an exclusive Angel One session.')
        sys.exit(1)

    # Kill any existing Iris process before starting
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            os.kill(old_pid, signal.SIGTERM)
            logger.info(f'Sent SIGTERM to existing Iris process (PID {old_pid}); waiting...')
            time.sleep(3)
        except (ProcessLookupError, ValueError):
            pass
        PID_FILE.unlink(missing_ok=True)

    PID_FILE.write_text(str(os.getpid()))
    FLAG_PATH.touch()   # arm the watchdog
    logger.info('iris_active.flag created.')

    try:
        obj, auth_token, api_key, client_code = _login()
        instrument_df = _load_instrument_df()
        iris = Iris(obj, auth_token, api_key, client_code, instrument_df)
        iris.run()
    except KeyboardInterrupt:
        logger.info('KeyboardInterrupt.')
    except Exception as e:
        logger.exception(f'Unhandled exception: {e}')
        _slack(f'🚨 *Iris* crashed: {e}', SLACK_ERRORS_CHANNEL)
    finally:
        FLAG_PATH.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)
        try:
            obj.terminateSession(str(pd.read_csv(CREDS_FILE).iloc[0]['client_id']))
        except Exception:
            pass
        logger.info('Session terminated.')


if __name__ == '__main__':
    main()
