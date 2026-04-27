"""
backtest_wing_salvage.py — Athena Phase 2.1 Research
Logic: When the CE Parachute is triggered, exit the PE Wing immediately.
"""

import os
import sys
import logging
import pandas as pd
import numpy as np
from datetime import datetime

# Path setup
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "athena_backtest"))

import backtest as bt

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Monkeypatching the Window Scanner
# ---------------------------------------------------------------------------

def append_1min_snapshots_window_salvage(from_ts, to_ts, nifty_1m, vix_1m,
                                  ce_sell_df, pe_sell_df, ce_buy_df, pe_buy_df,
                                  ce_sell_strike, pe_sell_strike,
                                  ce_sell_entry, ce_buy_entry,
                                  pe_sell_entry, pe_buy_entry,
                                  total_net_debit, max_theoretical_profit,
                                  entry_spot, elm_time, trade_log,
                                  last_ce_sell_ltp, last_ce_buy_ltp,
                                  last_pe_sell_ltp, last_pe_buy_ltp,
                                  running_realised_pl=0.0, running_peak_pl=0.0,
                                  entry_time=None, sell_expiry_end=None,
                                  adjustment_already_made=False,
                                  ce_wing_df=None, pe_wing_df=None,
                                  ce_wing_entry=0.0, pe_wing_entry=0.0,
                                  last_ce_wing_ltp=0.0, last_pe_wing_ltp=0.0,
                                  opt_df_cache=None, buy_expiry_end=None):
    
    running_ce_sell, running_ce_buy = last_ce_sell_ltp, last_ce_buy_ltp
    running_pe_sell, running_pe_buy = last_pe_sell_ltp, last_pe_buy_ltp
    running_ce_wing, running_pe_wing = last_ce_wing_ltp, last_pe_wing_ltp
    window_realised_pl = 0.0
    emer_active, emer_strike, emer_entry, emer_ltp, emer_df, emer_attempts = False, None, 0.0, 0.0, None, 0
    sl_hit_ts, sl_hit_reason, adj_trigger_ts, adj_winning_side = None, None, None, None

    # Track Salvage state
    pe_wing_is_active = (pe_wing_entry > 0)

    window = nifty_1m[(nifty_1m.index > from_ts) & (nifty_1m.index <= to_ts)]

    for ts, row in window.iterrows():
        spot = float(row['close'])
        vix = bt.get_1min_value(vix_1m, ts, 'close')
        
        # Update LTPs
        v = bt.get_option_price(ce_sell_df, ts, 'close'); 
        if v is not None: running_ce_sell = v
        v = bt.get_option_price(ce_buy_df, ts, 'close');  
        if v is not None: running_ce_buy = v
        v = bt.get_option_price(pe_sell_df, ts, 'close'); 
        if v is not None: running_pe_sell = v
        v = bt.get_option_price(pe_buy_df, ts, 'close');  
        if v is not None: running_pe_buy = v
        
        if pe_wing_is_active:
            v = bt.get_option_price(pe_wing_df, ts, 'close')
            if v is not None: running_pe_wing = v

        # --- Emergency Hedge Logic ---
        if bt.ENABLE_EMERGENCY_HEDGE and not emer_active and emer_attempts < bt.EMERGENCY_MAX_ATTEMPTS:
            if spot >= ce_sell_strike - bt.EMERGENCY_TRIGGER_OFFSET:
                stk, pr = bt.select_strike(spot, buy_expiry_end, ts, 'ce', opt_df_cache, bt.EMERGENCY_HEDGE_DELTA)
                if stk:
                    emer_strike, emer_entry, emer_ltp = stk, bt.apply_slippage(pr, True), pr
                    emer_df = opt_df_cache.get((buy_expiry_end, stk, 'ce'))
                    emer_active = True
                    emer_attempts += 1
                    logger.info(f"  [PARACHUTE] Triggered at {ts} | spot={spot:.0f}")

                    # SALVAGE PE WING
                    if pe_wing_is_active:
                        exit_price = bt.apply_slippage(running_pe_wing, is_buy=False)
                        salvage_gain = round(exit_price - pe_wing_entry, 2)
                        window_realised_pl += salvage_gain
                        pe_wing_is_active = False
                        running_pe_wing = exit_price # Locked at exit
                        logger.info(f"  [SALVAGE] Exited PE Wing at {running_pe_wing:.1f} | Realised: {salvage_gain:+.1f} pts")

        if emer_active:
            v = bt.get_option_price(emer_df, ts, 'close')
            if v is not None: emer_ltp = v
            if spot <= ce_sell_strike + bt.EMERGENCY_EXIT_OFFSET:
                exit_pr = bt.apply_slippage(emer_ltp, False)
                window_realised_pl += round(exit_pr - emer_entry, 2)
                emer_active = False

        # Exit checks
        if elm_time and ts >= elm_time: sl_hit_ts, sl_hit_reason = ts, 'pre_expiry'; break
        if bt.check_index_sl(spot, ce_sell_strike, pe_sell_strike):
            sl_hit_ts, sl_hit_reason = ts, 'index_sl'; break

    # Final cleanup
    if emer_active:
        exit_pr = bt.apply_slippage(emer_ltp, False)
        window_realised_pl += round(exit_pr - emer_entry, 2)

    return (running_ce_sell, running_ce_buy, running_pe_sell, running_pe_buy,
            sl_hit_ts, sl_hit_reason, running_peak_pl,
            adj_trigger_ts, adj_winning_side,
            running_ce_wing, running_pe_wing,
            round(window_realised_pl, 2))

# Apply Monkeypatch
bt.append_1min_snapshots_window = append_1min_snapshots_window_salvage

def main():
    logger.info("=== Running Athena Phase 2.1 (PE Wing Salvage) Backtest ===")
    nifty_1m, vix_1m = bt.load_index_data()
    contracts_df = bt.load_contracts()
    all_trades = bt.run_backtest(nifty_1m, vix_1m, contracts_df)
    bt.save_trade_summary(all_trades)

if __name__ == "__main__":
    main()
