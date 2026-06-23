"""iron_condor.py — Artemis Production Iron Condor"""

import os
from credit_spread import CreditSpread
from datetime import datetime, timedelta
from math import floor
from os.path import exists
from functions import sleep, handle_exception, slack_bot_sendtext, reset_counters, increment_rms_poll, OrderFillWatcher
from configs import (
    pd, lot_size, monitor_frequency, lot_calc, lot_capital,
    vix_threshold, entry_window_minutes, exchange_segment,
    instrument, underlying_token, REPO_ROOT,
    api_key, user_name,
    SLACK_TRADEBOT_CHANNEL, SLACK_TRADE_ALERTS, SLACK_TRADE_UPDATES, SLACK_ERRORS_CHANNEL,
)
from logger_setup import get_logger
from websocket_feed import SharedFeed, EXCHANGE_BSE_CM, EXCHANGE_BSE_FO

logger = get_logger('artemis.iron_condor')

# IronCondor class consisting of pe and ce credit spreads
class IronCondor:
    # Private method to set current datetime for the object
    def _set_current_datetime(self):
        self.current_datetime = datetime.now()
        self.current_time = self.current_datetime.time()
        self.current_date = self.current_datetime.date()

    # Private method to capitalize and format the spread status for Slack messages
    def _format_status(self, spread_status):
        if spread_status == 'open':
            return 'Open'
        elif spread_status == 'active':
            return 'Active'
        elif spread_status == 'closed':
            return 'Closed'
        elif spread_status == 'adjusted':
            return 'Adjusted'
        elif spread_status == 'adjusted_additional':
            return 'Adjusted Additional'
        elif spread_status == 'adjusted_elm':
            return 'Adjusted ELM'
        elif spread_status == 'adjusted_additional_elm':
            return 'Adjusted Additional ELM'
        elif spread_status == 'active_elm':
            return 'Active ELM'
        elif spread_status == 'active_additional_elm':
            return 'Active Additional ELM'
        elif spread_status == 'active_additional':
            return 'Active Additional'

    # Initialize after initializing pe and ce spreads
    def __init__(self):
        self._set_current_datetime()
        self.pe_spread = CreditSpread('pe')
        self.ce_spread = CreditSpread('ce')
        self.expiry = self.pe_spread.expiry
        self.lots = self.pe_spread.lots
        self.additional_lots = self.lots//2
        self.elm_time = self.pe_spread.elm_time
        self.feed = SharedFeed()
        self._update_elapsed = 0.0
        # Check and set ELM status
        if self.current_datetime > self.elm_time:
            if self.ce_spread.spread_status == 'active' and self.pe_spread.spread_status == 'active':
                self.adjusted_for_elm = False
            elif self.pe_spread.spread_status == 'closed' and (self.ce_spread.spread_status[-3:] == 'elm' or self.ce_spread.spread_status == 'active'):
                self.adjusted_for_elm = True
            elif self.ce_spread.spread_status == 'closed' and (self.pe_spread.spread_status[-3:] == 'elm' or self.pe_spread.spread_status == 'active'):
                self.adjusted_for_elm = True
            else:
                self.adjusted_for_elm = False
        else:
            self.adjusted_for_elm = False
        # Check and set trade status
        if self.pe_spread.spread_status == 'closed' and self.ce_spread.spread_status == 'closed':
            self.trade_status = False
        else:
            self.trade_status = True
        # Load trade book and cast columns to appropriate dtypes
        if exists("data/trade_book.csv"):
            self.trade_book_df = pd.read_csv("data/trade_book.csv", parse_dates=['entry_time'])
            self.trade_book_df['entry_price'] = self.trade_book_df['entry_price'].astype('float64')
            self.trade_book_df['ltp'] = self.trade_book_df['ltp'].astype('float64')
            self.trade_book_df['exit_price'] = self.trade_book_df['exit_price'].astype('float64')
            self.trade_book_df['pl'] = self.trade_book_df['pl'].astype('float64')
        else:
            self.trade_book_df = pd.DataFrame({'entry_time': [self.pe_spread.entry, self.pe_spread.entry, self.ce_spread.entry, self.ce_spread.entry],
                                               'role': ['pe_buy', 'pe_sell', 'ce_buy', 'ce_sell'],
                                               'symbol': [self.pe_spread.buy_symbol, self.pe_spread.sell_symbol, self.ce_spread.buy_symbol, self.ce_spread.sell_symbol],
                                               'entry_price': [self.pe_spread.buy_entry, self.pe_spread.sell_entry, self.ce_spread.buy_entry, self.ce_spread.sell_entry],
                                               'status': ['open', 'open', 'open', 'open'],
                                               'ltp': [self.pe_spread.buy_ltp, self.pe_spread.sell_ltp, self.ce_spread.buy_ltp, self.ce_spread.sell_ltp],
                                               'exit_price': [self.pe_spread.buy_exit, self.pe_spread.sell_exit, self.ce_spread.buy_exit, self.ce_spread.sell_exit],
                                               'pl': [(self.pe_spread.buy_entry-self.pe_spread.buy_ltp), (self.pe_spread.sell_entry-self.pe_spread.sell_ltp), (self.ce_spread.buy_entry-self.ce_spread.buy_ltp), (self.ce_spread.sell_entry-self.ce_spread.sell_ltp)]})
            self.trade_book_df.to_csv('data/trade_book.csv', index=False)
        if exists('data/trade_log.csv'):
            self.trade_log_df = pd.read_csv('data/trade_log.csv', parse_dates=['time_stamp'])

    def set_session(self, obj, auth_token, instrument_df):
        """
        Receive the authenticated SmartConnect object, JWT auth token, and
        filtered Sensex instrument DataFrame from Leto. Called immediately
        after IronCondor() is instantiated.
        """
        self._set_current_datetime()
        self.obj = obj
        self.instrument_df = instrument_df

        # Propagate to both spreads
        self.pe_spread.obj = self.ce_spread.obj = self.obj
        self.pe_spread.instrument_df = self.ce_spread.instrument_df = self.instrument_df

        # Start shared order fill WS daemon; assign to both spreads
        feed_token = obj.getfeedToken()
        watcher = OrderFillWatcher()
        watcher.start(auth_token, api_key, user_name, feed_token)
        self.pe_spread._order_watcher = self.ce_spread._order_watcher = watcher

        # Start LTP WebSocket feed; propagate to both spreads
        try:
            self.feed.start(
                auth_token, api_key, user_name, feed_token,
                startup_tokens=[(EXCHANGE_BSE_CM, underlying_token)],
                alert_callback=lambda msg: slack_bot_sendtext(f"⚠️ *Artemis*: {msg}", SLACK_ERRORS_CHANNEL)
            )
            index_ltp = self.feed.get_ltp(underlying_token)
            slack_bot_sendtext(
                (f"✅ *Artemis*: WebSocket LTP feed live. {instrument}: {index_ltp:.2f}"
                 if index_ltp
                 else "✅ *Artemis*: WebSocket LTP feed live. LTPs not yet available."),
                SLACK_TRADEBOT_CHANNEL)
        except Exception as e:
            logger.warning(f"WS LTP feed failed to start: {e}. Using REST fallback.")
            slack_bot_sendtext("⚠️ *Artemis*: WS LTP feed failed to start — REST fallback active.", SLACK_ERRORS_CHANNEL)
        self.pe_spread.feed = self.ce_spread.feed = self.feed

        # Subscribe existing leg tokens on restart (if already in_trade)
        if self.trade_status:
            self._subscribe_active_tokens()
            self._reconcile_positions()

        # Position sizing — only runs on a fresh trade (both spreads still 'open')
        if lot_calc and self.pe_spread.spread_status == 'open' and self.ce_spread.spread_status == 'open':
            while True:
                try:
                    margin = float(self.obj.rmsLimit()['data']['availablecash'])
                    increment_rms_poll()
                    self.lots = floor(margin / lot_capital)
                    self.additional_lots = self.lots // 2
                    break
                except Exception as e:
                    handle_exception(e)
            self.pe_spread.lots = self.ce_spread.lots = self.lots
            self.pe_spread.trade_params_df.iloc[0, 1] = self.pe_spread.lots
            self.ce_spread.trade_params_df.iloc[0, 1] = self.ce_spread.lots
            self.pe_spread.trade_params_df.to_csv("data/pe_trade_params.csv", index=False)
            self.ce_spread.trade_params_df.to_csv("data/ce_trade_params.csv", index=False)
            sleep(1)
            reset_counters()

        msg_txt = f"Artemis session ready at {self.current_datetime:%Y-%m-%d %H:%M:%S}."
        logger.info(msg_txt)
        slack_bot_sendtext(msg_txt, SLACK_TRADEBOT_CHANNEL)

    # Method to execute trade
    def execute_trade(self):
        """
        Entry gate: checks entry window and VIX before executing either spread.
        If either check fails, cleans up state files and returns immediately —
        artemis.run() detects the 'open' status and skips the monitoring loop.
        """
        # Only run for a fresh trade (both spreads still waiting for entry)
        if self.pe_spread.spread_status == 'open' and self.ce_spread.spread_status == 'open':
            self._set_current_datetime()

            # Wait until entry time — so that VIX check happens at entry, not at 09:15
            if self.current_datetime < self.pe_spread.entry:
                msg_txt = (f"Waiting till {self.pe_spread.entry:%H:%M} to execute trade. "
                           f"*Lots that will be traded:* _{self.lots}_")
                logger.info(msg_txt)
                slack_bot_sendtext(msg_txt, SLACK_TRADE_ALERTS)
                sleep(int((self.pe_spread.entry - datetime.now()).total_seconds()))
                reset_counters()
                self._set_current_datetime()

            # Gate 1: entry window check — sit out the week if window has passed
            entry_by = self.pe_spread.entry + timedelta(minutes=entry_window_minutes)
            if self.current_datetime > entry_by:
                msg_txt = (f"Entry window closed at {entry_by:%H:%M}. "
                           f"Standing down for the week.")
                logger.info(msg_txt)
                slack_bot_sendtext(msg_txt, SLACK_TRADE_ALERTS)
                self._cleanup_state_files()
                return

            # Gate 2: VIX check at entry time — skipped when routing is manually forced
            routing_mode = 'auto'
            try:
                import importlib, leto_config as _lc
                importlib.reload(_lc)
                routing_mode = _lc.ROUTING_MODE
            except Exception:
                pass
            if routing_mode == 'auto':
                vix = self.pe_spread._fetch_ltp("NSE", "India VIX", "99926017")
                if vix and vix > vix_threshold:
                    msg_txt = (f"VIX {vix:.2f} above threshold {vix_threshold} at entry time. "
                               f"Standing down for the week.")
                    logger.info(msg_txt)
                    slack_bot_sendtext(msg_txt, SLACK_TRADE_ALERTS)
                    self._cleanup_state_files()
                    return

        # Both gates passed — execute both spreads
        while self.pe_spread.spread_status == 'open' or self.ce_spread.spread_status == 'open':
            try:
                for spread in [self.pe_spread, self.ce_spread]:
                    if spread.spread_status == 'open':
                        spread.initialize_spread()
                        spread.execute_spread()
                if self.pe_spread.spread_status == 'active' and self.ce_spread.spread_status == 'active':
                    self._update_trade_book_entry()
            except Exception as e:
                handle_exception(e)
                continue
        self._subscribe_active_tokens()
        self._update_elapsed = 0.0

    def _subscribe_active_tokens(self):
        """Subscribe WS LTP for all currently active spread leg tokens."""
        if not self.feed.is_connected():
            return
        tokens = []
        for spread in [self.pe_spread, self.ce_spread]:
            if spread.spread_status != 'closed':
                for attr in ('buy_token', 'sell_token', 'additional_buy_token'):
                    tok = getattr(spread, attr, None)
                    if tok and isinstance(tok, str) and tok.strip():
                        tokens.append(tok)
        if tokens:
            self.feed.subscribe_options(tokens, EXCHANGE_BSE_FO)

    # Private method to clean up state files when standing down before entry
    def _cleanup_state_files(self):
        """
        Remove state files created during __init__ when Artemis stands down
        before executing any trade. Leaves data/ clean for next week.
        Called when the entry window has passed or VIX check fails.
        """
        for filepath in [
            'data/pe_trade_params.csv',
            'data/ce_trade_params.csv',
            'data/trade_book.csv',
        ]:
            if exists(filepath):
                os.remove(filepath)

    # Private method to update trade log at chosen interval
    def _update_trade_log(self):
        if not exists('data/trade_log.csv'):
            self.trade_log_df = pd.DataFrame({'time_stamp': [self.current_datetime],
                                              'index_value': [self.ce_spread.index_ltp],
                                              'pe_buy_strike': [self.pe_spread.buy_strike],
                                              'pe_buy_ltp': [self.pe_spread.buy_ltp],
                                              'additional_pe_buy_strike': [self.pe_spread.additional_buy_strike],
                                              'additional_pe_buy_ltp': [self.pe_spread.additional_buy_ltp],
                                              'ce_buy_strike': [self.ce_spread.buy_strike],
                                              'ce_buy_ltp': [self.ce_spread.buy_ltp],
                                              'additional_ce_buy_strike': [self.ce_spread.additional_buy_strike],
                                              'additional_ce_buy_ltp': [self.ce_spread.additional_buy_ltp],
                                              'pe_sell_strike': [self.pe_spread.sell_strike],
                                              'pe_sell_ltp': [self.pe_spread.sell_ltp],
                                              'ce_sell_strike': [self.ce_spread.sell_strike],
                                              'ce_sell_ltp': [self.ce_spread.sell_ltp],
                                              'pl': [(self.pe_spread.pl + self.ce_spread.pl)*lot_size],
                                              'additional_pl': [(self.pe_spread.additional_pl + self.ce_spread.additional_pl)*lot_size],
                                              'total_pl':[((((self.pe_spread.pl + self.ce_spread.pl) * self.lots) + ((self.pe_spread.additional_pl + self.ce_spread.additional_pl) * self.additional_lots)) / self.lots)*lot_size]})
        else:
            if self.ce_spread.spread_status == 'closed':
                time_stamp = self.pe_spread.current_datetime
                index_ltp = self.pe_spread.index_ltp
            else:
                time_stamp = self.ce_spread.current_datetime
                index_ltp = self.ce_spread.index_ltp
            new_record_df = pd.DataFrame({'time_stamp': [time_stamp],
                                        'index_value': [index_ltp],
                                        'pe_buy_strike': [self.pe_spread.buy_strike],
                                        'pe_buy_ltp': [self.pe_spread.buy_ltp],
                                        'additional_pe_buy_strike': [self.pe_spread.additional_buy_strike],
                                        'additional_pe_buy_ltp': [self.pe_spread.additional_buy_ltp],
                                        'ce_buy_strike': [self.ce_spread.buy_strike],
                                        'ce_buy_ltp': [self.ce_spread.buy_ltp],
                                        'additional_ce_buy_strike': [self.ce_spread.additional_buy_strike],
                                        'additional_ce_buy_ltp': [self.ce_spread.additional_buy_ltp],
                                        'pe_sell_strike': [self.pe_spread.sell_strike],
                                        'pe_sell_ltp': [self.pe_spread.sell_ltp],
                                        'ce_sell_strike': [self.ce_spread.sell_strike],
                                        'ce_sell_ltp': [self.ce_spread.sell_ltp],
                                        'pl': [(self.pe_spread.pl + self.ce_spread.pl)*lot_size],
                                        'additional_pl': [(self.pe_spread.additional_pl + self.ce_spread.additional_pl)*lot_size],
                                        'total_pl':[((((self.pe_spread.pl + self.ce_spread.pl) * self.lots) + ((self.pe_spread.additional_pl + self.ce_spread.additional_pl) * self.additional_lots)) / self.lots)*lot_size]})
            self.trade_log_df = pd.concat([self.trade_log_df, new_record_df], ignore_index=True)
        self.trade_log_df.to_csv('data/trade_log.csv', index=False)

    # Private method to create trade entries after entering the initial trade
    def _update_trade_book_entry(self):
        self._set_current_datetime()
        self.trade_book_df.iloc[0, 0] = self.pe_spread.entry
        self.trade_book_df.iloc[0, 2] = self.pe_spread.buy_symbol
        self.trade_book_df.iloc[0, 3] = self.pe_spread.buy_entry
        self.trade_book_df.iloc[0, 4] = 'active'
        self.trade_book_df.iloc[0, 5] = self.pe_spread.buy_ltp
        self.trade_book_df.iloc[0, 7] = self.pe_spread.buy_entry - self.pe_spread.buy_ltp
        self.trade_book_df.iloc[1, 0] = self.pe_spread.entry
        self.trade_book_df.iloc[1, 2] = self.pe_spread.sell_symbol
        self.trade_book_df.iloc[1, 3] = self.pe_spread.sell_entry
        self.trade_book_df.iloc[1, 4] = 'active'
        self.trade_book_df.iloc[1, 5] = self.pe_spread.sell_ltp
        self.trade_book_df.iloc[1, 7] = self.pe_spread.sell_entry - self.pe_spread.sell_ltp
        self.trade_book_df.iloc[2, 0] = self.ce_spread.entry
        self.trade_book_df.iloc[2, 2] = self.ce_spread.buy_symbol
        self.trade_book_df.iloc[2, 3] = self.ce_spread.buy_entry
        self.trade_book_df.iloc[2, 4] = 'active'
        self.trade_book_df.iloc[2, 5] = self.ce_spread.buy_ltp
        self.trade_book_df.iloc[2, 7] = self.ce_spread.buy_entry - self.ce_spread.buy_ltp
        self.trade_book_df.iloc[3, 0] = self.ce_spread.entry
        self.trade_book_df.iloc[3, 2] = self.ce_spread.sell_symbol
        self.trade_book_df.iloc[3, 3] = self.ce_spread.sell_entry
        self.trade_book_df.iloc[3, 4] = 'active'
        self.trade_book_df.iloc[3, 5] = self.ce_spread.sell_ltp
        self.trade_book_df.iloc[3, 7] = self.ce_spread.sell_entry - self.ce_spread.sell_ltp
        self.trade_book_df.to_csv('data/trade_book.csv', index=False)

    # Private method to update trade book on re-entry of any spread
    def _update_trade_book_re_entry(self):
        self._set_current_datetime()
        if self.pe_spread.spread_status == 'active' or self.pe_spread.spread_status == 'active_additional':
            new_trade_book_record_df = pd.DataFrame({'entry_time': [self.pe_spread.entry, self.pe_spread.entry],
                                                     'role': ['pe_buy', 'pe_sell'],
                                                     'symbol': [self.pe_spread.buy_symbol, self.pe_spread.sell_symbol],
                                                     'entry_price': [self.pe_spread.buy_entry, self.pe_spread.sell_entry],
                                                     'status': ['active', 'active'],
                                                     'ltp': [self.pe_spread.buy_ltp, self.pe_spread.sell_ltp],
                                                     'exit_price': [0.0, 0.0],
                                                     'pl': [(self.pe_spread.buy_entry-self.pe_spread.buy_ltp), (self.pe_spread.sell_entry-self.pe_spread.sell_ltp)]})
            self.trade_book_df = pd.concat([self.trade_book_df, new_trade_book_record_df], ignore_index=True)
            if self.pe_spread.spread_status == 'active_additional':
                new_trade_book_record_df = pd.DataFrame({'entry_time': [self.pe_spread.entry, self.pe_spread.entry],
                                                         'role': ['additional_pe_buy', 'additional_pe_sell'],
                                                         'symbol': [self.pe_spread.additional_buy_symbol, self.pe_spread.sell_symbol],
                                                         'entry_price': [self.pe_spread.buy_entry, self.pe_spread.sell_entry],
                                                         'status': ['active', 'active'],
                                                         'ltp': [self.pe_spread.additional_buy_ltp, self.pe_spread.sell_ltp],
                                                         'exit_price': [0.0, 0.0],
                                                         'pl': [(self.pe_spread.additional_buy_entry-self.pe_spread.additional_buy_ltp), (self.pe_spread.sell_entry-self.pe_spread.sell_ltp)]})
                self.trade_book_df = pd.concat([self.trade_book_df, new_trade_book_record_df], ignore_index=True)
        if self.ce_spread.spread_status == 'active' or self.ce_spread.spread_status == 'active_additional':
            new_trade_book_record_df = pd.DataFrame({'entry_time': [self.ce_spread.entry, self.ce_spread.entry],
                                                  'role': ['ce_buy', 'ce_sell'],
                                                  'symbol': [self.ce_spread.buy_symbol, self.ce_spread.sell_symbol],
                                                  'entry_price': [self.ce_spread.buy_entry, self.ce_spread.sell_entry],
                                                  'status': ['active', 'active'],
                                                  'ltp': [self.ce_spread.buy_ltp, self.ce_spread.sell_ltp],
                                                  'exit_price': [0.0, 0.0],
                                                  'pl': [(self.ce_spread.buy_entry-self.ce_spread.buy_ltp), (self.ce_spread.sell_entry-self.ce_spread.sell_ltp)]})
            self.trade_book_df = pd.concat([self.trade_book_df, new_trade_book_record_df], ignore_index=True)
            if self.ce_spread.spread_status == 'active_additional':
                new_trade_book_record_df = pd.DataFrame({'entry_time': [self.ce_spread.entry, self.ce_spread.entry],
                                                         'role': ['additional_ce_buy', 'additional_ce_sell'],
                                                         'symbol': [self.ce_spread.additional_buy_symbol, self.ce_spread.sell_symbol],
                                                         'entry_price': [self.ce_spread.buy_entry, self.ce_spread.sell_entry],
                                                         'status': ['active', 'active'],
                                                         'ltp': [self.ce_spread.additional_buy_ltp, self.ce_spread.sell_ltp],
                                                         'exit_price': [0.0, 0.0],
                                                         'pl': [(self.ce_spread.additional_buy_entry-self.ce_spread.additional_buy_ltp), (self.ce_spread.sell_entry-self.ce_spread.sell_ltp)]})
                self.trade_book_df = pd.concat([self.trade_book_df, new_trade_book_record_df], ignore_index=True)
        self.trade_book_df.to_csv('data/trade_book.csv', index=False)

    # Private method to update trade book if adjustment is made
    def _update_trade_book_adjustment(self):
        self._set_current_datetime()
        if self.pe_spread.spread_status == 'adjusted' or self.pe_spread.spread_status == 'adjusted_additional':
            for i in range(len(self.trade_book_df)):
                if self.trade_book_df.iloc[i].iloc[1] == 'pe_sell' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                    self.trade_book_df.iloc[i, 4] = 'closed'
                    self.trade_book_df.iloc[i, 5] = self.pe_spread.sell_exit
                    self.trade_book_df.iloc[i, 6] = self.pe_spread.sell_exit
                    self.trade_book_df.iloc[i, 7] = self.trade_book_df.iloc[i, 3] - self.pe_spread.sell_exit
            new_trade_book_record_df = pd.DataFrame({'entry_time': [self.pe_spread.entry],
                                                     'role': ['pe_sell'],
                                                     'symbol': [self.pe_spread.sell_symbol],
                                                     'entry_price': [self.pe_spread.sell_entry],
                                                     'status': ['active'],
                                                     'ltp': [self.pe_spread.sell_ltp],
                                                     'exit_price': [0.0],
                                                     'pl': [(self.pe_spread.sell_entry-self.pe_spread.sell_ltp)]})
            self.trade_book_df = pd.concat([self.trade_book_df, new_trade_book_record_df], ignore_index=True)
            if self.pe_spread.spread_status == 'adjusted_additional':
                new_trade_book_record_df = pd.DataFrame({'entry_time': [self.pe_spread.entry, self.pe_spread.entry],
                                                         'role': ['additional_pe_buy', 'additional_pe_sell'],
                                                         'symbol': [self.pe_spread.additional_buy_symbol, self.pe_spread.sell_symbol],
                                                         'entry_price': [self.pe_spread.additional_buy_entry, self.pe_spread.sell_entry],
                                                         'status': ['active', 'active'],
                                                         'ltp': [self.pe_spread.additional_buy_ltp, self.pe_spread.sell_ltp],
                                                         'exit_price': [0.0, 0.0],
                                                         'pl': [(self.pe_spread.additional_buy_entry-self.pe_spread.additional_buy_ltp),(self.pe_spread.sell_entry-self.pe_spread.sell_ltp)]})
                self.trade_book_df = pd.concat([self.trade_book_df, new_trade_book_record_df], ignore_index=True)
        if self.ce_spread.spread_status == 'adjusted' or self.ce_spread.spread_status == 'adjusted_additional':
            for i in range(len(self.trade_book_df)):
                if self.trade_book_df.iloc[i].iloc[1] == 'ce_sell' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                    self.trade_book_df.iloc[i, 4] = 'closed'
                    self.trade_book_df.iloc[i, 5] = self.ce_spread.sell_exit
                    self.trade_book_df.iloc[i, 6] = self.ce_spread.sell_exit
                    self.trade_book_df.iloc[i, 7] = self.trade_book_df.iloc[i, 3] - self.ce_spread.sell_exit
            new_trade_book_record_df = pd.DataFrame({'entry_time': [self.ce_spread.entry],
                                                     'role': ['ce_sell'],
                                                     'symbol': [self.ce_spread.sell_symbol],
                                                     'entry_price': [self.ce_spread.sell_entry],
                                                     'status': ['active'],
                                                     'ltp': [self.ce_spread.sell_ltp],
                                                     'exit_price': [0.0],
                                                     'pl': [(self.ce_spread.sell_entry-self.ce_spread.sell_ltp)]})
            self.trade_book_df = pd.concat([self.trade_book_df, new_trade_book_record_df], ignore_index=True)
            if self.ce_spread.spread_status == 'adjusted_additional':
                new_trade_book_record_df = pd.DataFrame({'entry_time': [self.ce_spread.entry, self.ce_spread.entry],
                                                         'role': ['additional_ce_buy', 'additional_ce_sell'],
                                                         'symbol': [self.ce_spread.additional_buy_symbol, self.ce_spread.sell_symbol],
                                                         'entry_price': [self.ce_spread.additional_buy_entry, self.ce_spread.sell_entry],
                                                         'status': ['active', 'active'],
                                                         'ltp': [self.ce_spread.additional_buy_ltp, self.ce_spread.sell_ltp],
                                                         'exit_price': [0.0, 0.0],
                                                         'pl': [(self.ce_spread.additional_buy_entry-self.ce_spread.additional_buy_ltp),(self.ce_spread.sell_entry-self.ce_spread.sell_ltp)]})
                self.trade_book_df = pd.concat([self.trade_book_df, new_trade_book_record_df], ignore_index=True)
        self.trade_book_df.to_csv('data/trade_book.csv', index=False)

    # Private method to update trade book if hedge is brought in
    def _update_trade_book_elm_adjustment(self, spread_type):
        self._set_current_datetime()
        if spread_type == 'ce':
            for i in range(len(self.trade_book_df)):
                if self.trade_book_df.iloc[i].iloc[1] == 'ce_buy' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                    self.trade_book_df.iloc[i, 4] = 'closed'
                    self.trade_book_df.iloc[i, 5] = self.ce_spread.buy_exit
                    self.trade_book_df.iloc[i, 6] = self.ce_spread.buy_exit
                    self.trade_book_df.iloc[i, 7] = self.ce_spread.buy_exit - self.trade_book_df.iloc[i, 3]
                if self.trade_book_df.iloc[i].iloc[1] == 'additional_ce_buy' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                    self.trade_book_df.iloc[i, 4] = 'closed'
                    self.trade_book_df.iloc[i, 5] = self.ce_spread.additional_buy_exit
                    self.trade_book_df.iloc[i, 6] = self.ce_spread.additional_buy_exit
                    self.trade_book_df.iloc[i, 7] = self.ce_spread.additional_buy_exit - self.trade_book_df.iloc[i, 3]
                if self.trade_book_df.iloc[i].iloc[1] == 'additional_ce_sell' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                    self.trade_book_df.iloc[i, 4] = 'closed'
                    self.trade_book_df.iloc[i, 5] = self.ce_spread.additional_sell_exit
                    self.trade_book_df.iloc[i, 6] = self.ce_spread.additional_sell_exit
                    self.trade_book_df.iloc[i, 7] = self.trade_book_df.iloc[i, 3] - self.ce_spread.additional_sell_exit
            new_trade_book_record_df = pd.DataFrame({'entry_time': [self.ce_spread.new_buy_entry_time],
                                                  'role': ['ce_buy'],
                                                  'symbol': [self.ce_spread.buy_symbol],
                                                  'entry_price': [self.ce_spread.buy_entry],
                                                  'status': ['active'],
                                                  'ltp': [self.ce_spread.buy_ltp],
                                                  'exit_price': [0.0],
                                                  'pl': [(self.ce_spread.buy_ltp-self.ce_spread.buy_entry)]})
            self.trade_book_df = pd.concat([self.trade_book_df, new_trade_book_record_df], ignore_index=True)
        if spread_type == 'pe':
            for i in range(len(self.trade_book_df)):
                if self.trade_book_df.iloc[i].iloc[1] == 'pe_buy' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                    self.trade_book_df.iloc[i, 4] = 'closed'
                    self.trade_book_df.iloc[i, 5] = self.pe_spread.buy_exit
                    self.trade_book_df.iloc[i, 6] = self.pe_spread.buy_exit
                    self.trade_book_df.iloc[i, 7] = self.pe_spread.buy_exit - self.trade_book_df.iloc[i, 3]
                if self.trade_book_df.iloc[i].iloc[1] == 'additional_pe_buy' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                    self.trade_book_df.iloc[i, 4] = 'closed'
                    self.trade_book_df.iloc[i, 5] = self.pe_spread.additional_buy_exit
                    self.trade_book_df.iloc[i, 6] = self.pe_spread.additional_buy_exit
                    self.trade_book_df.iloc[i, 7] = self.pe_spread.additional_buy_exit - self.trade_book_df.iloc[i, 3]
                if self.trade_book_df.iloc[i].iloc[1] == 'additional_pe_sell' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                    self.trade_book_df.iloc[i, 4] = 'closed'
                    self.trade_book_df.iloc[i, 5] = self.pe_spread.additional_sell_exit
                    self.trade_book_df.iloc[i, 6] = self.pe_spread.additional_sell_exit
                    self.trade_book_df.iloc[i, 7] = self.trade_book_df.iloc[i, 3] - self.pe_spread.additional_sell_exit
            new_trade_book_record_df = pd.DataFrame({'entry_time': [self.pe_spread.new_buy_entry_time],
                                                  'role': ['pe_buy'],
                                                  'symbol': [self.pe_spread.buy_symbol],
                                                  'entry_price': [self.pe_spread.buy_entry],
                                                  'status': ['active'],
                                                  'ltp': [self.pe_spread.buy_ltp],
                                                  'exit_price': [0.0],
                                                  'pl': [(self.pe_spread.buy_ltp-self.pe_spread.buy_entry)]})
            self.trade_book_df = pd.concat([self.trade_book_df, new_trade_book_record_df], ignore_index=True)
        self.trade_book_df.to_csv('data/trade_book.csv', index=False)

    # Private method to update trade book if SL is hit for either spread
    def _update_trade_book_exit(self):
        self._set_current_datetime()
        if self.pe_spread.spread_status == 'closed':
            self.previous_pe_strike = self.pe_spread.sell_strike
            self.previous_pe_pl = self.pe_spread.pl
            self.previous_pe_additional_lots = self.pe_spread.additional_lots
            self.previous_pe_additional_pl = self.pe_spread.additional_pl
            for i in range(len(self.trade_book_df)):
                if self.trade_book_df.iloc[i].iloc[1] == 'pe_buy' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                    self.trade_book_df.iloc[i, 4] = 'closed'
                    self.trade_book_df.iloc[i, 5] = self.pe_spread.buy_exit
                    self.trade_book_df.iloc[i, 6] = self.pe_spread.buy_exit
                    self.trade_book_df.iloc[i, 7] = self.pe_spread.buy_exit - self.pe_spread.buy_entry
                if self.trade_book_df.iloc[i].iloc[1] == 'pe_sell' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                    self.trade_book_df.iloc[i, 4] = 'closed'
                    self.trade_book_df.iloc[i, 5] = self.pe_spread.sell_exit
                    self.trade_book_df.iloc[i, 6] = self.pe_spread.sell_exit
                    self.trade_book_df.iloc[i, 7] = self.pe_spread.sell_entry - self.pe_spread.sell_exit
                if self.trade_book_df.iloc[i].iloc[1] == 'additional_pe_buy' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                    self.trade_book_df.iloc[i, 4] = 'closed'
                    self.trade_book_df.iloc[i, 5] = self.pe_spread.additional_buy_exit
                    self.trade_book_df.iloc[i, 6] = self.pe_spread.additional_buy_exit
                    self.trade_book_df.iloc[i, 7] = self.pe_spread.additional_buy_exit - self.trade_book_df.iloc[i, 3]
                if self.trade_book_df.iloc[i].iloc[1] == 'additional_pe_sell' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                    self.trade_book_df.iloc[i, 4] = 'closed'
                    self.trade_book_df.iloc[i, 5] = self.pe_spread.sell_exit
                    self.trade_book_df.iloc[i, 6] = self.pe_spread.sell_exit
                    self.trade_book_df.iloc[i, 7] = self.trade_book_df.iloc[i, 3] - self.pe_spread.sell_exit
        if self.ce_spread.spread_status == 'closed':
            self.previous_ce_strike = self.ce_spread.sell_strike
            self.previous_ce_pl = self.ce_spread.pl
            self.previous_ce_additional_lots = self.ce_spread.additional_lots
            self.previous_ce_additional_pl = self.ce_spread.additional_pl
            for i in range(len(self.trade_book_df)):
                if self.trade_book_df.iloc[i].iloc[1] == 'ce_buy' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                    self.trade_book_df.iloc[i, 4] = 'closed'
                    self.trade_book_df.iloc[i, 5] = self.ce_spread.buy_exit
                    self.trade_book_df.iloc[i, 6] = self.ce_spread.buy_exit
                    self.trade_book_df.iloc[i, 7] = self.ce_spread.buy_exit - self.ce_spread.buy_entry
                if self.trade_book_df.iloc[i].iloc[1] == 'ce_sell' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                    self.trade_book_df.iloc[i, 4] = 'closed'
                    self.trade_book_df.iloc[i, 5] = self.ce_spread.sell_exit
                    self.trade_book_df.iloc[i, 6] = self.ce_spread.sell_exit
                    self.trade_book_df.iloc[i, 7] = self.ce_spread.sell_entry - self.ce_spread.sell_exit
                if self.trade_book_df.iloc[i].iloc[1] == 'additional_ce_buy' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                    self.trade_book_df.iloc[i, 4] = 'closed'
                    self.trade_book_df.iloc[i, 5] = self.ce_spread.additional_buy_exit
                    self.trade_book_df.iloc[i, 6] = self.ce_spread.additional_buy_exit
                    self.trade_book_df.iloc[i, 7] = self.ce_spread.additional_buy_exit - self.trade_book_df.iloc[i, 3]
                if self.trade_book_df.iloc[i].iloc[1] == 'additional_ce_sell' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                    self.trade_book_df.iloc[i, 4] = 'closed'
                    self.trade_book_df.iloc[i, 5] = self.ce_spread.sell_exit
                    self.trade_book_df.iloc[i, 6] = self.ce_spread.sell_exit
                    self.trade_book_df.iloc[i, 7] = self.trade_book_df.iloc[i, 3] - self.ce_spread.sell_exit
        self.trade_book_df.to_csv('data/trade_book.csv', index=False)

    # Private method to update trade book at chosen interval
    def _update_trade_book(self):
        self._set_current_datetime()
        for i in range(len(self.trade_book_df)):
            if self.trade_book_df.iloc[i].iloc[1] == 'pe_buy' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                self.trade_book_df.iloc[i, 5] = self.pe_spread.buy_ltp
                self.trade_book_df.iloc[i, 7] = self.pe_spread.buy_ltp - self.pe_spread.buy_entry
            if self.trade_book_df.iloc[i].iloc[1] == 'pe_sell' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                self.trade_book_df.iloc[i, 5] = self.pe_spread.sell_ltp
                self.trade_book_df.iloc[i, 7] = self.pe_spread.sell_entry - self.pe_spread.sell_ltp
            if self.trade_book_df.iloc[i].iloc[1] == 'additional_pe_buy' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                self.trade_book_df.iloc[i, 5] = self.pe_spread.additional_buy_ltp
                self.trade_book_df.iloc[i, 7] = self.pe_spread.additional_buy_ltp - self.pe_spread.additional_buy_entry
            if self.trade_book_df.iloc[i].iloc[1] == 'additional_pe_sell' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                self.trade_book_df.iloc[i, 5] = self.pe_spread.sell_ltp
                self.trade_book_df.iloc[i, 7] = self.pe_spread.sell_entry - self.pe_spread.sell_ltp
            if self.trade_book_df.iloc[i].iloc[1] == 'ce_buy' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                self.trade_book_df.iloc[i, 5] = self.ce_spread.buy_ltp
                self.trade_book_df.iloc[i, 7] = self.ce_spread.buy_ltp - self.ce_spread.buy_entry
            if self.trade_book_df.iloc[i].iloc[1] == 'ce_sell' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                self.trade_book_df.iloc[i, 5] = self.ce_spread.sell_ltp
                self.trade_book_df.iloc[i, 7] = self.ce_spread.sell_entry - self.ce_spread.sell_ltp
            if self.trade_book_df.iloc[i].iloc[1] == 'additional_ce_buy' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                self.trade_book_df.iloc[i, 5] = self.ce_spread.additional_buy_ltp
                self.trade_book_df.iloc[i, 7] = self.ce_spread.additional_buy_ltp - self.ce_spread.additional_buy_entry
            if self.trade_book_df.iloc[i].iloc[1] == 'additional_ce_sell' and self.trade_book_df.iloc[i].iloc[4] == 'active':
                self.trade_book_df.iloc[i, 5] = self.ce_spread.sell_ltp
                self.trade_book_df.iloc[i, 7] = self.ce_spread.sell_entry - self.ce_spread.sell_ltp
            self.trade_book_df.to_csv('data/trade_book.csv', index=False)

    def _check_slack_commands(self):
        """
        Check for persistent Slack command flags during the live trade.
        Handles EXIT (liquidate), KILL (halt immediately), and ADJUST:pe/ce.
        """
        flag_path = os.path.join(REPO_ROOT, 'data', 'SLACK_COMMAND.flag')
        if exists(flag_path):
            try:
                with open(flag_path, 'r') as f:
                    command = f.read().strip()

                if command == "EXIT":
                    os.remove(flag_path)
                    msg = "⚠️ *Artemis*: Slack `Exit Trade` detected. Liquidating..."
                    logger.warning(msg.replace('*', ''))
                    slack_bot_sendtext(msg, SLACK_TRADE_ALERTS)
                    if self.trade_status:
                        if self.pe_spread.spread_status not in ('closed', 'open'):
                            self.pe_spread.exit_spread()
                        if self.ce_spread.spread_status not in ('closed', 'open'):
                            self.ce_spread.exit_spread()
                        self._update_trade_book_exit()
                        self._archive_trade()
                    raise Exception("Session terminated by Slack !exit command.")

                elif command == "KILL":
                    msg = "🚨 *Artemis*: Slack `Kill Switch` detected. Dropping control immediately."
                    logger.warning(msg.replace('*', ''))
                    slack_bot_sendtext(msg, SLACK_TRADE_ALERTS)
                    raise Exception("Session terminated by Slack !kill command.")

                elif command.startswith("ADJUST:"):
                    side = command.split(":")[1].lower()
                    if side in ('pe', 'ce'):
                        self._pending_adjustment = side
                        os.remove(flag_path)
                        logger.info(f"Manual adjustment queued for {side.upper()} side.")

                # If command == "DISABLE", we do nothing inside the loop.
                # Leto will catch it on the next startup.
            except Exception as e:
                if "Session terminated" in str(e): raise
                handle_exception(e)

    # Method to monitor spread
    def monitor_trade(self):
        if not self.trade_status:
            self._communicate_closed_status()
            self._archive_trade()
            return False
        self._set_current_datetime()
        if self.pe_spread.spread_status == 'closed' and self.ce_spread.spread_status == 'closed':
            self.trade_status = False
        self.pe_spread_result = self.pe_spread.monitor_spread()
        self.ce_spread_result = self.ce_spread.monitor_spread()
        side = getattr(self, '_pending_adjustment', None)
        if side is not None:
            self._pending_adjustment = None
            spread = self.pe_spread if side == 'pe' else self.ce_spread
            if spread.spread_status not in ('closed', 'open'):
                if side == 'pe':
                    self.pe_spread_result = 'manual_sl'
                else:
                    self.ce_spread_result = 'manual_sl'
                logger.info(f"Manual adjustment: overriding {side.upper()} result to manual_sl.")
            else:
                slack_bot_sendtext(
                    f"⚠️ *Artemis*: Manual adjustment ignored — {side.upper()} spread is "
                    f"already {spread.spread_status}.",
                    SLACK_ERRORS_CHANNEL)
        return True

    # Method to update everything and send update before sleeping for specified time
    def continue_monitoring(self):
        if (self.ce_spread_result in ('continue', 'closed')) and (self.pe_spread_result in ('continue', 'closed')):
            if self._update_elapsed >= monitor_frequency or not self.feed.is_connected():
                self._update_trade_book()
                self._update_trade_log()
                self._send_status_update()
                self._update_elapsed = 0.0
            self._sleep_for_set_time()
            self._set_current_datetime()

    # Method to handle trade if SL hits for either spread
    def evaluate_handle_sl(self):
        if self.pe_spread_result in ('index_sl', 'option_sl', 'manual_sl'):
            self.pe_spread.exit_spread()
            self._update_trade_book_exit()
            if self.ce_spread.spread_status == 'closed':
                self.ce_spread.initialize_spread()
                while self.ce_spread.spread_status == 'open':
                    self.ce_spread.execute_spread()
                if self.ce_spread.spread_status in ('active', 'active_additional'):
                    self._update_trade_book_re_entry()
            else:
                self.ce_spread.adjust_spread()
                self._update_trade_book_adjustment()
        if self.ce_spread_result in ('index_sl', 'option_sl', 'manual_sl'):
            self.ce_spread.exit_spread()
            self._update_trade_book_exit()
            if self.pe_spread.spread_status == 'closed':
                self.pe_spread.initialize_spread()
                while self.pe_spread.spread_status == 'open':
                    self.pe_spread.execute_spread()
                if self.pe_spread.spread_status in ('active', 'active_additional'):
                    self._update_trade_book_re_entry()
            else:
                self.pe_spread.adjust_spread()
                self._update_trade_book_adjustment()
        self._subscribe_active_tokens()

    # Method to handle the extra 2% Extra Loss Margin for expiry day
    def evaluate_adjust_for_elm(self):
        self._set_current_datetime()
        if self.current_datetime > self.elm_time and not self.adjusted_for_elm:
            if self.ce_spread.spread_status == 'active' and self.pe_spread.spread_status == 'active':
                if (self.ce_spread.sell_ltp - self.ce_spread.buy_ltp) > (self.pe_spread.sell_ltp - self.pe_spread.buy_ltp):
                    self.pe_spread.exit_spread()
                    self._update_trade_book_exit()
                else:
                    self.ce_spread.exit_spread()
                    self._update_trade_book_exit()
            elif self.pe_spread.spread_status == 'closed' and (
                self.ce_spread.spread_status in ['adjusted', 'adjusted_additional', 'active_additional']):
                self.ce_spread.adjust_for_elm()
                self._update_trade_book_elm_adjustment('ce')
            elif self.ce_spread.spread_status == 'closed' and (
                self.pe_spread.spread_status in ['adjusted', 'adjusted_additional', 'active_additional']):
                self.pe_spread.adjust_for_elm()
                self._update_trade_book_elm_adjustment('pe')
            self.adjusted_for_elm = True

    # Private method to archive all files after trade is closed
    def _archive_trade(self):
        os.makedirs('data/archived', exist_ok=True)
        if exists('data/pe_trade_params.csv') and exists('data/ce_trade_params.csv') and exists('data/trade_book.csv') and exists('data/trade_log.csv'):
            self.trade_status = False
            self.pe_spread.spread_status = self.ce_spread.spread_status = 'closed'
            self.pe_spread.trade_params_df.iloc[0, 16] = self.pe_spread.spread_status
            self.ce_spread.trade_params_df.iloc[0, 16] = self.ce_spread.spread_status
            self.pe_spread.trade_params_df.to_csv('data/pe_trade_params.csv', index=False)
            self.ce_spread.trade_params_df.to_csv('data/ce_trade_params.csv', index=False)
            for status in range(len(self.trade_book_df)):
                if self.trade_book_df.iloc[status, 4] == 'active':
                    self.trade_book_df.iloc[status, 4] = 'expired'
            self.trade_book_df.to_csv('data/trade_book.csv', index=False)
            os.rename("data/pe_trade_params.csv", f"data/archived/{self.pe_spread.trade_params_df.iloc[0].iloc[3]:%Y-%m-%d} pe_trade_params.csv")
            os.rename("data/ce_trade_params.csv", f"data/archived/{self.ce_spread.trade_params_df.iloc[0].iloc[3]:%Y-%m-%d} ce_trade_params.csv")
            os.rename("data/trade_book.csv", f"data/archived/{self.pe_spread.trade_params_df.iloc[0].iloc[3]:%Y-%m-%d} trade_book.csv")
            os.rename("data/trade_log.csv", f"data/archived/{self.pe_spread.trade_params_df.iloc[0].iloc[3]:%Y-%m-%d} trade_log.csv")
            # instrument_master.csv and scrip_master.csv are no longer written
            # to artemis_production/data/ — Leto owns the scrip master.
            # Guards prevent FileNotFoundError on weeks where these files exist
            # from a manual run of the old codebase.
            if exists('data/instrument_master.csv'):
                os.remove('data/instrument_master.csv')
            if exists('data/scrip_master.csv'):
                os.remove('data/scrip_master.csv')
            msg_txt = f"*Artemis:*\nTrade has been archived at {self.current_datetime:%Y-%m-%d %H:%M:%S}."
            logger.info(msg_txt)
            slack_bot_sendtext(msg_txt, SLACK_TRADE_UPDATES)
        else:
            for file in ['data/trade_book.csv',
                         'data/instrument_master.csv', 'data/scrip_master.csv']:
                if exists(file):
                    os.remove(file)
            msg_txt = "Trade has already been archived."
            logger.info(msg_txt)
            slack_bot_sendtext(msg_txt, SLACK_TRADE_UPDATES)

    # Private method to send status update
    def _send_status_update(self):
        if self.ce_spread.spread_status == 'closed':
            index_ltp = self.pe_spread.index_ltp
            current_datetime = self.pe_spread.current_datetime
        else:
            index_ltp = self.ce_spread.index_ltp
            current_datetime = self.ce_spread.current_datetime
        msg_txt = f"*Artemis:*\n*Time:* _{current_datetime:%Y-%m-%d %H:%M:%S}_\n*Index Value:* _{index_ltp}_\n*PE Spread PL:* _{self.pe_spread.pl*lot_size:.2f}_\n*PE Spread Status:* _{self._format_status(self.pe_spread.spread_status)}_\n*Additional PE Spread PL:* _{self.pe_spread.additional_pl*lot_size*self.pe_spread.additional_lots/self.pe_spread.lots:.2f}_\n*CE Spread PL:* _{self.ce_spread.pl*lot_size:.2f}_\n*CE Spread Status:* _{self._format_status(self.ce_spread.spread_status)}_\n*Additional CE Spread PL:* _{self.ce_spread.additional_pl*lot_size*self.ce_spread.additional_lots/self.ce_spread.lots:.2f}_\n*Lots:* _{self.pe_spread.lots}_\n*Overall PL:* _{(self.pe_spread.pl+self.ce_spread.pl+(self.pe_spread.additional_pl*self.pe_spread.additional_lots/self.pe_spread.lots)+(self.ce_spread.additional_pl*self.ce_spread.additional_lots/self.ce_spread.lots))*lot_size:.2f}_"
        slack_bot_sendtext(msg_txt, SLACK_TRADE_UPDATES)

    # Private method to send message if trade is closed
    def _communicate_closed_status(self):
        msg_txt = f"*Artemis:*\nThis trade is closed.\n*Overall PL:* _{(self.pe_spread.pl+self.ce_spread.pl+(self.pe_spread.additional_pl*self.pe_spread.additional_lots/self.pe_spread.lots)+(self.ce_spread.additional_pl*self.ce_spread.additional_lots/self.ce_spread.lots))*lot_size:.2f}_"
        logger.info(msg_txt)
        slack_bot_sendtext(msg_txt, SLACK_TRADE_UPDATES)

    def _sleep_for_set_time(self):
        if not self.feed.is_connected():
            sleep(monitor_frequency)
            reset_counters()
            return
        sleep(0.5)
        self._update_elapsed += 0.5

    def logout(self):
        """
        Final update, trade log, status message and archive if expired.
        Does NOT call terminateSession — Leto handles session teardown.
        """
        self._set_current_datetime()
        if self.trade_status:
            self.monitor_trade()
            self._update_trade_book()
            self._update_trade_log()
            self._send_status_update()
            if self.current_datetime > self.expiry:
                self._communicate_closed_status()
                self._archive_trade()
        msg_txt = f"Artemis session complete at {self.current_datetime:%Y-%m-%d %H:%M:%S}."
        logger.info(msg_txt)
        slack_bot_sendtext(msg_txt, SLACK_TRADE_UPDATES)

    def _reconcile_positions(self):
        """
        Compare broker position data against local state on restart.
        Checks sign only (long/short) for each active spread leg — exact qty
        is not verified because additional lots complicate the expected count.
        Alerts on mismatch — does not auto-correct.
        """
        try:
            resp = self.obj.position()
            data = (resp or {}).get('data') or []
        except Exception as e:
            logger.warning(f"Position reconciliation: broker call failed ({e}). Proceeding without check.")
            return

        pos = {str(p['symboltoken']): int(p.get('netqty', 0))
               for p in data if p.get('symboltoken')}
        mismatches = []

        for side, spread in [('PE', self.pe_spread), ('CE', self.ce_spread)]:
            if spread.spread_status in ('closed', 'open'):
                continue
            for attr, expected_sign, label in [
                ('sell_token', -1, 'sell'),
                ('buy_token',  +1, 'buy'),
            ]:
                tok = str(getattr(spread, attr, '') or '')
                if not tok or tok == 'nan':
                    continue
                actual = pos.get(tok, 0)
                if expected_sign == -1 and actual >= 0:
                    mismatches.append(f"{side} {label} token={tok}: expected short, broker={actual:+d}")
                elif expected_sign == +1 and actual <= 0:
                    mismatches.append(f"{side} {label} token={tok}: expected long, broker={actual:+d}")

        if mismatches:
            msg = ("*Artemis* ALERT: Position mismatch on restart — "
                   + " | ".join(mismatches)
                   + " — verify manually before trading continues.")
            logger.error(f"Position reconciliation FAILED: {mismatches}")
            slack_bot_sendtext(msg, SLACK_ERRORS_CHANNEL)
        else:
            logger.info("Position reconciliation OK — broker positions match state.")

    def get_session_summary(self):
        """
        Build a session summary dict for the end-of-day report.
        Must be called BEFORE logout() so spread statuses reflect the live session
        (logout/archive sets both to 'closed', erasing the outcome signal).
        """
        pe_st = self.pe_spread.spread_status
        ce_st = self.ce_spread.spread_status

        traded = not (pe_st == 'open' and ce_st == 'open')
        if not traded:
            return {'strategy': 'Artemis', 'traded': False, 'no_trade_reason': 'Stand-down'}

        # Outcome: if one side was SL-hit and closed, the surviving side is
        # 'reinforced' only if it actually has additional lots (status contains
        # 'additional'); otherwise it's just 'active'.
        if pe_st == 'closed' and ce_st not in ('closed', 'open'):
            ce_label = 'CE reinforced' if 'additional' in ce_st else 'CE active'
            outcome = f'PE side closed — {ce_label}'
        elif ce_st == 'closed' and pe_st not in ('closed', 'open'):
            pe_label = 'PE reinforced' if 'additional' in pe_st else 'PE active'
            outcome = f'CE side closed — {pe_label}'
        else:
            outcome = 'Neutral'

        pl_rs = round(
            (self.pe_spread.pl + self.ce_spread.pl +
             (self.pe_spread.additional_pl * self.pe_spread.additional_lots / max(self.pe_spread.lots, 1)) +
             (self.ce_spread.additional_pl * self.ce_spread.additional_lots / max(self.ce_spread.lots, 1))
            ) * lot_size, 2)
        realised_rs = round(
            (self.pe_spread.booked_pl + self.ce_spread.booked_pl +
             (self.pe_spread.additional_booked_pl * self.pe_spread.additional_lots / max(self.pe_spread.lots, 1)) +
             (self.ce_spread.additional_booked_pl * self.ce_spread.additional_lots / max(self.ce_spread.lots, 1))
            ) * lot_size, 2)
        unrealised_rs = round(pl_rs - realised_rs, 2)

        if self.current_datetime > self.expiry:
            exit_reason = 'Expiry'
        elif pe_st == 'closed' and ce_st == 'closed':
            exit_reason = 'Market close'
        else:
            exit_reason = 'overnight_hold'

        try:
            entry_str = self.trade_book_df.iloc[0]['entry_time'].strftime('%d %b %H:%M')
        except Exception:
            entry_str = '?'

        return {
            'strategy':       'Artemis',
            'traded':         True,
            'lots':           self.pe_spread.lots,
            'outcome':        outcome,
            'pnl_rs':         pl_rs,
            'realised_pnl_rs':   realised_rs,
            'unrealised_pnl_rs': unrealised_rs,
            'exit_reason':    exit_reason,
            'entry_time':     entry_str,
        }