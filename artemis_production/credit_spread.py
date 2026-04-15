"""
credit_spread.py — Artemis Production Credit Spread
No changes to trading logic. chdir removed — wrapper sets cwd.
"""

from datetime import datetime, time
from numpy import busday_count
from math import floor, ceil
from functions import slack_bot_sendtext, sleep, exists, handle_exception, increment_poll_counter, increment_order_counter, reset_counters#, telegram_bot_sendtext
from configs import pd, contracts_df, strike_iteration_interval, hedge_points, expected_option_premium, strike_values_iterator, qty_freeze, lot_size, lot_count, sl_4_dte, sl_3_dte, sl_2_dte, sl_1_dte, sl_0_dte, adjustment_distance, instrument, underlying_token, exchange_segment, fo_exchange_segment, minimum_gap, minimum_gap_iterator, index_sl_offset

# Main class for option spread
class CreditSpread:
    # Private method to fetch symbol and token
    def _fetch_symbol_and_token(self, strike):
        # Filter based on expiry, strike, and option type
        row = self.instrument_df.loc[
            (self.instrument_df['expiry'] == self.contract) &
            (self.instrument_df['strike'] == (strike * 100)) &
            (self.instrument_df['symbol'].str[-2:] == self.spread_type.upper())
            ]
        # Extract symbol and token
        symbol = row['symbol'].iloc[0]
        token = str(row['token'].iloc[0])
        return symbol, token
    
    # Private method to set index and option SL. Will be called after spread execution or adjustment
    def _set_sl(self):
        # Count only weekdays between current date and expiry
        self.days_to_expiry = busday_count(
            self.current_datetime.date(),
            self.expiry.date()
        )
        if self.days_to_expiry > 3:
            self.option_sl = self.sell_entry * sl_4_dte
        elif self.days_to_expiry == 3:
            self.option_sl = self.sell_entry * sl_3_dte
        elif self.days_to_expiry == 2:
            self.option_sl = self.sell_entry * sl_2_dte
        elif self.days_to_expiry == 1:
            self.option_sl = self.sell_entry * sl_1_dte
        elif self.days_to_expiry == 0:
            self.option_sl = self.sell_entry * sl_0_dte
        else:
            self.option_sl = self.sell_entry
        if self.spread_type == 'pe':
            self.index_sl = self.sell_strike + index_sl_offset
        if self.spread_type == 'ce':
            self.index_sl = self.sell_strike - index_sl_offset

    # Private method to fetch ltp of an instrument or index
    def _fetch_ltp(self, exchange, symbol, token):
        while True:
            try:
                instrument_ltp = self.obj.ltpData(exchange, symbol, token)['data']['ltp']
                increment_poll_counter()
                if instrument_ltp is not None:
                    return instrument_ltp
            except Exception as e:
                handle_exception(e)
            sleep(1)
            reset_counters()
        
    # Private method to place order, and return total orders and orderid list
    def _place_order(self, transaction_type, symbol, token, quantity):
        l_limit = qty_freeze / lot_size
        order_quantities = []

        # If quantity is less than or equal to l_limit, just place one order for that quantity
        if quantity <= l_limit:
            order_quantities.append(quantity)
        else:
            full_lots = int(quantity // l_limit)
            remainder = quantity % l_limit
            for _ in range(full_lots):
                order_quantities.append(l_limit)
            if remainder > 0:
                order_quantities.append(remainder)

        orderID_list = []
        for lots in order_quantities:
            orderparams = {
                "variety": "NORMAL",
                "tradingsymbol": symbol,
                "symboltoken": token,
                "transactiontype": transaction_type,
                "exchange": fo_exchange_segment,
                "ordertype": "MARKET",
                "producttype": "CARRYFORWARD",
                "duration": "DAY",
                "quantity": str(lots * lot_size)
            }
            while True:
                try:
                    order_response = self.obj.placeOrderFullResponse(orderparams)
                    increment_order_counter()
                    if order_response['message'] == 'SUCCESS':
                        orderID_list.append(order_response['data']['orderid'])
                        break
                except Exception as e:
                    handle_exception(e)
                sleep(1)
                reset_counters()
        return orderID_list
      
    # Private method to fetch order book
    def _fetch_order_book(self):
        while True:
            try:
                self.order_book = self.obj.orderBook()
                break
            except Exception as e:
                handle_exception(e)
            sleep(1)
            reset_counters()
        
    # Private method to fetch average fill price and average fill time
    def _fetch_order_details(self, orderID_list):
        # Function to fetch and populate the price, quantity and orderID lists
        def get_details(order_book, orderID_list):
            executed_price_list = []
            quantity_list = []
            executed_time_list = []
            # Run a loop to run through the orderID_list (to get the order right) & order book and fetch the entry price and time when the orderId is matched.
            for order_id in orderID_list:
                for order in order_book['data']:
                    if order['orderid'] == order_id:
                        price = order['averageprice']
                        qty = int(order['quantity'])
                        executed_price_list.append(price * qty)
                        quantity_list.append(qty)
                        executed_time_list.append(datetime.strptime(order['updatetime'], '%d-%b-%Y %H:%M:%S'))
            return executed_price_list, quantity_list, executed_time_list
        executed_price = None
        entry_time = None
        # Loop till the executed prices or quantities are not zero or None
        while executed_price is None or entry_time is None or any(p == 0 for p in executed_price_list) or any(q == 0 for q in quantity_list):
            executed_price_list, quantity_list, executed_time_list = get_details(self.order_book, orderID_list)
            if executed_price_list and quantity_list and sum(quantity_list) > 0:
                executed_price = sum(executed_price_list) / sum(quantity_list)
                entry_time = max(executed_time_list) if executed_time_list else None
            if executed_price is None or entry_time is None or any(p == 0 for p in executed_price_list) or any(q == 0 for q in quantity_list):
                sleep(1)
                reset_counters()
                self._fetch_order_book()
        return executed_price, entry_time
        
    # Method to intialize object
    def __init__(self, spread_type):
        self.current_datetime = datetime.now()
        # Determine the correct expiry to trade
        for i in range(len(contracts_df)):
            if self.current_datetime > contracts_df.iloc[i].loc['expiry'] and self.current_datetime < contracts_df.iloc[i+1].loc['expiry']:
                self.expiry = contracts_df.iloc[i+1].loc['expiry']
                self.contract = self.expiry.strftime("%d%b%Y").upper()
                self.entry = contracts_df.iloc[i+1].loc['entry']
                self.exit_time = self.expiry
                self.elm_time = contracts_df.iloc[i+1].loc['elm_time']
                self.cutoff_time = contracts_df.iloc[i+1].loc['cutoff_time']
                break
        if exists(f"data/{spread_type}_trade_params.csv") or exists(f"data/archived/{self.expiry:%Y-%m-%d} {spread_type}_trade_params.csv"):
            if exists(f"data/{spread_type}_trade_params.csv"):
                filepath = f"data/{spread_type}_trade_params.csv"
            else:
                filepath = f"data/archived/{self.expiry:%Y-%m-%d} {spread_type}_trade_params.csv"
            self.trade_params_df = pd.read_csv(filepath, parse_dates=['entry','expiry','elm_time','cutoff_time'])
            self.trade_params_df['sell_token'] = self.trade_params_df['sell_token'].astype(str)
            self.trade_params_df['buy_token'] = self.trade_params_df['buy_token'].astype(str)
            self.trade_params_df['additional_buy_token'] = self.trade_params_df['additional_buy_token'].astype(str)
            self.trade_params_df['sell_symbol'] = self.trade_params_df['sell_symbol'].astype(str)
            self.trade_params_df['buy_symbol'] = self.trade_params_df['buy_symbol'].astype(str)
            self.trade_params_df['additional_buy_symbol'] = self.trade_params_df['additional_buy_symbol'].astype(str)
            self.trade_params_df['index_entry'] = self.trade_params_df['index_entry'].astype('float64')
            self.trade_params_df['buy_entry'] = self.trade_params_df['buy_entry'].astype('float64')
            self.trade_params_df['sell_entry'] = self.trade_params_df['sell_entry'].astype('float64')
            self.trade_params_df['index_sl'] = self.trade_params_df['index_sl'].astype('float64')
            self.trade_params_df['option_sl'] = self.trade_params_df['option_sl'].astype('float64')
            self.trade_params_df['buy_exit'] = self.trade_params_df['buy_exit'].astype('float64')
            self.trade_params_df['sell_exit'] = self.trade_params_df['sell_exit'].astype('float64')
            self.trade_params_df['pl'] = self.trade_params_df['pl'].astype('float64')
            self.trade_params_df['booked_pl'] = self.trade_params_df['booked_pl'].astype('float64')
            self.trade_params_df['buy_ltp'] = self.trade_params_df['buy_ltp'].astype('float64')
            self.trade_params_df['sell_ltp'] = self.trade_params_df['sell_ltp'].astype('float64')
            self.trade_params_df['additional_buy_entry'] = self.trade_params_df['additional_buy_entry'].astype('float64')
            self.trade_params_df['additional_buy_ltp'] = self.trade_params_df['additional_buy_ltp'].astype('float64')
            self.trade_params_df['additional_buy_exit'] = self.trade_params_df['additional_buy_exit'].astype('float64')
            self.trade_params_df['additional_booked_pl'] = self.trade_params_df['additional_booked_pl'].astype('float64')
            self.trade_params_df['additional_pl'] = self.trade_params_df['additional_pl'].astype('float64')
            self.spread_type = self.trade_params_df.iloc[0].loc['spread_type']
            self.lots = self.trade_params_df.iloc[0].loc['lots']
            self.additional_lots = self.trade_params_df.iloc[0].loc['additional_lots']
            self.entry = self.trade_params_df.iloc[0].loc['entry']
            self.index_entry = self.trade_params_df.iloc[0].loc['index_entry']
            self.contract = self.trade_params_df.iloc[0].loc['contract']
            self.expiry = self.trade_params_df.iloc[0].loc['expiry']
            self.buy_strike = self.trade_params_df.iloc[0].loc['buy_strike']
            self.buy_symbol = self.trade_params_df.iloc[0].loc['buy_symbol']
            self.buy_token = self.trade_params_df.iloc[0].loc['buy_token']
            self.buy_entry = self.trade_params_df.iloc[0].loc['buy_entry']
            self.sell_strike = self.trade_params_df.iloc[0].loc['sell_strike']
            self.sell_symbol = self.trade_params_df.iloc[0].loc['sell_symbol']
            self.sell_token = self.trade_params_df.iloc[0].loc['sell_token']
            self.sell_entry = self.trade_params_df.iloc[0].loc['sell_entry']
            self.additional_buy_strike = self.trade_params_df.iloc[0].loc['additional_buy_strike']
            self.additional_buy_symbol = self.trade_params_df.iloc[0].loc['additional_buy_symbol']
            self.additional_buy_token = self.trade_params_df.iloc[0].loc['additional_buy_token']
            self.additional_buy_entry = self.trade_params_df.iloc[0].loc['additional_buy_entry']
            self.index_sl = self.trade_params_df.iloc[0].loc['index_sl']
            self.spread_status = self.trade_params_df.iloc[0].loc['spread_status']
            self.buy_exit = self.trade_params_df.iloc[0].loc['buy_exit']
            self.sell_exit = self.trade_params_df.iloc[0].loc['sell_exit']
            self.additional_buy_exit = self.trade_params_df.iloc[0].loc['additional_buy_exit']
            self.additional_booked_pl = self.trade_params_df.iloc[0].loc['additional_booked_pl']
            self.additional_pl = self.trade_params_df.iloc[0].loc['additional_pl']
            self.time_to_expiry = (self.expiry - self.current_datetime)
            self.days_to_expiry = self.time_to_expiry.days
            if self.spread_status != 'closed' or self.spread_status != 'open':
                self._set_sl()
            else:
                self.option_sl = self.trade_params_df.iloc[0].loc['option_sl']
            self.trade_params_df.iloc[0, 14] = self.index_sl
            self.trade_params_df.iloc[0, 15] = self.option_sl
            self.exit_time = self.trade_params_df.iloc[0].loc['exit_time']
            self.pl = self.trade_params_df.iloc[0].loc['pl']
            self.booked_pl = self.trade_params_df.iloc[0].loc['booked_pl']
            self.buy_ltp = self.trade_params_df.iloc[0].loc['buy_ltp']
            self.sell_ltp = self.trade_params_df.iloc[0].loc['sell_ltp']
            self.additional_buy_ltp = self.trade_params_df.iloc[0].loc['additional_buy_ltp']
            self.elm_time = self.trade_params_df.iloc[0].loc['elm_time']
            self.cutoff_time = self.trade_params_df.iloc[0].loc['cutoff_time']
            self.trade_params_df.to_csv(filepath, index=False)
        else:
            self.spread_type = spread_type
            self.lots = lot_count
            self.index_entry = 0.0
            self.buy_strike = 0
            self.buy_symbol = instrument
            self.buy_token = ''
            self.buy_entry = 0.0
            self.sell_strike = 0
            self.sell_symbol = instrument
            self.sell_token = ''
            self.sell_entry = 0.0
            self.index_sl = 0.0
            self.spread_status = 'open'
            self.buy_exit = 0.0
            self.sell_exit = 0.0
            self.option_sl = 0.0
            self.pl = 0.0
            self.booked_pl = 0.0
            self.buy_ltp = 0.0
            self.sell_ltp = 0.0
            self.time_to_expiry = (self.expiry - self.current_datetime)
            self.days_to_expiry = self.time_to_expiry.days
            self.additional_lots = 0
            self.additional_buy_strike = 0
            self.additional_buy_symbol = instrument
            self.additional_buy_token = ''
            self.additional_buy_entry = 0.0
            self.additional_buy_ltp = 0.0
            self.additional_buy_exit = 0.0
            self.additional_booked_pl = 0.0
            self.additional_pl = 0.0
            self.trade_params_df = pd.DataFrame({'spread_type':[self.spread_type],
                                                'lots':[self.lots],
                                                'entry':[self.entry],
                                                'expiry':[self.expiry],
                                                'index_entry':[self.index_entry],
                                                'contract':[self.contract],
                                                'buy_strike':[self.buy_strike],
                                                'buy_symbol':[self.buy_symbol],
                                                'buy_token':[self.buy_token],
                                                'buy_entry':[self.buy_entry],
                                                'sell_strike':[self.sell_strike],
                                                'sell_symbol':[self.sell_symbol],
                                                'sell_token':[self.sell_token],
                                                'sell_entry':[self.sell_entry],
                                                'index_sl':[self.index_sl],
                                                'option_sl':[self.option_sl],
                                                'spread_status':[self.spread_status],
                                                'buy_exit':[self.buy_exit],
                                                'sell_exit':[self.sell_exit],
                                                'exit_time':[self.exit_time],
                                                'pl':[self.pl],
                                                'booked_pl':[self.booked_pl],
                                                'buy_ltp':[self.buy_ltp],
                                                'sell_ltp':[self.sell_ltp],
                                                'elm_time': [self.elm_time],
                                                'cutoff_time': [self.cutoff_time],
                                                'additional_lots': [self.additional_lots],
                                                'additional_buy_strike': [self.additional_buy_strike],
                                                'additional_buy_symbol': [self.additional_buy_symbol],
                                                'additional_buy_token': [self.additional_buy_token],
                                                'additional_buy_entry': [self.additional_buy_entry],
                                                'additional_buy_ltp': [self.additional_buy_ltp],
                                                'additional_buy_exit': [self.additional_buy_exit],
                                                'additional_booked_pl': [self.additional_booked_pl],
                                                'additional_pl': [self.additional_pl]})
            self.trade_params_df.to_csv(f"data/{spread_type}_trade_params.csv", index=False)
        
    # Method to calculate and intialize initial trade parameters
    def initialize_spread(self):   
        self.current_datetime = datetime.now()
        # Initialize trade only if the current time is before elm_time
        if self.current_datetime < self.elm_time:
            if self.current_datetime.time() < time(9, 16):
                sleep((datetime.combine(self.current_datetime.date(), time(9, 16)) - self.current_datetime).total_seconds())
                reset_counters()
            if self.spread_status != 'closed':
                self.index_entry = self._fetch_ltp(exchange_segment, instrument, underlying_token)
                if self.spread_type == 'pe':
                    index_rounded_value = floor(self.index_entry / strike_iteration_interval) * strike_iteration_interval
                    option_list_dict = {'strike':[],
                                        'symbol':[],
                                        'token':[],
                                        'ltp':[]}
                    for i in range(index_rounded_value, index_rounded_value - strike_values_iterator, -strike_iteration_interval):
                        option_list_dict['strike'].append(i)
                        temp_symbol, temp_token = self._fetch_symbol_and_token(i)
                        option_list_dict['symbol'].append(temp_symbol)
                        option_list_dict['token'].append(temp_token)
                        option_list_dict['ltp'].append(self._fetch_ltp(fo_exchange_segment, option_list_dict['symbol'][-1], option_list_dict['token'][-1]))
                    # Find the 'strike' with the 'value' closest to expected_option_premium
                    closest_value_index = min(range(len(option_list_dict['ltp'])), key=lambda i: abs(option_list_dict['ltp'][i] - expected_option_premium))
                    # Get the corresponding 'strike'
                    self.sell_strike = option_list_dict['strike'][closest_value_index]
                    self.sell_symbol = option_list_dict['symbol'][closest_value_index]
                    self.sell_token = option_list_dict['token'][closest_value_index]
                    self.buy_strike = self.sell_strike - hedge_points
                    self.buy_symbol, self.buy_token = self._fetch_symbol_and_token(self.buy_strike)
                if self.spread_type == 'ce':
                    index_rounded_value = ceil(self.index_entry / strike_iteration_interval) * strike_iteration_interval
                    option_list_dict = {'strike':[],
                                        'symbol':[],
                                        'token':[],
                                        'ltp':[]}
                    for i in range(index_rounded_value, index_rounded_value + strike_values_iterator, strike_iteration_interval):
                        option_list_dict['strike'].append(i)
                        temp_symbol, temp_token = self._fetch_symbol_and_token(i)
                        option_list_dict['symbol'].append(temp_symbol)
                        option_list_dict['token'].append(temp_token)
                        option_list_dict['ltp'].append(self._fetch_ltp(fo_exchange_segment, option_list_dict['symbol'][-1], option_list_dict['token'][-1]))
                    # Find the 'strike' with the 'value' closest to expected option premium
                    closest_value_index = min(range(len(option_list_dict['ltp'])), key=lambda i: abs(option_list_dict['ltp'][i] - expected_option_premium))
                    # Get the corresponding 'strike'
                    self.sell_strike = option_list_dict['strike'][closest_value_index]
                    self.sell_symbol = option_list_dict['symbol'][closest_value_index]
                    self.sell_token = option_list_dict['token'][closest_value_index]
                    self.buy_strike = self.sell_strike + hedge_points
                    self.buy_symbol, self.buy_token = self._fetch_symbol_and_token(self.buy_strike)
                self.additional_flag = False
            else:
                self.index_entry = self._fetch_ltp(exchange_segment, instrument, underlying_token)
                # Initialize if spread_type is 'ce'
                if self.spread_type == 'ce':
                    if self.sell_strike - self.index_entry > minimum_gap:
                        self.sell_strike = self.sell_strike - adjustment_distance
                    else:
                        self.sell_strike = (ceil(self.index_entry / strike_iteration_interval) * strike_iteration_interval) + minimum_gap_iterator
                    self.sell_symbol, self.sell_token = self._fetch_symbol_and_token(self.sell_strike)
                    self.buy_strike = self.sell_strike + hedge_points
                    self.buy_symbol, self.buy_token = self._fetch_symbol_and_token(self.buy_strike)
                # Initialize if spread_type is 'pe'
                if self.spread_type == 'pe':
                    if self.index_entry - self.sell_strike > minimum_gap:
                        self.sell_strike = self.sell_strike + adjustment_distance
                    else:
                        self.sell_strike = (floor(self.index_entry / strike_iteration_interval) * strike_iteration_interval) - minimum_gap_iterator
                    self.sell_symbol, self.sell_token = self._fetch_symbol_and_token(self.sell_strike)
                    self.buy_strike = self.sell_strike - hedge_points
                    self.buy_symbol, self.buy_token = self._fetch_symbol_and_token(self.buy_strike)
                # Initialize additional spread variables if current_datetime is lesser than cutoff_time
                if self.current_datetime < self.cutoff_time:
                    self.additional_lots = self.lots//2
                    self.additional_buy_strike = self.buy_strike
                    self.additional_buy_symbol = self.buy_symbol
                    self.additional_buy_token = self.buy_token
                    self.additional_flag = True
                else:
                    self.additional_lots = 0
                    self.additional_flag = False
                self.spread_status = 'open'
            self.booked_pl = self.pl
            self.additional_booked_pl = self.additional_pl
            self.trade_params_df.iloc[0, 1] = self.lots
            self.trade_params_df.iloc[0, 4] = self.index_entry
            self.trade_params_df.iloc[0, 6] = self.buy_strike
            self.trade_params_df.iloc[0, 7] = self.buy_symbol
            self.trade_params_df.iloc[0, 8] = self.buy_token
            self.trade_params_df.iloc[0, 10] = self.sell_strike
            self.trade_params_df.iloc[0, 11] = self.sell_symbol
            self.trade_params_df.iloc[0, 12] = self.sell_token
            self.trade_params_df.iloc[0, 16] = self.spread_status
            self.trade_params_df.iloc[0, 21] = self.booked_pl
            if self.additional_flag:
                self.trade_params_df.iloc[0, 26] = self.additional_lots
                self.trade_params_df.iloc[0, 27] = self.additional_buy_strike
                self.trade_params_df.iloc[0, 28] = self.additional_buy_symbol
                self.trade_params_df.iloc[0, 29] = self.additional_buy_token
            self.trade_params_df.to_csv(f"data/{self.spread_type}_trade_params.csv", index=False)
        else:
            # Update index ltp even if spread is not initialized to ensure trade_log is updated
            self.index_ltp = self._fetch_ltp(exchange_segment, instrument, underlying_token)
            msg_txt = f"*Artemis:*\n{self.spread_type.upper()} Spread wont be initialized after ELM cutoff time.\n*Lots:* _{self.lots}_"
            print(msg_txt)
            #telegram_bot_sendtext(msg_txt)
            #telegram_bot_sendtext(msg_txt, 'bot')
            slack_bot_sendtext(msg_txt, "#trade-alerts")

    # Method to execute the spread orders        
    def execute_spread(self):
        self.current_datetime = datetime.now()
        if self.current_datetime < self.elm_time:
            if self.additional_flag:
                total_lots = self.lots + self.additional_lots
            else:
                total_lots = self.lots
            # Execute buy order first
            buy_orderID_list = self._place_order('BUY', self.buy_symbol, self.buy_token, total_lots)
            # Then execute sell order
            sell_orderID_list = self._place_order('SELL', self.sell_symbol, self.sell_token, total_lots)
            # Get order book
            sleep(1)
            reset_counters()
            self._fetch_order_book()
            # Fetch executed price and entry time for all traded instruments
            self.buy_entry, self.entry = self._fetch_order_details(buy_orderID_list)
            self.sell_entry, self.entry = self._fetch_order_details(sell_orderID_list)
            # Set stop losses as per the entry parameters
            self._set_sl()
            # Update the class variables and save to file
            if not self.additional_flag:
                self.spread_status = 'active'
                msg_txt = f"*Artemis:*\n{self.spread_type.upper()} Spread executed at {self.current_datetime:%Y-%m-%d %H:%M:%S}.\n*Lots:* _{self.lots}_"
            else:
                self.spread_status = 'active_additional'
                self.additional_buy_entry = self.buy_entry
                self.additional_buy_ltp = self.buy_entry
                self.trade_params_df.iloc[0, 30] = self.additional_buy_entry
                self.trade_params_df.iloc[0, 31] = self.additional_buy_ltp
                msg_txt = f"*Artemis:*\n{self.spread_type.upper()} Spread executed at {self.current_datetime:%Y-%m-%d %H:%M:%S}.\n*Lots:* _{self.lots}_\nAdditional {self.spread_type.upper()} Spread executed at {self.current_datetime:%Y-%m-%d %H:%M:%S}.\n*Lots:* _{self.additional_lots}_"
            self.buy_ltp = self.buy_entry
            self.sell_ltp = self.sell_entry
            self.trade_params_df.iloc[0, 2] = self.entry
            self.trade_params_df.iloc[0, 9] = self.buy_entry
            self.trade_params_df.iloc[0, 13] = self.sell_entry
            self.trade_params_df.iloc[0, 14] = self.index_sl
            self.trade_params_df.iloc[0, 15] = self.option_sl
            self.trade_params_df.iloc[0, 16] = self.spread_status
            self.trade_params_df.iloc[0, 22] = self.buy_ltp
            self.trade_params_df.iloc[0, 23] = self.sell_ltp
            self.trade_params_df.to_csv(f"data/{self.spread_type}_trade_params.csv", index=False)
            # Send update
            print(msg_txt)
            #telegram_bot_sendtext(msg_txt)
            #telegram_bot_sendtext(msg_txt, 'bot')
            slack_bot_sendtext(msg_txt, "#trade-alerts")
        else:
            msg_txt = f"*Artemis:*\n{self.spread_type.upper()} Spread wont be executed after ELM cutoff time.\n*Lots:* _{self.lots}_"
            print(msg_txt)
            #telegram_bot_sendtext(msg_txt)
            #telegram_bot_sendtext(msg_txt, 'bot')
            slack_bot_sendtext(msg_txt, "#trade-alerts")

    # Method to exit spread    
    def exit_spread(self):
        self.current_datetime = datetime.now()
        # Code to exit trade only when the spread has the original number of lots
        if self.spread_status == 'adjusted' or self.spread_status == 'active' or self.spread_status == 'active_additional_elm' or self.spread_status == 'adjusted_elm' or self.spread_status == 'adjusted_additional_elm':
            sell_exit_orderID_list = self._place_order('BUY', self.sell_symbol, self.sell_token, self.lots)
            buy_exit_orderID_list = self._place_order('SELL', self.buy_symbol, self.buy_token, self.lots)
            sleep(1)
            reset_counters()
            self._fetch_order_book()
            self.sell_exit, self.exit_time = self._fetch_order_details(sell_exit_orderID_list)
            self.buy_exit, self.exit_time = self._fetch_order_details(buy_exit_orderID_list)
            msg_txt = f"*Artemis:*\n{self.spread_type.upper()} Spread exited at {self.current_datetime:%Y-%m-%d %H:%M:%S}.\n*Lots:* _{self.lots}_"
        # Code to exit trade if it has additional lots
        else:    
            # Exit sold option first
            sell_exit_orderID_list = self._place_order('BUY', self.sell_symbol, self.sell_token, (self.lots+self.additional_lots))
            # Exit bought options next
            if self.buy_token == self.additional_buy_token:
                # Code to exit buy if bought option for the additional spread is the same as the original spread
                buy_exit_orderID_list = self._place_order('SELL', self.buy_symbol, self.buy_token, (self.lots+self.additional_lots))
                # Get order book
                sleep(1)
                reset_counters()
                self._fetch_order_book()
                # Fetch exit details for all instruments
                self.sell_exit, self.exit_time = self._fetch_order_details(sell_exit_orderID_list)
                self.buy_exit, self.exit_time = self._fetch_order_details(buy_exit_orderID_list)
                self.additional_buy_exit = self.buy_exit
                self.additional_buy_exit_time = self.exit_time
            else:
                # Code to exit original bought option
                buy_exit_orderID_list = self._place_order('SELL', self.buy_symbol, self.buy_token, self.lots)
                # Code to exit additional bought option
                additional_buy_exit_orderID_list = self._place_order('SELL', self.additional_buy_symbol, self.additional_buy_token, self.additional_lots)
                # Get order book
                sleep(1)
                reset_counters()
                self._fetch_order_book()
                # Fetch exit details for all the instruments
                self.sell_exit, self.exit_time = self._fetch_order_details(sell_exit_orderID_list)
                self.buy_exit, self.exit_time = self._fetch_order_details(buy_exit_orderID_list)
                self.additional_buy_exit, self.additional_buy_exit_time = self._fetch_order_details(additional_buy_exit_orderID_list)
            self.additional_booked_pl = self.additional_booked_pl + self.additional_buy_exit - self.additional_buy_entry + self.sell_entry - self.sell_exit
            self.additional_pl = self.additional_booked_pl
            self.additional_buy_ltp = self.additional_buy_exit
            self.trade_params_df.iloc[0, 31] = self.additional_buy_ltp
            self.trade_params_df.iloc[0, 32] = self.additional_buy_exit
            self.trade_params_df.iloc[0, 33] = self.additional_booked_pl
            self.trade_params_df.iloc[0, 34] = self.additional_pl
            msg_txt = f"*Artemis:*\n{self.spread_type.upper()} Spread exited at {self.current_datetime:%Y-%m-%d %H:%M:%S}.\n*Lots:* _{self.lots}_\nAdditional {self.spread_type.upper()} Spread exited at {self.current_datetime:%Y-%m-%d %H:%M:%S}.\n*Lots:* _{self.additional_lots}_"
        self.spread_status = 'closed'
        self.booked_pl = self.booked_pl + self.buy_exit - self.buy_entry + self.sell_entry - self.sell_exit
        self.pl = self.booked_pl
        self.buy_ltp = self.buy_exit
        self.sell_ltp = self.sell_exit
        self.trade_params_df.iloc[0, 16] = self.spread_status
        self.trade_params_df.iloc[0, 17] = self.buy_exit
        self.trade_params_df.iloc[0, 18] = self.sell_exit
        self.trade_params_df.iloc[0, 19] = self.exit_time
        self.trade_params_df.iloc[0, 20] = self.pl
        self.trade_params_df.iloc[0, 21] = self.booked_pl
        self.trade_params_df.iloc[0, 22] = self.buy_ltp
        self.trade_params_df.iloc[0, 23] = self.sell_ltp
        self.trade_params_df.to_csv(f"data/{self.spread_type}_trade_params.csv", index=False)
        print(msg_txt)
        #telegram_bot_sendtext(msg_txt)
        #telegram_bot_sendtext(msg_txt, 'bot')
        slack_bot_sendtext(msg_txt, "#trade-alerts")

    # Method to adjust spread            
    def adjust_spread(self):
        self.current_datetime = datetime.now()
        # Get strike, symbol and token for the option to be sold
        if self.spread_type == 'ce':
            if self.sell_strike - self.index_ltp > minimum_gap:
                self.new_sell_strike = self.sell_strike - adjustment_distance
            else:
                self.new_sell_strike = (ceil(self.index_ltp / strike_iteration_interval) * strike_iteration_interval) + minimum_gap_iterator
        if self.spread_type == 'pe':
            if self.index_ltp - self.sell_strike > minimum_gap:
                self.new_sell_strike = self.sell_strike + adjustment_distance
            else:
                self.new_sell_strike = (floor(self.index_ltp / strike_iteration_interval) * strike_iteration_interval) - minimum_gap_iterator
        self.new_sell_symbol, self.new_sell_token = self._fetch_symbol_and_token(self.new_sell_strike)
        # Code to only adjust existing spread without entering an additional spread
        if self.current_datetime < self.elm_time and self.current_datetime > self.cutoff_time:
            # Exit existing sell
            sell_exit_orderID_list = self._place_order('BUY', self.sell_symbol, self.sell_token, self.lots)
            # Enter new sell
            new_sellorderID_list = self._place_order('SELL', self.new_sell_symbol, self.new_sell_token, self.lots)
            # Get order book
            sleep(1)
            reset_counters()
            self._fetch_order_book()
            # Get order details for all instruments
            self.sell_exit, self.sell_exit_time = self._fetch_order_details(sell_exit_orderID_list)
            self.new_sell_entry, self.new_sell_entry_time = self._fetch_order_details(new_sellorderID_list)
            # Update spread status
            self.spread_status = 'adjusted'
            # Update msg_txt
            msg_txt = f"*Artemis:*\n{self.spread_type.upper()} Spread adjusted at {self.current_datetime:%Y-%m-%d %H:%M:%S}.\n*Lots:* _{self.lots}_"
        # Code to make adjustment and enter additional spread
        if self.current_datetime < self.elm_time and self.current_datetime < self.cutoff_time:
            self.additional_lots = self.lots//2
            # Get the strike and token for the additional lots hedge
            if self.spread_type == 'ce':
                self.additional_buy_strike = self.new_sell_strike + hedge_points
            else:
                self.additional_buy_strike = self.new_sell_strike - hedge_points
            self.additional_buy_symbol, self.additional_buy_token = self._fetch_symbol_and_token(self.additional_buy_strike)
            # Exit existing sell
            sell_exit_orderID_list = self._place_order('BUY', self.sell_symbol, self.sell_token, self.lots)
            # Buy hedges for additional lots
            additional_buy_orderID_list = self._place_order('BUY', self.additional_buy_symbol, self.additional_buy_token, self.additional_lots)
            # Enter new sell with original lots + additional lots
            new_sellorderID_list = self._place_order('SELL', self.new_sell_symbol, self.new_sell_token, (self.lots+self.additional_lots))
            # Get order book
            sleep(1)
            reset_counters()
            self._fetch_order_book()
            # Get order details for all instruments
            self.sell_exit, self.sell_exit_time = self._fetch_order_details(sell_exit_orderID_list)
            self.additional_buy_entry, self.additional_buy_entry_time = self._fetch_order_details(additional_buy_orderID_list)
            self.new_sell_entry, self.new_sell_entry_time = self._fetch_order_details(new_sellorderID_list)
            # Update spread status
            self.spread_status = 'adjusted_additional'
            # Update msg_txt
            msg_txt = f"*Artemis:*\n{self.spread_type.upper()} Spread adjusted at {self.current_datetime:%Y-%m-%d %H:%M:%S}.\n*Lots:* _{self.lots}_\nAdditional {self.spread_type.upper()} Spread executed at {self.current_datetime:%Y-%m-%d %H:%M:%S}.\n*Lots:* _{self.additional_lots}_"
        if self.current_datetime > self.elm_time:
            msg_txt = f"*Artemis:*\n{self.spread_type.upper()} Spread wont be adjusted after ELM cutoff time.\n*Lots:* _{self.lots}_"
            print(msg_txt)
            #telegram_bot_sendtext(msg_txt)
            #telegram_bot_sendtext(msg_txt, 'bot')
            slack_bot_sendtext(msg_txt, "#trade-alerts")
            return
        # Update the variables
        self.booked_pl = self.booked_pl + self.sell_entry - self.sell_exit
        self.sell_strike = self.new_sell_strike
        self.sell_symbol = self.new_sell_symbol
        self.sell_token = self.new_sell_token
        self.sell_entry = self.new_sell_entry
        self.entry = self.new_sell_entry_time
        self.sell_ltp = self.sell_entry
        self.pl = self.booked_pl + self.buy_ltp - self.buy_entry + self.sell_entry - self.sell_ltp
        self._set_sl()
        # Update trade_params and save to file
        self.trade_params_df.iloc[0, 2] = self.entry
        self.trade_params_df.iloc[0, 10] = self.sell_strike
        self.trade_params_df.iloc[0, 11] = self.sell_symbol
        self.trade_params_df.iloc[0, 12] = self.sell_token
        self.trade_params_df.iloc[0, 13] = self.sell_entry
        self.trade_params_df.iloc[0, 14] = self.index_sl
        self.trade_params_df.iloc[0, 15] = self.option_sl
        self.trade_params_df.iloc[0, 16] = self.spread_status
        self.trade_params_df.iloc[0, 18] = self.sell_exit
        self.trade_params_df.iloc[0, 19] = self.exit_time
        self.trade_params_df.iloc[0, 20] = self.pl
        self.trade_params_df.iloc[0, 21] = self.booked_pl
        self.trade_params_df.iloc[0, 23] = self.sell_ltp
        if self.spread_status == 'adjusted_additional':
            self.additional_buy_ltp = self.additional_buy_entry
            self.additional_pl = self.additional_booked_pl + self.additional_buy_ltp - self.additional_buy_entry + self.sell_entry - self.sell_ltp
            self.trade_params_df.iloc[0, 26] = self.additional_lots
            self.trade_params_df.iloc[0, 27] = self.additional_buy_strike
            self.trade_params_df.iloc[0, 28] = self.additional_buy_symbol
            self.trade_params_df.iloc[0, 29] = self.additional_buy_token
            self.trade_params_df.iloc[0, 30] = self.additional_buy_entry
            self.trade_params_df.iloc[0, 31] = self.additional_buy_ltp
            self.trade_params_df.iloc[0, 34] = self.additional_pl
        self.trade_params_df.to_csv(f"data/{self.spread_type}_trade_params.csv", index=False)
        print(msg_txt)
        #telegram_bot_sendtext(msg_txt)
        #telegram_bot_sendtext(msg_txt, 'bot')
        slack_bot_sendtext(msg_txt, "#trade-alerts")
    
    # Method to bring in hedge to within 'hedge_points' points and / or exit additional spread          
    def adjust_for_elm(self):
        self.current_datetime = datetime.now()
        # Get strike for the new buy option
        if self.spread_type == 'ce':
            self.new_buy_strike = self.sell_strike + hedge_points
        if self.spread_type == 'pe':
            self.new_buy_strike = self.sell_strike - hedge_points
        self.new_buy_symbol, self.new_buy_token = self._fetch_symbol_and_token(self.new_buy_strike)
        if self.spread_status == 'adjusted':
            # Execute new buy
            new_buy_orderID_list = self._place_order('BUY', self.new_buy_symbol, self.new_buy_token, self.lots)
            # Exit previous buy
            buy_exit_orderID_list = self._place_order('SELL', self.buy_symbol, self.buy_token, self.lots)
            # Get order book
            sleep(1)
            reset_counters()
            self._fetch_order_book()
            # Get order details for all instruments traded
            self.new_buy_entry, self.new_buy_entry_time = self._fetch_order_details(new_buy_orderID_list)
            self.buy_exit, self.buy_exit_time = self._fetch_order_details(buy_exit_orderID_list)
            # Update spread status
            self.spread_status = 'adjusted_elm'
            # Update msg_txt
            msg_txt = f"*Artemis:*\n{self.spread_type.upper()} Hedge brought in at {self.current_datetime:%Y-%m-%d %H:%M:%S}.\n*Lots:* _{self.lots}_"
        if self.spread_status == 'adjusted_additional':
            # Exit additional sold options
            additional_sell_orderID_list = self._place_order('BUY', self.sell_symbol, self.sell_token, self.additional_lots)
            # Buy hedge for difference
            new_buy_orderID_list = self._place_order('BUY', self.new_buy_symbol, self.new_buy_token, (self.lots-self.additional_lots))
            # Sell the hedge option for the original lots
            sleep(1)
            buy_exit_orderID_list = self._place_order('SELL', self.buy_symbol, self.buy_token, self.lots)
            # Get order book
            sleep(1)
            reset_counters()
            self._fetch_order_book()
            # Get order details for all instruments traded
            self.additional_sell_exit, self.additional_sell_exit_time = self._fetch_order_details(additional_sell_orderID_list)
            self.new_buy_entry, self.new_buy_entry_time = self._fetch_order_details(new_buy_orderID_list)
            self.buy_exit, self.buy_exit_time = self._fetch_order_details(buy_exit_orderID_list)
            # Update spread status
            self.spread_status = 'adjusted_additional_elm'
            self.additional_buy_exit = self.new_buy_entry
            # Update msg_txt
            msg_txt = f"*Artemis:*\nAdditional {self.spread_type.upper()} Spread exited at {self.current_datetime:%Y-%m-%d %H:%M:%S}.\n*Lots:* _{self.additional_lots}_\n{self.spread_type.upper()} Hedge brought in at {self.current_datetime:%Y-%m-%d %H:%M:%S}.\n*Lots:* _{self.lots}_"     
        if self.spread_status == 'active_additional':
            # Exit additional sold options
            additional_sell_orderID_list = self._place_order('BUY', self.sell_symbol, self.sell_token, self.additional_lots)
            # Exit hedge for additional spread
            additional_buy_orderID_list = self._place_order('SELL', self.buy_symbol, self.buy_token, self.additional_lots)
            # Get order book
            sleep(1)
            reset_counters()
            self._fetch_order_book()
            # Get order details for all instruments traded
            self.additional_sell_exit, self.additional_sell_exit_time = self._fetch_order_details(additional_sell_orderID_list)
            self.additional_buy_exit, self.additional_buy_exit_time = self._fetch_order_details(additional_buy_orderID_list)
            # Update spread status
            self.spread_status = 'active_additional_elm'
            # Update msg_txt
            msg_txt = f"*Artemis:*\nAdditional {self.spread_type.upper()} Spread exited at {self.current_datetime:%Y-%m-%d %H:%M:%S}.\n*Lots:* _{self.additional_lots}_"
        # Update variables, trade_params and save to file
        if self.spread_status == 'adjusted_elm' or self.spread_status == 'adjusted_additional_elm':
            self.booked_pl = self.booked_pl + self.buy_exit - self.buy_entry
            self.buy_strike = self.new_buy_strike
            self.buy_symbol = self.new_buy_symbol
            self.buy_token = self.new_buy_token
            self.buy_entry = self.new_buy_entry
            self.buy_ltp = self.buy_entry
            self.trade_params_df.iloc[0, 6] = self.buy_strike
            self.trade_params_df.iloc[0, 7] = self.buy_symbol
            self.trade_params_df.iloc[0, 8] = self.buy_token
            self.trade_params_df.iloc[0, 9] = self.buy_entry
        if self.spread_status == 'active_additional_elm' or self.spread_status == 'adjusted_additional_elm':
            self.additional_booked_pl = self.additional_booked_pl + self.additional_buy_exit - self.additional_buy_entry + self.sell_entry - self.additional_sell_exit 
            self.additional_pl = self.additional_booked_pl
            self.additional_buy_ltp = self.additional_buy_exit
            self.trade_params_df.iloc[0, 31] = self.additional_buy_ltp
            self.trade_params_df.iloc[0, 32] = self.additional_buy_exit
            self.trade_params_df.iloc[0, 33] = self.additional_booked_pl
            self.trade_params_df.iloc[0, 34] = self.additional_pl
        self.pl = self.booked_pl + self.buy_ltp - self.buy_entry + self.sell_entry - self.sell_ltp
        self.trade_params_df.iloc[0, 16] = self.spread_status
        self.trade_params_df.iloc[0, 20] = self.pl
        self.trade_params_df.iloc[0, 21] = self.booked_pl
        self.trade_params_df.iloc[0, 22] = self.buy_ltp
        self.trade_params_df.to_csv(f"data/{self.spread_type}_trade_params.csv", index=False)
        print(msg_txt)
        #telegram_bot_sendtext(msg_txt)
        #telegram_bot_sendtext(msg_txt, 'bot')
        slack_bot_sendtext(msg_txt, "#trade-alerts")

    # Method to monitor spread
    def monitor_spread(self):
        self.current_datetime = datetime.now()
        if self.spread_status == 'closed':
            return 'closed'
        self.index_ltp = self._fetch_ltp(exchange_segment, instrument, underlying_token)
        self.buy_ltp = self._fetch_ltp(fo_exchange_segment, self.buy_symbol, self.buy_token)
        self.sell_ltp = self._fetch_ltp(fo_exchange_segment, self.sell_symbol, self.sell_token)
        self.pl = self.booked_pl + self.buy_ltp - self.buy_entry + self.sell_entry - self.sell_ltp
        if self.spread_status == 'adjusted_additional' or self.spread_status == 'active_additional':
            if self.spread_status == 'adjusted_additional':
                self.additional_buy_ltp = self._fetch_ltp(fo_exchange_segment, self.additional_buy_symbol, self.additional_buy_token)
            else:
                self.additional_buy_ltp = self.buy_ltp
            self.additional_pl = self.additional_booked_pl + self.additional_buy_ltp - self.additional_buy_entry + self.sell_entry - self.sell_ltp
            self.trade_params_df.iloc[0, 31] = self.additional_buy_ltp
            self.trade_params_df.iloc[0, 34] = self.additional_pl
        if self.spread_type == 'ce' and self.index_ltp > self.index_sl:
            if self.current_datetime.time() > time(9,16):
                return 'index_sl'
            else:
                msg_txt = f"*Artemis:*\n{self.spread_type.upper()} Spread Index SL hit at {self.current_datetime:%Y-%m-%d %H:%M:%S}. Waiting till 9:16 to take exit decision."
                print(msg_txt)
                #telegram_bot_sendtext(msg_txt)
                #telegram_bot_sendtext(msg_txt, 'bot')
                slack_bot_sendtext(msg_txt, "#trade-alerts")
                sleep((datetime.combine(self.current_datetime.date(), time(9, 16)) - datetime.now()).total_seconds())
                reset_counters()
                self.index_ltp = self._fetch_ltp(exchange_segment, instrument, underlying_token)
                if self.index_ltp > self.index_sl:
                    return'index_sl'
                else:
                    return 'continue'
        if self.spread_type == 'pe' and self.index_ltp < self.index_sl:
            if self.current_datetime.time() > time(9,16):
                return 'index_sl'
            else:
                msg_txt = f"*Artemis:*\n{self.spread_type.upper()} Spread Index SL hit at {self.current_datetime:%Y-%m-%d %H:%M:%S}. Waiting till 9:16 to take exit decision."
                print(msg_txt)
                #telegram_bot_sendtext(msg_txt)
                #telegram_bot_sendtext(msg_txt, 'bot')
                slack_bot_sendtext(msg_txt, "#trade-alerts")
                sleep((datetime.combine(self.current_datetime.date(), time(9, 16)) - datetime.now()).total_seconds())
                reset_counters()
                self.index_ltp = self._fetch_ltp(exchange_segment, instrument, underlying_token)
                if self.index_ltp < self.index_sl:
                    return'index_sl'
                else:
                    return 'continue'
        if self.sell_ltp > self.option_sl:
            if self.current_datetime.time() > time(9,16):
                return 'option_sl'
            else:
                msg_txt = f"*Artemis:*\n{self.spread_type.upper()} Spread Option SL hit at {self.current_datetime:%Y-%m-%d %H:%M:%S}. Waiting till 9:16 to take exit decision."
                print(msg_txt)
                #telegram_bot_sendtext(msg_txt)
                #telegram_bot_sendtext(msg_txt, 'bot')
                slack_bot_sendtext(msg_txt, "#trade-alerts")
                sleep((datetime.combine(self.current_datetime.date(), time(9, 16)) - datetime.now()).total_seconds())
                reset_counters()
                self.sell_ltp = self._fetch_ltp(fo_exchange_segment, self.sell_symbol, self.sell_token)
                if self.sell_ltp > self.option_sl:
                    return'option_sl'
                else:
                    return 'continue'
        self.trade_params_df.iloc[0, 20] = self.pl
        self.trade_params_df.iloc[0, 22] = self.buy_ltp
        self.trade_params_df.iloc[0, 23] = self.sell_ltp
        self.trade_params_df.to_csv(f"data/{self.spread_type}_trade_params.csv", index=False)
        return 'continue'