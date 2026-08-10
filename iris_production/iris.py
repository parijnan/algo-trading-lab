"""
Iris — Nifty directional scalping strategy.
ST_FAST (5m+15m supertrend), ITM-150 long call/put, nearest weekly expiry.

Lifecycle:
  Start  → arm watchdog (status=watching)
  Signal → enter long option (status=in_trade)
  Exit   → disarm or keep watching; exit triggers: profit target, stop loss,
            trend flip, time cutoff
  Stop   → graceful teardown

DRY_RUN is ON by default (DRY_RUN=True in configs.py).
Set DRY_RUN=False only after paper-trading parity is confirmed.
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

from iris_configs import (
    DRY_RUN, DATA_DIR, FLAG_PATH, PID_FILE, STATE_FILE, CREDS_FILE, HOLIDAYS_FILE,
    REPO_ROOT, LOT_COUNT, LOT_SIZE, LOT_CALC, CASH_PER_LOT_REQUIRED,
    NIFTY_TOKEN, VIX_TOKEN,
    ST_PERIOD, ST_MULTIPLIER, ENTRY_TF_MIN, REGIME_TF_MIN,
    PROFIT_TARGET_PCT, STOP_LOSS_PCT, MAX_HOLD_MIN, EXIT_BY_TIME,
    MARKET_OPEN, MARKET_CLOSE, TRADE_UPDATE_SEC, INDEX_EXCHANGE, FO_EXCHANGE,
    SKIP_ENTRY_WINDOWS, MIN_ENTRY_TIME, MAX_ENTRY_TIME,
    CANDLE_FETCH_RETRIES, CANDLE_FETCH_RETRY_INTERVAL, CANDLE_POLL_JITTER_MS,
)
from iris_state import IrisState, save_state, load_state
from iris_logger_setup import get_logger
from iris_functions import (
    seed_st, compute_st, fetch_candles, _candles_to_df,
    _resample_to_15m, persist_series,
    select_expiry, select_strike_and_token,
    place_order, get_fill_price, OrderFillWatcher,
    check_no_active_strategies, fetch_ltp_rest,
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
    def __init__(self, obj, auth_token: str, instrument_df: pd.DataFrame,
                 api_key: str = None, client_code: str = None):
        self.obj            = obj
        self.auth_token     = auth_token
        if api_key is None or client_code is None:
            _creds       = pd.read_csv(CREDS_FILE).iloc[0]
            api_key      = api_key      or str(_creds['api_key'])
            client_code  = client_code  or str(_creds['user_name'])
        self._api_key       = api_key
        self._client_code   = client_code
        self.instrument_df    = instrument_df
        self.state            = load_state()
        self.feed             = None
        self.order_watcher    = OrderFillWatcher()
        self._shutdown        = False

        # Session summary for Leto session report
        self._summary = {
            'strategy':        'Iris',
            'traded':          False,
            'no_trade_reason': 'No signal',
        }
        self._trade_count   = 0
        self._total_pnl_pts = 0.0
        self._total_pnl_rs  = 0.0
        self._peak_pnl_pts  = 0.0

        # Live ST state — seeded in _setup()
        self._df_5m        = None   # 5-min bar history with supertrend
        self._df_15m       = None   # 15-min bar history with supertrend
        self._regime_trend = None   # most recent 15-min trend (True=bull, False=bear)

        # Candle-fetch retry/backoff (§1) — non-blocking: the run loop keeps
        # ticking (in-trade exit checks, Slack commands) while a retry is
        # pending; only a real API call is gated behind _next_candle_retry_at.
        self._candle_retry_count   = 0
        self._next_candle_retry_at = None   # datetime | None
        self._missed_candle_ts_list = []    # bar-close timestamps still owed after exhausting retries

        # Exit signal handler
        signal.signal(signal.SIGINT,  self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info('Shutdown signal received.')
        self._shutdown = True

    # -----------------------------------------------------------------------
    # Lot sizing
    # -----------------------------------------------------------------------

    def _calculate_lots(self) -> int:
        """
        LOT_CALC = False: return LOT_COUNT from configs (manual control).
        LOT_CALC = True:  auto-calculate from available pure cash.
                          Iris buys naked long options; only cash (not collateral) is usable.
                          Lots = max(1, pure_cash // CASH_PER_LOT_REQUIRED).
        """
        if not LOT_CALC:
            logger.debug(f"Lot sizing: fixed LOT_COUNT={LOT_COUNT}")
            return LOT_COUNT
        try:
            rms         = self.obj.rmsLimit()['data']
            total_power = float(rms['availablecash'])
            collateral  = float(rms['collateral'])
            pure_cash   = round(total_power - collateral, 2)
            lots        = max(1, int(pure_cash // CASH_PER_LOT_REQUIRED))
            logger.info(
                f"Lot sizing: Total Power={total_power:,.0f} | Pure Cash={pure_cash:,.0f} | "
                f"CASH_PER_LOT={CASH_PER_LOT_REQUIRED:,} | Final Lots={lots}")
            return lots
        except Exception as e:
            logger.warning(f"rmsLimit() failed ({e}) — falling back to LOT_COUNT={LOT_COUNT}")
            return LOT_COUNT

    # -----------------------------------------------------------------------
    # Circuit breaker
    # -----------------------------------------------------------------------

    def _check_slack_commands(self) -> None:
        """
        Check for persistent Slack command flags during the live session.
        Handles EXIT (liquidate + halt) and KILL (halt immediately, positions held).
        DISABLE is a startup gate handled by Leto; no action is taken here.
        """
        flag_path = REPO_ROOT / 'data' / 'SLACK_COMMAND.flag'
        if not flag_path.exists():
            return
        try:
            command = flag_path.read_text().strip()

            if command == "EXIT":
                msg = "⚠️ *Iris*: Slack `Exit Trade` detected. Liquidating..."
                logger.critical(msg.replace('*', ''))
                _slack(msg, SLACK_TRADE_ALERTS)
                if self.state.status == 'in_trade':
                    self._execute_exit(reason='slack_exit')
                raise Exception("Session terminated by Slack !exit command.")

            elif command == "KILL":
                msg = "🚨 *Iris*: Slack `Kill Switch` detected. Dropping control immediately."
                logger.critical(msg.replace('*', ''))
                _slack(msg, SLACK_TRADE_ALERTS)
                # State left as-is so the position can be resumed or managed manually.
                raise Exception("Session terminated by Slack !kill command.")

            # DISABLE: do nothing here. Leto catches it at next startup.

        except Exception as e:
            if "Session terminated" in str(e): raise
            logger.error(f"Error reading slack command flag: {e}")

    # -----------------------------------------------------------------------
    # Setup / teardown
    # -----------------------------------------------------------------------

    def _setup(self) -> bool:
        logger.info(f'Iris starting  [DRY_RUN={DRY_RUN}]')
        _slack(f'{"[PAPER] " if DRY_RUN else ""}⚡ *Iris* starting — '
               f'ST_FAST ITM-150, LOT_COUNT={LOT_COUNT}',
               SLACK_TRADEBOT_CHANNEL)

        # Seed signal
        now = datetime.now()
        self._df_5m, self._df_15m = seed_st(self.obj, now)
        if self._df_5m is None or self._df_5m.empty:
            logger.error('ST_FAST seed failed — cannot start.')
            return False

        # Incident 2026-08-10: Path B (today's live poll) can fail all its
        # retries and return nothing, leaving the seed with only past-day
        # data — silently stale (5m/15m trend reflects yesterday's close,
        # not today's actual state) rather than an outright seed failure.
        # _build_today_5m now retries harder, but surface it loudly here too
        # in case it still comes up empty, rather than let it pass unnoticed
        # like it did that day.
        today = now.date()
        todays_5m_rows = self._df_5m[self._df_5m['time_stamp'].dt.date == today]
        first_bar_close = datetime.combine(
            today, datetime.strptime(MARKET_OPEN, '%H:%M').time()) + timedelta(minutes=ENTRY_TF_MIN)
        if todays_5m_rows.empty and now >= first_bar_close:
            logger.error(f'Seed has ZERO today candles despite market open since {MARKET_OPEN} — '
                         f'5m/15m trend reflects only past days and may be stale until the next '
                         f'live candle corrects it.')
            _slack(f'🚨 *Iris*: Seeded with **no today candles** (past-day data only) — '
                   f'5m/15m trend may be stale until the next live poll corrects it. '
                   f'Likely cause: candle fetch was rate-limited at startup.',
                   SLACK_ERRORS_CHANNEL)

        # Prime regime trend from 15-min seed
        valid_15m = self._df_15m[self._df_15m['trend'].notna()]
        if not valid_15m.empty:
            self._regime_trend = bool(valid_15m['trend'].iloc[-1])
            logger.info(f'Initial 15-min regime: '
                        f'{"bullish" if self._regime_trend else "bearish"}')
        else:
            logger.warning('15-min ST warmup not complete — regime undefined.')

        # §4: report seed completion + initial regime state to Slack (Apollo parity —
        # previously Iris's startup message was generic and said nothing about the
        # seeded regime, unlike Apollo's "Supertrend seeded (N candles). 75-min
        # trend: bullish/bearish.").
        last5 = self._df_5m.iloc[-1]
        trend5_str = ('bullish' if bool(last5['trend']) else 'bearish') \
                     if not pd.isna(last5['trend']) else 'warmup'
        regime_str = ('bullish' if self._regime_trend else 'bearish') \
                     if self._regime_trend is not None else 'warmup'
        _slack(f'{"[PAPER] " if DRY_RUN else ""}✅ *Iris*: Supertrend seeded '
               f'({len(self._df_5m)} 5m / {len(self._df_15m)} 15m bars). '
               f'5m trend: {trend5_str} · 15m regime: {regime_str}',
               SLACK_TRADEBOT_CHANNEL)

        self._check_missed_flip_at_startup()

        # WebSocket price feed (Nifty index LTP only; option token added on entry)
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

        # Order-update WebSocket (live only — dry run skips)
        if not DRY_RUN:
            self.order_watcher.start(
                auth_token   = self.auth_token,
                api_key      = self._api_key,
                client_code  = self._client_code,
                feed_token   = feed_token,
            )

        # If restarting mid-trade, restore in-trade state
        if self.state.status == 'in_trade' and self.state.token:
            logger.info('Resuming in-trade state.')
            self.feed.subscribe_options([self.state.token])

        self.state.status = 'watching'
        save_state(self.state)
        logger.info('Setup complete — watchdog armed.')
        return True

    def _check_missed_flip_at_startup(self) -> None:
        """
        Alert-only missed-flip detection (§4). If today's seeded 5m or 15m
        series already contains a flip by the time this session starts (a
        fresh start well after market open, or a restart after downtime),
        report it to Slack — otherwise it's only ever visible by reading the
        log file after the fact, exactly what the original §0 incident
        investigation required.

        Deliberately alert-only, unlike Apollo's get_last_completed_flip()
        which auto-enters if the window's still open: a flip discovered
        after an unknown restart delay means entering partway into a move
        rather than at its start, which doesn't fit a strategy built around
        a short, controlled hold time. A human decides whether to act.
        """
        if self.state.status == 'in_trade':
            return

        today = datetime.now().date()
        for label, dff in (('5m', self._df_5m), ('15m', self._df_15m)):
            if dff is None or dff.empty:
                continue
            todays_flips = dff[(dff['time_stamp'].dt.date == today) & (dff['trend_flip'] == True)]
            if todays_flips.empty:
                continue
            flip = todays_flips.iloc[-1]
            direction = 'bullish' if bool(flip['trend']) else 'bearish'
            logger.info(f'Missed-flip check — {label}: last flip today at '
                        f'{flip["time_stamp"]:%H:%M} → {direction} '
                        f'(close={flip["close"]:.2f}, ST={flip["supertrend"]:.2f})')
            _slack(f'ℹ️ *Iris*: {label} flip earlier today at {flip["time_stamp"]:%H:%M} → '
                   f'*{direction}* (close={flip["close"]:.2f}, ST={flip["supertrend"]:.2f}). '
                   f'Not auto-acting — review manually if still relevant.',
                   SLACK_TRADEBOT_CHANNEL)

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
        _slack(f'{"[PAPER] " if DRY_RUN else ""}⏹ *Iris* stopped.',
               SLACK_TRADEBOT_CHANNEL)

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    def run(self) -> tuple[bool, dict]:
        if not self._setup():
            self._summary['no_trade_reason'] = 'Setup failed'
            return False, self._summary

        next_5m_close  = self._next_bar_close(datetime.now(), ENTRY_TF_MIN)
        last_update_ts = time.time()

        market_close = datetime.strptime(MARKET_CLOSE, '%H:%M').time()

        try:
            while not self._shutdown and FLAG_PATH.exists():
                self._check_slack_commands()
                now = datetime.now()

                # ── Market-close auto-shutdown ──────────────────────────
                if now.time() >= market_close:
                    logger.info(f'Market closed ({MARKET_CLOSE}) — shutting down.')
                    _slack(f'{"[PAPER] " if DRY_RUN else ""}⏹ *Iris* — market closed. Shutting down.',
                           SLACK_TRADEBOT_CHANNEL)
                    break

                # ── In-trade: tight exit loop ───────────────────────────
                if self.state.status == 'in_trade':
                    self._check_exit_conditions(now)

                    # Periodic Slack update
                    if time.time() - last_update_ts >= TRADE_UPDATE_SEC:
                        self._send_trade_update()
                        last_update_ts = time.time()

                # ── 5-min bar close: update ST, check entry/flip ────────
                # Retry/backoff (§1) is non-blocking: a failed fetch schedules
                # _next_candle_retry_at and falls through to the loop's normal
                # 0.5-1s sleep, so in-trade exit checks above keep running
                # every tick instead of freezing for up to
                # CANDLE_FETCH_RETRIES × CANDLE_FETCH_RETRY_INTERVAL seconds.
                if now >= next_5m_close:
                    ready_to_try = (self._next_candle_retry_at is None or
                                    now >= self._next_candle_retry_at)
                    if ready_to_try:
                        # Jitter (untested hypothesis, 2026-08-10): a small delay
                        # before the FIRST poll at this boundary only, to avoid
                        # firing at the exact clock tick where every other bot on
                        # the broker may also be polling. Not applied to retries
                        # -- those are already offset by a second or more.
                        if self._candle_retry_count == 0 and CANDLE_POLL_JITTER_MS > 0:
                            time.sleep(CANDLE_POLL_JITTER_MS / 1000)

                        candle = self._fetch_candle(next_5m_close, ENTRY_TF_MIN)

                        if candle:
                            if self._candle_retry_count > 0:
                                logger.info(f'Candle recovered on retry '
                                            f'{self._candle_retry_count} for {next_5m_close:%H:%M}.')
                                _slack(f'*Iris*: Candle data recovered on retry '
                                       f'{self._candle_retry_count} for {next_5m_close:%H:%M}. '
                                       f'Resuming normally.', SLACK_ERRORS_CHANNEL)
                            self._candle_retry_count   = 0
                            self._next_candle_retry_at = None

                            self._recover_missed_5m_candles()

                            flip, direction = self._update_5m_st(candle)

                            # Update 15-min regime at each 15-min boundary
                            if next_5m_close.minute % REGIME_TF_MIN == 0:
                                self._update_15m_regime(next_5m_close)

                            persist_series(self._df_5m, self._df_15m)

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

                        else:
                            self._candle_retry_count += 1
                            if self._candle_retry_count > CANDLE_FETCH_RETRIES:
                                logger.error(f'Candle unavailable for {next_5m_close:%H:%M} after '
                                             f'{CANDLE_FETCH_RETRIES} retries. ST will be incomplete '
                                             f'for this bar; will keep trying to recover it in the '
                                             f'background before future bars.')
                                _slack(f'🚨 *Iris*: Candle unavailable for {next_5m_close:%H:%M} '
                                       f'after {CANDLE_FETCH_RETRIES} retries. ST incomplete for this '
                                       f'bar — will keep retrying in the background.',
                                       SLACK_ERRORS_CHANNEL)
                                self._missed_candle_ts_list.append(next_5m_close)
                                next_5m_close += timedelta(minutes=ENTRY_TF_MIN)
                                self._candle_retry_count   = 0
                                self._next_candle_retry_at = None
                            else:
                                logger.warning(f'Candle fetch failed for {next_5m_close:%H:%M} '
                                               f'(attempt {self._candle_retry_count}/'
                                               f'{1 + CANDLE_FETCH_RETRIES}) — retrying in '
                                               f'{CANDLE_FETCH_RETRY_INTERVAL}s.')
                                if self._candle_retry_count == 1:
                                    _slack(f'⚠️ *Iris*: No candle data for {next_5m_close:%H:%M}. '
                                           f'Retrying up to {CANDLE_FETCH_RETRIES}x '
                                           f'({CANDLE_FETCH_RETRY_INTERVAL}s apart)...',
                                           SLACK_ERRORS_CHANNEL)
                                self._next_candle_retry_at = (
                                    now + timedelta(seconds=CANDLE_FETCH_RETRY_INTERVAL))

                sleep_secs = 0.5 if self.state.status == 'in_trade' else 1.0
                time.sleep(sleep_secs)

        except Exception as e:
            if "Session terminated" in str(e): raise
            logger.error(f"Unhandled exception in Iris run loop: {e}")
            _slack(f"⚠️ *Iris* EXCEPTION: {e}", SLACK_ERRORS_CHANNEL)
        finally:
            self._teardown()
        return False, self._summary

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

    def _merge_candle_5m(self, candle: dict) -> None:
        """
        Insert a candle into df_5m at its correct chronological position and
        recompute ST. Handles both the normal live-append case and
        missed-candle recovery, where the candle can be older than the
        series' current last row — compute_st assumes chronological order,
        so a plain append-without-sort would silently corrupt the ST
        computation for a late-recovered bar (§1).
        """
        new_row = pd.DataFrame([{
            'time_stamp': candle['time_stamp'],
            'open':  candle['open'],  'high': candle['high'],
            'low':   candle['low'],   'close': candle['close'],
            'volume': candle.get('volume', 0),
        }])
        combined = pd.concat([self._df_5m, new_row], ignore_index=True)
        combined = combined.drop_duplicates(subset=['time_stamp'], keep='last')
        combined = combined.sort_values('time_stamp').reset_index(drop=True)
        self._df_5m = compute_st(combined, ST_PERIOD, ST_MULTIPLIER)

    def _recover_missed_5m_candles(self) -> None:
        """
        Attempt one fetch per pending missed bar, chronologically, before
        processing the current live candle (§1) — mirrors Apollo's
        _missed_candle_ts_list recovery pattern. Deliberately does not act on
        a recovered bar's flip: a flip that fired several minutes ago is
        stale by the time it's discovered, so only the live current-bar flip
        (from _update_5m_st, called right after this) drives entries/exits.
        """
        if not self._missed_candle_ts_list:
            return
        recovered = []
        for missed_ts in list(self._missed_candle_ts_list):
            candle = self._fetch_candle(missed_ts, ENTRY_TF_MIN)
            if candle:
                logger.info(f'Recovered missed candle {missed_ts:%H:%M} — merging into history.')
                _slack(f'*Iris*: Recovered missed candle {missed_ts:%H:%M}. '
                       f'Merging into ST history before current bar.', SLACK_ERRORS_CHANNEL)
                self._merge_candle_5m(candle)
                recovered.append(missed_ts)
            else:
                logger.warning(f'Still no data for missed bar {missed_ts:%H:%M}.')
        for ts in recovered:
            self._missed_candle_ts_list.remove(ts)

    def _st15_snapshot_str(self) -> str:
        """§7: current ST_15 value/trend for per-cycle logging, whether or not this cycle updated it."""
        if self._df_15m is None or self._df_15m.empty:
            return 'n/a'
        last15 = self._df_15m.iloc[-1]
        if pd.isna(last15['supertrend']):
            return 'warmup'
        trend15_str = 'bullish' if bool(last15['trend']) else 'bearish'
        return f'{last15["supertrend"]:.2f} ({trend15_str}, as of {last15["time_stamp"]:%H:%M})'

    def _update_5m_st(self, candle: dict) -> tuple[bool, str | None]:
        """
        Merge the new 5-min candle into df_5m, recompute ST, detect flip.
        Returns (flip_occurred, new_direction) for THIS candle specifically.
        """
        self._merge_candle_5m(candle)
        row = self._df_5m[self._df_5m['time_stamp'] == candle['time_stamp']]
        if row.empty:
            return False, None
        last = row.iloc[-1]
        bar_ts = candle['time_stamp'].strftime('%H:%M')
        # §7: log ST_15 alongside ST_5 every cycle, not only when the 15m
        # boundary itself updates it — previously only visible on 15m bars.
        st15_str = self._st15_snapshot_str()

        if pd.isna(last['trend']) or not last['trend_flip']:
            if not pd.isna(last['supertrend']):
                trend_str = 'bullish' if bool(last['trend']) else 'bearish'
                logger.info(f'Bar {bar_ts} — ST_5={last["supertrend"]:.2f} '
                            f'(close={last["close"]:.2f}, {trend_str})  no flip  |  ST_15={st15_str}')
            return False, None

        direction = 'bullish' if bool(last['trend']) else 'bearish'
        logger.info(f'Bar {bar_ts} — ST_5={last["supertrend"]:.2f} '
                    f'(close={last["close"]:.2f})  FLIP → {direction}  |  ST_15={st15_str}')
        # §6: dedicated regime-change alert, independent of any resulting trade action.
        _slack(f'🔄 *Iris*: 5-min ST flip → *{direction}* at {bar_ts} '
               f'(close={last["close"]:.2f}, ST={last["supertrend"]:.2f})',
               SLACK_TRADEBOT_CHANNEL)
        return True, direction

    def _update_15m_regime(self, close_ts: datetime) -> None:
        """
        Resample the 15-min regime from self._df_5m (Path B, §8) — no
        separate FIFTEEN_MINUTE API call. Reuses the exact same resample
        function seed_st uses, so there's one 5m→15m code path for the whole
        session, not a live-loop one drifting from the seed-time one.
        Always recomputes ST over the full resampled series rather than
        incrementally appending — the Supertrend ratchet path is
        history-dependent (see §8), so this must match seed_st's approach.
        """
        df_15m_raw = _resample_to_15m(self._df_5m)
        if df_15m_raw.empty:
            return
        self._df_15m = compute_st(df_15m_raw, ST_PERIOD, ST_MULTIPLIER)
        last = self._df_15m.iloc[-1]
        if not pd.isna(last['trend']):
            prev = self._regime_trend
            self._regime_trend = bool(last['trend'])
            if prev != self._regime_trend:
                regime_str = 'bullish' if self._regime_trend else 'bearish'
                logger.info(f'15-min regime flipped → {regime_str}')
                # §6: dedicated regime-change alert, independent of any resulting trade action.
                _slack(f'🔄 *Iris*: 15-min regime flipped → *{regime_str}* '
                       f'(ST={last["supertrend"]:.2f}, close={last["close"]:.2f})',
                       SLACK_TRADEBOT_CHANNEL)

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

        lots = self._calculate_lots()

        order_id = place_order(self.obj, 'BUY', symbol, token, lots, DRY_RUN)
        if not order_id:
            logger.error('Entry order failed.')
            return

        # get_fill_price handles feed subscription, WS fast path, and REST fallback
        fill_price = get_fill_price(self.obj, self.order_watcher, order_id,
                                    symbol, token, lots, DRY_RUN, self.feed)
        if fill_price is None and not DRY_RUN:
            logger.error('Fill verification failed — position state unknown.')
            return

        fill_price = fill_price or 0.0

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
        self.state.lots        = lots
        save_state(self.state)

        self._trade_count += 1
        self._summary.update({
            'traded':      True,
            'direction':   direction,
            'lots':        lots,
            'entry_time':  now.strftime('%H:%M'),
            'spot_entry':  spot,
        })
        self._summary.pop('no_trade_reason', None)

        msg = (f'{"[PAPER] " if DRY_RUN else ""}⚡ *Iris*: Entered {direction.upper()}\n'
               f'Nifty: {spot:.0f} | {strike}{option_type.upper()} {expiry}\n'
               f'Entry premium: {fill_price:.1f} pts | Lots: {lots}\n'
               f'Stop: -{STOP_LOSS_PCT*100:.0f}%  Target: +{PROFIT_TARGET_PCT*100:.0f}%  '
               f'Exit by: {EXIT_BY_TIME}')
        logger.info(msg.replace('\n', '  '))
        _slack(msg)

    # -----------------------------------------------------------------------
    # Exit
    # -----------------------------------------------------------------------

    def _get_ltp_ws(self, token: str, exchange: str, symbol: str) -> float | None:
        """
        Get LTP from the shared WS feed if connected; fall back to a single
        rate-limited REST call if not. Mirrors
        artemis_production/credit_spread.py::_get_ltp_ws — same pattern,
        ported here since Iris had no REST fallback of its own.
        """
        if self.feed is not None and self.feed.is_connected():
            ltp = self.feed.get_ltp(token)
            if ltp is not None:
                return ltp
        return fetch_ltp_rest(self.obj, exchange, symbol, token)

    def _check_exit_conditions(self, now: datetime) -> None:
        if self.state.status != 'in_trade':
            return

        ltp = self._get_ltp_ws(self.state.token or NIFTY_TOKEN, FO_EXCHANGE, self.state.symbol)
        if ltp:
            self.state.last_ltp = ltp
            save_state(self.state)

        # P&L checks — only when entry price is known
        entry = self.state.entry_price or 0
        if entry > 0 and ltp:
            unrealised_pts = ltp - entry
            if unrealised_pts > self._peak_pnl_pts:
                self._peak_pnl_pts = unrealised_pts
            pnl_pct = unrealised_pts / entry
            if pnl_pct >= PROFIT_TARGET_PCT:
                self._execute_exit(f'profit_target ({pnl_pct:+.1%})')
                return
            if pnl_pct <= -STOP_LOSS_PCT:
                self._execute_exit(f'stop_loss ({pnl_pct:+.1%})')
                return

        # Time-based exits — always fire regardless of entry price
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

        order_id   = place_order(self.obj, 'SELL', symbol, token, lots, DRY_RUN)
        fill_price = get_fill_price(self.obj, self.order_watcher, order_id,
                                    symbol, token, lots, DRY_RUN, self.feed)
        if fill_price is None:
            fill_price = self.feed.get_ltp(token) or entry

        pnl_pts = fill_price - entry                    # option points per unit
        pnl_rs  = pnl_pts * lots * LOT_SIZE            # total rupees

        self._total_pnl_pts += pnl_pts
        self._total_pnl_rs  += pnl_rs
        self._summary.update({
            'exit_time':    datetime.now().strftime('%H:%M'),
            'exit_reason':  reason,
            'pnl_pts':      self._total_pnl_pts,
            'pnl_rs':       self._total_pnl_rs,
            'peak_pnl_pts': self._peak_pnl_pts,
            'trade_count':  self._trade_count,
        })

        self.feed.unsubscribe_options([token])

        self.state.status = 'watching'
        save_state(self.state)

        msg = (f'{"[PAPER] " if DRY_RUN else ""}✅ *Iris*: Exited — {reason}\n'
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
        if self.state.status != 'in_trade':
            return
        ltp   = self.feed.get_ltp(self.state.token or '') or 0
        spot  = self.feed.get_ltp(NIFTY_TOKEN) or 0
        entry = self.state.entry_price or 0

        # P&L — only meaningful when entry price is known
        if entry > 0 and ltp:
            pnl_pts = ltp - entry
            pnl_rs  = pnl_pts * (self.state.lots or 0) * LOT_SIZE
            pnl_str = f'{pnl_pts:+.1f} pts  ₹{pnl_rs:+,.0f}'
        else:
            pnl_str = 'P&L unknown (entry price not captured)'

        # Time remaining to nearest exit
        time_str = ''
        if self.state.entry_time:
            entry_dt   = datetime.fromisoformat(self.state.entry_time)
            hold_exit  = entry_dt + timedelta(minutes=MAX_HOLD_MIN)
            ebt_exit   = datetime.now().replace(
                hour=int(EXIT_BY_TIME.split(':')[0]),
                minute=int(EXIT_BY_TIME.split(':')[1]),
                second=0, microsecond=0)
            next_exit  = min(hold_exit, ebt_exit)
            mins_left  = max(0, (next_exit - datetime.now()).total_seconds() / 60)
            time_str   = f'  Exit in {mins_left:.0f}m'

        prefix = '[PAPER] ' if DRY_RUN else ''
        msg = (f'{prefix}📊 *Iris* update: {self.state.direction.upper()}  '
               f'{self.state.symbol}  '
               f'Nifty: {spot:.0f}  LTP: {ltp:.1f}  Entry: {entry:.1f}  '
               f'{pnl_str}{time_str}')
        logger.info(msg.replace(prefix, '').replace('\n', '  '))
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
        iris = Iris(obj, auth_token, instrument_df, api_key=api_key, client_code=client_code)
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
