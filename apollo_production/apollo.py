"""
apollo.py — Apollo Production Main Entry Point
Nifty High-VIX ITM Debit Spread Strategy — Live Execution
Frozen production config: D-R-D06g

Called by wrapper.py — not run directly.

Architecture:
    - Apollo class owns ST seed, run loop, entry/exit logic, order placement, feed teardown
    - wrapper.py owns login, market/holiday check, scrip master, session teardown
    - websocket_feed.ApolloFeed     — tick feed, LTP/OHLC access, option subscribe/unsub
    - supertrend.SupertrendManager  — ST seeding and 15-min candle updates
    - state.ApolloState             — persistent trade state across restarts
    - functions.py                  — Slack/Telegram messaging, exception handling
    - logger_setup.py               — dual console+file logging, level from configs_live

Execution model (mirrors backtest exactly):
    - Signal fires on 15-min candle CLOSE
    - Entry/exit executes at OPEN of the next 15-min candle
    - Hard stop and profit target polled every ~1s between candle closes
    - Every TRADE_UPDATE_INTERVAL seconds: trade log row appended + Slack update
    - Time gate checked once at 09:30 on gate day
    - Trend flip checked at every 15-min candle close
"""

import os
import sys
import signal
import pandas as pd
from datetime import datetime, date, timedelta
from time import sleep

from configs_live import (
    user_name,
    NIFTY_INDEX_TOKEN, VIX_TOKEN,
    MARKET_OPEN, MARKET_CLOSE,
    VIX_THRESHOLD,
    SPREAD_TYPE, BUY_LEG_OFFSET, HEDGE_POINTS, STRIKE_STEP, MIN_DTE, LOT_SIZE,
    LOT_CALC, LOT_COUNT, LOT_CAPITAL,
    EXCLUDE_TRADE_DAYS, EXCLUDE_SIGNAL_CANDLES,
    EXCLUDE_BEARISH_DAYS, EXCLUDE_BULLISH_DAYS,
    ENABLE_HARD_STOP, HARD_STOP_POINTS_BULL, HARD_STOP_POINTS_BEAR,
    ENABLE_PROFIT_TARGET, PROFIT_TARGET_PCT_BULL, PROFIT_TARGET_PCT_BEAR,
    ENABLE_TIME_GATE, TIME_GATE_DAYS_BULL, TIME_GATE_DAYS_BEAR,
    TIME_GATE_CHECK_TIME,
    TIME_GATE_MIN_PROFIT_PCT_BULL, TIME_GATE_MIN_PROFIT_PCT_BEAR,
    ELM_SECONDS_BEFORE_EXPIRY,
    NO_EXIT_BEFORE,
    FO_EXCHANGE_SEGMENT,
    TRADE_UPDATE_INTERVAL,
    DRY_RUN,
    SLACK_TRADEBOT_CHANNEL, SLACK_TRADE_ALERTS, SLACK_TRADE_UPDATES, SLACK_ERRORS_CHANNEL,
    TRADES_FILE, DATA_DIR,
    CANDLE_FETCH_RETRIES, CANDLE_FETCH_RETRY_INTERVAL,
)
from websocket_feed import ApolloFeed, NIFTY_TOKEN, VIX_TOKEN as FEED_VIX_TOKEN
from supertrend import SupertrendManager
from state import ApolloState, load_state, save_state, init_state, clear_trade_fields
from functions import slack_bot_sendtext, handle_exception
from logger_setup import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Rate limit counters — module-level, same pattern as Artemis functions.py
# ---------------------------------------------------------------------------
_poll_counter  = 0
_order_counter = 0
_POLL_LIMIT    = 10
_ORDER_LIMIT   = 9


def _increment_poll():
    global _poll_counter, _order_counter
    _poll_counter += 1
    if _poll_counter >= _POLL_LIMIT:
        sleep(1)
        _poll_counter = 0
        _order_counter = 0


def _increment_order():
    global _poll_counter, _order_counter
    _order_counter += 1
    if _order_counter >= _ORDER_LIMIT:
        sleep(1)
        _poll_counter = 0
        _order_counter = 0


def _reset_counters():
    global _poll_counter, _order_counter
    _poll_counter = 0
    _order_counter = 0


# Trade log directory
_TRADE_LOGS_DIR = os.path.join(DATA_DIR, "trade_logs")


class Apollo:
    """
    Apollo live execution engine.
    Receives an authenticated SmartConnect object, JWT auth token, and
    filtered Nifty instrument DataFrame from wrapper.py.

    Owns: ST seed, WebSocket feed start/stop, run loop, entry/exit logic.
    Does NOT own: login, session teardown, market/holiday checks.
    """

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def __init__(self, obj, auth_token, instrument_df):
        """
        Parameters
        ----------
        obj            : SmartConnect — authenticated session from wrapper
        auth_token     : str          — JWT token from generateSession response
        instrument_df  : DataFrame    — Nifty NFO rows from scrip master
        """
        self.obj            = obj
        self.auth_token     = auth_token
        self.instrument_df  = instrument_df

        # Holidays loaded locally — still needed for ELM date and gate date
        # computation inside Apollo. Does not affect market/holiday entry check
        # (that's Leto's job).
        self.holidays       = set()
        self._load_holidays()

        self.feed           = ApolloFeed()
        self.st             = SupertrendManager()
        self.state          = load_state()

        self._opening_time  = datetime.strptime(MARKET_OPEN,  "%H:%M").time()
        self._closing_time  = datetime.strptime(MARKET_CLOSE, "%H:%M").time()
        self._no_exit_time  = datetime.strptime(NO_EXIT_BEFORE, "%H:%M").time()

        # Qty freeze for Nifty on NFO — 1800 units (36 lots of 50)
        self._qty_freeze    = 1800

        # Per-trade log state
        self._trade_log      = []
        self._trade_counter  = self._load_trade_counter()
        self._update_elapsed = 0

        # Missed candle recovery — timestamps of bars skipped due to API failure
        self._missed_candle_ts_list = []

        # Register signal handlers for clean shutdown
        signal.signal(signal.SIGINT,  self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        logger.info(
            f"Apollo initialised. State: {self.state.status}. "
            f"Trade counter: {self._trade_counter}. "
            f"DRY_RUN: {DRY_RUN}.")

    def _handle_signal(self, signum, frame):
        """Clean shutdown on SIGINT (Ctrl+C) or SIGTERM (kill)."""
        logger.info(f"Shutdown signal received ({signum}). Stopping feed.")
        slack_bot_sendtext(
            f"*Apollo*: Shutdown signal received ({signum}). "
            f"Stopping feed at {datetime.now():%Y-%m-%d %H:%M:%S}.",
            SLACK_TRADEBOT_CHANNEL)
        self._teardown()
        sys.exit(0)

    def _setup(self):
        """
        Seed Supertrend, start WebSocket feed, handle restart recovery.
        Called at the start of run() before the main loop.
        """
        logger.info(f"Apollo setup started at {datetime.now():%Y-%m-%d %H:%M:%S}.")

        self.st.seed(self.obj)
        slack_bot_sendtext(
            f"*Apollo*: Supertrend seeded ({self.st.get_cache().shape[0]} candles). "
            f"75-min trend: {'bullish' if self.st.get_current_trend_75() else 'bearish'}.",
            SLACK_TRADEBOT_CHANNEL)

        self.feed.start(self.obj, self.auth_token, user_name)

        nifty_ltp = self.feed.get_ltp(NIFTY_TOKEN)
        vix_ltp   = self.feed.get_ltp(FEED_VIX_TOKEN)
        logger.info(f"WebSocket feed live. Nifty LTP: {nifty_ltp}. VIX LTP: {vix_ltp}.")

        slack_bot_sendtext(
            f"*Apollo*: WebSocket feed live. "
            f"Nifty: {nifty_ltp:.2f}  VIX: {vix_ltp:.2f}"
            if nifty_ltp is not None and vix_ltp is not None
            else "*Apollo*: WebSocket feed live. LTPs not yet available.",
            SLACK_TRADEBOT_CHANNEL)

        if self.state.status == 'in_trade':
            logger.info(
                f"Restarting with active trade. "
                f"Direction: {self.state.direction}. "
                f"Buy: {self.state.buy_strike} @ {self.state.buy_entry}. "
                f"Sell: {self.state.sell_strike} @ {self.state.sell_entry}.")
            self.feed.subscribe_options(self.state.buy_token, self.state.sell_token)
            self._load_trade_log()
            slack_bot_sendtext(
                f"*Apollo*: Restarted — resuming active {self.state.direction.upper()} trade. "
                f"Buy {self.state.buy_strike} @ {self.state.buy_entry:.1f} | "
                f"Sell {self.state.sell_strike} @ {self.state.sell_entry:.1f}",
                SLACK_TRADE_ALERTS)

        if self.state.status == 'exiting':
            logger.warning("Restarted with status=exiting. Manual intervention required.")
            slack_bot_sendtext(
                "*Apollo* ALERT: Restarted with status=exiting. "
                "Check open positions manually and update apollo_state.csv.",
                SLACK_ERRORS_CHANNEL)
            self._teardown()
            sys.exit(1)

        self._check_missed_flip_at_startup()

    def _teardown(self):
        """
        Stop WebSocket feed and send session-complete alert.
        Called at the end of run() and from _handle_signal().
        Does NOT call terminateSession — wrapper owns that.
        """
        logger.info("Apollo teardown: stopping feed.")
        self.feed.stop()
        slack_bot_sendtext(
            f"*Apollo*: Session complete. Feed stopped at {datetime.now():%Y-%m-%d %H:%M:%S}.",
            SLACK_TRADEBOT_CHANNEL)

    def run(self):
        """
        Main entry point called by wrapper.py.
        Seeds ST, starts feed, runs the main loop, stops feed before returning.
        Wrapper calls terminateSession after this returns.
        """
        self._setup()

        vix_now = self._get_todays_vix()
        logger.info(
            f"Run loop started. VIX: {vix_now:.2f}. "
            f"Threshold: {VIX_THRESHOLD}. "
            f"Active: {vix_now > VIX_THRESHOLD}.")

        slack_bot_sendtext(
            f"*Apollo*: Run loop started. "
            f"VIX today: {vix_now:.2f}  Threshold: {VIX_THRESHOLD}",
            SLACK_TRADEBOT_CHANNEL)

        try:
            while True:
                now = datetime.now()
                if now.time() >= self._closing_time:
                    logger.info("Market close time reached. Exiting run loop.")
                    break

                next_close = self._next_candle_close(now)
                seconds_to_close = (next_close - now).total_seconds()
                logger.debug(
                    f"Next candle close: {next_close:%H:%M}  "
                    f"({seconds_to_close:.0f}s away).")

                elapsed = 0
                while elapsed < seconds_to_close - 2:
                    sleep(1)
                    elapsed += 1
                    self._update_elapsed += 1

                    if self.state.status == 'in_trade':
                        buy_ltp  = self.feed.get_ltp(self.state.buy_token)
                        sell_ltp = self.feed.get_ltp(self.state.sell_token)
                        if buy_ltp is not None and sell_ltp is not None:
                            unrealised = (
                                (buy_ltp  - self.state.buy_entry) -
                                (sell_ltp - self.state.sell_entry))
                            logger.debug(
                                f"Poll — buy_ltp={buy_ltp:.2f}  sell_ltp={sell_ltp:.2f}  "
                                f"unrealised={unrealised:+.2f}  "
                                f"hard_stop_level={-self.state.hard_stop_pts:.1f}  "
                                f"pt_level={self.state.profit_target_pts:.1f}")
                            self.state.last_buy_ltp  = round(buy_ltp,  2)
                            self.state.last_sell_ltp = round(sell_ltp, 2)
                            if unrealised > self.state.max_unrealised_pl:
                                self.state.max_unrealised_pl = round(unrealised, 2)
                            save_state(self.state)

                        if self._check_hard_stop():
                            continue
                        if self._check_profit_target():
                            continue

                        if self._update_elapsed >= TRADE_UPDATE_INTERVAL:
                            self._append_trade_log_row()
                            self._send_trade_update()
                            self._update_elapsed = 0

                remaining = (next_close - datetime.now()).total_seconds()
                if remaining > 0:
                    sleep(remaining)

                candle = self._fetch_latest_candle(next_close)
                if candle is None:
                    logger.warning(
                        f"No candle data returned for {next_close}. "
                        f"Retrying up to {CANDLE_FETCH_RETRIES} more times "
                        f"({CANDLE_FETCH_RETRY_INTERVAL}s apart)...")
                    slack_bot_sendtext(
                        f"*Apollo* ALERT: No candle data for {next_close:%H:%M}. "
                        f"Retrying up to {CANDLE_FETCH_RETRIES}x "
                        f"({CANDLE_FETCH_RETRY_INTERVAL}s apart)...",
                        SLACK_ERRORS_CHANNEL)
                    for retry in range(CANDLE_FETCH_RETRIES):
                        sleep(CANDLE_FETCH_RETRY_INTERVAL)
                        _reset_counters()
                        candle = self._fetch_latest_candle(next_close)
                        if candle is not None:
                            logger.info(
                                f"Candle data recovered on retry {retry + 1} "
                                f"for {next_close:%H:%M}.")
                            slack_bot_sendtext(
                                f"*Apollo*: Candle data recovered on retry "
                                f"{retry + 1} for {next_close:%H:%M}. Resuming normally.",
                                SLACK_ERRORS_CHANNEL)
                            break
                        logger.warning(
                            f"Retry {retry + 1}/{CANDLE_FETCH_RETRIES} — "
                            f"still no data for {next_close:%H:%M}.")
                        slack_bot_sendtext(
                            f"*Apollo* ALERT: Retry {retry + 1}/{CANDLE_FETCH_RETRIES} — "
                            f"still no candle data for {next_close:%H:%M}.",
                            SLACK_ERRORS_CHANNEL)
                if candle is None:
                    logger.error(
                        f"Candle data unavailable for {next_close:%H:%M} after all "
                        f"retries. ST and signals will not update this bar. "
                        f"Will try again next bar. Please monitor manually till then.")
                    slack_bot_sendtext(
                        f"*Apollo* ERROR: Candle data unavailable for {next_close:%H:%M} "
                        f"after all retries. ST and signals will not update this bar. "
                        f"Will try again next bar. Please monitor manually till then.",
                        SLACK_ERRORS_CHANNEL)
                    self._missed_candle_ts_list.append(next_close)
                    continue

                # Recover any missed candles before processing current bar
                if self._missed_candle_ts_list:
                    recovered = []
                    for missed_ts in self._missed_candle_ts_list:
                        logger.info(
                            f"Attempting recovery fetch for missed bar {missed_ts:%H:%M}.")
                        missed_candle = self._fetch_latest_candle(missed_ts)
                        if missed_candle is not None:
                            logger.info(
                                f"Recovered missed candle {missed_ts:%H:%M}. "
                                f"Processing into ST.")
                            slack_bot_sendtext(
                                f"*Apollo*: Recovered missed candle {missed_ts:%H:%M}. "
                                f"Processing into ST before current bar.",
                                SLACK_ERRORS_CHANNEL)
                            try:
                                self.st.update(missed_candle)
                            except Exception as e:
                                handle_exception(e)
                            recovered.append(missed_ts)
                        else:
                            logger.warning(
                                f"Still no data for missed bar {missed_ts:%H:%M}. "
                                f"Skipping — ST will remain incomplete for this bar.")
                            slack_bot_sendtext(
                                f"*Apollo* ALERT: Still no data for missed bar "
                                f"{missed_ts:%H:%M}. ST remains incomplete.",
                                SLACK_ERRORS_CHANNEL)
                    for ts in recovered:
                        self._missed_candle_ts_list.remove(ts)

                ts = candle['time_stamp']
                logger.info(
                    f"Candle closed: {ts}  "
                    f"O={candle['open']:.2f} H={candle['high']:.2f} "
                    f"L={candle['low']:.2f} C={candle['close']:.2f}")

                try:
                    trend_15, flip_15, trend_75, flip_75 = self.st.update(candle)
                except Exception as e:
                    handle_exception(e)
                    continue

                if trend_15 is None or trend_75 is None:
                    logger.debug("ST warmup period — skipping signal check.")
                    continue

                if self.state.status == 'in_trade':
                    logger.debug(
                        f"In trade — checking exits. "
                        f"trend_15={trend_15} flip_15={flip_15}  "
                        f"trend_75={trend_75}")
                    if self._check_hard_stop():
                        continue
                    if self._check_profit_target():
                        continue
                    if self._check_time_gate(ts):
                        continue
                    if self._check_trend_flip(trend_15, flip_15):
                        continue
                    if self._check_pre_expiry(ts):
                        continue

                if self.state.status == 'idle':
                    vix = self._get_todays_vix()
                    logger.debug(
                        f"Idle — checking entry. "
                        f"VIX={vix:.2f} (threshold={VIX_THRESHOLD})  "
                        f"flip_15={flip_15}  "
                        f"trend_15={trend_15}  trend_75={trend_75}  "
                        f"ts={ts}  day={ts.dayofweek}  time={ts.strftime('%H:%M')}")

                    if not self._vix_gate_passes():
                        logger.debug(f"VIX gate blocked: {vix:.2f} <= {VIX_THRESHOLD}")
                        continue
                    if not flip_15:
                        logger.debug("No 15-min flip — no entry.")
                        continue

                    direction = self._resolve_direction(trend_15, trend_75)
                    if direction is None:
                        logger.debug(
                            f"Timeframes misaligned — no entry. "
                            f"trend_15={trend_15} trend_75={trend_75}")
                        continue

                    if not self._check_entry_filters(ts, direction):
                        logger.info(
                            f"Entry filter blocked {direction} signal at {ts}. "
                            f"day={ts.dayofweek} time={ts.strftime('%H:%M')}")
                        continue

                    logger.info(
                        f"Entry signal: {direction.upper()} at {ts}. "
                        f"trend_15={trend_15} trend_75={trend_75}.")
                    self._execute_entry(direction, ts)

        except Exception as e:
            handle_exception(e)

        finally:
            if self.state.status == 'in_trade':
                logger.info(
                    f"Market close with open trade — holding overnight. "
                    f"Expiry: {self.state.expiry}. Pre-expiry exit will fire at 15:15 "
                    f"on the last trading day before expiry.")
                slack_bot_sendtext(
                    f"*Apollo*: Market close with open trade. "
                    f"Holding overnight. Expiry: {self.state.expiry}.",
                    SLACK_TRADEBOT_CHANNEL)

            # Always stop the feed before returning to wrapper
            self._teardown()
        
        return False

    # -----------------------------------------------------------------------
    # Missed flip recovery on restart
    # -----------------------------------------------------------------------

    def _check_missed_flip_at_startup(self):
        """
        Detect and act on a flip that occurred in the last completed candle
        before this restart, which Apollo missed because it was not running.
        """
        if self.state.status != 'idle':
            return

        flip_row = self.st.get_last_completed_flip()
        if flip_row is None:
            logger.debug("No missed flip detected at startup.")
            return

        flip_ts            = flip_row['time_stamp']
        flip_trend         = flip_row['trend']
        entry_window_close = flip_ts + timedelta(minutes=15)

        logger.debug(
            f"Missed flip candidate: {flip_ts}  trend={flip_trend}  "
            f"entry window closes at {entry_window_close:%H:%M}  "
            f"current time: {datetime.now():%H:%M:%S}")

        if datetime.now() >= entry_window_close:
            logger.info(
                f"Missed flip at {flip_ts:%H:%M} — entry window closed at "
                f"{entry_window_close:%H:%M}. Skipping.")
            return

        trend_75 = self.st.get_current_trend_75()
        if trend_75 is None:
            logger.debug("75-min trend not available — cannot recover missed flip.")
            return

        direction = self._resolve_direction(bool(flip_trend), trend_75)
        if direction is None:
            logger.info(
                f"Missed flip at {flip_ts:%H:%M} — timeframes misaligned "
                f"(flip_trend={flip_trend} trend_75={trend_75}). Skipping.")
            return

        if not self._check_entry_filters(flip_ts, direction):
            logger.info(
                f"Missed flip at {flip_ts:%H:%M} — blocked by entry filter "
                f"(direction={direction} day={flip_ts.dayofweek} "
                f"time={flip_ts.strftime('%H:%M')}). Skipping.")
            return

        logger.info(
            f"Missed flip recovered: {direction.upper()} at {flip_ts:%H:%M}. "
            f"Entry window still open until {entry_window_close:%H:%M}. Executing entry.")

        slack_bot_sendtext(
            f"*Apollo*: Missed flip detected at {flip_ts:%H:%M} — "
            f"entry window still open until {entry_window_close:%H:%M}. "
            f"Executing {direction.upper()} entry.",
            SLACK_TRADE_ALERTS)

        self._execute_entry(direction, flip_ts)

    # -----------------------------------------------------------------------
    # VIX and entry filters
    # -----------------------------------------------------------------------

    def _get_todays_vix(self):
        vix = self.feed.get_ltp(FEED_VIX_TOKEN)
        return vix if vix is not None else VIX_THRESHOLD + 1

    def _vix_gate_passes(self):
        return self._get_todays_vix() > VIX_THRESHOLD

    def _check_entry_filters(self, ts, direction=None):
        """Check entry filters. direction='bullish'|'bearish'|None (None skips direction check)."""
        if ts.dayofweek in EXCLUDE_TRADE_DAYS:
            return False
        if ts.strftime('%H:%M') in EXCLUDE_SIGNAL_CANDLES:
            return False
        if direction == 'bearish' and ts.dayofweek in EXCLUDE_BEARISH_DAYS:
            return False
        if direction == 'bullish' and ts.dayofweek in EXCLUDE_BULLISH_DAYS:
            return False
        return True

    def _resolve_direction(self, trend_15, trend_75):
        if trend_75 is True  and trend_15 is True:
            return 'bullish'
        if trend_75 is False and trend_15 is False:
            return 'bearish'
        return None

    # -----------------------------------------------------------------------
    # Strike and expiry selection
    # -----------------------------------------------------------------------

    def _compute_elm_date(self, expiry_date):
        """
        Compute the ELM exit date for a given expiry — the last trading
        day before expiry (after stepping back through weekends and holidays).
        This is the day the pre-expiry exit fires at 15:15.
        """
        expiry_dt = datetime.combine(
            expiry_date,
            datetime.strptime('15:30', '%H:%M').time())
        elm_dt   = expiry_dt - timedelta(seconds=ELM_SECONDS_BEFORE_EXPIRY)
        elm_date = elm_dt.date()
        while elm_date.weekday() >= 5 or elm_date in self.holidays:
            elm_date -= timedelta(days=1)
        return elm_date

    def _select_expiry(self):
        today = date.today()
        expiry_dates = (
            self.instrument_df['expiry']
            .drop_duplicates()
            .apply(lambda x: datetime.strptime(x, '%d%b%Y').date())
            .sort_values()
        )
        future = expiry_dates[expiry_dates >= today]
        if future.empty:
            logger.warning("No future expiries found in scrip master.")
            return None
        for expiry in future:
            elm_date     = self._compute_elm_date(expiry)
            calendar_dte = (expiry - today).days
            logger.debug(
                f"Expiry selection: candidate={expiry} "
                f"calendar_DTE={calendar_dte} ELM={elm_date}")
            if elm_date > today:
                logger.debug(
                    f"Using expiry: {expiry} "
                    f"(ELM={elm_date} is after today={today})")
                return expiry
            else:
                logger.debug(
                    f"Skipping expiry {expiry} — ELM={elm_date} is today. "
                    f"Rolling to next.")
        logger.warning("No valid expiry found after ELM check.")
        return None

    def _fetch_symbol_and_token(self, strike, option_type, expiry_date):
        expiry_str = expiry_date.strftime('%d%b%Y').upper()
        row = self.instrument_df[
            (self.instrument_df['expiry'] == expiry_str) &
            (self.instrument_df['strike'] == strike * 100) &
            (self.instrument_df['symbol'].str[-2:] == option_type.upper())
        ]
        if row.empty:
            logger.warning(
                f"Symbol/token not found: strike={strike} type={option_type} "
                f"expiry={expiry_str}")
            return None, None
        symbol = row['symbol'].iloc[0]
        token  = str(row['token'].iloc[0])
        logger.debug(f"Symbol lookup: {strike}{option_type.upper()} {expiry_str} -> {symbol} ({token})")
        return symbol, token

    def _select_strikes(self, direction, spot, expiry_date):
        option_type = 'ce' if direction == 'bullish' else 'pe'
        atm = round(spot / STRIKE_STEP) * STRIKE_STEP

        if direction == 'bullish':
            buy_strike  = int(atm + BUY_LEG_OFFSET)
            sell_strike = int(buy_strike + HEDGE_POINTS)
        else:
            buy_strike  = int(atm - BUY_LEG_OFFSET)
            sell_strike = int(buy_strike - HEDGE_POINTS)

        logger.debug(
            f"Strike selection: direction={direction}  spot={spot:.2f}  "
            f"ATM={atm}  buy_strike={buy_strike}  sell_strike={sell_strike}  "
            f"option_type={option_type}")

        buy_symbol,  buy_token  = self._fetch_symbol_and_token(
            buy_strike,  option_type, expiry_date)
        sell_symbol, sell_token = self._fetch_symbol_and_token(
            sell_strike, option_type, expiry_date)

        if None in (buy_symbol, buy_token, sell_symbol, sell_token):
            logger.error(
                f"Strike lookup failed: buy={buy_strike} sell={sell_strike} "
                f"type={option_type} expiry={expiry_date}")
            return None

        return (buy_strike, sell_strike, option_type,
                buy_symbol, buy_token, sell_symbol, sell_token)

    # -----------------------------------------------------------------------
    # Lot sizing
    # -----------------------------------------------------------------------

    def _calculate_lots(self):
        """
        Calculate the number of lots to trade.

        LOT_CALC = False: return LOT_COUNT directly (manual control)
        LOT_CALC = True:  fetch available margin from Angel One rmsLimit(),
                          compute floor(margin / LOT_CAPITAL), floor at 1.

        Returns int >= 1.
        """
        if not LOT_CALC:
            logger.debug(f"Lot sizing: fixed LOT_COUNT={LOT_COUNT}")
            return LOT_COUNT

        while True:
            try:
                margin = float(self.obj.rmsLimit()['data']['availablecash'])
                lots   = max(1, int(margin // LOT_CAPITAL))
                logger.info(
                    f"Lot sizing: available_margin={margin:.0f}  "
                    f"LOT_CAPITAL={LOT_CAPITAL}  lots={lots}")
                return lots
            except Exception as e:
                handle_exception(e)
            sleep(1)
            _reset_counters()

    # -----------------------------------------------------------------------
    # Entry execution
    # -----------------------------------------------------------------------

    def _execute_entry(self, direction, signal_ts):
        spot = self.feed.get_ltp(NIFTY_TOKEN)
        if spot is None:
            logger.error("Entry aborted — no Nifty LTP from feed.")
            slack_bot_sendtext(
                "*Apollo* ALERT: Entry aborted — no Nifty LTP from feed.",
                SLACK_ERRORS_CHANNEL)
            return

        expiry_date = self._select_expiry()
        if expiry_date is None:
            logger.error("Entry aborted — no valid expiry found.")
            slack_bot_sendtext(
                "*Apollo* ALERT: Entry aborted — no valid expiry found.",
                SLACK_ERRORS_CHANNEL)
            return

        strikes = self._select_strikes(direction, spot, expiry_date)
        if strikes is None:
            slack_bot_sendtext(
                f"*Apollo* ALERT: Entry aborted — strike lookup failed "
                f"(spot={spot:.0f}, direction={direction}).",
                SLACK_ERRORS_CHANNEL)
            return

        (buy_strike, sell_strike, option_type,
         buy_symbol, buy_token, sell_symbol, sell_token) = strikes

        lots = self._calculate_lots()

        logger.info(
            f"Executing entry: {direction.upper()}  "
            f"Buy {buy_strike}{option_type.upper()} ({buy_token})  "
            f"Sell {sell_strike}{option_type.upper()} ({sell_token})  "
            f"Expiry: {expiry_date}  Spot: {spot:.2f}  Lots: {lots}")

        buy_orderid_list = self._place_order('BUY', buy_symbol, buy_token, lots)
        sleep(1)
        _reset_counters()
        self._fetch_order_book()
        buy_fill, buy_time = self._fetch_order_details(buy_orderid_list, buy_token)
        logger.info(f"Buy fill: {buy_fill:.2f} at {buy_time}")

        sell_orderid_list = self._place_order('SELL', sell_symbol, sell_token, lots)
        sleep(1)
        _reset_counters()
        self._fetch_order_book()
        sell_fill, sell_time = self._fetch_order_details(sell_orderid_list, sell_token)
        logger.info(f"Sell fill: {sell_fill:.2f} at {sell_time}")

        net_debit         = buy_fill - sell_fill
        max_profit        = HEDGE_POINTS - net_debit

        pt_pct            = PROFIT_TARGET_PCT_BULL if direction == 'bullish' else PROFIT_TARGET_PCT_BEAR
        hard_stop_pts     = HARD_STOP_POINTS_BULL  if direction == 'bullish' else HARD_STOP_POINTS_BEAR
        gate_min_pct      = TIME_GATE_MIN_PROFIT_PCT_BULL if direction == 'bullish' else TIME_GATE_MIN_PROFIT_PCT_BEAR
        profit_target_pts = max_profit * pt_pct

        logger.info(
            f"Spread metrics: net_debit={net_debit:.2f}  "
            f"max_profit={max_profit:.2f}  "
            f"pt_pct={pt_pct:.0%}  profit_target={profit_target_pts:.2f}  "
            f"hard_stop={hard_stop_pts}  gate_min_pct={gate_min_pct:.0%}  "
            f"lots={lots}  lot_size={LOT_SIZE}  "
            f"max_loss_rs={hard_stop_pts * lots * LOT_SIZE:.0f}")

        self.state.status              = 'in_trade'
        self.state.direction           = direction
        self.state.buy_strike          = buy_strike
        self.state.sell_strike         = sell_strike
        self.state.option_type         = option_type
        self.state.expiry              = expiry_date.strftime('%Y-%m-%d')
        self.state.buy_token           = buy_token
        self.state.sell_token          = sell_token
        self.state.buy_symbol          = buy_symbol
        self.state.sell_symbol         = sell_symbol
        self.state.lots                = lots
        self.state.buy_entry           = round(buy_fill,  2)
        self.state.sell_entry          = round(sell_fill, 2)
        self.state.net_debit           = round(net_debit, 2)
        self.state.max_profit          = round(max_profit, 2)
        self.state.profit_target_pts   = round(profit_target_pts, 2)
        self.state.hard_stop_pts       = hard_stop_pts
        self.state.entry_time          = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.state.entry_spot          = round(spot, 2)
        self.state.entry_vix           = round(self.feed.get_ltp(FEED_VIX_TOKEN) or 0, 2)
        self.state.gate_date           = self._compute_gate_date(expiry_date, direction)
        self.state.gate_checked        = False
        self.state.gate_min_profit_pct = round(gate_min_pct, 4)
        self.state.max_unrealised_pl   = 0.0
        self.state.last_buy_ltp        = round(buy_fill,  2)
        self.state.last_sell_ltp       = round(sell_fill, 2)
        save_state(self.state)

        self._trade_log      = []
        self._update_elapsed = 0

        self.feed.subscribe_options(buy_token, sell_token)

        slack_bot_sendtext(
            f"*Apollo* ENTRY {direction.upper()} | "
            f"Buy  {buy_strike}{option_type.upper()} @ {buy_fill:.1f} | "
            f"Sell {sell_strike}{option_type.upper()} @ {sell_fill:.1f} | "
            f"Net debit: {net_debit:.1f} | Max profit: {max_profit:.1f} | "
            f"PT: {profit_target_pts:.1f} pts ({pt_pct:.0%}) | "
            f"Hard stop: {hard_stop_pts} pts | "
            f"Gate: {gate_min_pct:.0%} | "
            f"Lots: {lots} | Expiry: {expiry_date} | Spot: {spot:.0f}",
            SLACK_TRADE_ALERTS)

    # -----------------------------------------------------------------------
    # Exit triggers
    # -----------------------------------------------------------------------

    def _check_hard_stop(self):
        if not ENABLE_HARD_STOP or self.state.status != 'in_trade':
            return False
        if datetime.now().time() < self._no_exit_time:
            return False
        buy_ltp  = self.feed.get_ltp(self.state.buy_token)
        sell_ltp = self.feed.get_ltp(self.state.sell_token)
        if buy_ltp is None or sell_ltp is None:
            return False
        unrealised = (buy_ltp - self.state.buy_entry) - (sell_ltp - self.state.sell_entry)
        self.state.last_buy_ltp  = round(buy_ltp,  2)
        self.state.last_sell_ltp = round(sell_ltp, 2)
        if unrealised <= -self.state.hard_stop_pts:
            logger.warning(
                f"Hard stop triggered: unrealised={unrealised:.2f} "
                f"<= -{self.state.hard_stop_pts}")
            self._execute_exit('hard_stop')
            return True
        return False

    def _check_profit_target(self):
        if not ENABLE_PROFIT_TARGET or self.state.status != 'in_trade':
            return False
        if datetime.now().time() < self._no_exit_time:
            return False
        buy_ltp  = self.feed.get_ltp(self.state.buy_token)
        sell_ltp = self.feed.get_ltp(self.state.sell_token)
        if buy_ltp is None or sell_ltp is None:
            return False
        unrealised = (buy_ltp - self.state.buy_entry) - (sell_ltp - self.state.sell_entry)
        if unrealised >= self.state.profit_target_pts:
            logger.info(
                f"Profit target triggered: unrealised={unrealised:.2f} "
                f">= {self.state.profit_target_pts:.2f}")
            self._execute_exit('profit_target')
            return True
        return False

    def _check_time_gate(self, ts):
        if not ENABLE_TIME_GATE or self.state.status != 'in_trade':
            return False
        if self.state.gate_checked:
            return False
        if ts.strftime('%Y-%m-%d') != self.state.gate_date:
            return False
        if ts.strftime('%H:%M') < TIME_GATE_CHECK_TIME:
            return False

        self.state.gate_checked = True
        save_state(self.state)

        gate_pct  = self.state.gate_min_profit_pct or TIME_GATE_MIN_PROFIT_PCT_BULL
        threshold = self.state.max_profit * gate_pct
        logger.info(
            f"Time gate check: max_unrealised_pl={self.state.max_unrealised_pl:.2f}  "
            f"threshold={threshold:.2f} ({gate_pct:.0%})  "
            f"gate_passes={self.state.max_unrealised_pl >= threshold}")

        if self.state.max_unrealised_pl < threshold:
            self._execute_exit('time_gate')
            return True
        return False

    def _check_trend_flip(self, trend_15, flip_15):
        if self.state.status != 'in_trade' or not flip_15:
            return False
        if self.state.direction == 'bullish' and trend_15 is False:
            logger.info("Trend flip exit: bullish trade, 15-min flipped bearish.")
            self._execute_exit('trend_flip_15')
            return True
        if self.state.direction == 'bearish' and trend_15 is True:
            logger.info("Trend flip exit: bearish trade, 15-min flipped bullish.")
            self._execute_exit('trend_flip_15')
            return True
        return False

    def _check_pre_expiry(self, ts):
        if self.state.status != 'in_trade' or self.state.expiry is None:
            return False
        expiry_date = datetime.strptime(self.state.expiry, '%Y-%m-%d').date()
        elm_date    = self._compute_elm_date(expiry_date)
        expiry_dt   = datetime.combine(
            expiry_date,
            datetime.strptime('15:30', '%H:%M').time())
        elm_time    = (expiry_dt - timedelta(seconds=ELM_SECONDS_BEFORE_EXPIRY)).time()
        elm_dt      = datetime.combine(elm_date, elm_time)
        logger.debug(f"Pre-expiry check: elm_time={elm_dt}  now={datetime.now()}")
        if datetime.now() >= elm_dt:
            logger.info(f"Pre-expiry exit triggered at {datetime.now():%H:%M:%S}.")
            self._execute_exit('pre_expiry_exit')
            return True
        return False

    # -----------------------------------------------------------------------
    # Exit execution
    # -----------------------------------------------------------------------

    def _execute_exit(self, reason):
        if self.state.status not in ('in_trade',):
            return

        logger.info(
            f"Executing exit: reason={reason}  "
            f"direction={self.state.direction}  "
            f"buy={self.state.buy_strike}  sell={self.state.sell_strike}")

        self.state.status = 'exiting'
        save_state(self.state)

        lots = self.state.lots

        sell_close_ids = self._place_order(
            'BUY', self.state.sell_symbol, self.state.sell_token, lots)
        sleep(1)
        _reset_counters()
        self._fetch_order_book()
        sell_exit_fill, _ = self._fetch_order_details(sell_close_ids, self.state.sell_token)
        logger.info(f"Sell leg exit fill: {sell_exit_fill:.2f}")

        buy_close_ids = self._place_order(
            'SELL', self.state.buy_symbol, self.state.buy_token, lots)
        sleep(1)
        _reset_counters()
        self._fetch_order_book()
        buy_exit_fill, _ = self._fetch_order_details(buy_close_ids, self.state.buy_token)
        logger.info(f"Buy leg exit fill: {buy_exit_fill:.2f}")

        pl_points = round(
            (buy_exit_fill  - self.state.buy_entry) -
            (sell_exit_fill - self.state.sell_entry), 2)
        pl_rupees = round(pl_points * lots * LOT_SIZE, 2)

        logger.info(
            f"Exit P&L: {pl_points:+.2f} pts ({pl_rupees:+,.0f} Rs)  "
            f"lots={lots}  reason={reason}")

        self._append_trade_log_row(
            exit_reason=reason,
            realised_pl_pts=pl_points,
            realised_pl_rs=pl_rupees)
        self._save_trade_log()

        self.feed.unsubscribe_options(self.state.buy_token, self.state.sell_token)

        self._log_trade(reason, buy_exit_fill, sell_exit_fill, pl_points, pl_rupees)

        slack_bot_sendtext(
            f"*Apollo* EXIT {reason.upper()} | "
            f"{self.state.direction.upper()} | "
            f"Buy  {self.state.buy_strike} exit @ {buy_exit_fill:.1f} | "
            f"Sell {self.state.sell_strike} exit @ {sell_exit_fill:.1f} | "
            f"Lots: {lots} | "
            f"P&L: {pl_points:+.1f} pts ({pl_rupees:+,.0f} Rs)",
            SLACK_TRADE_ALERTS)

        clear_trade_fields(self.state)
        save_state(self.state)

        self._trade_log      = []
        self._update_elapsed = 0

    # -----------------------------------------------------------------------
    # Order management
    # -----------------------------------------------------------------------

    def _place_order(self, transaction_type, symbol, token, lots):
        if DRY_RUN:
            dry_id = f"DRY_{token}_{transaction_type}_{datetime.now():%H%M%S}"
            logger.info(
                f"[DRY RUN] {transaction_type} {lots} lot(s) {symbol} "
                f"(token={token}) — ID: {dry_id}")
            slack_bot_sendtext(
                f"*Apollo* DRY RUN | {transaction_type} {lots} lot(s) | "
                f"{symbol} | token: {token} | ID: {dry_id}",
                SLACK_TRADE_ALERTS)
            return [dry_id]

        l_limit = self._qty_freeze / LOT_SIZE
        order_quantities = []

        if lots <= l_limit:
            order_quantities.append(lots)
        else:
            full = int(lots // l_limit)
            rem  = lots % l_limit
            for _ in range(full):
                order_quantities.append(l_limit)
            if rem > 0:
                order_quantities.append(rem)

        orderid_list = []
        for lot_chunk in order_quantities:
            orderparams = {
                "variety":         "NORMAL",
                "tradingsymbol":   symbol,
                "symboltoken":     token,
                "transactiontype": transaction_type,
                "exchange":        FO_EXCHANGE_SEGMENT,
                "ordertype":       "MARKET",
                "producttype":     "CARRYFORWARD",
                "duration":        "DAY",
                "quantity":        str(int(lot_chunk * LOT_SIZE)),
            }
            while True:
                try:
                    response = self.obj.placeOrderFullResponse(orderparams)
                    _increment_order()
                    if response['message'] == 'SUCCESS':
                        orderid_list.append(response['data']['orderid'])
                        logger.info(
                            f"Order placed: {transaction_type} {symbol}  "
                            f"ID: {response['data']['orderid']}")
                        break
                except Exception as e:
                    handle_exception(e)
                sleep(1)
                _reset_counters()

        return orderid_list

    def _fetch_order_book(self):
        if DRY_RUN:
            return
        while True:
            try:
                self.order_book = self.obj.orderBook()
                _increment_poll()
                break
            except Exception as e:
                handle_exception(e)
            sleep(1)
            _reset_counters()

    def _fetch_order_details(self, orderid_list, token):
        if DRY_RUN:
            fill = self.feed.get_ltp(token)
            if fill is None or fill == 0.0:
                try:
                    row = self.instrument_df[
                        self.instrument_df['token'].astype(str) == str(token)]
                    if not row.empty:
                        symbol = row['symbol'].iloc[0]
                        fill   = self._fetch_option_ltp(symbol, token)
                    else:
                        fill = 0.0
                except Exception as e:
                    handle_exception(e)
                    fill = 0.0
            fill_time = datetime.now()
            logger.info(
                f"[DRY RUN] Fill for token {token}: {fill:.2f} at {fill_time:%H:%M:%S}")
            return fill, fill_time

        def get_details(order_book, orderid_list):
            price_list = []
            qty_list   = []
            time_list  = []
            for oid in orderid_list:
                for order in order_book['data']:
                    if order['orderid'] == oid:
                        price = order['averageprice']
                        qty   = int(order['quantity'])
                        price_list.append(price * qty)
                        qty_list.append(qty)
                        time_list.append(
                            datetime.strptime(order['updatetime'], '%d-%b-%Y %H:%M:%S'))
            return price_list, qty_list, time_list

        executed_price = None
        fill_time      = None
        price_list     = []
        qty_list       = []

        while (executed_price is None or fill_time is None or
               any(p == 0 for p in price_list) or
               any(q == 0 for q in qty_list)):
            price_list, qty_list, time_list = get_details(
                self.order_book, orderid_list)
            if price_list and qty_list and sum(qty_list) > 0:
                executed_price = sum(price_list) / sum(qty_list)
                fill_time      = max(time_list) if time_list else None
            if (executed_price is None or fill_time is None or
                    any(p == 0 for p in price_list) or
                    any(q == 0 for q in qty_list)):
                sleep(1)
                _reset_counters()
                self._fetch_order_book()

        logger.info(f"Order fill: {executed_price:.2f} at {fill_time}")
        return executed_price, fill_time

    def _fetch_option_ltp(self, symbol, token):
        while True:
            try:
                ltp = self.obj.ltpData(
                    FO_EXCHANGE_SEGMENT, symbol, token)['data']['ltp']
                _increment_poll()
                if ltp is not None:
                    return float(ltp)
            except Exception as e:
                handle_exception(e)
            sleep(1)
            _reset_counters()

    # -----------------------------------------------------------------------
    # Candle polling
    # -----------------------------------------------------------------------

    def _next_candle_close(self, now):
        minute    = now.minute
        remainder = minute % 15
        minutes_to_next = 15 - remainder if remainder != 0 else 15
        return now.replace(second=0, microsecond=0) + timedelta(minutes=minutes_to_next)

    def _fetch_latest_candle(self, candle_close_ts):
        """
        Fetch the completed candle for the window ending at candle_close_ts.
        Handles both open-time and close-time API timestamp formats.
        """
        expected_open_ts  = candle_close_ts - timedelta(minutes=15)
        fetch_from = candle_close_ts - timedelta(minutes=45)
        params = {
            "exchange":    "NSE",
            "symboltoken": NIFTY_INDEX_TOKEN,
            "interval":    "FIFTEEN_MINUTE",
            "fromdate":    fetch_from.strftime('%Y-%m-%d %H:%M'),
            "todate":      candle_close_ts.strftime('%Y-%m-%d %H:%M'),
        }
        logger.debug(
            f"Fetching candle: {expected_open_ts.strftime('%Y-%m-%d %H:%M')} "
            f"to {candle_close_ts.strftime('%Y-%m-%d %H:%M')}")

        for attempt in range(3):
            try:
                response = self.obj.getCandleData(params)
                _increment_poll()
                data = response.get('data', [])
                if data:
                    candles = []
                    for row in data:
                        ts_str = str(row[0]).replace('T', ' ')[:19]
                        candles.append({
                            'time_stamp': pd.Timestamp(ts_str),
                            'open':       float(row[1]),
                            'high':       float(row[2]),
                            'low':        float(row[3]),
                            'close':      float(row[4]),
                            'volume':     float(row[5]),
                        })

                    target = None
                    for c in candles:
                        if c['time_stamp'] == expected_open_ts:
                            target = c
                            break
                        elif c['time_stamp'] == candle_close_ts:
                            c['time_stamp'] = expected_open_ts
                            logger.debug(
                                f"Normalised close-time timestamp "
                                f"{candle_close_ts} -> {expected_open_ts}")
                            target = c
                            break

                    if target is not None:
                        logger.debug(
                            f"Candle fetched: {target['time_stamp']}  "
                            f"O={target['open']:.2f} H={target['high']:.2f} "
                            f"L={target['low']:.2f} C={target['close']:.2f}")
                        return target

                    got_ts = [str(c['time_stamp']) for c in candles]
                    logger.debug(
                        f"Target candle {expected_open_ts} not found. "
                        f"Got timestamps: {got_ts}. Retrying.")

            except Exception as e:
                handle_exception(e)
            logger.debug(f"Candle fetch attempt {attempt+1} failed. Retrying in 2s.")
            sleep(2)
            _reset_counters()

        return None

    # -----------------------------------------------------------------------
    # Per-trade logging
    # -----------------------------------------------------------------------

    def _append_trade_log_row(self, exit_reason=None,
                               realised_pl_pts=None, realised_pl_rs=None):
        if self.state.status not in ('in_trade', 'exiting'):
            return

        now       = datetime.now()
        buy_ltp   = self.feed.get_ltp(self.state.buy_token)
        sell_ltp  = self.feed.get_ltp(self.state.sell_token)
        nifty_ltp = self.feed.get_ltp(NIFTY_TOKEN)
        vix_ltp   = self.feed.get_ltp(FEED_VIX_TOKEN)

        ohlc_nifty = self.feed.get_ohlc(NIFTY_TOKEN)
        ohlc_vix   = self.feed.get_ohlc(FEED_VIX_TOKEN)
        ohlc_buy   = self.feed.get_ohlc(self.state.buy_token)
        ohlc_sell  = self.feed.get_ohlc(self.state.sell_token)

        unrealised_pts = None
        unrealised_rs  = None
        if buy_ltp is not None and sell_ltp is not None:
            unrealised_pts = round(
                (buy_ltp  - self.state.buy_entry) -
                (sell_ltp - self.state.sell_entry), 2)
            unrealised_rs = round(unrealised_pts * self.state.lots * LOT_SIZE, 2)

        logger.debug(
            f"Trade log row: buy_ltp={buy_ltp}  sell_ltp={sell_ltp}  "
            f"unrealised={unrealised_pts}  exit_reason={exit_reason}")

        row = {
            'time_stamp':        now.strftime('%Y-%m-%d %H:%M:%S'),
            'nifty_open':        ohlc_nifty['open']  if ohlc_nifty else nifty_ltp,
            'nifty_high':        ohlc_nifty['high']  if ohlc_nifty else nifty_ltp,
            'nifty_low':         ohlc_nifty['low']   if ohlc_nifty else nifty_ltp,
            'nifty_close':       ohlc_nifty['close'] if ohlc_nifty else nifty_ltp,
            'vix_open':          ohlc_vix['open']    if ohlc_vix   else vix_ltp,
            'vix_high':          ohlc_vix['high']    if ohlc_vix   else vix_ltp,
            'vix_low':           ohlc_vix['low']     if ohlc_vix   else vix_ltp,
            'vix_close':         ohlc_vix['close']   if ohlc_vix   else vix_ltp,
            'buy_open':          ohlc_buy['open']    if ohlc_buy   else buy_ltp,
            'buy_high':          ohlc_buy['high']    if ohlc_buy   else buy_ltp,
            'buy_low':           ohlc_buy['low']     if ohlc_buy   else buy_ltp,
            'buy_ltp':           buy_ltp,
            'sell_open':         ohlc_sell['open']   if ohlc_sell  else sell_ltp,
            'sell_high':         ohlc_sell['high']   if ohlc_sell  else sell_ltp,
            'sell_low':          ohlc_sell['low']    if ohlc_sell  else sell_ltp,
            'sell_ltp':          sell_ltp,
            'unrealised_pl_pts': unrealised_pts,
            'unrealised_pl_rs':  unrealised_rs,
            'realised_pl_pts':   realised_pl_pts,
            'realised_pl_rs':    realised_pl_rs,
            'exit_reason':       exit_reason,
        }
        self._trade_log.append(row)

        if exit_reason is None:
            self._flush_trade_log()

    def _flush_trade_log(self):
        if not self._trade_log:
            return
        try:
            os.makedirs(_TRADE_LOGS_DIR, exist_ok=True)
            entry_dt  = datetime.strptime(self.state.entry_time, '%Y-%m-%d %H:%M:%S')
            entry_str = entry_dt.strftime('%Y-%m-%d_%H%M')
            filename  = f"trade_{self._trade_counter + 1:04d}_{entry_str}.csv"
            filepath  = os.path.join(_TRADE_LOGS_DIR, filename)
            pd.DataFrame(self._trade_log).to_csv(filepath, index=False)
            logger.debug(f"Trade log flushed: {filename} ({len(self._trade_log)} rows)")
        except Exception as e:
            logger.error(f"Failed to flush trade log: {e}")

    def _save_trade_log(self):
        if not self._trade_log:
            return
        os.makedirs(_TRADE_LOGS_DIR, exist_ok=True)
        try:
            entry_dt  = datetime.strptime(self.state.entry_time, '%Y-%m-%d %H:%M:%S')
            entry_str = entry_dt.strftime('%Y-%m-%d_%H%M')
        except Exception:
            entry_str = datetime.now().strftime('%Y-%m-%d_%H%M')

        self._trade_counter += 1
        filename = f"trade_{self._trade_counter:04d}_{entry_str}.csv"
        filepath = os.path.join(_TRADE_LOGS_DIR, filename)
        pd.DataFrame(self._trade_log).to_csv(filepath, index=False)
        logger.info(f"Trade log saved: {filename} ({len(self._trade_log)} rows)")
        self._save_trade_counter()

    def _load_trade_log(self):
        try:
            entry_dt  = datetime.strptime(self.state.entry_time, '%Y-%m-%d %H:%M:%S')
            entry_str = entry_dt.strftime('%Y-%m-%d_%H%M')
        except Exception:
            logger.debug('Could not parse entry_time for trade log load.')
            return

        filename = f"trade_{self._trade_counter + 1:04d}_{entry_str}.csv"
        filepath = os.path.join(_TRADE_LOGS_DIR, filename)

        if not os.path.exists(filepath):
            logger.debug(f'No existing trade log found at {filename}.')
            return

        try:
            df = pd.read_csv(filepath)
            df = df[df['exit_reason'].isna()].copy()
            self._trade_log = df.to_dict('records')
            logger.info(
                f'Loaded {len(self._trade_log)} rows from existing trade log '
                f'{filename}.')
        except Exception as e:
            logger.error(f'Failed to load existing trade log: {e}')
            self._trade_log = []

    def _load_trade_counter(self):
        counter_file = os.path.join(DATA_DIR, 'trade_counter.txt')
        if os.path.exists(counter_file):
            try:
                with open(counter_file, 'r') as f:
                    return int(f.read().strip())
            except Exception:
                pass
        return 0

    def _save_trade_counter(self):
        counter_file = os.path.join(DATA_DIR, 'trade_counter.txt')
        try:
            with open(counter_file, 'w') as f:
                f.write(str(self._trade_counter))
        except Exception:
            pass

    def _log_trade(self, exit_reason, buy_exit, sell_exit, pl_points, pl_rupees):
        record = {
            'entry_time':        self.state.entry_time,
            'exit_time':         datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'direction':         self.state.direction,
            'expiry':            self.state.expiry,
            'buy_strike':        self.state.buy_strike,
            'sell_strike':       self.state.sell_strike,
            'option_type':       self.state.option_type,
            'buy_entry':         self.state.buy_entry,
            'sell_entry':        self.state.sell_entry,
            'buy_exit':          round(buy_exit,  2),
            'sell_exit':         round(sell_exit, 2),
            'net_debit':         self.state.net_debit,
            'max_profit':        self.state.max_profit,
            'pl_points':         pl_points,
            'pl_rupees':         pl_rupees,
            'exit_reason':       exit_reason,
            'entry_vix':         self.state.entry_vix,
            'entry_spot':        self.state.entry_spot,
            'max_unrealised_pl': self.state.max_unrealised_pl,
            'lots':              self.state.lots,
        }
        os.makedirs(DATA_DIR, exist_ok=True)
        df_new = pd.DataFrame([record])
        if os.path.exists(TRADES_FILE):
            df_new.to_csv(TRADES_FILE, mode='a', header=False, index=False)
        else:
            df_new.to_csv(TRADES_FILE, index=False)
        logger.info(
            f"Trade logged: {exit_reason}  "
            f"P&L={pl_points:+.2f} pts ({pl_rupees:+,.0f} Rs)")

    # -----------------------------------------------------------------------
    # Trade update (Slack #trade-updates)
    # -----------------------------------------------------------------------

    def _send_trade_update(self):
        if self.state.status != 'in_trade':
            return
        buy_ltp  = self.feed.get_ltp(self.state.buy_token)
        sell_ltp = self.feed.get_ltp(self.state.sell_token)
        nifty    = self.feed.get_ltp(NIFTY_TOKEN)
        vix      = self.feed.get_ltp(FEED_VIX_TOKEN)
        if None in (buy_ltp, sell_ltp):
            return
        unrealised    = round(
            (buy_ltp  - self.state.buy_entry) -
            (sell_ltp - self.state.sell_entry), 2)
        unrealised_rs = round(unrealised * self.state.lots * LOT_SIZE, 2)
        logger.debug(
            f"Trade update: nifty={nifty:.2f}  vix={vix:.2f}  "
            f"buy_ltp={buy_ltp:.2f}  sell_ltp={sell_ltp:.2f}  "
            f"unrealised={unrealised:+.2f}  lots={self.state.lots}")
        slack_bot_sendtext(
            f"*Apollo* UPDATE | {self.state.direction.upper()} | "
            f"Nifty: {nifty:.2f} | VIX: {vix:.2f} | "
            f"Buy LTP: {buy_ltp:.1f} | Sell LTP: {sell_ltp:.1f} | "
            f"Lots: {self.state.lots} | "
            f"Unrealised: {unrealised:+.1f} pts ({unrealised_rs:+,.0f} Rs) | "
            f"Peak: {self.state.max_unrealised_pl:+.1f} pts",
            SLACK_TRADE_UPDATES)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _compute_gate_date(self, expiry_date, direction):
        gate_days = TIME_GATE_DAYS_BULL if direction == 'bullish' else TIME_GATE_DAYS_BEAR
        gate = date.today() + timedelta(days=gate_days)
        while gate.weekday() >= 5 or gate in self.holidays:
            gate += timedelta(days=1)
        if gate >= expiry_date:
            gate = expiry_date
        logger.debug(
            f"Gate date computed: {gate} "
            f"(days={gate_days}, direction={direction})")
        return gate.strftime('%Y-%m-%d')

    def _load_holidays(self):
        """
        Load holidays for ELM and gate date computation.
        This is internal to Apollo — unrelated to the Leto's market check.
        """
        holidays_file = os.path.join(DATA_DIR, 'holidays.csv')
        if os.path.exists(holidays_file):
            df = pd.read_csv(holidays_file, parse_dates=['date'])
            self.holidays = set(df['date'].dt.date)
            logger.debug(f"Holidays loaded: {len(self.holidays)} dates.")
        else:
            self.holidays = set()
            logger.warning("holidays.csv not found. No holiday exclusions applied.")