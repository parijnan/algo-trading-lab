"""
generate_contracts_p4.py — Phase 4 Contract Schedule Generator

Handles the Nifty Expiry Shift:
- Pre-Sep 2025: Thursday Expiry -> Monday Entry
- Post-Sep 2025: Tuesday Expiry -> Previous Thursday Entry
Covers January 2020 to Present.
"""

import os
import pandas as pd
from datetime import timedelta

# Paths
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINE_CFG = os.path.join(REPO_ROOT, 'data_pipeline', 'config')
HOLIDAYS_FILE = os.path.join(PIPELINE_CFG, 'holidays.csv')
NIFTY_LIST_FILE = os.path.join(PIPELINE_CFG, 'options_list_nf.csv')
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'contracts_p4.csv')

def load_holidays(path):
    df = pd.read_csv(path, parse_dates=['date'])
    return set(df['date'].dt.date)

def compute_entry_p4(expiry_date, holidays):
    """
    Dynamic entry to maintain ~4 day window:
    - Tuesday (1): Prev Thursday (-5 days)
    - Thursday (3): Monday (-3 days)
    """
    weekday = expiry_date.weekday()
    
    if weekday == 1: # Tuesday
        entry_date = expiry_date - timedelta(days=5)
    elif weekday == 3: # Thursday
        entry_date = expiry_date - timedelta(days=3)
    else:
        # Generic fallback: Monday of same week
        entry_date = expiry_date - timedelta(days=expiry_date.weekday())

    # Skip holidays/weekends
    while entry_date.weekday() >= 5 or entry_date in holidays:
        entry_date += timedelta(days=1)
    return entry_date

def compute_elm_p4(expiry_date, holidays):
    elm_date = expiry_date - timedelta(days=1)
    while elm_date.weekday() >= 5 or elm_date in holidays:
        elm_date -= timedelta(days=1)
    return elm_date

def main():
    print("=== Phase 4: Generating Unified Nifty Contracts (2020-Present) ===")
    holidays = load_holidays(HOLIDAYS_FILE)
    
    df = pd.read_csv(NIFTY_LIST_FILE)
    df['expiry_ts'] = pd.to_datetime(df['end_date'], utc=True).dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    
    # Filter for Jan 2020 onwards
    start_date = pd.Timestamp('2020-01-01')
    df = df[df['expiry_ts'] >= start_date].copy()
    
    rows = []
    for _, row in df.iterrows():
        exp_ts = row['expiry_ts']
        exp_date = exp_ts.date()
        
        entry_date = compute_entry_p4(exp_date, holidays)
        elm_date = compute_elm_p4(exp_date, holidays)
        
        rows.append({
            'instrument': 'nifty',
            'expiry': exp_ts,
            'entry': pd.Timestamp(f"{entry_date} 10:30:00"),
            'elm_time': pd.Timestamp(f"{elm_date} 15:15:00"),
            'cutoff_time': pd.Timestamp(f"{elm_date} 09:15:00")
        })
    
    out_df = pd.DataFrame(rows).sort_values('expiry').reset_index(drop=True)
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    out_df.to_csv(OUTPUT_FILE, index=False)
    print(f"Generated {len(out_df)} contracts in {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
