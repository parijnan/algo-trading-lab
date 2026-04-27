"""
oi_aggregator.py — Option Chain OI Aggregator for ML Features
Scans expiry folders to calculate Total CE OI, Total PE OI, and PCR 
at a 1-minute resolution. 

This data is crucial for detecting "Institutional Intent" and "OI Walls".
"""

import os
import sys
import logging
import pandas as pd
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from configs_debit_phase2 import NIFTY_OPTIONS_PATH, PRECOMPUTED_DIR

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
# Constants
# ---------------------------------------------------------------------------
OI_MASTER_FILE = os.path.join(PRECOMPUTED_DIR, "oi_dynamics_master.csv")

def get_expiry_folders():
    """Get list of all expiry folders sorted by date."""
    folders = [f for f in os.listdir(NIFTY_OPTIONS_PATH) if os.path.isdir(os.path.join(NIFTY_OPTIONS_PATH, f))]
    return sorted(folders)

def aggregate_oi_for_expiry(expiry_folder):
    """
    Aggregate CE and PE OI for a single expiry folder across all minutes.
    """
    path = os.path.join(NIFTY_OPTIONS_PATH, expiry_folder)
    files = [f for f in os.listdir(path) if f.endswith('.csv')]
    
    all_data = []
    for file in files:
        is_ce = file.endswith('ce.csv')
        try:
            # We only need datetime and open_interest
            df = pd.read_csv(os.path.join(path, file), usecols=['datetime', 'open_interest'])
            df['is_ce'] = is_ce
            all_data.append(df)
        except Exception as e:
            continue
            
    if not all_data:
        return pd.DataFrame()
        
    full_df = pd.concat(all_data)
    full_df['datetime'] = pd.to_datetime(full_df['datetime'])
    
    # Pivot to get CE and PE OI per minute
    pivot = full_df.groupby(['datetime', 'is_ce'])['open_interest'].sum().unstack(fill_value=0)
    pivot.columns = ['pe_oi', 'ce_oi'] # False is pe, True is ce if sorted alphabetically (False < True)
    # Wait, unstack order depends on values. Let's be explicit.
    # is_ce is boolean. False (0) -> pe_oi, True (1) -> ce_oi.
    # columns will be [False, True]
    pivot.columns = ['pe_oi', 'ce_oi']
    
    return pivot.reset_index()

def main():
    logger.info("=== OI Aggregator starting ===")
    folders = get_expiry_folders()
    logger.info(f"Found {len(folders)} expiry folders.")
    
    # In a real production backtest, we would process all.
    # For this prototype, let's process the last 12 months to demonstrate.
    recent_folders = folders[-12:] 
    
    master_oi = []
    for folder in recent_folders:
        logger.info(f"Processing {folder}...")
        df = aggregate_oi_for_expiry(folder)
        if not df.empty:
            master_oi.append(df)
            
    if not master_oi:
        logger.error("No OI data found!")
        return
        
    final_df = pd.concat(master_oi)
    # Group by datetime because multiple expiries might be active (weekly + monthly)
    final_df = final_df.groupby('datetime').sum().sort_index().reset_index()
    
    # Feature Engineering
    final_df['total_oi'] = final_df['ce_oi'] + final_df['pe_oi']
    final_df['pcr'] = final_df['pe_oi'] / final_df['ce_oi'].replace(0, 1)
    
    # OI Velocity (1-min change)
    final_df['oi_velocity'] = final_df['total_oi'].diff()
    final_df['pcr_velocity'] = final_df['pcr'].diff()
    
    logger.info(f"Saving OI master with {len(final_df):,} rows...")
    final_df.to_csv(OI_MASTER_FILE, index=False)
    logger.info("=== OI Aggregation Complete ===")

if __name__ == "__main__":
    main()
