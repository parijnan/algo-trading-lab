"""
generate_contracts_p4.py — Phase 4 Contract Schedule Generator

Specifically designed for Unified Nifty Ecosystem (Tuesday Expiry).
Computes entry/expiry cycles to maintain a ~4-day theta window:
  - Expiry: Tuesday 15:30
  - Entry: Previous Thursday 10:30 (approx 4 trading days: Thu, Fri, Mon, Tue)
  - ELM: Monday 15:15 (Day before expiry)
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
    For Tuesday expiry, enter on previous Thursday (expiry - 5 days).
    If Thursday is a holiday, move forward to Friday, then Monday.
    """
    entry_date = expiry_date - timedelta(days=5)
    while entry_date.weekday() >= 5 or entry_date in holidays:
        entry_date += timedelta(days=1)
    return entry_date

def compute_elm_p4(expiry_date, holidays):
    """
    ELM is the day before expiry (Monday).
    If Monday is a holiday, move back to Friday.
    """
    elm_date = expiry_date - timedelta(days=1)
    while elm_date.weekday() >= 5 or elm_date in holidays:
        elm_date -= timedelta(days=1)
    return elm_date

def main():
    print("=== Phase 4: Generating Nifty Tuesday Contracts ===")
    holidays = load_holidays(HOLIDAYS_FILE)
    
    df = pd.read_csv(NIFTY_LIST_FILE)
    df['expiry_ts'] = pd.to_datetime(df['end_date'], utc=True).dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    
    rows = []
    for _, row in df.iterrows():
        exp_ts = row['expiry_ts']
        exp_date = exp_ts.date()
        
        # Only process expiries that fall on Tuesday (Phase 4 Focus)
        if exp_date.weekday() != 1:
            continue
            
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
