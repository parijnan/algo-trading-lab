"""
athena.py — Athena Production Main Entry Point
Nifty Double Calendar Condor Strategy — Live Execution

Architecture:
    - Athena class owns run loop, entry/exit logic, order placement.
    - state.AthenaState             — persistent trade state across restarts
    - functions.py                  — Slack/Telegram messaging, exception handling
    - logger_setup.py               — dual console+file logging
"""

import os
import sys
import signal
import pandas as pd
import mibian
from datetime import datetime, date, timedelta
from time import sleep

from configs_live import (
    user_name, NIFTY_INDEX_TOKEN, VIX_TOKEN,
    MARKET_OPEN, MARKET_CLOSE,
    ENTRY_TIME, ELM_EXIT_TIME,
    TARGET_DELTA_SOLD, SAFETY_WING_DELTA, ENABLE_SAFETY_WINGS,
    STRIKE_STEP, BUY_LEG_MIN_DTE, LOT_SIZE, LOT_COUNT,
    SLIPPAGE_POINTS, DRY_RUN, TRADE_UPDATE_INTERVAL,
    EXCHANGE_NSE, EXCHANGE_NFO, FO_EXCHANGE_SEGMENT,
    SLACK_TRADE_ALERTS, SLACK_TRADE_UPDATES, SLACK_ERRORS_CHANNEL,
    DATA_DIR, TRADE_LOGS_DIR, RISK_FREE_RATE
)
from state import AthenaState, load_state, save_state, clear_trade_fields
from functions import (
    slack_bot_sendtext, handle_exception, 
    _increment_poll, _increment_order, _reset_counters
)
from logger_setup import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Repo root — used for holiday file
# ---------------------------------------------------------------------------
REPO_ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

class Athena:
    """
    Athena live execution engine.
    """

    def __init__(self, obj, auth_token, instrument_df):
        self.obj            = obj
        self.auth_token     = auth_token
        self.instrument_df  = instrument_df
        
        self.holidays       = set()
        self._load_holidays()
        
        self.state          = load_state()
        
        self._opening_time  = datetime.strptime(MARKET_OPEN,  "%H:%M").time()
        self._closing_time  = datetime.strptime(MARKET_CLOSE, "%H:%M").time()
        self._entry_time    = datetime.strptime(ENTRY_TIME,   "%H:%M").time()
        self._exit_time     = datetime.strptime(ELM_EXIT_TIME, "%H:%M").time()
        
        # Qty freeze for Nifty on NFO
        self._qty_freeze    = 1800
        
        # Register signal handlers
        signal.signal(signal.SIGINT,  self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        logger.info(f"Athena initialised. State: {self.state.status}. DRY_RUN: {DRY_RUN}.")

    def _handle_signal(self, signum, frame):
        logger.info(f"Shutdown signal received ({signum}).")
        sys.exit(0)

    def _load_holidays(self):
        holidays_file = os.path.join(REPO_ROOT, 'data', 'holidays.csv')
        if os.path.exists(holidays_file):
            df = pd.read_csv(holidays_file)
            self.holidays = set(pd.to_datetime(df['date']).dt.date)
        else:
            logger.warning("holidays.csv not found.")

    def _is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def _last_trading_day_before(self, target_date: date) -> date:
        d = target_date - timedelta(days=1)
        for _ in range(10):
            if self._is_trading_day(d):
                return d
            d -= timedelta(days=1)
        return None

    def _get_ltp(self, exchange, symbol, token):
        _increment_poll()
        try:
            return float(self.obj.ltpData(exchange, symbol, token)['data']['ltp'])
        except Exception as e:
            logger.error(f"LTP fetch failed for {symbol}: {e}")
            return None

    def _select_expiries(self):
        today = date.today()
        all_expiries = self._get_expiry_dates()
        
        # 1. Sell Expiry: The next available expiry
        sell_expiry = next((exp for exp in all_expiries if exp >= today), None)
        if not sell_expiry:
            logger.error("No future expiries found.")
            return None, None
            
        # 2. Buy Expiry: Monthly expiry (usually the last Thursday/Tuesday of the month)
        # We need at least BUY_LEG_MIN_DTE
        buy_expiry = None
        for exp in all_expiries:
            if exp >= sell_expiry + timedelta(days=BUY_LEG_MIN_DTE):
                # Check if it's the last expiry of its month
                # (Simple heuristic: next expiry is in a different month)
                idx = all_expiries.index(exp)
                if idx + 1 < len(all_expiries):
                    next_exp = all_expiries[idx+1]
                    if next_exp.month != exp.month:
                        buy_expiry = exp
                        break
                else:
                    # Last available expiry in master
                    buy_expiry = exp
                    break
        
        if not buy_expiry:
            # Fallback to the latest available if min DTE not met for monthly
            buy_expiry = all_expiries[-1]
            
        return sell_expiry, buy_expiry

    def _fetch_symbol_and_token(self, strike, option_type, expiry_date):
        expiry_str = expiry_date.strftime('%d%b%Y').upper()
        # instrument_df uses strike in paise (strike * 100)
        row = self.instrument_df[
            (self.instrument_df['expiry'] == expiry_str) &
            (self.instrument_df['strike'] == float(strike) * 100) &
            (self.instrument_df['symbol'].str[-2:] == option_type.upper())
        ]
        if row.empty:
            return None, None
        return row.iloc[0]['symbol'], str(row.iloc[0]['token'])

    def _find_delta_strike(self, spot, vix, expiry_date, target_delta, option_type):
        """
        Iterate strikes to find the one closest to target_delta.
        """
        dte = (expiry_date - date.today()).days
        if dte <= 0: dte = 0.5 # Expiry day
        
        atm = round(spot / STRIKE_STEP) * STRIKE_STEP
        
        best_strike = atm
        min_delta_diff = 999.0
        
        # Search range: ATM +/- 2000 pts
        search_range = range(-2000, 2100, STRIKE_STEP)
        
        for offset in search_range:
            strike = atm + offset
            symbol, token = self._fetch_symbol_and_token(strike, option_type, expiry_date)
            if not symbol: continue
            
            ltp = self._get_ltp(EXCHANGE_NFO, symbol, token)
            if ltp is None or ltp <= 0: continue
            
            # Compute delta using mibian
            # Nifty doesn't have dividends for simple BS
            c = mibian.BS([spot, strike, RISK_FREE_RATE, dte], volatility=vix)
            current_delta = abs(c.callDelta) if option_type == 'ce' else abs(c.putDelta)
            
            diff = abs(current_delta - target_delta)
            if diff < min_delta_diff:
                min_delta_diff = diff
                best_strike = strike
            else:
                # If diff starts increasing, we are moving away from target
                if diff > min_delta_diff + 0.05:
                    break
        
        return best_strike

    def _place_order(self, transaction_type, symbol, token, lots):
        if DRY_RUN:
            dry_id = f"DRY_{token}_{transaction_type}_{datetime.now():%H%M%S}"
            logger.info(f"[DRY RUN] {transaction_type} {lots} lot(s) {symbol} ({token}) — ID: {dry_id}")
            return [dry_id]

        l_limit = self._qty_freeze / LOT_SIZE
        order_quantities = []
        if lots <= l_limit:
            order_quantities.append(lots)
        else:
            full = int(lots // l_limit)
            rem  = lots % l_limit
            for _ in range(full): order_quantities.append(int(l_limit))
            if rem > 0: order_quantities.append(int(rem))

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
                    _increment_order()
                    response = self.obj.placeOrderFullResponse(orderparams)
                    if response['message'] == 'SUCCESS':
                        oid = response['data']['orderid']
                        orderid_list.append(oid)
                        logger.info(f"Order placed: {transaction_type} {symbol} ID: {oid}")
                        break
                    else:
                        logger.error(f"Order failed: {response['message']}")
                        break
                except Exception as e:
                    handle_exception(e)
                    sleep(1)
        return orderid_list

    def _fetch_order_details(self, orderid_list, token, symbol):
        if DRY_RUN:
            # For dry run, use current LTP as fill price
            fill = self._get_ltp(EXCHANGE_NFO, symbol, token) or 0.0
            logger.info(f"[DRY RUN] Simulating fill for {symbol} at {fill:.2f}")
            return fill, datetime.now()

        # Wait for fills
        sleep(2)
        total_qty = 0
        total_val = 0.0
        fill_time = datetime.now()
        
        try:
            _increment_poll()
            book = self.obj.orderBook()['data']
            for oid in orderid_list:
                for order in book:
                    if order['orderid'] == oid:
                        q = int(order['filledshares'])
                        p = float(order['averageprice'])
                        total_qty += q
                        total_val += (p * q)
                        # Use last fill time
                        ft = datetime.strptime(order['updatetime'], '%d-%b-%Y %H:%M:%S')
                        if ft > fill_time: fill_time = ft
            
            avg_price = round(total_val / total_qty, 2) if total_qty > 0 else 0.0
            return avg_price, fill_time
        except Exception as e:
            handle_exception(e)
            return 0.0, datetime.now()

    def _execute_entry(self, strikes_dict, spot, vix):
        logger.info("=== EXECUTING ENTRY ===")
        lots = LOT_COUNT
        self.state.wings_enabled = ENABLE_SAFETY_WINGS
        
        # 1. Place BUY orders (Margin protection)
        buy_orders = {}
        # Leg keys to process
        buy_keys = ['ce_buy', 'pe_buy']
        if ENABLE_SAFETY_WINGS: buy_keys += ['ce_wing', 'pe_wing']
        
        for key in buy_keys:
            strike = strikes_dict[f'{key}_strike']
            exp    = strikes_dict['buy_expiry']
            sym, tok = self._fetch_symbol_and_token(strike, key.split('_')[1], exp)
            if not sym:
                logger.error(f"Could not fetch symbol for {key} strike {strike}")
                return False
            
            oids = self._place_order('BUY', sym, tok, lots)
            buy_orders[key] = {'oids': oids, 'sym': sym, 'tok': tok, 'strike': strike}

        # 2. Confirm BUY fills and update state
        for key, info in buy_orders.items():
            fill, ft = self._fetch_order_details(info['oids'], info['tok'], info['sym'])
            setattr(self.state, f"{key}_strike", info['strike'])
            setattr(self.state, f"{key}_token",  info['tok'])
            setattr(self.state, f"{key}_symbol", info['sym'])
            setattr(self.state, f"{key}_entry",  fill)

        # 3. Place SELL orders
        sell_orders = {}
        for side in ['ce', 'pe']:
            key = f"{side}_sell"
            strike = strikes_dict[f'{key}_strike']
            exp    = strikes_dict['sell_expiry']
            sym, tok = self._fetch_symbol_and_token(strike, side, exp)
            
            oids = self._place_order('SELL', sym, tok, lots)
            sell_orders[key] = {'oids': oids, 'sym': sym, 'tok': tok, 'strike': strike}

        # 4. Confirm SELL fills and update state
        for key, info in sell_orders.items():
            fill, ft = self._fetch_order_details(info['oids'], info['tok'], info['sym'])
            setattr(self.state, f"{key}_strike", info['strike'])
            setattr(self.state, f"{key}_token",  info['tok'])
            setattr(self.state, f"{key}_symbol", info['sym'])
            setattr(self.state, f"{key}_entry",  fill)

        # Finalise state
        self.state.status = 'in_trade'
        self.state.lots = lots
        self.state.entry_time = datetime.now().isoformat()
        self.state.entry_spot = spot
        self.state.entry_vix  = vix
        self.state.sell_expiry = strikes_dict['sell_expiry'].isoformat()
        self.state.buy_expiry  = strikes_dict['buy_expiry'].isoformat()
        
        # Calculate net debit
        net = (self.state.ce_buy_entry + self.state.pe_buy_entry) - \
              (self.state.ce_sell_entry + self.state.pe_sell_entry)
        if ENABLE_SAFETY_WINGS:
            net += (self.state.ce_wing_entry + self.state.pe_wing_entry)
        self.state.net_debit = round(net, 2)
        
        save_state(self.state)
        logger.info(f"Entry complete. Net Debit: {self.state.net_debit}")
        
        # Send Slack Alert
        msg = f"*Athena* ENTRY | Spot: {spot:.2f} | VIX: {vix:.2f}\n" \
              f"Sold CE {self.state.ce_sell_strike} @ {self.state.ce_sell_entry:.1f}\n" \
              f"Sold PE {self.state.pe_sell_strike} @ {self.state.pe_sell_entry:.1f}\n" \
              f"Net Debit: {self.state.net_debit:.1f}"
        slack_bot_sendtext(msg, SLACK_TRADE_ALERTS)
        return True

    def _execute_exit(self, reason):
        logger.info(f"=== EXECUTING EXIT: {reason.upper()} ===")
        lots = self.state.lots
        
        # 1. Buy back SHORT legs (CE sell, PE sell)
        sell_legs = [
            ('ce_sell', self.state.ce_sell_symbol, self.state.ce_sell_token),
            ('pe_sell', self.state.pe_sell_symbol, self.state.pe_sell_token)
        ]
        
        exit_fills = {}
        for key, sym, tok in sell_legs:
            oids = self._place_order('BUY', sym, tok, lots)
            fill, ft = self._fetch_order_details(oids, tok, sym)
            exit_fills[key] = fill

        # 2. Sell to close LONG legs (CE buy, PE buy, CE wing, PE wing)
        buy_keys = ['ce_buy', 'pe_buy']
        if self.state.wings_enabled: buy_keys += ['ce_wing', 'pe_wing']
        
        for key in buy_keys:
            sym = getattr(self.state, f"{key}_symbol")
            tok = getattr(self.state, f"{key}_token")
            oids = self._place_order('SELL', sym, tok, lots)
            fill, ft = self._fetch_order_details(oids, tok, sym)
            exit_fills[key] = fill

        # Final P&L Calculation
        pl_pts = (exit_fills['ce_buy'] - self.state.ce_buy_entry) + \
                 (exit_fills['pe_buy'] - self.state.pe_buy_entry) + \
                 (self.state.ce_sell_entry - exit_fills['ce_sell']) + \
                 (self.state.pe_sell_entry - exit_fills['pe_sell'])
        
        if self.state.wings_enabled:
            pl_pts += (exit_fills['ce_wing'] - self.state.ce_wing_entry) + \
                      (exit_fills['pe_wing'] - self.state.pe_wing_entry)

        pl_rs = round(pl_pts * lots * LOT_SIZE, 2)
        
        # Final log and slack
        self._append_trade_log_row(exit_reason=reason, exit_fills=exit_fills)
        
        msg = f"*Athena* EXIT {reason.upper()} | P&L: {pl_pts:+.1f} pts ({pl_rs:+,.0f} Rs)"
        slack_bot_sendtext(msg, SLACK_TRADE_ALERTS)
        
        clear_trade_fields(self.state)
        save_state(self.state)
        return True

    def _poll_prices(self):
        prices = {}
        # 1. Spot
        prices['spot'] = self._get_ltp(EXCHANGE_NSE, 'NIFTY 50', NIFTY_INDEX_TOKEN)
        # 2. Active legs
        keys = ['ce_sell', 'pe_sell', 'ce_buy', 'pe_buy']
        if self.state.wings_enabled: keys += ['ce_wing', 'pe_wing']
        
        for key in keys:
            sym = getattr(self.state, f"{key}_symbol")
            tok = getattr(self.state, f"{key}_token")
            prices[key] = self._get_ltp(EXCHANGE_NFO, sym, tok)
            
        return prices

    def _append_trade_log_row(self, exit_reason=None, exit_fills=None):
        if self.state.status not in ('in_trade', 'exiting'): return
        
        prices = self._poll_prices()
        # Use fill prices if provided (for exit row)
        if exit_fills:
            for k, v in exit_fills.items(): prices[k] = v
            
        now = datetime.now()
        
        # Calculate unrealised
        try:
            pl_pts = (prices['ce_buy'] - self.state.ce_buy_entry) + \
                     (prices['pe_buy'] - self.state.pe_buy_entry) + \
                     (self.state.ce_sell_entry - prices['ce_sell']) + \
                     (self.state.pe_sell_entry - prices['pe_sell'])
            if self.state.wings_enabled:
                pl_pts += (prices['ce_wing'] - self.state.ce_wing_entry) + \
                          (prices['pe_wing'] - self.state.pe_wing_entry)
        except:
            pl_pts = 0.0

        row = {
            'time_stamp': now.strftime('%Y-%m-%d %H:%M:%S'),
            'spot': prices['spot'],
            'ce_sell_ltp': prices['ce_sell'], 'pe_sell_ltp': prices['pe_sell'],
            'ce_buy_ltp': prices['ce_buy'], 'pe_buy_ltp': prices['pe_buy'],
            'unrealised_pl': round(pl_pts, 2),
            'exit_reason': exit_reason
        }
        if self.state.wings_enabled:
            row['ce_wing_ltp'] = prices['ce_wing']
            row['pe_wing_ltp'] = prices['pe_wing']

        log_file = self._get_log_filepath()
        df = pd.DataFrame([row])
        df.to_csv(log_file, mode='a', index=False, header=not os.path.exists(log_file))

    def _get_log_filepath(self):
        d_str = datetime.fromisoformat(self.state.entry_time).strftime('%Y-%m-%d')
        return os.path.join(TRADE_LOGS_DIR, f"trade_{d_str}.csv")

    def _send_trade_update(self):
        if self.state.status != 'in_trade': return
        
        prices = self._poll_prices()
        try:
            pl_pts = (prices['ce_buy'] - self.state.ce_buy_entry) + \
                     (prices['pe_buy'] - self.state.pe_buy_entry) + \
                     (self.state.ce_sell_entry - prices['ce_sell']) + \
                     (self.state.pe_sell_entry - prices['pe_sell'])
            if self.state.wings_enabled:
                pl_pts += (prices['ce_wing'] - self.state.ce_wing_entry) + \
                          (prices['pe_wing'] - self.state.pe_wing_entry)
            
            pl_pts = round(pl_pts, 2)
            if pl_pts > self.state.max_unrealised_pl:
                self.state.max_unrealised_pl = pl_pts
            
            # Update last known LTPs in state
            self.state.last_spot = prices['spot']
            self.state.last_ce_sell_ltp = prices['ce_sell']
            self.state.last_pe_sell_ltp = prices['pe_sell']
            self.state.last_ce_buy_ltp = prices['ce_buy']
            self.state.last_pe_buy_ltp = prices['pe_buy']
            if self.state.wings_enabled:
                self.state.last_ce_wing_ltp = prices['ce_wing']
                self.state.last_pe_wing_ltp = prices['pe_wing']
            
            save_state(self.state)
        except:
            return
            
        pl_rs = round(pl_pts * self.state.lots * LOT_SIZE, 2)
        
        msg = f"*Athena* UPDATE | Spot: {prices['spot']:.2f} | " \
              f"P&L: {pl_pts:+.1f} pts ({pl_rs:+,.0f} Rs) | " \
              f"Peak: {self.state.max_unrealised_pl:+.1f} pts"
        slack_bot_sendtext(msg, SLACK_TRADE_UPDATES)

    def run(self):
        logger.info("=== Athena run loop started ===")
        
        while True:
            now = datetime.now()
            if now.time() >= self._closing_time: break
            
            # --- 1. ENTRY LOGIC ---
            if self.state.status == 'idle':
                sell_exp, _ = self._select_expiries()
                if sell_exp:
                    entry_day = self._last_trading_day_before(sell_exp)
                    if now.date() == entry_day and now.time() >= self._entry_time:
                        spot = self._get_ltp(EXCHANGE_NSE, 'NIFTY 50', NIFTY_INDEX_TOKEN)
                        vix  = self._get_ltp(EXCHANGE_NSE, 'INDIA VIX', VIX_TOKEN)
                        if spot and vix:
                            strikes = self._select_all_strikes(spot, vix)
                            if strikes:
                                self._execute_entry(strikes, spot, vix)
                
            # --- 2. EXIT LOGIC ---
            if self.state.status == 'in_trade':
                sell_exp_dt = date.fromisoformat(self.state.sell_expiry)
                exit_day = self._last_trading_day_before(sell_exp_dt)
                
                if now.date() == exit_day and now.time() >= self._exit_time:
                    self._execute_exit(reason='pre_expiry')

            # --- 3. MONITORING ---
            if self.state.status == 'in_trade':
                self._append_trade_log_row()
                self._send_trade_update()
                
                # Sleep in small chunks to remain responsive to exit time
                for _ in range(int(TRADE_UPDATE_INTERVAL / 10)):
                    sleep(10)
                    if datetime.now().time() >= self._exit_time: break
            else:
                sleep(60)
                
            _reset_counters()

if __name__ == "__main__":
    # For independent testing on local/dev
    # Usage: python athena.py (will fail without obj from leto)
    print("Standalone run requires SmartConnect object from leto.py.")
    print("Checking if state can be loaded...")
    s = load_state()
    print(f"Current State Status: {s.status}")
