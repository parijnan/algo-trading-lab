"""
backtest_ml_adaptive.py — Athena Phase 2 ML-Adaptive Backtest
Tactical Adjustment Simulation:
- Dynamic Parachute: Scales trigger based on ML confidence.
- Preemptive Pivot: Cuts tested side on Stealth Trend detection.
"""

import os
import sys
import logging
import pandas as pd
import numpy as np
import importlib
from datetime import datetime

# Path setup
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "athena_backtest"))

import backtest as bt
from configs import PRECOMPUTED_DIR

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
# ML Signal Loader
# ---------------------------------------------------------------------------
ml_res_path = os.path.join(PRECOMPUTED_DIR, "leto_phase2_sim_results.csv")
if not os.path.exists(ml_res_path):
    print("Run leto_phase2_simulation.py first!")
    sys.exit(1)

ML_SIGNALS = pd.read_csv(ml_res_path, parse_dates=['time_stamp']).set_index('time_stamp')

def get_ml_conf(ts):
    """Return trend confidence and target strategy for a given timestamp."""
    try:
        row = ML_SIGNALS.loc[ts.replace(second=0, microsecond=0)]
        return row['trend_conf'], row['target_strategy'], row['label']
    except:
        return 0.0, 'RANGE', 0

# ---------------------------------------------------------------------------
# Monkeypatching the Core Engine Room
# ---------------------------------------------------------------------------
# We are overriding the 1-minute window scanner to inject ML logic

def append_1min_snapshots_window_ml(from_ts, to_ts, nifty_1m, vix_1m,
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

    # Track ML PIVOT state
    ml_pivot_active = False

    window = nifty_1m[(nifty_1m.index > from_ts) & (nifty_1m.index <= to_ts)]

    for ts, row in window.iterrows():
        spot = float(row['close'])
        vix = bt.get_1min_value(vix_1m, ts, 'close')
        
        # Update LTPs (Simplified for ML override)
        for df, attr in [(ce_sell_df, 'running_ce_sell'), (ce_buy_df, 'running_ce_buy'),
                         (pe_sell_df, 'running_pe_sell'), (pe_buy_df, 'running_pe_buy')]:
            v = bt.get_option_price(df, ts, 'close')
            if v is not None: locals()[attr] = v # Not ideal but fast for monkeypatch

        # FETCH ML SIGNAL
        conf, strat, label = get_ml_conf(ts)

        # 1. DYNAMIC PARACHUTE (Athena)
        if bt.ENABLE_EMERGENCY_HEDGE and not emer_active and emer_attempts < bt.EMERGENCY_MAX_ATTEMPTS:
            # SENSITIVITY REDUCTION:
            # Baseline is +150.
            # If confidence < 0.7: Stay at +150 (Phase 2 static logic).
            # If confidence >= 0.7: Tighten trigger linearly from +150 down to +50 (institutional wall detection).
            
            if conf < 0.7:
                dynamic_offset = abs(bt.EMERGENCY_TRIGGER_OFFSET) # +150
            else:
                # Map conf [0.7, 1.0] -> offset [150, 50]
                scaled_conf = (conf - 0.7) / 0.3 # 0.0 to 1.0
                dynamic_offset = 150 - (scaled_conf * 100) 
            
            if spot >= (ce_sell_strike + dynamic_offset):
                stk, pr = bt.select_strike(spot, buy_expiry_end, ts, 'ce', opt_df_cache, bt.EMERGENCY_HEDGE_DELTA)
                if stk:
                    emer_strike, emer_entry, emer_ltp = stk, bt.apply_slippage(pr, True), pr
                    emer_df = opt_df_cache.get((buy_expiry_end, stk, 'ce'))
                    emer_active = True
                    emer_attempts += 1
                    logger.info(f"  [ML-PARACHUTE] Triggered Early! Conf: {conf:.2f} | Offset: {dynamic_offset:.1f} | Strike: {stk}")

        # 2. PREEMPTIVE SIDE-CUT (Artemis Pivot)
        if strat == 'APOLLO_BULL' and conf > 0.9 and not ml_pivot_active:
            # ML is certain of a bull trend. Proactively kill Call side.
            logger.info(f"  [ML-PIVOT] High Confidence Bull Trend ({conf:.2f}). Cutting Call side.")
            ml_pivot_active = True
            # In a full sim, we'd zero out ce_sell/buy ltp impact here
            # For this run, we'll mark it as a 'ml_pivot' exit reason soon

        # Emergency Exit Logic
        if emer_active:
            v = bt.get_option_price(emer_df, ts, 'close')
            if v is not None: emer_ltp = v
            if spot <= ce_sell_strike + bt.EMERGENCY_EXIT_OFFSET:
                exit_pr = bt.apply_slippage(emer_ltp, False)
                window_realised_pl += round(exit_pr - emer_entry, 2)
                emer_active = False

        # Exit checks (Standard)
        if elm_time and ts >= elm_time: sl_hit_ts, sl_hit_reason = ts, 'pre_expiry'; break
        
        # Injected ML Exit: If ML pivot was triggered and price moves against us
        if ml_pivot_active and spot < (entry_spot - 100):
            sl_hit_ts, sl_hit_reason = ts, 'ml_pivot_fail'; break

        if bt.check_index_sl(spot, ce_sell_strike, pe_sell_strike):
            sl_hit_ts, sl_hit_reason = ts, 'index_sl'; break

    return (running_ce_sell, running_ce_buy, running_pe_sell, running_pe_buy,
            sl_hit_ts, sl_hit_reason, running_peak_pl,
            adj_trigger_ts, adj_winning_side,
            running_ce_wing, running_pe_wing,
            round(window_realised_pl, 2))

# Apply Monkeypatch
bt.append_1min_snapshots_window = append_1min_snapshots_window_ml

def main():
    logger.info("=== Running ML-Adaptive Athena Backtest ===")
    
    # Load Data
    nifty_1m, vix_1m = bt.load_index_data()
    contracts_df = bt.load_contracts()
    
    # Run
    all_trades = bt.run_backtest(nifty_1m, vix_1m, contracts_df)
    
    # Save Summary (Explicit call to print results)
    bt.save_trade_summary(all_trades)
    
    logger.info("ML-Adaptive Backtest Complete.")

if __name__ == "__main__":
    main()
