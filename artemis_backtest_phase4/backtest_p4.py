"""
backtest_p4.py — Phase 4 Artemis Backtest Engine
Unified Nifty Ecosystem (Tuesday Expiry)

Features:
- Nifty Tuesday cycle (Thu Entry, Tue Expiry)
- Cross-Pollinated Hedging: Weekend Smart Parachute (2% spot move trigger)
- Strategy Handoff ready: Returns status to Leto layer (simulated)
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

# Reuse existing data loader logic (it is instrument agnostic)
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
    """Business day count for Nifty cycle."""
    return int(np.busday_count(pd.Timestamp(from_date).date(), expiry_ts.date()))

def check_weekend_parachute(spot_fri_close, spot_mon_open):
    """
    Athena-style logic: If spot gaps more than X% over the weekend,
    trigger a parachute exit at Monday open.
    """
    if not ENABLE_WEEKEND_PARACHUTE:
        return False
    
    move = abs(spot_mon_open - spot_fri_close) / spot_fri_close * 100
    if move >= PARACHUTE_DISTANCE_PERCENT:
        return True
    return False

def make_spread_p4(spread_type):
    return {
        'type': spread_type,
        'status': 'open',
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
    logger.info("Starting Phase 4.1 Backtest: Nifty Unified Artemis")
    
    # 1. Load data
    index_df = load_index_data(NIFTY_INDEX_FILE)
    vix_df   = load_vix_daily(VIX_INDEX_FILE)
    contracts = pd.read_csv(CONTRACTS_FILE, parse_dates=['expiry', 'entry', 'elm_time', 'cutoff_time'])
    
    summary = []
    
    # 2. Iterate through expiries
    for idx, contract in contracts.iterrows():
        exp_ts = contract['expiry']
        entry_ts = contract['entry']
        elm_ts = contract['elm_time']
        
        logger.info(f"Processing Expiry: {exp_ts.date()} (Entry: {entry_ts.date()})")
        
        # Check VIX Gate at entry
        vix = vix_df.get(entry_ts.date(), 20.0) # Default 20 if missing
        if vix >= VIX_THRESHOLD:
            logger.info(f"  VIX {vix:.2f} >= {VIX_THRESHOLD}. Skipping week.")
            continue
            
        # Entry Price
        spot_entry = get_index_price(index_df, entry_ts)
        if spot_entry is None:
            continue
            
        # Initial Spread setup
        pe_spread = make_spread_p4('pe')
        ce_spread = make_spread_p4('ce')
        
        # Select Strikes (Logic: find premium closest to EXPECTED_PREMIUM)
        pe_sell, pe_price = scan_strikes_for_premium(INSTRUMENT, NIFTY_OPTIONS_PATH, exp_ts, 'pe', 
                                                     floor(spot_entry/STRIKE_INTERVAL)*STRIKE_INTERVAL, -STRIKE_INTERVAL, -1, EXPECTED_PREMIUM, entry_ts)
        ce_sell, ce_price = scan_strikes_for_premium(INSTRUMENT, NIFTY_OPTIONS_PATH, exp_ts, 'ce', 
                                                     ceil(spot_entry/STRIKE_INTERVAL)*STRIKE_INTERVAL, STRIKE_INTERVAL, 1, EXPECTED_PREMIUM, entry_ts)
        
        if not pe_sell or not ce_sell:
            continue
            
        # Fill spreads
        for s, sell_strike, buy_strike in [(pe_spread, pe_sell, pe_sell-HEDGE_POINTS), 
                                           (ce_spread, ce_sell, ce_sell+HEDGE_POINTS)]:
            s['sell_strike'] = sell_strike
            s['buy_strike'] = buy_strike
            s['sell_df'] = load_option_data(INSTRUMENT, NIFTY_OPTIONS_PATH, exp_ts, sell_strike, s['type'])
            s['buy_df'] = load_option_data(INSTRUMENT, NIFTY_OPTIONS_PATH, exp_ts, buy_strike, s['type'])
            
            # Entry execution (Open of next candle)
            exec_ts = entry_ts + timedelta(minutes=1)
            s['sell_entry'] = get_price(s['sell_df'], exec_ts, 'open')
            s['buy_entry'] = get_price(s['buy_df'], exec_ts, 'open')
            s['status'] = 'active'
            
            # Set SL based on DTE 4 (Entry Day)
            mult = SL_DTE_MULTIPLIERS['vix_lt16'][4]
            s['option_sl'] = s['sell_entry'] * mult

        # 3. Monitoring Loop (1-minute bars)
        curr_ts = entry_ts + timedelta(minutes=2)
        end_ts = exp_ts
        
        # State tracking for weekend gap
        is_friday_close = False
        spot_fri_close = None
        
        while curr_ts <= end_ts:
            spot = get_index_price(index_df, curr_ts)
            if spot is None:
                curr_ts += timedelta(minutes=1)
                continue
            
            # Weekend Parachute Check (Monday 09:15)
            if curr_ts.weekday() == 0 and curr_ts.time() == dtime(9, 15) and spot_fri_close:
                if check_weekend_parachute(spot_fri_close, spot):
                    logger.info(f"  *** Weekend Parachute Triggered at {curr_ts} (Gap detected) ***")
                    # Force exit both spreads at Monday Open
                    for s in [pe_spread, ce_spread]:
                        if s['status'] == 'active':
                            s['sell_exit'] = get_price(s['sell_df'], curr_ts, 'open')
                            s['buy_exit'] = get_price(s['buy_df'], curr_ts, 'open')
                            s['status'] = 'closed'
                            s['exit_reason'] = 'parachute'
                            s['exit_time'] = curr_ts
                    break # End trade for this week

            # Monitoring Active Spreads
            for s in [pe_spread, ce_spread]:
                if s['status'] != 'active': continue
                
                # Update LTP
                s['sell_ltp'] = get_price(s['sell_df'], curr_ts, 'close')
                s['buy_ltp'] = get_price(s['buy_df'], curr_ts, 'close')
                
                if s['sell_ltp'] is None: continue
                
                # Check SL
                if s['sell_ltp'] >= s['option_sl']:
                    logger.info(f"  {s['type'].upper()} SL Hit at {curr_ts} ({s['sell_ltp']:.2f} >= {s['option_sl']:.2f})")
                    s['sell_exit'] = get_next_open(s['sell_df'], curr_ts)
                    s['buy_exit'] = get_next_open(s['buy_df'], curr_ts)
                    s['status'] = 'closed'
                    s['exit_reason'] = 'sl'
                    s['exit_time'] = curr_ts
                    
            # Update SL based on DTE as days pass
            dte = compute_dte_p4(curr_ts, exp_ts)
            mult = SL_DTE_MULTIPLIERS['vix_lt16'].get(dte, 1.2)
            for s in [pe_spread, ce_spread]:
                if s['status'] == 'active':
                    s['option_sl'] = s['sell_entry'] * mult

            # Capture Friday Close for weekend check
            if curr_ts.weekday() == 4 and curr_ts.time() == dtime(15, 29):
                spot_fri_close = spot
                
            # Expiry / ELM Check (Tuesday)
            if curr_ts >= elm_ts and any(s['status'] == 'active' for s in [pe_spread, ce_spread]):
                for s in [pe_spread, ce_spread]:
                    if s['status'] == 'active':
                        s['sell_exit'] = get_price(s['sell_df'], curr_ts, 'close')
                        s['buy_exit'] = get_price(s['buy_df'], curr_ts, 'close')
                        s['status'] = 'closed'
                        s['exit_reason'] = 'expiry'
                        s['exit_time'] = curr_ts
                break

            curr_ts += timedelta(minutes=1)

        # 4. Result Calculation
        total_pl = 0
        for s in [pe_spread, ce_spread]:
            if s['sell_entry'] and s['sell_exit']:
                pl = (s['sell_entry'] - s['sell_exit']) + (s['buy_exit'] - s['buy_entry'])
                total_pl += pl * LOT_SIZE
        
        summary.append({
            'expiry': exp_ts.date(),
            'entry_vix': vix,
            'pl': total_pl,
            'exit_reason': pe_spread['exit_reason'] or ce_spread['exit_reason']
        })

    # 5. Output Summary
    res_df = pd.DataFrame(summary)
    res_df.to_csv(TRADE_SUMMARY_FILE, index=False)
    logger.info(f"Backtest complete. Results: {TRADE_SUMMARY_FILE}")
    print(res_df.describe())

if __name__ == "__main__":
    run_backtest()
