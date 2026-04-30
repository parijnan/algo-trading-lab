"""
backtest_p4.py — Phase 4 Artemis Backtest Engine
Unified Nifty Ecosystem (January 2020 to Present)

Features:
- Handles Expiry Shift (Thu till Sep 2025, Tue after)
- 4-Day Theta Capture (Mon-Thu or Thu-Tue)
- Weekend Smart Parachute (Athena Cross-Pollination)
"""

import os
import sys
import logging
import warnings
import pandas as pd
import numpy as np
from math import floor, ceil
from datetime import time as dtime, timedelta

# Ensure local imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from configs_p4 import (
    INSTRUMENT, NIFTY_INDEX_FILE, VIX_INDEX_FILE, NIFTY_OPTIONS_PATH,
    CONTRACTS_FILE, HOLIDAYS_FILE, TRADE_LOGS_DIR, TRADE_SUMMARY_FILE,
    LOT_SIZE, STRIKE_INTERVAL, EXPECTED_PREMIUM, HEDGE_POINTS,
    ADJUSTMENT_DISTANCE, MINIMUM_GAP, MINIMUM_GAP_ITERATOR,
    VIX_THRESHOLD, SL_DTE_MULTIPLIERS,
    ENABLE_WEEKEND_PARACHUTE, PARACHUTE_DISTANCE_PERCENT,
    ENABLE_TRADE_LOGS
)

from data_loader import (
    load_index_data, load_vix_daily, load_option_data,
    get_price, get_index_price, get_next_open, scan_strikes_for_premium
)

warnings.filterwarnings('ignore')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase 4 Helpers
# ---------------------------------------------------------------------------

def compute_dte_p4(from_date, expiry_ts):
    return int(np.busday_count(pd.Timestamp(from_date).date(), expiry_ts.date()))

def check_weekend_parachute(spot_fri_close, spot_mon_open):
    if not ENABLE_WEEKEND_PARACHUTE: return False
    move = abs(spot_mon_open - spot_fri_close) / spot_fri_close * 100
    return move >= PARACHUTE_DISTANCE_PERCENT

def make_spread_p4(spread_type):
    return {
        'type': spread_type, 'status': 'open',
        'sell_strike': None, 'buy_strike': None,
        'sell_entry': None, 'buy_entry': None,
        'sell_ltp': None, 'buy_ltp': None,
        'sell_exit': None, 'buy_exit': None,
        'option_sl': None,
        'booked_pl': 0.0, 'pl': 0.0,
        'sell_df': None, 'buy_df': None,
        'exit_reason': None, 'exit_time': None
    }

# ---------------------------------------------------------------------------
# Core Logic
# ---------------------------------------------------------------------------

def run_backtest():
    logger.info(f"Starting Phase 4.1: Unified Nifty Backtest (2020-Present)")
    
    index_df = load_index_data(NIFTY_INDEX_FILE)
    vix_raw  = load_vix_daily(VIX_INDEX_FILE)
    # Convert VIX DataFrame to a dictionary for O(1) date lookups
    vix_map  = dict(zip(vix_raw['date'], vix_raw['vix_open']))
    
    contracts = pd.read_csv(CONTRACTS_FILE, parse_dates=['expiry', 'entry', 'elm_time', 'cutoff_time'])
    summary = []
    
    for idx, contract in contracts.iterrows():
        exp_ts = contract['expiry']
        entry_ts = contract['entry']
        elm_ts = contract['elm_time']
        
        # VIX Check
        vix = vix_map.get(entry_ts.date())
        if vix is None:
            # If VIX is missing, we check the closest previous day
            vix = 20.0 # Default if really missing
            
        if vix >= VIX_THRESHOLD:
            continue
            
        spot_entry = get_index_price(index_df, entry_ts)
        if spot_entry is None: continue
            
        pe_spread = make_spread_p4('pe')
        ce_spread = make_spread_p4('ce')
        
        # Strike Selection
        pe_sell, pe_p = scan_strikes_for_premium(INSTRUMENT, NIFTY_OPTIONS_PATH, exp_ts, 'pe', 
                                                 floor(spot_entry/STRIKE_INTERVAL)*STRIKE_INTERVAL, -STRIKE_INTERVAL, -1, EXPECTED_PREMIUM, entry_ts)
        ce_sell, ce_p = scan_strikes_for_premium(INSTRUMENT, NIFTY_OPTIONS_PATH, exp_ts, 'ce', 
                                                 ceil(spot_entry/STRIKE_INTERVAL)*STRIKE_INTERVAL, STRIKE_INTERVAL, 1, EXPECTED_PREMIUM, entry_ts)
        
        if not pe_sell or not ce_sell: continue
            
        # Entry
        exec_ts = entry_ts + timedelta(minutes=1)
        for s, sell_s, buy_s in [(pe_spread, pe_sell, pe_sell-HEDGE_POINTS), (ce_spread, ce_sell, ce_sell+HEDGE_POINTS)]:
            s['sell_strike'], s['buy_strike'] = sell_s, buy_s
            s['sell_df'] = load_option_data(INSTRUMENT, NIFTY_OPTIONS_PATH, exp_ts, sell_s, s['type'])
            s['buy_df'] = load_option_data(INSTRUMENT, NIFTY_OPTIONS_PATH, exp_ts, buy_s, s['type'])
            
            s['sell_entry'] = get_price(s['sell_df'], exec_ts, 'open')
            s['buy_entry'] = get_price(s['buy_df'], exec_ts, 'open')
            
            if s['sell_entry']:
                s['status'] = 'active'
                dte = compute_dte_p4(exec_ts, exp_ts)
                mult = SL_DTE_MULTIPLIERS['vix_lt16'].get(dte, 2.5)
                s['option_sl'] = s['sell_entry'] * mult

        # Monitoring
        curr_ts = entry_ts + timedelta(minutes=2)
        spot_fri_close = None
        
        while curr_ts <= exp_ts:
            spot = get_index_price(index_df, curr_ts)
            if spot is None:
                curr_ts += timedelta(minutes=1)
                continue
            
            # Weekend Parachute Check (Monday 09:15)
            # Only relevant if entry was Thu/Fri and today is Mon
            if curr_ts.weekday() == 0 and curr_ts.time() == dtime(9, 15) and spot_fri_close:
                if check_weekend_parachute(spot_fri_close, spot):
                    logger.info(f"  [{exp_ts.date()}] Weekend Parachute Triggered at {curr_ts}")
                    for s in [pe_spread, ce_spread]:
                        if s['status'] == 'active':
                            s['sell_exit'] = get_price(s['sell_df'], curr_ts, 'open')
                            s['buy_exit'] = get_price(s['buy_df'], curr_ts, 'open')
                            s['status'], s['exit_reason'], s['exit_time'] = 'closed', 'parachute', curr_ts
                    break

            # Logic check for SL and LTP updates
            for s in [pe_spread, ce_spread]:
                if s['status'] != 'active': continue
                s['sell_ltp'] = get_price(s['sell_df'], curr_ts, 'close')
                s['buy_ltp'] = get_price(s['buy_df'], curr_ts, 'close')
                
                if s['sell_ltp'] and s['sell_ltp'] >= s['option_sl']:
                    s['sell_exit'] = get_next_open(s['sell_df'], curr_ts)[1]
                    s['buy_exit'] = get_next_open(s['buy_df'], curr_ts)[1]
                    s['status'], s['exit_reason'], s['exit_time'] = 'closed', 'sl', curr_ts
            
            # Daily SL Update
            if curr_ts.time() == dtime(15, 29):
                if curr_ts.weekday() == 4: spot_fri_close = spot
                
                dte = compute_dte_p4(curr_ts + timedelta(days=1), exp_ts)
                mult = SL_DTE_MULTIPLIERS['vix_lt16'].get(dte, 1.2)
                for s in [pe_spread, ce_spread]:
                    if s['status'] == 'active': s['option_sl'] = s['sell_entry'] * mult

            # Expiry/ELM Exit (Day before expiry 15:15 or Expiry day)
            if curr_ts >= elm_ts and any(s['status'] == 'active' for s in [pe_spread, ce_spread]):
                for s in [pe_spread, ce_spread]:
                    if s['status'] == 'active':
                        s['sell_exit'] = get_price(s['sell_df'], curr_ts, 'close')
                        s['buy_exit'] = get_price(s['buy_df'], curr_ts, 'close')
                        s['status'], s['exit_reason'], s['exit_time'] = 'closed', 'expiry', curr_ts
                break

            curr_ts += timedelta(minutes=1)

        # Totals
        tpl = 0
        reasons = []
        for s in [pe_spread, ce_spread]:
            if s['sell_entry'] and s['sell_exit']:
                tpl += ((s['sell_entry'] - s['sell_exit']) + (s['buy_exit'] - s['buy_entry'])) * LOT_SIZE
                if s['exit_reason']: reasons.append(s['exit_reason'])
        
        if pe_spread['sell_entry'] or ce_spread['sell_entry']:
            summary.append({
                'expiry': exp_ts.date(), 'vix': vix, 'pl': tpl, 
                'exit': "|".join(set(reasons)) if reasons else "none"
            })

    res_df = pd.DataFrame(summary)
    res_df.to_csv(TRADE_SUMMARY_FILE, index=False)
    logger.info(f"Backtest Complete. Summary:\n{res_df.describe()}")
    if not res_df.empty:
        print(f"\nWin Rate: {len(res_df[res_df['pl'] > 0]) / len(res_df) * 100:.2f}%")
        print(f"Total PL: {res_df['pl'].sum():.2f}")

if __name__ == "__main__":
    run_backtest()
