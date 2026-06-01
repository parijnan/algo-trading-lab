"""
athena_engine.py — Athena Production Main Entry Point
Nifty Double Calendar Condor Strategy — Live Execution

Called by leto.py — not run directly.

Architecture:
    - Athena class owns run loop, entry/exit logic, order placement.
    - leto.py                       — owns login, market/holiday check, scrip master, session teardown
    - state.AthenaState             — persistent trade state across restarts
    - functions.py                  — Slack/Telegram messaging, exception handling
    - logger_setup.py               — dual console+file logging
"""

import os
import sys
import signal
import pandas as pd
import mibian
from datetime import datetime, date, timedelta, time
from time import sleep
from SmartApi.smartExceptions import DataException, NetworkException

from configs_live import (
    api_key, user_name,
    NIFTY_INDEX_TOKEN, VIX_TOKEN,
    MARKET_OPEN, MARKET_CLOSE,
    ENTRY_TIME, ELM_EXIT_TIME,
    VIX_FILTER_LOW, VIX_FILTER_HIGH,
    TARGET_DELTA_SOLD, SAFETY_WING_DELTA, ENABLE_SAFETY_WINGS,
    ENABLE_EMERGENCY_HEDGE, EMERGENCY_HEDGE_DELTA, ORDER_TIMEOUT_SEC,
    EMERGENCY_TRIGGER_OFFSET, EMERGENCY_EXIT_OFFSET, EMERGENCY_MAX_ATTEMPTS,

    STRIKE_STEP, BUY_LEG_MIN_DTE, LOT_SIZE, LOT_COUNT,
    LOT_CALC, LOT_CAPITAL, CASH_PER_LOT_REQUIRED,
    DRY_RUN, FORCE_ENTRY, TRADE_UPDATE_INTERVAL, QTY_FREEZE,
    EXCHANGE_NSE, EXCHANGE_NFO, FO_EXCHANGE_SEGMENT,
    SLACK_TRADEBOT_CHANNEL, SLACK_TRADE_ALERTS, SLACK_TRADE_UPDATES, SLACK_ERRORS_CHANNEL,
    TRADE_LOGS_DIR, RISK_FREE_RATE
)
from state import load_state, save_state, clear_trade_fields
from functions import (
    slack_bot_sendtext, handle_exception,
    _increment_rms_poll, _increment_order_book_poll, _increment_ltp_poll,
    _increment_order, _reset_counters,
    OrderFillWatcher,
)
from logger_setup import get_logger
from websocket_feed import SharedFeed, EXCHANGE_NSE_CM

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
        """
        Main entry point called by leto.py.
        Initialises state, holidays, and signal handlers.

        Parameters
        ----------
        obj            : SmartConnect — authenticated session from Leto
        auth_token     : str          — JWT token from generateSession response
        instrument_df  : DataFrame    — Nifty NFO rows from scrip master
        """
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
        self._qty_freeze    = QTY_FREEZE

        self.feed           = SharedFeed()
        self._update_elapsed = 0.0

        self._order_watcher = OrderFillWatcher()

        self._summary = {'strategy': 'Athena', 'traded': False, 'no_trade_reason': 'No signal'}

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
        try:
            ltp = float(self.obj.ltpData(exchange, symbol, token)['data']['ltp'])
            _increment_ltp_poll()
            return ltp
        except Exception as e:
            logger.error(f"LTP fetch failed for {symbol}: {e}")
            return None

    def _get_expiry_dates(self):
        expiry_dates = (
            self.instrument_df['expiry']
            .drop_duplicates()
            .apply(lambda x: datetime.strptime(x, '%d%b%Y').date())
            .sort_values()
            .tolist()
        )
        return expiry_dates

    def _select_expiries(self):
        today = date.today()
        all_expiries = self._get_expiry_dates()
        
        # 1. Sell Expiry: We sell the SECOND available expiry from today
        future_expiries = [exp for exp in all_expiries if exp >= today]
        
        if len(future_expiries) < 2:
            logger.error("Not enough future expiries found to form a calendar.")
            return None, None
            
        sell_expiry = future_expiries[1]
        
        # 2. Buy Expiry: Monthly expiry
        buy_expiry = None
        for exp in all_expiries:
            if exp >= today + timedelta(days=BUY_LEG_MIN_DTE):
                idx = all_expiries.index(exp)
                if idx + 1 < len(all_expiries):
                    next_exp = all_expiries[idx+1]
                    if next_exp.month != exp.month:
                        buy_expiry = exp
                        break
                else:
                    buy_expiry = exp
                    break
        
        if not buy_expiry:
            buy_expiry = all_expiries[-1]
            
        return sell_expiry, buy_expiry

    def _fetch_symbol_and_token(self, strike, option_type, expiry_date):
        expiry_str = expiry_date.strftime('%d%b%Y').upper()
        row = self.instrument_df[
            (self.instrument_df['expiry'] == expiry_str) &
            (self.instrument_df['strike'] == float(strike) * 100) &
            (self.instrument_df['symbol'].str[-2:] == option_type.upper())
        ]
        if row.empty:
            return None, None
        return row.iloc[0]['symbol'], str(row.iloc[0]['token'])

    def _find_delta_strike(self, spot, vix, expiry_date, target_delta, option_type):
        dte = (expiry_date - date.today()).days
        if dte <= 0: dte = 0.5
        atm = round(spot / STRIKE_STEP) * STRIKE_STEP
        delta_map = []
        search_range = range(-2000, 2100, STRIKE_STEP)
        for offset in search_range:
            strike = atm + offset
            c = mibian.BS([spot, strike, RISK_FREE_RATE, dte], volatility=vix)
            current_delta = abs(c.callDelta) if option_type == 'ce' else abs(c.putDelta)
            delta_map.append({'strike': strike, 'delta_diff': abs(current_delta - target_delta)})
        top_candidates = sorted(delta_map, key=lambda x: x['delta_diff'])[:3]
        for candidate in top_candidates:
            strike = candidate['strike']
            symbol, token = self._fetch_symbol_and_token(strike, option_type, expiry_date)
            if not symbol: continue
            ltp = self._get_ltp(EXCHANGE_NFO, symbol, token)
            if ltp is not None and ltp > 0:
                logger.info(f"Selected {strike}{option_type.upper()} | Target: {target_delta} | LTP: {ltp}")
                return strike
        return top_candidates[0]['strike']

    def _select_all_strikes(self, spot, vix):
        sell_exp, buy_exp = self._select_expiries()
        if not sell_exp: return None
        logger.info(f"Selecting strikes for Spot: {spot:.2f}, VIX: {vix:.2f}")
        ce_sell_strike = self._find_delta_strike(spot, vix, sell_exp, TARGET_DELTA_SOLD, 'ce')
        pe_sell_strike = self._find_delta_strike(spot, vix, sell_exp, TARGET_DELTA_SOLD, 'pe')
        ce_buy_strike = ce_sell_strike
        pe_buy_strike = pe_sell_strike
        if ENABLE_SAFETY_WINGS:
            ce_wing_strike = self._find_delta_strike(spot, vix, buy_exp, SAFETY_WING_DELTA, 'ce')
            pe_wing_strike = self._find_delta_strike(spot, vix, buy_exp, SAFETY_WING_DELTA, 'pe')
        else:
            ce_wing_strike = pe_wing_strike = None
        return {
            'sell_expiry': sell_exp, 'buy_expiry': buy_exp,
            'ce_sell_strike': ce_sell_strike, 'pe_sell_strike': pe_sell_strike,
            'ce_buy_strike': ce_buy_strike, 'pe_buy_strike': pe_buy_strike,
            'ce_wing_strike': ce_wing_strike, 'pe_wing_strike': pe_wing_strike
        }

    def _place_order(self, transaction_type, symbol, token, lots):
        if DRY_RUN:
            dry_id = f"DRY_{token}_{transaction_type}_{datetime.now():%H%M%S}"; logger.info(f"[DRY RUN] {transaction_type} {lots} lot(s) {symbol} ({token}) — ID: {dry_id}"); return [dry_id]
        
        # We track all IDs placed in THIS strategy run to avoid ghost-recovery collisions
        if not hasattr(self, '_placed_order_ids'): self._placed_order_ids = set()
        
        l_limit = self._qty_freeze // LOT_SIZE
        order_quantities = []
        rem = lots
        while rem > 0:
            chunk = min(rem, l_limit); order_quantities.append(chunk); rem -= chunk
        
        orderid_list = []
        for lot_chunk in order_quantities:
            qty_shares = int(lot_chunk * LOT_SIZE)
            orderparams = {
                "variety": "NORMAL", "tradingsymbol": symbol, "symboltoken": token,
                "transactiontype": transaction_type, "exchange": FO_EXCHANGE_SEGMENT,
                "ordertype": "MARKET", "producttype": "CARRYFORWARD",
                "duration": "DAY", "quantity": str(qty_shares)
            }
            rejection_count = 0
            while True:
                try:
                    response = self.obj.placeOrderFullResponse(orderparams)
                    _increment_order()
                    if response.get('message') == 'SUCCESS':
                        oid = response['data']['orderid']
                        orderid_list.append(oid); self._placed_order_ids.add(oid)
                        logger.info(f"Order placed: {transaction_type} {symbol} ID: {oid}"); break
                    else:
                        rejection_count += 1
                        err_msg = response.get('message', 'Unknown error')
                        logger.error(f"Order rejected ({rejection_count}/3): {symbol} — {err_msg}")
                        if rejection_count >= 3:
                            slack_bot_sendtext(f"⚠️ *Athena*: Order rejected 3× for {symbol}. {err_msg}. Stopping.", SLACK_ERRORS_CHANNEL)
                            break
                        sleep(1); _reset_counters(); continue
                except (DataException, NetworkException) as e:
                    err_msg = str(e).lower()
                    if "access rate" in err_msg:
                        logger.warning(f"Rate limit hit ({symbol}). Cooling down 2s..."); sleep(2); _reset_counters(); continue

                    logger.warning(f"Connectivity issue ({type(e).__name__}) during {symbol}. Verifying order book...")
                    sleep(2); _reset_counters()
                    try:
                        book = self.obj.orderBook()['data']
                        _increment_order_book_poll()
                        found = False
                        for order in book:
                            if (order['tradingsymbol'] == symbol and
                                order['transactiontype'] == transaction_type and
                                int(order['quantity']) == qty_shares and
                                order['status'] in ('complete', 'open', 'validation pending')):

                                oid = order['orderid']
                                if oid not in self._placed_order_ids:
                                    ut = datetime.strptime(order['updatetime'], '%d-%b-%Y %H:%M:%S')
                                    if (datetime.now() - ut).total_seconds() < 60:
                                        orderid_list.append(oid); self._placed_order_ids.add(oid)
                                        logger.info(f"Ghost Order recovered! ID: {oid}")
                                        found = True; break
                        if found: break
                        else: logger.info("Order not found in book. Retrying placement..."); continue
                    except Exception as e_inner:
                        logger.error(f"Error checking book: {e_inner}. Retrying placement..."); continue
                except Exception as e:

                    if "token" in str(e).lower() or "invalid" in str(e).lower():
                        logger.critical(f"Session failure detected: {e}. Aborting to Leto."); raise e
                    handle_exception(e); sleep(1); _reset_counters()
        return orderid_list

    def _fetch_order_details(self, orderid_list, token, symbol, expected_lots=0):
        if DRY_RUN:
            fill = self._get_ltp(EXCHANGE_NFO, symbol, token) or 0.0
            return fill, expected_lots, datetime.now()

        start_time = datetime.now()

        # --- WebSocket fast path ---
        if self._order_watcher._ws_ready.is_set():
            while (datetime.now() - start_time).total_seconds() < ORDER_TIMEOUT_SEC:
                with self._order_watcher._lock:
                    orders = dict(self._order_watcher.live_orders)
                if all(oid in orders for oid in orderid_list):
                    total_qty = 0
                    total_val = 0.0
                    fill_time = datetime.now()
                    for oid in orderid_list:
                        od     = orders[oid]
                        filled = int(od.get('filledshares') or 0)
                        avg    = float(od.get('averageprice') or 0.0)
                        total_qty += filled
                        total_val += avg * filled
                        try:
                            ft = datetime.strptime(od['updatetime'], '%d-%b-%Y %H:%M:%S')
                            if ft > fill_time:
                                fill_time = ft
                        except Exception:
                            pass
                    filled_lots = int(total_qty // LOT_SIZE)
                    avg_price   = round(total_val / total_qty, 2) if total_qty > 0 else 0.0
                    if expected_lots > 0 and filled_lots < expected_lots:
                        logger.warning(f"Partial fill (WS) on {symbol}: expected {expected_lots}, filled {filled_lots}.")
                        slack_bot_sendtext(
                            f"⚠️ *Athena*: Partial fill (WS) on {symbol}. "
                            f"Expected {expected_lots} lots, filled {filled_lots}.",
                            SLACK_ERRORS_CHANNEL)
                    return avg_price, filled_lots, fill_time
                sleep(0.05)
            logger.warning(f"WS fill timeout for {symbol}. Falling back to REST orderBook.")
            slack_bot_sendtext(
                f"⚠️ *Athena*: WS timeout for {symbol}. Switching to REST fallback.",
                SLACK_ERRORS_CHANNEL)
        else:
            logger.info(f"Order WS not ready — using REST orderBook for {symbol}.")

        # --- REST fallback path ---
        start_time = datetime.now()
        timeout    = ORDER_TIMEOUT_SEC

        while True:
            total_qty = 0
            total_val = 0.0
            fill_time = datetime.now()

            try:
                book_res = self.obj.orderBook()
                _increment_order_book_poll()

                if book_res and book_res.get('status'):
                    book = book_res['data']
                    matched_ids = []

                    for oid in orderid_list:
                        for order in book:
                            if order['orderid'] == oid:
                                matched_ids.append(oid)
                                q = int(order['filledshares'])
                                p = float(order['averageprice'])
                                total_qty += q
                                total_val += (p * q)
                                try:
                                    ft = datetime.strptime(order['updatetime'], '%d-%b-%Y %H:%M:%S')
                                    if ft > fill_time: fill_time = ft
                                except: pass

                    filled_lots = int(total_qty // LOT_SIZE)
                    all_ids_found = all(oid in matched_ids for oid in orderid_list)

                    # 1. SUCCESS: All IDs found AND quantity matches/exceeds expectation
                    if all_ids_found and (expected_lots == 0 or filled_lots >= expected_lots):
                        avg_price = round(total_val / total_qty, 2) if total_qty > 0 else 0.0
                        return avg_price, filled_lots, fill_time

                    # 2. FAILURE: All orders reached a final terminal state but didn't fill as expected
                    all_final = all_ids_found
                    if all_ids_found:
                        for oid in orderid_list:
                            for order in book:
                                if order['orderid'] == oid:
                                    if order['status'] not in ('complete', 'rejected', 'cancelled'):
                                        all_final = False; break
                            if not all_final: break

                    if all_final:
                        if total_qty > 0:
                            logger.info(f"Order sequence for {symbol} finalized with partial fill: {filled_lots}/{expected_lots} lots.")
                            slack_bot_sendtext(f"⚠️ *Athena*: Partial fill on {symbol}. Expected {expected_lots} lots, filled {filled_lots}.", SLACK_ERRORS_CHANNEL)
                            return round(total_val/total_qty, 2), filled_lots, fill_time
                        else:
                            logger.warning(f"Order sequence for {symbol} finalized with ZERO fills.")
                            if expected_lots > 0:
                                slack_bot_sendtext(f"⚠️ *Athena*: Zero fills on {symbol}. Expected {expected_lots} lots.", SLACK_ERRORS_CHANNEL)
                            return 0.0, 0, datetime.now()

                # 3. DISCREPANCY/LATENCY: IDs not yet visible or slow partial fill.
                elapsed = (datetime.now() - start_time).total_seconds()
                if elapsed >= timeout:
                    logger.warning(f"Timeout reaching {timeout}s for {symbol}. Returning current state.")
                    avg_price = round(total_val / total_qty, 2) if total_qty > 0 else 0.0
                    filled_at_timeout = int(total_qty // LOT_SIZE)
                    if expected_lots > 0 and filled_at_timeout < expected_lots:
                        slack_bot_sendtext(f"⚠️ *Athena*: Partial fill (timeout) on {symbol}. Expected {expected_lots} lots, filled {filled_at_timeout}.", SLACK_ERRORS_CHANNEL)
                    return avg_price, filled_at_timeout, fill_time

                sleep(1); _reset_counters()

            except Exception as e:
                logger.error(f"Error in _fetch_order_details for {symbol}: {e}")
                if (datetime.now() - start_time).total_seconds() >= timeout: break
                sleep(1); _reset_counters()

        if expected_lots > 0:
            slack_bot_sendtext(f"⚠️ *Athena*: Zero fills on {symbol}. Expected {expected_lots} lots.", SLACK_ERRORS_CHANNEL)
        return 0.0, 0, datetime.now()

    def _calculate_lots(self, strikes_dict=None):
        if not LOT_CALC: return LOT_COUNT
        while True:
            try:
                rms = self.obj.rmsLimit()["data"]
                _increment_rms_poll()
                total_power = float(rms["availablecash"])
                pure_cash = float(rms.get("cashbalance", 0.0))
                if pure_cash <= 0:
                    collateral = float(rms.get("collateral", 0.0))
                    pure_cash = round(total_power - collateral, 2)
                lots_by_capital = int(total_power // LOT_CAPITAL)
                debit_est = CASH_PER_LOT_REQUIRED
                if strikes_dict:
                    try:
                        lp = self._get_ltp(EXCHANGE_NFO, *self._fetch_symbol_and_token(strikes_dict["ce_buy_strike"], "ce", strikes_dict["buy_expiry"])) or 300
                        pp = self._get_ltp(EXCHANGE_NFO, *self._fetch_symbol_and_token(strikes_dict["pe_buy_strike"], "pe", strikes_dict["buy_expiry"])) or 300
                        wp = self._get_ltp(EXCHANGE_NFO, *self._fetch_symbol_and_token(strikes_dict["pe_wing_strike"], "pe", strikes_dict["buy_expiry"])) or 50
                        ls = self._get_ltp(EXCHANGE_NFO, *self._fetch_symbol_and_token(strikes_dict["ce_sell_strike"], "ce", strikes_dict["sell_expiry"])) or 100
                        ps = self._get_ltp(EXCHANGE_NFO, *self._fetch_symbol_and_token(strikes_dict["pe_sell_strike"], "pe", strikes_dict["sell_expiry"])) or 100
                        market_debit = (lp + pp + wp - ls - ps) * LOT_SIZE
                        market_debit *= 1.15
                        debit_est = max(CASH_PER_LOT_REQUIRED, market_debit)
                        logger.info(f"Debit Estimate: {debit_est:,.0f} Rs/lot (Market: {market_debit:,.0f})")
                    except: pass
                lots_by_cash = int(pure_cash // debit_est)
                lots = max(1, min(lots_by_capital, lots_by_cash))
                logger.info(f"Lot sizing: Power={total_power:,.0f} | Cash={pure_cash:,.0f} | Cap={lots_by_capital} | Cash={lots_by_cash} | Final={lots}")
                return lots
            except Exception as e: handle_exception(e); sleep(1); _reset_counters()

    def _reconcile_positions(self):
        """
        Compare broker position data against local state on restart.
        Alerts if quantities don't match — does not auto-correct.
        """
        try:
            resp = self.obj.position()
            data = (resp or {}).get('data') or []
        except Exception as e:
            logger.warning(f"Position reconciliation: broker call failed ({e}). Proceeding without check.")
            return

        pos = {str(p['symboltoken']): int(p.get('netqty', 0))
               for p in data if p.get('symboltoken')}
        lots = self.state.lots

        expected = {
            str(self.state.ce_sell_token): -lots * LOT_SIZE,
            str(self.state.pe_sell_token): -lots * LOT_SIZE,
            str(self.state.ce_buy_token):  +lots * LOT_SIZE,
            str(self.state.pe_buy_token):  +lots * LOT_SIZE,
        }
        if self.state.wings_enabled and self.state.pe_wing_token:
            expected[str(self.state.pe_wing_token)] = +lots * LOT_SIZE
        if self.state.emer_active and self.state.emer_token:
            expected[str(self.state.emer_token)] = +lots * LOT_SIZE

        mismatches = [
            f"token={tok}: expected {exp:+d}, broker={pos.get(tok, 0):+d}"
            for tok, exp in expected.items()
            if pos.get(tok, 0) != exp
        ]

        if mismatches:
            msg = ("*Athena* ALERT: Position mismatch on restart — "
                   + " | ".join(mismatches)
                   + " — verify manually before trading continues.")
            logger.error(f"Position reconciliation FAILED: {mismatches}")
            slack_bot_sendtext(msg, SLACK_ERRORS_CHANNEL)
        else:
            logger.info(f"Position reconciliation OK — {len(expected)} legs match broker.")

    def _execute_entry(self, strikes_dict, spot, vix):
        logger.info("=== EXECUTING ENTRY ===")
        target_lots = self._calculate_lots(strikes_dict)
        self.state.wings_enabled = ENABLE_SAFETY_WINGS
        
        # 1. Determine batches based on QTY_FREEZE
        l_limit = self._qty_freeze // LOT_SIZE
        batches = []
        rem = target_lots
        while rem > 0:
            c = min(rem, l_limit)
            batches.append(c); rem -= c
            
        # 2. Pre-fetch core leg details
        legs = {}
        for side in ["ce", "pe"]:
            for t in ["buy", "sell"]:
                exp = strikes_dict["buy_expiry"] if t == "buy" else strikes_dict["sell_expiry"]
                stk = strikes_dict[f"{side}_{t}_strike"]
                sym, tok = self._fetch_symbol_and_token(stk, side, exp)
                if not sym: return False
                legs[f"{side}_{t}"] = (sym, tok, stk)

        fill_data = {k: {"qty": 0, "val": 0.0} for k in legs.keys()}
        total_actual_lots = 0

        # 3. Interleaved Batch Loop for Core Legs
        for b_idx, b_lots in enumerate(batches):
            logger.info(f"Processing core batch {b_idx + 1}/{len(batches)}: {b_lots} lots")
            
            # Fire all 4 core legs in a rapid burst
            batch_order_receipts = {}
            
            # Sub-Burst A: Longs (Monthly) first for margin collateral
            for side in ["ce", "pe"]:
                k = f"{side}_buy"; sym, tok, stk = legs[k]
                oids = self._place_order("BUY", sym, tok, b_lots)
                batch_order_receipts[k] = (oids, tok, sym)
                
            # Sub-Burst B: Shorts (Weekly) immediately after
            for side in ["ce", "pe"]:
                k = f"{side}_sell"; sym, tok, stk = legs[k]
                oids = self._place_order("SELL", sym, tok, b_lots)
                batch_order_receipts[k] = (oids, tok, sym)
                
            # Now Verify all 4 legs of this batch (Wait up to 10s for the whole group)
            b_filled_min = b_lots
            for k, (oids, tok, sym) in batch_order_receipts.items():
                px, f_lots, ft = self._fetch_order_details(oids, tok, sym, b_lots)
                fill_data[k]["qty"] += (f_lots * LOT_SIZE)
                fill_data[k]["val"] += (px * f_lots * LOT_SIZE)
                if f_lots < b_filled_min: b_filled_min = f_lots
                
            total_actual_lots += b_filled_min
            if b_filled_min < b_lots:
                logger.warning(f"Batch {b_idx + 1} partial fill ({b_filled_min}/{b_lots}). Stopping entry.")
                break
            
        # 4. Universal Orphan Leg Cleanup (Balanced Strategy)
        for k in ["ce_buy", "pe_buy", "ce_sell", "pe_sell"]:
            actual_qty = fill_data[k]["qty"]
            expected_qty = total_actual_lots * LOT_SIZE
            if actual_qty > expected_qty:
                excess_lots = int((actual_qty - expected_qty) // LOT_SIZE)
                if excess_lots > 0:
                    sym, tok, stk = legs[k]
                    # If it was a BUY leg, SELL to close. If SELL, BUY to close.
                    tx_type = "SELL" if "_buy" in k else "BUY"
                    logger.warning(f"Excess quantity in {k}: {excess_lots} lots. Liquidating ({tx_type}).")
                    slack_bot_sendtext(f"⚠️ *Athena*: Excess quantity in {sym}. Liquidating {excess_lots} lots.", SLACK_ERRORS_CHANNEL)
                    self._place_order(tx_type, sym, tok, excess_lots)

        if total_actual_lots == 0:
            logger.error("No core lots filled successfully. Aborting entry."); clear_trade_fields(self.state); save_state(self.state); return False


        # Store core leg state (Averages)
        for k in ["ce_buy", "pe_buy", "ce_sell", "pe_sell"]:
            sym, tok, stk = legs[k]
            setattr(self.state, f"{k}_strike", stk); setattr(self.state, f"{k}_token", tok)
            setattr(self.state, f"{k}_symbol", sym)
            avg = round(fill_data[k]["val"] / fill_data[k]["qty"], 2) if fill_data[k]["qty"] > 0 else 0.0
            setattr(self.state, f"{k}_entry", avg)

        # 4. Buy PE Wings LAST for the total quantity
        if ENABLE_SAFETY_WINGS:
            k = "pe_wing"; stk = strikes_dict[f"{k}_strike"]; exp = strikes_dict["buy_expiry"]
            sym, tok = self._fetch_symbol_and_token(stk, "pe", exp)
            if sym:
                logger.info(f"Buying PE Wings: {total_actual_lots} lots")
                oids = self._place_order("BUY", sym, tok, total_actual_lots)
                fill, filled_q, ft = self._fetch_order_details(oids, tok, sym, total_actual_lots)
                setattr(self.state, f"{k}_strike", stk); setattr(self.state, f"{k}_token", tok)
                setattr(self.state, f"{k}_symbol", sym); setattr(self.state, f"{k}_entry", fill)
                if filled_q < total_actual_lots: total_actual_lots = filled_q

        self.state.status = 'in_trade'; self.state.lots = total_actual_lots; self.state.entry_time = datetime.now().isoformat()
        self._summary.update({
            'traded':     True,
            'lots':       total_actual_lots,
            'entry_time': datetime.now().strftime('%H:%M'),
            'spot_entry': round(spot, 2),
        })
        sell_exp_dt = strikes_dict['sell_expiry']; exit_day = self._last_trading_day_before(sell_exp_dt)
        self.state.exit_timestamp = datetime.combine(exit_day, self._exit_time).isoformat()
        self.state.entry_spot = spot; self.state.entry_vix = vix; self.state.sell_expiry = strikes_dict['sell_expiry'].isoformat()
        self.state.buy_expiry = strikes_dict['buy_expiry'].isoformat()
        net = (self.state.ce_buy_entry + self.state.pe_buy_entry) - (self.state.ce_sell_entry + self.state.pe_sell_entry)
        if ENABLE_SAFETY_WINGS: net += self.state.pe_wing_entry
        self.state.net_debit = round(net, 2); save_state(self.state)
        msg = f"*Athena* ENTRY | Lots: {self.state.lots} | Spot: {spot:.2f} | Net Debit: {self.state.net_debit:.1f}"
        slack_bot_sendtext(msg, SLACK_TRADE_ALERTS); return True

    def _execute_exit(self, reason):
        logger.info(f"=== EXECUTING EXIT: {reason.upper()} ===")
        self._close_emer_if_active()
        lots = self.state.lots; self.state.status = 'exiting'; save_state(self.state)
        
        # 1. Define all exit legs
        # Sell legs (shorts) are closed with BUY orders
        exit_legs = [
            ('ce_sell', self.state.ce_sell_symbol, self.state.ce_sell_token, 'BUY'),
            ('pe_sell', self.state.pe_sell_symbol, self.state.pe_sell_token, 'BUY')
        ]
        # Buy legs (longs) are closed with SELL orders
        exit_legs += [
            ('ce_buy', self.state.ce_buy_symbol, self.state.ce_buy_token, 'SELL'),
            ('pe_buy', self.state.pe_buy_symbol, self.state.pe_buy_token, 'SELL')
        ]
        if self.state.wings_enabled:
            exit_legs.append(('pe_wing', self.state.pe_wing_symbol, self.state.pe_wing_token, 'SELL'))
            
        # 2. Fire all market orders in a fast burst
        order_receipts = {}
        for key, sym, tok, side in exit_legs:
            logger.info(f"Exiting {key}: Firing {side} for {lots} lots...")
            oids = self._place_order(side, sym, tok, lots)
            order_receipts[key] = (oids, tok, sym)
            
        # 3. Fetch fill details for PnL report (the 10s wait happens here, after all orders are in)
        exit_fills = {}
        for key, (oids, tok, sym) in order_receipts.items():
            fill, q, ft = self._fetch_order_details(oids, tok, sym, lots)
            exit_fills[key] = fill
            
        # 4. Final P&L Calculation
        pl_pts = round(
            (exit_fills['ce_buy'] - self.state.ce_buy_entry) + 
            (exit_fills['pe_buy'] - self.state.pe_buy_entry) + 
            (self.state.ce_sell_entry - exit_fills['ce_sell']) + 
            (self.state.pe_sell_entry - exit_fills['pe_sell']), 2
        )
        if self.state.wings_enabled:
            pl_pts = round(pl_pts + (exit_fills['pe_wing'] - self.state.pe_wing_entry), 2)
            
        pl_pts = round(pl_pts + self.state.running_realised_pl, 2)
        pl_rs_per_lot = round(pl_pts * LOT_SIZE, 2)
        
        # Log and Alert
        self._append_trade_log_row(exit_reason=reason, exit_fills=exit_fills)
        msg = f"*Athena* EXIT {reason.upper()} | Lots: {lots} | Final P&L: {pl_pts:+.1f} pts ({pl_rs_per_lot:+,.0f} Rs/lot)"
        slack_bot_sendtext(msg, SLACK_TRADE_ALERTS)
        
        self.feed.unsubscribe_all_options()
        self._summary.update({
            'exit_time':    datetime.now().strftime('%H:%M'),
            'exit_reason':  reason,
            'pnl_pts':      pl_pts,
            'pnl_rs':       round(pl_pts * lots * LOT_SIZE, 2),
            'peak_pnl_pts': self.state.max_unrealised_pl,
            'spot_exit':    round(self.feed.get_ltp(NIFTY_INDEX_TOKEN) or 0, 2),
        })
        clear_trade_fields(self.state); save_state(self.state); return True

    def _poll_prices(self):
        if not self.feed.is_connected():
            return self._poll_prices_rest()
        prices = {'spot': self.feed.get_ltp(NIFTY_INDEX_TOKEN)}
        keys = ['ce_sell', 'pe_sell', 'ce_buy', 'pe_buy']
        if self.state.wings_enabled: keys += ['pe_wing']
        if self.state.emer_active: keys += ['emer']
        for key in keys:
            tok = getattr(self.state, f"{key}_token")
            ltp = self.feed.get_ltp(tok)
            if ltp is None: ltp = getattr(self.state, f"last_{key}_ltp", None) or getattr(self.state, f"{key}_entry", 0.0)
            prices[key] = ltp
        return prices

    def _poll_prices_rest(self):
        prices = {'spot': self._get_ltp(EXCHANGE_NSE, 'NIFTY 50', NIFTY_INDEX_TOKEN)}
        keys = ['ce_sell', 'pe_sell', 'ce_buy', 'pe_buy']
        if self.state.wings_enabled: keys += ['pe_wing']
        if self.state.emer_active: keys += ['emer']
        for key in keys:
            sym = getattr(self.state, f"{key}_symbol"); tok = getattr(self.state, f"{key}_token"); ltp = self._get_ltp(EXCHANGE_NFO, sym, tok)
            if ltp is None: ltp = getattr(self.state, f"last_{key}_ltp", None) or getattr(self.state, f"{key}_entry", 0.0)
            prices[key] = ltp
        return prices

    def _get_log_filepath(self):
        os.makedirs(TRADE_LOGS_DIR, exist_ok=True); entry_dt = datetime.fromisoformat(self.state.entry_time)
        return os.path.join(TRADE_LOGS_DIR, f"trade_{entry_dt.strftime('%Y-%m-%d_%H%M')}.csv")

    def _append_trade_log_row(self, exit_reason=None, exit_fills=None, prices=None):
        if self.state.status not in ('in_trade', 'exiting'): return
        p = prices if prices is not None else self._poll_prices()
        if exit_fills:
            for k, v in exit_fills.items(): p[k] = v
        now = datetime.now()
        try:
            pl_pts = round((p['ce_buy'] - self.state.ce_buy_entry) + (p['pe_buy'] - self.state.pe_buy_entry) + (self.state.ce_sell_entry - p['ce_sell']) + (self.state.pe_sell_entry - p['pe_sell']), 2)
            if self.state.wings_enabled: pl_pts = round(pl_pts + (p['pe_wing'] - self.state.pe_wing_entry), 2)
            if self.state.emer_active and 'emer' in p: pl_pts = round(pl_pts + (p['emer'] - self.state.emer_entry), 2)
            pl_pts = round(pl_pts + self.state.running_realised_pl, 2)
        except: pl_pts = 0.0
        row = {'time_stamp': now.strftime('%Y-%m-%d %H:%M:%S'), 'spot': p.get('spot'), 'ce_sell_ltp': p.get('ce_sell'), 'pe_sell_ltp': p.get('pe_sell'), 'ce_buy_ltp': p.get('ce_buy'), 'pe_buy_ltp': p.get('pe_buy'), 'unrealised_pl': round(pl_pts, 2), 'exit_reason': exit_reason}
        if self.state.wings_enabled: row['pe_wing_ltp'] = p.get('pe_wing')
        if self.state.emer_active: row['emer_ltp'] = p.get('emer')
        log_file = self._get_log_filepath(); df = pd.DataFrame([row]); df.to_csv(log_file, mode='a', index=False, header=not os.path.exists(log_file))

    def _send_trade_update(self, prices=None):
        if self.state.status != 'in_trade': return
        p = prices if prices is not None else self._poll_prices()
        try:
            pl_pts = round((p['ce_buy'] - self.state.ce_buy_entry) + (p['pe_buy'] - self.state.pe_buy_entry) + (self.state.ce_sell_entry - p['ce_sell']) + (self.state.pe_sell_entry - p['pe_sell']), 2)
            if self.state.wings_enabled: pl_pts = round(pl_pts + (p['pe_wing'] - self.state.pe_wing_entry), 2)
            if self.state.emer_active and 'emer' in p: pl_pts = round(pl_pts + (p['emer'] - self.state.emer_entry), 2)
            pl_pts = round(pl_pts + self.state.running_realised_pl, 2)
            if pl_pts > self.state.max_unrealised_pl: self.state.max_unrealised_pl = pl_pts
            self.state.last_spot = p.get('spot'); self.state.last_ce_sell_ltp = p.get('ce_sell'); self.state.last_pe_sell_ltp = p.get('pe_sell'); self.state.last_ce_buy_ltp = p.get('ce_buy'); self.state.last_pe_buy_ltp = p.get('pe_buy')
            if self.state.wings_enabled: self.state.last_pe_wing_ltp = p.get('pe_wing')
            if self.state.emer_active: self.state.last_emer_ltp = p.get('emer')
            save_state(self.state)
        except: return
        pl_rs_per_lot = round(pl_pts * LOT_SIZE, 2)
        msg = f"*Athena* UPDATE | Lots: {self.state.lots} | Spot: {p['spot']:.2f} | P&L: {pl_pts:+.1f} pts ({pl_rs_per_lot:+,.0f} Rs/lot) | Peak: {self.state.max_unrealised_pl:+.1f} pts"
        logger.info(msg.replace('*', '')); slack_bot_sendtext(msg, SLACK_TRADE_UPDATES)

    def _close_emer_if_active(self):
        if not self.state.emer_active:
            return
        oids = self._place_order('SELL', self.state.emer_symbol, self.state.emer_token, self.state.lots)
        fill, q, ft = self._fetch_order_details(oids, self.state.emer_token, self.state.emer_symbol, self.state.lots)
        if fill > 0:
            realised = round(fill - self.state.emer_entry, 2)
            self.state.running_realised_pl += realised
            slack_bot_sendtext(
                f"🏁 *Athena EMERGENCY*: Closed Parachute CE {self.state.emer_strike} @ {fill:.1f} | Realised: {realised:+.1f} pts",
                SLACK_TRADE_ALERTS
            )
            self.state.emer_active = False
            self.state.emer_strike = self.state.emer_symbol = self.state.emer_token = None
            self.state.emer_entry = 0.0
            save_state(self.state)

    def _manage_emergency_hedge(self, current_spot, force=False):
        if not ENABLE_EMERGENCY_HEDGE: return
        if datetime.now().time() < time(9, 16): return
        if not self.state.emer_active and (force or self.state.emer_attempts < EMERGENCY_MAX_ATTEMPTS):
            if force or current_spot >= (self.state.ce_sell_strike + EMERGENCY_TRIGGER_OFFSET):
                buy_exp = datetime.strptime(self.state.buy_expiry, '%Y-%m-%d').date()
                vix = self.feed.get_ltp(VIX_TOKEN) or self._get_ltp(EXCHANGE_NSE, 'INDIA VIX', VIX_TOKEN) or 18.0
                stk = self._find_delta_strike(current_spot, vix, buy_exp, EMERGENCY_HEDGE_DELTA, 'ce')
                if stk:
                    sym, tok = self._fetch_symbol_and_token(stk, 'ce', buy_exp)
                    if sym:
                        oids = self._place_order('BUY', sym, tok, self.state.lots); fill, q, ft = self._fetch_order_details(oids, tok, sym, self.state.lots)
                        self.state.emer_attempts += 1
                        if fill > 0:
                            self.state.emer_active = True; self.state.emer_strike = stk; self.state.emer_symbol = sym; self.state.emer_token = tok; self.state.emer_entry = fill; save_state(self.state)
                            self.feed.subscribe_options([tok])
                            slack_bot_sendtext(f"🪂 *Athena EMERGENCY*: Bought Parachute CE {stk} @ {fill:.1f}", SLACK_TRADE_ALERTS)
                        else:
                            save_state(self.state)
                            logger.warning(f"Emer hedge zero fill. Attempt {self.state.emer_attempts}/{EMERGENCY_MAX_ATTEMPTS}.")
                            slack_bot_sendtext(f"⚠️ *Athena*: Emer hedge zero fill (attempt {self.state.emer_attempts}/{EMERGENCY_MAX_ATTEMPTS}).", SLACK_ERRORS_CHANNEL)
        elif self.state.emer_active:
            if force or current_spot <= (self.state.ce_sell_strike + EMERGENCY_EXIT_OFFSET):
                self._close_emer_if_active()

    def _check_slack_commands(self):
        """
        Check for persistent Slack command flags during the live trade.
        Handles EXIT (liquidate), KILL (halt immediately), and ATHENA_PARACHUTE:enter/exit.
        """
        flag_path = os.path.join(REPO_ROOT, 'data', 'SLACK_COMMAND.flag')
        if os.path.exists(flag_path):
            try:
                with open(flag_path, 'r') as f:
                    command = f.read().strip()

                if command == "EXIT":
                    msg = "⚠️ *Athena*: Slack `Exit Trade` detected. Liquidating..."
                    logger.critical(msg.replace('*', ''))
                    slack_bot_sendtext(msg, SLACK_TRADE_ALERTS)
                    if self.state.status == 'in_trade':
                        self._execute_exit(reason='slack_exit')
                    raise Exception("Session terminated by Slack !exit command.")

                elif command == "KILL":
                    msg = "🚨 *Athena*: Slack `Kill Switch` detected. Dropping control immediately."
                    logger.critical(msg.replace('*', ''))
                    slack_bot_sendtext(msg, SLACK_TRADE_ALERTS)
                    raise Exception("Session terminated by Slack !kill command.")

                elif command.startswith("ATHENA_PARACHUTE:"):
                    action = command.split(":")[1].lower()
                    if action in ('enter', 'exit'):
                        self._pending_parachute = action
                        os.remove(flag_path)
                        logger.info(f"Manual parachute action queued: {action}.")

                # If command == "DISABLE", we do nothing inside the loop.
                # Leto will catch it on the next startup.
            except Exception as e:
                if "Session terminated" in str(e): raise
                logger.error(f"Error reading slack command flag: {e}")

    def run(self):
        """
        Main entry loop called by leto.py.
        Handles entry checks, position monitoring, and pre-expiry exits.
        Returns True to Leto if a VIX breach occurred at entry (requesting re-route),
        otherwise False.
        """
        logger.info("=== Athena run loop started ===")
        feed_token = self.obj.getfeedToken()
        self._order_watcher.start(self.auth_token, api_key, user_name, feed_token)
        logger.info("Order fill WS daemon started.")
        try:
            self.feed.start(
                self.auth_token, api_key, user_name, feed_token,
                startup_tokens=[(EXCHANGE_NSE_CM, NIFTY_INDEX_TOKEN), (EXCHANGE_NSE_CM, VIX_TOKEN)],
                alert_callback=lambda msg: slack_bot_sendtext(f"⚠️ *Athena*: {msg}", SLACK_ERRORS_CHANNEL)
            )
            nifty_ltp = self.feed.get_ltp(NIFTY_INDEX_TOKEN)
            vix_ltp   = self.feed.get_ltp(VIX_TOKEN)
            slack_bot_sendtext(
                (f"✅ *Athena*: WebSocket LTP feed live. Nifty: {nifty_ltp:.2f}  VIX: {vix_ltp:.2f}"
                 if nifty_ltp and vix_ltp
                 else "✅ *Athena*: WebSocket LTP feed live. LTPs not yet available."),
                SLACK_TRADEBOT_CHANNEL)
        except Exception as e:
            logger.warning(f"WS LTP feed failed to start: {e}. Using REST fallback.")
            slack_bot_sendtext("⚠️ *Athena*: WS LTP feed failed to start — REST fallback active.", SLACK_ERRORS_CHANNEL)
        if self.state.status == 'in_trade':
            self._reconcile_positions()
            _entry_dt = datetime.fromisoformat(self.state.entry_time) if self.state.entry_time else None
            self._summary.update({
                'traded':     True,
                'lots':       self.state.lots,
                'entry_time': _entry_dt.strftime('%d %b %H:%M') if _entry_dt else '?',
                'spot_entry': self.state.entry_spot,
            })
        if self.state.status == 'in_trade' and self.feed.is_connected():
            leg_tokens = [
                self.state.ce_sell_token, self.state.pe_sell_token,
                self.state.ce_buy_token,  self.state.pe_buy_token,
            ]
            if self.state.wings_enabled and self.state.pe_wing_token:
                leg_tokens.append(self.state.pe_wing_token)
            if self.state.emer_active and self.state.emer_token:
                leg_tokens.append(self.state.emer_token)
            self.feed.subscribe_options([t for t in leg_tokens if t])
        while True:
            self._check_slack_commands()
            now = datetime.now()
            if now.time() >= self._closing_time: break
            if self.state.status == 'idle':
                sell_exp, _ = self._select_expiries()
                if sell_exp:
                    entry_day = self._last_trading_day_before(sell_exp); is_entry_day = (now.date() == entry_day)
                    if FORCE_ENTRY: is_entry_day = True
                    if is_entry_day and now.time() >= self._entry_time:
                        spot = self.feed.get_ltp(NIFTY_INDEX_TOKEN) or self._get_ltp(EXCHANGE_NSE, 'NIFTY 50', NIFTY_INDEX_TOKEN)
                        vix  = self.feed.get_ltp(VIX_TOKEN) or self._get_ltp(EXCHANGE_NSE, 'INDIA VIX', VIX_TOKEN)
                        if vix and not (VIX_FILTER_LOW <= vix <= VIX_FILTER_HIGH):
                            self._summary['no_trade_reason'] = 'VIX out of range at entry'
                            return True, self._summary
                        if spot and vix:
                            strikes = self._select_all_strikes(spot, vix)
                            if strikes:
                                if not self._execute_entry(strikes, spot, vix):
                                    logger.error("Entry failed. Standing down to prevent accidental retries.")
                                    self._summary['no_trade_reason'] = 'Entry failed'
                                    return False, self._summary
                                leg_tokens = [
                                    self.state.ce_sell_token, self.state.pe_sell_token,
                                    self.state.ce_buy_token,  self.state.pe_buy_token,
                                ]
                                if self.state.wings_enabled and self.state.pe_wing_token:
                                    leg_tokens.append(self.state.pe_wing_token)
                                self.feed.subscribe_options([t for t in leg_tokens if t])
                                self._update_elapsed = 0.0
            if self.state.status == 'in_trade' and self.state.exit_timestamp:
                if now >= datetime.fromisoformat(self.state.exit_timestamp):
                    self._execute_exit(reason='pre_expiry')
            if self.state.status == 'in_trade':
                try:
                    prices = self._poll_prices(); spot = prices.get('spot')
                    if spot:
                        self._manage_emergency_hedge(spot)
                        pending = getattr(self, '_pending_parachute', None)
                        if pending is not None:
                            self._pending_parachute = None
                            if pending == 'enter':
                                if self.state.emer_active:
                                    slack_bot_sendtext("⚠️ *Athena*: Manual parachute entry ignored — parachute already active.", SLACK_ERRORS_CHANNEL)
                                else:
                                    self._manage_emergency_hedge(spot, force=True)
                            elif pending == 'exit':
                                if not self.state.emer_active:
                                    slack_bot_sendtext("⚠️ *Athena*: Manual parachute exit ignored — no active parachute.", SLACK_ERRORS_CHANNEL)
                                else:
                                    self._close_emer_if_active()
                        if self._update_elapsed >= TRADE_UPDATE_INTERVAL:
                            self._append_trade_log_row(prices=prices)
                            self._send_trade_update(prices=prices)
                            self._update_elapsed = 0.0
                except Exception as e: handle_exception(e)
                if self.feed.is_connected():
                    sleep(0.5)
                    self._update_elapsed += 0.5
                else:
                    sleep(TRADE_UPDATE_INTERVAL)
                    _reset_counters()
                    self._update_elapsed = TRADE_UPDATE_INTERVAL
            else:
                sleep(60)
                _reset_counters()
        if self.state.status == 'in_trade':
            try: self._send_trade_update()
            except: pass
            slack_bot_sendtext(
                f"*Athena*: Market close with open trade. Holding overnight. "
                f"Sell expiry: {self.state.sell_expiry}.",
                SLACK_TRADE_UPDATES)
            try:
                p = self._poll_prices()
                _pnl_pts = round(
                    (p['ce_buy'] - self.state.ce_buy_entry) +
                    (p['pe_buy'] - self.state.pe_buy_entry) +
                    (self.state.ce_sell_entry - p['ce_sell']) +
                    (self.state.pe_sell_entry - p['pe_sell']), 2)
                if self.state.wings_enabled and p.get('pe_wing'):
                    _pnl_pts = round(_pnl_pts + (p['pe_wing'] - self.state.pe_wing_entry), 2)
                if self.state.emer_active and p.get('emer'):
                    _pnl_pts = round(_pnl_pts + (p['emer'] - self.state.emer_entry), 2)
                _pnl_pts = round(_pnl_pts + self.state.running_realised_pl, 2)
                _pnl_rs  = round(_pnl_pts * self.state.lots * LOT_SIZE, 2)
                _spot    = p.get('spot')
            except Exception:
                _pnl_pts = None
                _pnl_rs  = None
                _spot    = None
            self._summary.update({
                'exit_reason':  'overnight_hold',
                'exit_time':    None,
                'peak_pnl_pts': self.state.max_unrealised_pl,
                'pnl_pts':      _pnl_pts,
                'pnl_rs':       _pnl_rs,
                'spot_exit':    round(_spot, 2) if _spot else None,
            })
        else:
            slack_bot_sendtext("*Athena*: Standing down for the day. No active positions.", SLACK_TRADE_UPDATES)
        logger.info("Market closed. Athena finished for the day.")
        return False, self._summary
