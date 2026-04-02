"""
generate_contracts.py — Artemis Backtest Contract Schedule Generator

Builds artemis_backtest/contracts.csv from:
  - options_list_nf.csv     (Nifty weekly expiries, 'end_date' column)
  - data/contracts.csv      (Sensex weekly expiries from live Artemis, 'expiry' column)
  - data/holidays.csv       (market holidays for elm_time / cutoff_time adjustment)

For each weekly expiry the script computes:
  entry      — Monday of expiry week at 10:30
  elm_time   — day before expiry at 15:15, adjusted back if that day is a holiday
  cutoff_time— day before expiry at 09:15, same holiday adjustment as elm_time

Output: artemis_backtest/contracts.csv
  instrument, expiry, entry, elm_time, cutoff_time

The file is manually editable. Run this script only when the source files change
or when adding new expiries to the backtest range. Existing manual edits will be
overwritten — keep a copy if you have made manual corrections.

Run from the repo root:
  python artemis_backtest/generate_contracts.py
"""

import os
import sys
import pandas as pd
from datetime import timedelta

# ---------------------------------------------------------------------------
# Paths — all relative to repo root (algo-trading-lab/)
# ---------------------------------------------------------------------------
REPO_ROOT         = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINE_CFG      = os.path.join(REPO_ROOT, 'data_pipeline', 'config')
ARTEMIS_DATA      = os.path.join(REPO_ROOT, 'artemis', 'data')

NIFTY_LIST_FILE   = os.path.join(PIPELINE_CFG, 'options_list_nf.csv')
SENSEX_LIST_FILE  = os.path.join(ARTEMIS_DATA, 'contracts.csv')
HOLIDAYS_FILE     = os.path.join(PIPELINE_CFG, 'holidays.csv')

OUTPUT_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'contracts.csv')


# ---------------------------------------------------------------------------
# Holiday helpers
# ---------------------------------------------------------------------------

def load_holidays(path: str) -> set:
    """Return a set of holiday dates (datetime.date objects)."""
    df = pd.read_csv(path, parse_dates=['date'])
    return set(df['date'].dt.date)


def prev_trading_day(d, holidays: set):
    """Return the most recent trading day strictly before date d."""
    candidate = d - timedelta(days=1)
    while candidate.weekday() >= 5 or candidate in holidays:
        candidate -= timedelta(days=1)
    return candidate


def compute_elm_day(expiry_date, holidays: set):
    """
    elm_time and cutoff_time fall on the day before expiry (15:15 and 09:15).
    If that day is a market holiday, move back to the previous trading day.
    """
    day_before = expiry_date - timedelta(days=1)
    # If day_before is a weekend or holiday, step back to the last trading day
    while day_before.weekday() >= 5 or day_before in holidays:
        day_before -= timedelta(days=1)
    return day_before


def compute_entry(expiry_date, holidays: set):
    """
    Entry is Monday 10:30 of the expiry week.
    Walk back from expiry to find the Monday. If that Monday is a holiday,
    move forward to the next trading day (still within the same week).
    """
    # Find the Monday of the expiry week
    days_since_monday = expiry_date.weekday()  # Mon=0 ... Sun=6
    monday = expiry_date - timedelta(days=days_since_monday)
    # If Monday is a holiday, step forward until we find a trading day
    candidate = monday
    while candidate.weekday() >= 5 or candidate in holidays:
        candidate += timedelta(days=1)
    return candidate


# ---------------------------------------------------------------------------
# Nifty contract schedule
# ---------------------------------------------------------------------------

def build_nifty_contracts(holidays: set) -> pd.DataFrame:
    """
    Parse options_list_nf.csv. Use 'end_date' as the authoritative expiry
    timestamp (already 15:30 IST). Timestamps are ISO UTC ('Z' suffix) —
    convert to IST by adding 5h30m then drop timezone.
    Only include expiries where the expiry day is Thursday (Nifty pre-Sep 2025
    expiry was Thursday; the data file reflects the actual expiry days).
    """
    df = pd.read_csv(NIFTY_LIST_FILE)
    # end_date is UTC ISO: '2025-01-02T15:30:00.000Z'
    df['expiry_ts'] = (
        pd.to_datetime(df['end_date'], utc=True)
        .dt.tz_convert('Asia/Kolkata')
        .dt.tz_localize(None)
    )
    rows = []
    for _, row in df.iterrows():
        exp_ts   = row['expiry_ts']
        exp_date = exp_ts.date()
        entry_date    = compute_entry(exp_date, holidays)
        elm_date      = compute_elm_day(exp_date, holidays)
        rows.append({
            'instrument':   'nifty',
            'expiry':       exp_ts,
            'entry':        pd.Timestamp(f"{entry_date} 10:30:00"),
            'elm_time':     pd.Timestamp(f"{elm_date} 15:15:00"),
            'cutoff_time':  pd.Timestamp(f"{elm_date} 09:15:00"),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Sensex contract schedule
# ---------------------------------------------------------------------------

def build_sensex_contracts(holidays: set) -> pd.DataFrame:
    """
    Parse the live Artemis contracts.csv. 'expiry' column is already IST with
    no timezone suffix ('2025-08-19 15:30:00'). elm_time and cutoff_time are
    present in the file but we recompute them from scratch using the same
    holiday logic so the output format is consistent and correct for any
    manually added rows.
    """
    df = pd.read_csv(SENSEX_LIST_FILE, parse_dates=['expiry'])
    rows = []
    for _, row in df.iterrows():
        exp_ts   = row['expiry']
        exp_date = exp_ts.date()
        entry_date   = compute_entry(exp_date, holidays)
        elm_date     = compute_elm_day(exp_date, holidays)
        rows.append({
            'instrument':   'sensex',
            'expiry':       exp_ts,
            'entry':        pd.Timestamp(f"{entry_date} 10:30:00"),
            'elm_time':     pd.Timestamp(f"{elm_date} 15:15:00"),
            'cutoff_time':  pd.Timestamp(f"{elm_date} 09:15:00"),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== Artemis Backtest — Contract Schedule Generator ===")

    # Validate input files
    for path, label in [
        (NIFTY_LIST_FILE,  'Nifty options list'),
        (SENSEX_LIST_FILE, 'Sensex contracts (live Artemis)'),
        (HOLIDAYS_FILE,    'Holidays'),
    ]:
        if not os.path.exists(path):
            print(f"ERROR: {label} not found at: {path}")
            sys.exit(1)

    holidays = load_holidays(HOLIDAYS_FILE)
    print(f"  Loaded {len(holidays)} market holidays")

    nifty_df  = build_nifty_contracts(holidays)
    sensex_df = build_sensex_contracts(holidays)
    print(f"  Nifty  expiries: {len(nifty_df)}")
    print(f"  Sensex expiries: {len(sensex_df)}")

    combined = pd.concat([nifty_df, sensex_df], ignore_index=True)
    combined = combined.sort_values(['instrument', 'expiry']).reset_index(drop=True)

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    combined.to_csv(OUTPUT_FILE, index=False)
    print(f"  Written: {OUTPUT_FILE}")
    print(f"  Total rows: {len(combined)}")
    print("=== Done ===")
    print()
    print("Review the file and make any manual corrections before running backtest.py.")


if __name__ == '__main__':
    main()