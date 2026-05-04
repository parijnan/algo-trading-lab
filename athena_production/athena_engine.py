"""
athena_engine.py — Athena Production Main Entry Point
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
    NIFTY_INDEX_TOKEN, VIX_TOKEN,
    MARKET_OPEN, MARKET_CLOSE,
    ENTRY_TIME, ELM_EXIT_TIME,
    VIX_FILTER_LOW, VIX_FILTER_HIGH,
    TARGET_DELTA_SOLD, SAFETY_WING_DELTA, ENABLE_SAFETY_WINGS,
    ENABLE_EMERGENCY_HEDGE, EMERGENCY_HEDGE_DELTA, 
    EMERGENCY_TRIGGER_OFFSET, EMERGENCY_EXIT_OFFSET, EMERGENCY_MAX_ATTEMPTS,
    STRIKE_STEP, BUY_LEG_MIN_DTE, LOT_SIZE, LOT_COUNT,
    LOT_CALC, LOT_CAPITAL, CASH_PER_LOT_REQUIRED,
    DRY_RUN, FORCE_ENTRY, TRADE_UPDATE_INTERVAL, QTY_FREEZE,
    EXCHANGE_NSE, EXCHANGE_NFO, FO_EXCHANGE_SEGMENT,
    SLACK_TRADE_ALERTS, SLACK_TRADE_UPDATES,
    DATA_DIR, TRADE_LOGS_DIR, RISK_FREE_RATE
)
from state import AthenaState, load_state, save_state, clear_trade_fields
from functions import (
    slack_bot_sendtext, handle_exception, 
    _increment_rms_poll, _increment_order_book_poll, _increment_ltp_poll,
    _increment_order, _reset_counters
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
        self._qty_freeze    = QTY_FREEZE
        
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
        _increment_ltp_poll()
        try:
            return float(self.obj.ltpData(exchange, symbol, token)['data']['ltp'])
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
                "variety": "NORMAL", "tradingsymbol": symbol, "symboltoken": token,
                "transactiontype": transaction_type, "exchange": FO_EXCHANGE_SEGMENT,
                "ordertype": "MARKET", "producttype": "CARRYFORWARD",
                "duration": "DAY", "quantity": str(int(lot_chunk * LOT_SIZE)),
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
                    handle_exception(e); sleep(1)
        return orderid_list

    def _fetch_order_details(self, orderid_list, token, symbol):
        if DRY_RUN:
            fill = self._get_ltp(EXCHANGE_NFO, symbol, token) or 0.0
            return fill, 0, datetime.now()
        total_qty = 0; total_val = 0.0; fill_time = datetime.now()
        try:
            _increment_order_book_poll()
            book = self.obj.orderBook()['data']
            for oid in orderid_list:
                for order in book:
                    if order['orderid'] == oid:
                        q = int(order['filledshares']); p = float(order['averageprice'])
                        total_qty += q; total_val += (p * q)
                        ft = datetime.strptime(order['updatetime'], '%d-%b-%Y %H:%M:%S')
                        if ft > fill_time: fill_time = ft
            avg_price = round(total_val / total_qty, 2) if total_qty > 0 else 0.0
            filled_lots = int(total_qty // LOT_SIZE)
            return avg_price, filled_lots, fill_time
        except Exception as e:
            handle_exception(e); return 0.0, 0, datetime.now()

    def _calculate_lots(self, strikes_dict=None):
        if not LOT_CALC: return LOT_COUNT
        while True:
            try:
                _increment_rms_poll()
                rms = self.obj.rmsLimit()["data"]
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
            except Exception as e: handle_exception(e); sleep(1)

    def _execute_entry(self, strikes_dict, spot, vix):
        logger.info("=== EXECUTING ENTRY ===")
        target_lots = self._calculate_lots(strikes_dict)
        self.state.wings_enabled = ENABLE_SAFETY_WINGS
        actual_lots = target_lots
        for side in ["ce", "pe"]:
            key = f"{side}_buy"; strike = strikes_dict[f"{key}_strike"]; exp = strikes_dict["buy_expiry"]
            sym, tok = self._fetch_symbol_and_token(strike, side, exp)
            if not sym: return False
            oids = self._place_order("BUY", sym, tok, actual_lots)
            fill, filled_q, ft = self._fetch_order_details(oids, tok, sym)
            setattr(self.state, f"{key}_strike", strike); setattr(self.state, f"{key}_token", tok)
            setattr(self.state, f"{key}_symbol", sym); setattr(self.state, f"{key}_entry", fill)
            if filled_q < actual_lots: actual_lots = filled_q
        if actual_lots == 0:
            logger.error("No longs filled. Aborting."); clear_trade_fields(self.state); save_state(self.state); return False
        for side in ["ce", "pe"]:
            key = f"{side}_sell"; strike = strikes_dict[f"{key}_strike"]; exp = strikes_dict["sell_expiry"]
            sym, tok = self._fetch_symbol_and_token(strike, side, exp)
            oids = self._place_order("SELL", sym, tok, actual_lots)
            fill, filled_q, ft = self._fetch_order_details(oids, tok, sym)
            setattr(self.state, f"{key}_strike", strike); setattr(self.state, f"{key}_token", tok)
            setattr(self.state, f"{key}_symbol", sym); setattr(self.state, f"{key}_entry", fill)
            if filled_q < actual_lots: actual_lots = filled_q
        if ENABLE_SAFETY_WINGS:
            key = "pe_wing"; strike = strikes_dict[f"{key}_strike"]; exp = strikes_dict["buy_expiry"]
            sym, tok = self._fetch_symbol_and_token(strike, "pe", exp)
            if sym:
                oids = self._place_order("BUY", sym, tok, actual_lots)
                fill, filled_q, ft = self._fetch_order_details(oids, tok, sym)
                setattr(self.state, f"{key}_strike", strike); setattr(self.state, f"{key}_token", tok)
                setattr(self.state, f"{key}_symbol", sym); setattr(self.state, f"{key}_entry", fill)
                if filled_q < actual_lots: actual_lots = filled_q
        self.state.status = 'in_trade'; self.state.lots = actual_lots; self.state.entry_time = datetime.now().isoformat()
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
        lots = self.state.lots; self.state.status = 'exiting'; save_state(self.state)
        sell_legs = [('ce_sell', self.state.ce_sell_symbol, self.state.ce_sell_token), ('pe_sell', self.state.pe_sell_symbol, self.state.pe_sell_token)]
        exit_fills = {}
        for key, sym, tok in sell_legs:
            oids = self._place_order('BUY', sym, tok, lots); fill, q, ft = self._fetch_order_details(oids, tok, sym); exit_fills[key] = fill
        buy_keys = ['ce_buy', 'pe_buy']
        if self.state.wings_enabled: buy_keys += ['pe_wing']
        for key in buy_keys:
            sym = getattr(self.state, f"{key}_symbol"); tok = getattr(self.state, f"{key}_token")
            oids = self._place_order('SELL', sym, tok, lots); fill, q, ft = self._fetch_order_details(oids, tok, sym); exit_fills[key] = fill
        pl_pts = round((exit_fills['ce_buy'] - self.state.ce_buy_entry) + (exit_fills['pe_buy'] - self.state.pe_buy_entry) + (self.state.ce_sell_entry - exit_fills['ce_sell']) + (self.state.pe_sell_entry - exit_fills['pe_sell']), 2)
        if self.state.wings_enabled: pl_pts = round(pl_pts + (exit_fills['pe_wing'] - self.state.pe_wing_entry), 2)
        pl_pts = round(pl_pts + self.state.running_realised_pl, 2); pl_rs_per_lot = round(pl_pts * LOT_SIZE, 2)
        self._append_trade_log_row(exit_reason=reason, exit_fills=exit_fills)
        msg = f"*Athena* EXIT {reason.upper()} | Lots: {lots} | Final P&L: {pl_pts:+.1f} pts ({pl_rs_per_lot:+,.0f} Rs/lot)"
        slack_bot_sendtext(msg, SLACK_TRADE_ALERTS); clear_trade_fields(self.state); save_state(self.state); return True

    def _poll_prices(self):
        prices = {'spot': self._get_ltp(EXCHANGE_NSE, 'NIFTY 50', NIFTY_INDEX_TOKEN)}
        keys = ['ce_sell', 'pe_sell', 'ce_buy', 'pe_buy']
        if self.state.wings_enabled: keys += ['pe_wing']
        if self.state.emer_active: keys += ['emer']
        for key in keys:
            sym = getattr(self.state, f"{key}_symbol"); tok = getattr(self.state, f"{key}_token"); ltp = self._get_ltp(EXCHANGE_NFO, sym, tok)
            if ltp is None: ltp = getattr(self.state, f"last_{key}_ltp") or getattr(self.state, f"{key}_entry")
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

    def _manage_emergency_hedge(self, current_spot):
        if not ENABLE_EMERGENCY_HEDGE: return
        if not self.state.emer_active and self.state.emer_attempts < EMERGENCY_MAX_ATTEMPTS:
            if current_spot >= (self.state.ce_sell_strike + EMERGENCY_TRIGGER_OFFSET):
                buy_exp = datetime.strptime(self.state.buy_expiry, '%Y-%m-%d').date(); vix = self._get_ltp(EXCHANGE_NSE, 'INDIA VIX', VIX_TOKEN) or 18.0
                stk = self._find_delta_strike(current_spot, vix, buy_exp, EMERGENCY_HEDGE_DELTA, 'ce')
                if stk:
                    sym, tok = self._fetch_symbol_and_token(stk, 'ce', buy_exp)
                    if sym:
                        oids = self._place_order('BUY', sym, tok, self.state.lots); fill, q, ft = self._fetch_order_details(oids, tok, sym)
                        if fill > 0:
                            self.state.emer_active = True; self.state.emer_strike = stk; self.state.emer_symbol = sym; self.state.emer_token = tok; self.state.emer_entry = fill; self.state.emer_attempts += 1; save_state(self.state)
                            slack_bot_sendtext(f"🪂 *Athena EMERGENCY*: Bought Parachute CE {stk} @ {fill:.1f}", SLACK_TRADE_ALERTS)
        elif self.state.emer_active:
            if current_spot <= (self.state.ce_sell_strike + EMERGENCY_EXIT_OFFSET):
                sym = self.state.emer_symbol; tok = self.state.emer_token; oids = self._place_order('SELL', sym, tok, self.state.lots); fill, q, ft = self._fetch_order_details(oids, tok, sym)
                if fill > 0:
                    realised = round(fill - self.state.emer_entry, 2); self.state.running_realised_pl += realised; slack_bot_sendtext(f"🏁 *Athena EMERGENCY*: Sold Parachute CE {self.state.emer_strike} @ {fill:.1f} | Realised: {realised:+.1f} pts", SLACK_TRADE_ALERTS)
                    self.state.emer_active = False; self.state.emer_strike = None; self.state.emer_symbol = None; self.state.emer_token = None; self.state.emer_entry = 0.0; save_state(self.state)

    def run(self):
        logger.info("=== Athena run loop started ===")
        while True:
            now = datetime.now()
            if now.time() >= self._closing_time: break
            if self.state.status == 'idle':
                sell_exp, _ = self._select_expiries()
                if sell_exp:
                    entry_day = self._last_trading_day_before(sell_exp); is_entry_day = (now.date() == entry_day)
                    if FORCE_ENTRY: is_entry_day = True
                    if is_entry_day and now.time() >= self._entry_time:
                        spot = self._get_ltp(EXCHANGE_NSE, 'NIFTY 50', NIFTY_INDEX_TOKEN); vix = self._get_ltp(EXCHANGE_NSE, 'INDIA VIX', VIX_TOKEN)
                        if vix and not (VIX_FILTER_LOW <= vix <= VIX_FILTER_HIGH): return True
                        if spot and vix:
                            strikes = self._select_all_strikes(spot, vix)
                            if strikes: self._execute_entry(strikes, spot, vix)
            if self.state.status == 'in_trade' and self.state.exit_timestamp:
                if now >= datetime.fromisoformat(self.state.exit_timestamp):
                    if self.state.emer_active:
                        sym = self.state.emer_symbol; tok = self.state.emer_token; oids = self._place_order('SELL', sym, tok, self.state.lots); fill, q, ft = self._fetch_order_details(oids, tok, sym)
                        self.state.running_realised_pl += round(fill - self.state.emer_entry, 2); self.state.emer_active = False
                    self._execute_exit(reason='pre_expiry')
            if self.state.status == 'in_trade':
                try:
                    prices = self._poll_prices(); spot = prices.get('spot')
                    if spot: self._manage_emergency_hedge(spot); self._append_trade_log_row(prices=prices); self._send_trade_update(prices=prices)
                except Exception as e: handle_exception(e)
                sleep(TRADE_UPDATE_INTERVAL)
            else: sleep(60)
            _reset_counters()
        if self.state.status == 'in_trade':
            try: self._send_trade_update()
            except: pass
        else: slack_bot_sendtext("*Athena*: Standing down for the day. No active positions.", SLACK_TRADE_UPDATES)
        logger.info("Market closed. Athena finished for the day."); return False
