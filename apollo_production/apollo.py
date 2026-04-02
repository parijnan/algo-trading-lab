"""
apollo.py — Apollo Production Main Entry Point
Nifty High-VIX ITM Debit Spread Strategy — Live Execution
Frozen production config: D-R-P2c

Run via cron on delos:
    14 9 * * 1-5 cd /home/parijnan/scripts/algo-trading-lab/apollo_production && \
    /home/parijnan/anaconda3/bin/python apollo.py >> logs/apollo_$(date +\%Y\%m\%d).log 2>&1

Architecture:
    - Apollo class owns login, run loop, entry/exit logic, order placement
    - websocket_feed.ApolloFeed     — tick feed, LTP/OHLC access, option subscribe/unsub
    - supertrend.SupertrendManager  — ST seeding and 15-min candle updates
    - state.ApolloState             — persistent trade state across restarts
    - functions.py                  — Slack/Telegram messaging, exception handling

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
import pandas as pd
from io import StringIO
from datetime import datetime, date, timedelta
from traceback import format_exc
from urllib.request import urlopen
from pyotp import TOTP
from time import sleep

from SmartApi import SmartConnect

from configs_live import (
    api_key, user_name, password, qr_code,
    NIFTY_INDEX_TOKEN, VIX_TOKEN,
    MARKET_OPEN, MARKET_CLOSE,
    VIX_THRESHOLD,
    SPREAD_TYPE, BUY_LEG_OFFSET, HEDGE_POINTS, STRIKE_STEP, MIN_DTE, LOT_SIZE,
    EXCLUDE_TRADE_DAYS, EXCLUDE_SIGNAL_CANDLES,
    ENABLE_HARD_STOP, HARD_STOP_POINTS,
    ENABLE_PROFIT_TARGET, PROFIT_TARGET_PCT,
    ENABLE_TIME_GATE, TIME_GATE_DAYS, TIME_GATE_CHECK_TIME, TIME_GATE_MIN_PROFIT_PCT,
    ELM_SECONDS_BEFORE_EXPIRY,
    NO_EXIT_BEFORE,
    FO_EXCHANGE_SEGMENT,
    TRADE_UPDATE_INTERVAL,
    SLACK_TRADEBOT_CHANNEL, SLACK_TRADE_ALERTS, SLACK_TRADE_UPDATES, SLACK_ERRORS_CHANNEL,
    TRADES_FILE, DATA_DIR,
)
from websocket_feed import ApolloFeed, NIFTY_TOKEN, VIX_TOKEN as FEED_VIX_TOKEN
from supertrend import SupertrendManager
from state import ApolloState, load_state, save_state, init_state, clear_trade_fields
from functions import slack_bot_sendtext, handle_exception

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


# ---------------------------------------------------------------------------
# Scrip master URL
# ---------------------------------------------------------------------------
_SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# Trade log directory
_TRADE_LOGS_DIR = os.path.join(DATA_DIR, "trade_logs")


class Apollo:
    """
    Apollo live execution engine.
    Owns the full session lifecycle: login -> seed -> run loop -> logout.
    """

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def __init__(self):
        self.obj            = None   # SmartConnect instance
        self.auth_token     = None
        self.instrument_df  = None   # Nifty options rows from scrip master
        self.holidays       = set()  # set of date objects

        self.feed           = ApolloFeed()
        self.st             = SupertrendManager()
        self.state          = load_state()

        self._opening_time  = datetime.strptime(MARKET_OPEN,  "%H:%M").time()
        self._closing_time  = datetime.strptime(MARKET_CLOSE, "%H:%M").time()
        self._no_exit_time  = datetime.strptime(NO_EXIT_BEFORE, "%H:%M").time()

        # Qty freeze for Nifty on NFO — 1800 units (36 lots of 50)
        self._qty_freeze    = 1800

        # Per-trade log state
        self._trade_log     = []     # list of row dicts for current trade
        self._trade_counter = self._load_trade_counter()
        self._update_elapsed = 0    # seconds since last trade update

    def login(self):
        """
        Login to Angel One, download scrip master, seed Supertrend,
        start WebSocket feed, and send session-start Slack alert.
        Exits the process if market is closed or it is a holiday.
        """
        now = datetime.now()

        # Market hours check
        if now.time() < self._opening_time or now.time() > self._closing_time:
            slack_bot_sendtext(
                f"APOLLO: Market is closed. Exiting at {now:%Y-%m-%d %H:%M:%S}.",
                SLACK_TRADEBOT_CHANNEL)
            sys.exit(0)

        # Holiday check
        self._load_holidays()
        if now.date() in self.holidays:
            slack_bot_sendtext(
                f"APOLLO: Market holiday today. Exiting at {now:%Y-%m-%d %H:%M:%S}.",
                SLACK_TRADEBOT_CHANNEL)
            sys.exit(0)

        # Login with retry — credentials imported from configs_live
        self.obj = SmartConnect(api_key=api_key)
        while True:
            try:
                totp            = TOTP(qr_code).now()
                data            = self.obj.generateSession(user_name, password, totp)
                break
            except Exception as e:
                handle_exception(e)
            sleep(1)

        self.auth_token = data['data']['jwtToken']

        slack_bot_sendtext(
            f"APOLLO: Logged in at {datetime.now():%Y-%m-%d %H:%M:%S}.",
            SLACK_TRADEBOT_CHANNEL)

        # Download and filter scrip master to Nifty NFO options
        scrip_df = pd.read_json(StringIO(urlopen(_SCRIP_MASTER_URL).read().decode()))
        self.instrument_df = scrip_df[
            (scrip_df['exch_seg'] == 'NFO') &
            (scrip_df['name'] == 'NIFTY')
        ].copy()

        # Seed Supertrend history
        self.st.seed(self.obj)
        slack_bot_sendtext(
            f"APOLLO: Supertrend seeded ({self.st.get_cache().shape[0]} candles).",
            SLACK_TRADEBOT_CHANNEL)

        # Start WebSocket feed
        self.feed.start(self.obj, self.auth_token, user_name)
        slack_bot_sendtext(
            f"APOLLO: WebSocket feed live. "
            f"Nifty: {self.feed.get_ltp(NIFTY_TOKEN):.2f}  "
            f"VIX: {self.feed.get_ltp(FEED_VIX_TOKEN):.2f}",
            SLACK_TRADEBOT_CHANNEL)

        # If restarting mid-trade, re-subscribe option tokens
        if self.state.status == 'in_trade':
            self.feed.subscribe_options(self.state.buy_token, self.state.sell_token)
            slack_bot_sendtext(
                f"APOLLO: Restarted — resuming active {self.state.direction.upper()} trade. "
                f"Buy {self.state.buy_strike} @ {self.state.buy_entry:.1f} | "
                f"Sell {self.state.sell_strike} @ {self.state.sell_entry:.1f}",
                SLACK_TRADE_ALERTS)

        # If crashed during exit, alert for manual review
        if self.state.status == 'exiting':
            slack_bot_sendtext(
                "APOLLO ALERT: Restarted with status=exiting. "
                "Check open positions manually and update apollo_state.csv.",
                SLACK_ERRORS_CHANNEL)
            sys.exit(1)

    def run(self):
        """
        Main run loop. Runs until market close.

        Each iteration waits for the next 15-min candle close, updates
        Supertrend, checks exits (if in trade) or entry filters (if idle).
        Between candle closes, polls LTPs every ~1s for hard stop and PT,
        and appends a trade log row + sends Slack update every
        TRADE_UPDATE_INTERVAL seconds.
        """
        slack_bot_sendtext(
            f"APOLLO: Run loop started. "
            f"VIX today: {self._get_todays_vix():.2f}  "
            f"Threshold: {VIX_THRESHOLD}",
            SLACK_TRADEBOT_CHANNEL)

        while True:
            now = datetime.now()
            if now.time() >= self._closing_time:
                break

            # ------------------------------------------------------------------
            # Wait for next 15-min candle close, polling every ~1s
            # ------------------------------------------------------------------
            next_close = self._next_candle_close(now)
            seconds_to_close = (next_close - now).total_seconds()

            elapsed = 0
            while elapsed < seconds_to_close - 2:
                sleep(1)
                elapsed += 1
                self._update_elapsed += 1

                if self.state.status == 'in_trade':
                    # Tick-by-tick hard stop and PT checks
                    if self._check_hard_stop():
                        continue
                    if self._check_profit_target():
                        continue

                    # Periodic trade log + Slack update
                    if self._update_elapsed >= TRADE_UPDATE_INTERVAL:
                        self._append_trade_log_row()
                        self._send_trade_update()
                        self._update_elapsed = 0

            # Sleep remaining fraction to land close to candle close
            remaining = (next_close - datetime.now()).total_seconds()
            if remaining > 0:
                sleep(remaining)

            # ------------------------------------------------------------------
            # Fetch the closed candle and update Supertrend
            # ------------------------------------------------------------------
            candle = self._fetch_latest_candle(next_close)
            if candle is None:
                continue

            ts = candle['time_stamp']
            try:
                trend_15, flip_15, trend_75, flip_75 = self.st.update(candle)
            except Exception as e:
                handle_exception(e)
                continue

            if trend_15 is None or trend_75 is None:
                continue   # ST warmup period

            # ------------------------------------------------------------------
            # In-trade exit checks (priority order per spec)
            # ------------------------------------------------------------------
            if self.state.status == 'in_trade':

                # Final hard stop and PT check at candle close
                if self._check_hard_stop():
                    continue
                if self._check_profit_target():
                    continue

                # Time gate — once at TIME_GATE_CHECK_TIME on gate day
                if self._check_time_gate(ts):
                    continue

                # Trend flip — primary exit signal
                if self._check_trend_flip(trend_15, flip_15):
                    continue

                # Pre-expiry exit — 15:15 day before expiry
                if self._check_pre_expiry(ts):
                    continue

            # ------------------------------------------------------------------
            # Idle entry logic
            # ------------------------------------------------------------------
            if self.state.status == 'idle':

                if not self._vix_gate_passes():
                    continue

                if not flip_15:
                    continue

                direction = self._resolve_direction(trend_15, trend_75)
                if direction is None:
                    continue

                if not self._check_entry_filters(ts):
                    continue

                self._execute_entry(direction, ts)

        # Market close — handle any position still open
        if self.state.status == 'in_trade':
            self._execute_exit('expiry_close')

    def logout(self):
        """Stop feed, terminate session, send close alert."""
        self.feed.stop()
        try:
            self.obj.terminateSession(user_name)
        except Exception as e:
            handle_exception(e)
        slack_bot_sendtext(
            f"APOLLO: Session complete. Logged out at {datetime.now():%Y-%m-%d %H:%M:%S}.",
            SLACK_TRADEBOT_CHANNEL)

    # -----------------------------------------------------------------------
    # VIX and entry filters
    # -----------------------------------------------------------------------

    def _get_todays_vix(self):
        vix = self.feed.get_ltp(FEED_VIX_TOKEN)
        return vix if vix is not None else VIX_THRESHOLD + 1

    def _vix_gate_passes(self):
        return self._get_todays_vix() > VIX_THRESHOLD

    def _check_entry_filters(self, ts):
        """
        D-R-P2c entry filters. Applied to signal candle timestamp.
        Returns True if entry is allowed, False if blocked.
        """
        if ts.dayofweek in EXCLUDE_TRADE_DAYS:
            return False
        if ts.strftime('%H:%M') in EXCLUDE_SIGNAL_CANDLES:
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

    def _select_expiry(self):
        """
        Select the appropriate Nifty weekly expiry.
        Uses current weekly if DTE >= MIN_DTE, else rolls to next weekly.
        Returns expiry as a date object, or None if not found.
        """
        today = date.today()
        expiry_dates = (
            self.instrument_df['expiry']
            .drop_duplicates()
            .apply(lambda x: datetime.strptime(x, '%d%b%Y').date())
            .sort_values()
        )
        future = expiry_dates[expiry_dates >= today]
        if future.empty:
            return None
        current_expiry = future.iloc[0]
        dte = (current_expiry - today).days
        if dte >= MIN_DTE:
            return current_expiry
        elif len(future) > 1:
            return future.iloc[1]
        return None

    def _fetch_symbol_and_token(self, strike, option_type, expiry_date):
        """
        Look up trading symbol and token from instrument_df.
        Returns (symbol, token) or (None, None) if not found.
        """
        expiry_str = expiry_date.strftime('%d%b%Y').upper()
        row = self.instrument_df[
            (self.instrument_df['expiry'] == expiry_str) &
            (self.instrument_df['strike'] == strike * 100) &
            (self.instrument_df['symbol'].str[-2:] == option_type.upper())
        ]
        if row.empty:
            return None, None
        return row['symbol'].iloc[0], str(row['token'].iloc[0])

    def _select_strikes(self, direction, spot, expiry_date):
        """
        Calculate buy (ITM) and sell (OTM) strikes for an ITM debit spread.

        BUY_LEG_OFFSET = -50 produces ITM in both directions:
            bullish CE: atm + (-50) = atm - 50 -> ITM CE (strike < spot)
            bearish PE: atm - (-50) = atm + 50 -> ITM PE (strike > spot)

        Returns (buy_strike, sell_strike, option_type,
                 buy_symbol, buy_token, sell_symbol, sell_token)
        or None if lookup fails.
        """
        option_type = 'ce' if direction == 'bullish' else 'pe'
        atm = round(spot / STRIKE_STEP) * STRIKE_STEP

        if direction == 'bullish':
            buy_strike  = int(atm + BUY_LEG_OFFSET)       # atm - 50 -> ITM CE
            sell_strike = int(buy_strike + HEDGE_POINTS)   # further OTM CE
        else:
            buy_strike  = int(atm - BUY_LEG_OFFSET)       # atm + 50 -> ITM PE
            sell_strike = int(buy_strike - HEDGE_POINTS)   # further OTM PE

        buy_symbol,  buy_token  = self._fetch_symbol_and_token(
            buy_strike,  option_type, expiry_date)
        sell_symbol, sell_token = self._fetch_symbol_and_token(
            sell_strike, option_type, expiry_date)

        if None in (buy_symbol, buy_token, sell_symbol, sell_token):
            return None

        return (buy_strike, sell_strike, option_type,
                buy_symbol, buy_token, sell_symbol, sell_token)

    # -----------------------------------------------------------------------
    # Entry execution
    # -----------------------------------------------------------------------

    def _execute_entry(self, direction, signal_ts):
        """
        Execute entry for a new debit spread position.
        Signal fired on candle close at signal_ts — entry executes now
        (next candle open). Always buy first, then sell.
        """
        spot = self.feed.get_ltp(NIFTY_TOKEN)
        if spot is None:
            slack_bot_sendtext(
                "APOLLO ALERT: Entry aborted — no Nifty LTP from feed.",
                SLACK_ERRORS_CHANNEL)
            return

        expiry_date = self._select_expiry()
        if expiry_date is None:
            slack_bot_sendtext(
                "APOLLO ALERT: Entry aborted — no valid expiry found.",
                SLACK_ERRORS_CHANNEL)
            return

        strikes = self._select_strikes(direction, spot, expiry_date)
        if strikes is None:
            slack_bot_sendtext(
                f"APOLLO ALERT: Entry aborted — strike lookup failed "
                f"(spot={spot:.0f}, direction={direction}).",
                SLACK_ERRORS_CHANNEL)
            return

        (buy_strike, sell_strike, option_type,
         buy_symbol, buy_token, sell_symbol, sell_token) = strikes

        # Always buy first
        buy_orderid_list = self._place_order('BUY', buy_symbol, buy_token, 1)
        sleep(1)
        _reset_counters()
        self._fetch_order_book()
        buy_fill, buy_time = self._fetch_order_details(buy_orderid_list)

        sell_orderid_list = self._place_order('SELL', sell_symbol, sell_token, 1)
        sleep(1)
        _reset_counters()
        self._fetch_order_book()
        sell_fill, sell_time = self._fetch_order_details(sell_orderid_list)

        # Populate state
        net_debit         = buy_fill - sell_fill
        max_profit        = HEDGE_POINTS - net_debit
        profit_target_pts = max_profit * PROFIT_TARGET_PCT

        self.state.status            = 'in_trade'
        self.state.direction         = direction
        self.state.buy_strike        = buy_strike
        self.state.sell_strike       = sell_strike
        self.state.option_type       = option_type
        self.state.expiry            = expiry_date.strftime('%Y-%m-%d')
        self.state.buy_token         = buy_token
        self.state.sell_token        = sell_token
        self.state.buy_symbol        = buy_symbol
        self.state.sell_symbol       = sell_symbol
        self.state.buy_entry         = round(buy_fill,  2)
        self.state.sell_entry        = round(sell_fill, 2)
        self.state.net_debit         = round(net_debit, 2)
        self.state.max_profit        = round(max_profit, 2)
        self.state.profit_target_pts = round(profit_target_pts, 2)
        self.state.hard_stop_pts     = HARD_STOP_POINTS
        self.state.entry_time        = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.state.entry_spot        = round(spot, 2)
        self.state.entry_vix         = round(self.feed.get_ltp(FEED_VIX_TOKEN) or 0, 2)
        self.state.gate_date         = self._compute_gate_date(expiry_date)
        self.state.gate_checked      = False
        self.state.max_unrealised_pl = 0.0
        self.state.last_buy_ltp      = round(buy_fill,  2)
        self.state.last_sell_ltp     = round(sell_fill, 2)
        save_state(self.state)

        # Initialise per-trade log and update timer
        self._trade_log      = []
        self._update_elapsed = 0

        # Subscribe option tokens to WebSocket
        self.feed.subscribe_options(buy_token, sell_token)

        slack_bot_sendtext(
            f"APOLLO ENTRY {direction.upper()} | "
            f"Buy  {buy_strike}{option_type.upper()} @ {buy_fill:.1f} | "
            f"Sell {sell_strike}{option_type.upper()} @ {sell_fill:.1f} | "
            f"Net debit: {net_debit:.1f} | Max profit: {max_profit:.1f} | "
            f"PT level: {profit_target_pts:.1f} pts | "
            f"Hard stop: {HARD_STOP_POINTS} pts | "
            f"Expiry: {expiry_date} | Spot: {spot:.0f}",
            SLACK_TRADE_ALERTS)

    # -----------------------------------------------------------------------
    # Exit triggers
    # -----------------------------------------------------------------------

    def _check_hard_stop(self):
        """
        Exit if unrealised P&L <= -HARD_STOP_POINTS.
        Also updates last LTPs and peak unrealised P&L in state.
        Returns True if exit triggered.
        """
        if not ENABLE_HARD_STOP or self.state.status != 'in_trade':
            return False

        buy_ltp  = self.feed.get_ltp(self.state.buy_token)
        sell_ltp = self.feed.get_ltp(self.state.sell_token)
        if buy_ltp is None or sell_ltp is None:
            return False

        unrealised = (buy_ltp - self.state.buy_entry) - (sell_ltp - self.state.sell_entry)
        self.state.last_buy_ltp  = round(buy_ltp,  2)
        self.state.last_sell_ltp = round(sell_ltp, 2)
        if unrealised > self.state.max_unrealised_pl:
            self.state.max_unrealised_pl = round(unrealised, 2)
            save_state(self.state)

        if unrealised <= -HARD_STOP_POINTS:
            self._execute_exit('hard_stop')
            return True
        return False

    def _check_profit_target(self):
        """
        Exit if unrealised P&L >= profit_target_pts.
        Returns True if exit triggered.
        """
        if not ENABLE_PROFIT_TARGET or self.state.status != 'in_trade':
            return False

        buy_ltp  = self.feed.get_ltp(self.state.buy_token)
        sell_ltp = self.feed.get_ltp(self.state.sell_token)
        if buy_ltp is None or sell_ltp is None:
            return False

        unrealised = (buy_ltp - self.state.buy_entry) - (sell_ltp - self.state.sell_entry)
        if unrealised >= self.state.profit_target_pts:
            self._execute_exit('profit_target')
            return True
        return False

    def _check_time_gate(self, ts):
        """
        Time gate: exit on gate day at TIME_GATE_CHECK_TIME if
        max_unrealised_pl < max_profit * TIME_GATE_MIN_PROFIT_PCT.
        Fires once per trade. Returns True if exit triggered.
        """
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

        threshold = self.state.max_profit * TIME_GATE_MIN_PROFIT_PCT
        if self.state.max_unrealised_pl < threshold:
            self._execute_exit('time_gate')
            return True
        return False

    def _check_trend_flip(self, trend_15, flip_15):
        """
        Exit on 15-min Supertrend flip against position direction.
        Returns True if exit triggered.
        """
        if self.state.status != 'in_trade' or not flip_15:
            return False
        if self.state.direction == 'bullish' and trend_15 is False:
            self._execute_exit('trend_flip_15')
            return True
        if self.state.direction == 'bearish' and trend_15 is True:
            self._execute_exit('trend_flip_15')
            return True
        return False

    def _check_pre_expiry(self, ts):
        """
        Pre-expiry exit: close position at 15:15 the day before expiry,
        adjusted for holidays. Returns True if exit triggered.
        """
        if self.state.status != 'in_trade' or self.state.expiry is None:
            return False

        expiry_dt = datetime.strptime(self.state.expiry, '%Y-%m-%d')
        elm_dt    = expiry_dt - timedelta(seconds=ELM_SECONDS_BEFORE_EXPIRY)

        elm_date  = elm_dt.date()
        while elm_date in self.holidays:
            elm_date -= timedelta(days=1)
        elm_dt = datetime.combine(elm_date, elm_dt.time())

        if datetime.now() >= elm_dt:
            self._execute_exit('pre_expiry_exit')
            return True
        return False

    # -----------------------------------------------------------------------
    # Exit execution
    # -----------------------------------------------------------------------

    def _execute_exit(self, reason):
        """
        Execute exit for the current debit spread position.
        Exit sold leg first, then buy leg.
        Saves final trade log row, appends trade record, resets state.
        """
        if self.state.status not in ('in_trade',):
            return

        self.state.status = 'exiting'
        save_state(self.state)

        # Exit sold leg first
        sell_close_ids = self._place_order(
            'BUY', self.state.sell_symbol, self.state.sell_token, 1)
        sleep(1)
        _reset_counters()
        self._fetch_order_book()
        sell_exit_fill, sell_exit_time = self._fetch_order_details(sell_close_ids)

        # Then exit buy leg
        buy_close_ids = self._place_order(
            'SELL', self.state.buy_symbol, self.state.buy_token, 1)
        sleep(1)
        _reset_counters()
        self._fetch_order_book()
        buy_exit_fill, buy_exit_time = self._fetch_order_details(buy_close_ids)

        pl_points = round(
            (buy_exit_fill  - self.state.buy_entry) -
            (sell_exit_fill - self.state.sell_entry), 2)
        pl_rupees = round(pl_points * LOT_SIZE, 2)

        # Unsubscribe option tokens from feed
        self.feed.unsubscribe_options(self.state.buy_token, self.state.sell_token)

        # Append final row to trade log and save file
        self._append_trade_log_row(
            exit_reason=reason,
            realised_pl_pts=pl_points,
            realised_pl_rs=pl_rupees)
        self._save_trade_log()

        # Append to apollo_trades.csv
        self._log_trade(reason, buy_exit_fill, sell_exit_fill, pl_points, pl_rupees)

        emoji = "checkmark" if pl_points > 0 else "x"
        slack_bot_sendtext(
            f"APOLLO EXIT {reason.upper()} | "
            f"{self.state.direction.upper()} | "
            f"Buy  {self.state.buy_strike} exit @ {buy_exit_fill:.1f} | "
            f"Sell {self.state.sell_strike} exit @ {sell_exit_fill:.1f} | "
            f"P&L: {pl_points:+.1f} pts ({pl_rupees:+,.0f} Rs)",
            SLACK_TRADE_ALERTS)

        clear_trade_fields(self.state)
        save_state(self.state)

        # Reset trade log and counter for next trade
        self._trade_log      = []
        self._update_elapsed = 0

    # -----------------------------------------------------------------------
    # Order management (ported from Artemis credit_spread.py)
    # -----------------------------------------------------------------------

    def _place_order(self, transaction_type, symbol, token, lots):
        """
        Place a market order, handling qty freeze splits.
        Returns list of order IDs. Retries on failure.
        """
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
                        break
                except Exception as e:
                    handle_exception(e)
                sleep(1)
                _reset_counters()

        return orderid_list

    def _fetch_order_book(self):
        """Fetch order book with retry. Stores in self.order_book."""
        while True:
            try:
                self.order_book = self.obj.orderBook()
                _increment_poll()
                break
            except Exception as e:
                handle_exception(e)
            sleep(1)
            _reset_counters()

    def _fetch_order_details(self, orderid_list):
        """
        Extract average fill price and fill time from order book.
        Loops until all orders have non-zero fill prices.
        Returns (avg_fill_price: float, fill_time: datetime).
        """
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

        return executed_price, fill_time

    # -----------------------------------------------------------------------
    # Candle polling
    # -----------------------------------------------------------------------

    def _next_candle_close(self, now):
        """Return datetime of the next 15-min candle close after now."""
        minute    = now.minute
        remainder = minute % 15
        minutes_to_next = 15 - remainder if remainder != 0 else 15
        return now.replace(second=0, microsecond=0) + timedelta(minutes=minutes_to_next)

    def _fetch_latest_candle(self, candle_close_ts):
        """
        Fetch the 15-min candle that just closed at candle_close_ts.
        Retries up to 3 times with 2s gaps.
        Returns dict with OHLCV keys, or None on failure.
        """
        candle_open = candle_close_ts - timedelta(minutes=15)
        params = {
            "exchange":    "NSE",
            "symboltoken": NIFTY_INDEX_TOKEN,
            "interval":    "FIFTEEN_MINUTE",
            "fromdate":    candle_open.strftime('%Y-%m-%d %H:%M'),
            "todate":      candle_close_ts.strftime('%Y-%m-%d %H:%M'),
        }

        for attempt in range(3):
            try:
                response = self.obj.getCandleData(params)
                _increment_poll()
                data = response.get('data', [])
                if data:
                    row = data[-1]
                    ts_str = str(row[0]).replace('T', ' ')[:19]
                    return {
                        'time_stamp': pd.Timestamp(ts_str),
                        'open':       float(row[1]),
                        'high':       float(row[2]),
                        'low':        float(row[3]),
                        'close':      float(row[4]),
                        'volume':     float(row[5]),
                    }
            except Exception as e:
                handle_exception(e)
            sleep(2)
            _reset_counters()

        return None

    # -----------------------------------------------------------------------
    # Per-trade logging
    # -----------------------------------------------------------------------

    def _append_trade_log_row(self, exit_reason=None,
                               realised_pl_pts=None, realised_pl_rs=None):
        """
        Append one row to the in-memory trade log.
        Called every TRADE_UPDATE_INTERVAL seconds and on exit.
        OHLC windows are consumed (reset) on each call via feed.get_ohlc().

        Columns mirror the backtest per-trade 1-min log schema for
        direct comparison, adapted to TRADE_UPDATE_INTERVAL intervals.
        """
        if self.state.status not in ('in_trade', 'exiting'):
            return

        now      = datetime.now()
        buy_ltp  = self.feed.get_ltp(self.state.buy_token)
        sell_ltp = self.feed.get_ltp(self.state.sell_token)
        nifty_ltp = self.feed.get_ltp(NIFTY_TOKEN)
        vix_ltp   = self.feed.get_ltp(FEED_VIX_TOKEN)

        # Consume OHLC windows — resets for next interval
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
            unrealised_rs = round(unrealised_pts * LOT_SIZE, 2)

        row = {
            'time_stamp':           now.strftime('%Y-%m-%d %H:%M:%S'),
            # Nifty OHLC for the interval
            'nifty_open':           ohlc_nifty['open']  if ohlc_nifty else nifty_ltp,
            'nifty_high':           ohlc_nifty['high']  if ohlc_nifty else nifty_ltp,
            'nifty_low':            ohlc_nifty['low']   if ohlc_nifty else nifty_ltp,
            'nifty_close':          ohlc_nifty['close'] if ohlc_nifty else nifty_ltp,
            # VIX OHLC for the interval
            'vix_open':             ohlc_vix['open']    if ohlc_vix  else vix_ltp,
            'vix_high':             ohlc_vix['high']    if ohlc_vix  else vix_ltp,
            'vix_low':              ohlc_vix['low']     if ohlc_vix  else vix_ltp,
            'vix_close':            ohlc_vix['close']   if ohlc_vix  else vix_ltp,
            # Buy leg OHLC
            'buy_open':             ohlc_buy['open']    if ohlc_buy  else buy_ltp,
            'buy_high':             ohlc_buy['high']    if ohlc_buy  else buy_ltp,
            'buy_low':              ohlc_buy['low']     if ohlc_buy  else buy_ltp,
            'buy_ltp':              buy_ltp,
            # Sell leg OHLC
            'sell_open':            ohlc_sell['open']   if ohlc_sell else sell_ltp,
            'sell_high':            ohlc_sell['high']   if ohlc_sell else sell_ltp,
            'sell_low':             ohlc_sell['low']    if ohlc_sell else sell_ltp,
            'sell_ltp':             sell_ltp,
            # P&L
            'unrealised_pl_pts':    unrealised_pts,
            'unrealised_pl_rs':     unrealised_rs,
            'realised_pl_pts':      realised_pl_pts,  # only on exit row
            'realised_pl_rs':       realised_pl_rs,   # only on exit row
            'exit_reason':          exit_reason,       # only on exit row
        }
        self._trade_log.append(row)

    def _save_trade_log(self):
        """
        Save the completed per-trade log to data/trade_logs/.
        Filename: trade_NNNN_YYYY-MM-DD_HHMM.csv — matches backtest naming.
        """
        if not self._trade_log:
            return

        os.makedirs(_TRADE_LOGS_DIR, exist_ok=True)
        entry_str = self.state.entry_time.replace(':', '').replace('-', '').replace(' ', '_')
        # Reformat to YYYY-MM-DD_HHMM
        try:
            entry_dt  = datetime.strptime(self.state.entry_time, '%Y-%m-%d %H:%M:%S')
            entry_str = entry_dt.strftime('%Y-%m-%d_%H%M')
        except Exception:
            pass

        self._trade_counter += 1
        filename = f"trade_{self._trade_counter:04d}_{entry_str}.csv"
        filepath = os.path.join(_TRADE_LOGS_DIR, filename)

        pd.DataFrame(self._trade_log).to_csv(filepath, index=False)
        self._save_trade_counter()

    def _load_trade_counter(self):
        """Load last trade counter from disk to maintain sequential numbering."""
        counter_file = os.path.join(DATA_DIR, 'trade_counter.txt')
        if os.path.exists(counter_file):
            try:
                with open(counter_file, 'r') as f:
                    return int(f.read().strip())
            except Exception:
                pass
        return 0

    def _save_trade_counter(self):
        """Persist trade counter to disk."""
        counter_file = os.path.join(DATA_DIR, 'trade_counter.txt')
        try:
            with open(counter_file, 'w') as f:
                f.write(str(self._trade_counter))
        except Exception:
            pass

    def _log_trade(self, exit_reason, buy_exit, sell_exit, pl_points, pl_rupees):
        """
        Append completed trade to apollo_trades.csv.
        Schema mirrors the backtest trade_summary for direct comparison.
        """
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
        }

        os.makedirs(DATA_DIR, exist_ok=True)
        df_new = pd.DataFrame([record])

        if os.path.exists(TRADES_FILE):
            df_new.to_csv(TRADES_FILE, mode='a', header=False, index=False)
        else:
            df_new.to_csv(TRADES_FILE, index=False)

    # -----------------------------------------------------------------------
    # Trade update (Slack #trade-updates)
    # -----------------------------------------------------------------------

    def _send_trade_update(self):
        """
        Send periodic trade status to #trade-updates (muted channel).
        Called every TRADE_UPDATE_INTERVAL seconds while in trade.
        """
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
        unrealised_rs = round(unrealised * LOT_SIZE, 2)

        slack_bot_sendtext(
            f"APOLLO UPDATE | {self.state.direction.upper()} | "
            f"Nifty: {nifty:.2f} | VIX: {vix:.2f} | "
            f"Buy LTP: {buy_ltp:.1f} | Sell LTP: {sell_ltp:.1f} | "
            f"Unrealised: {unrealised:+.1f} pts ({unrealised_rs:+,.0f} Rs) | "
            f"Peak: {self.state.max_unrealised_pl:+.1f} pts",
            SLACK_TRADE_UPDATES)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _compute_gate_date(self, expiry_date):
        """Next trading day after today, capped at expiry. Returns 'YYYY-MM-DD'."""
        gate = date.today() + timedelta(days=1)
        while gate.weekday() >= 5 or gate in self.holidays:
            gate += timedelta(days=1)
        if gate >= expiry_date:
            gate = expiry_date
        return gate.strftime('%Y-%m-%d')

    def _load_holidays(self):
        """Load holidays from data/holidays.csv into self.holidays set."""
        holidays_file = os.path.join(DATA_DIR, 'holidays.csv')
        if os.path.exists(holidays_file):
            df = pd.read_csv(holidays_file, parse_dates=['date'])
            self.holidays = set(df['date'].dt.date)
        else:
            self.holidays = set()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    apollo = Apollo()
    apollo.login()
    try:
        apollo.run()
    except Exception as e:
        handle_exception(e)
    finally:
        apollo.logout()