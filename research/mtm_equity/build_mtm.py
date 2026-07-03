"""
build_mtm.py — Per-strategy per-bar MTM extractors.

Each extractor returns a DataFrame with columns:
    ts        : datetime (tz-naive)
    trade_id  : str  (leto_trade_log row index)
    mtm_raw   : float (native unit — points for Athena/Artemis, rupees for Iris)
    exit_bar  : bool (True for the bar at or just before exit_ts)

The caller (build_all_mtm) handles auto-calibration to rupees.

Matching strategy:
    Athena    — match by entry DATE in filename (duplicates are identical runs)
    Artemis   — match by entry DATE (log's first-bar timestamp date)
    Iris      — match by entry DATE + entry TIME (HHMM) in filename
"""

import os
import re
import pandas as pd
import numpy as np

import sys
sys.path.insert(0, os.path.dirname(__file__))
from configs import (
    ATHENA_LOGS_DIR, ARTEMIS_NIFTY_DIR, ARTEMIS_SENEX_DIR, IRIS_LOGS_DIR,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_log_by_date(log_dir: str, entry_date: str) -> str | None:
    """Find a trade log file by entry date in filename. Returns first match."""
    if not os.path.isdir(log_dir):
        return None
    for f in os.listdir(log_dir):
        if entry_date in f and f.endswith('.csv'):
            return os.path.join(log_dir, f)
    return None


def _find_iris_log(entry_date: str, entry_time_hhmm: str) -> str | None:
    """Find Iris trade log by date AND entry time."""
    if not os.path.isdir(IRIS_LOGS_DIR):
        return None
    for f in os.listdir(IRIS_LOGS_DIR):
        if entry_date in f and entry_time_hhmm in f and f.endswith('.csv'):
            return os.path.join(IRIS_LOGS_DIR, f)
    return None


# ---------------------------------------------------------------------------
# Athena extractor
# ---------------------------------------------------------------------------

def extract_athena(row: pd.Series, trade_id: str) -> pd.DataFrame | None:
    """
    Athena: cumulative_pl column (points). Includes realised + unrealised.
    Last bar should == total_pl_points from summary.
    """
    entry_ts = pd.to_datetime(row['entry_ts'])
    exit_ts  = pd.to_datetime(row['exit_ts'])
    entry_date = entry_ts.strftime('%Y-%m-%d')

    path = _find_log_by_date(ATHENA_LOGS_DIR, entry_date)
    if path is None:
        return None

    df = pd.read_csv(path)
    df['ts'] = pd.to_datetime(df['time_stamp'])

    # cumulative_pl is the running total (unrealised + realised) in points
    df = df[['ts', 'cumulative_pl']].rename(columns={'cumulative_pl': 'mtm_raw'})
    df = df.dropna(subset=['mtm_raw'])

    # Truncate at exit_ts — bars after exit are post-trade tracking
    df = df[df['ts'] <= exit_ts]

    df['exit_bar'] = df['ts'] == df['ts'].max()

    df['trade_id'] = trade_id
    df['strategy'] = 'athena'
    return df[['ts', 'trade_id', 'strategy', 'mtm_raw', 'exit_bar']]


# ---------------------------------------------------------------------------
# Artemis extractor (Nifty + Sensex)
# ---------------------------------------------------------------------------

def extract_artemis(row: pd.Series, trade_id: str) -> pd.DataFrame | None:
    """
    Artemis: per-bar MTM in points =
        (pe_pl + ce_pl) + (pe_add_pl + ce_add_pl) / 2

    This includes booked_pl (realised from SL exits) + unrealised.
    The last bar may differ from total_pl_points (expiry settlement
    happens after the last logged bar).
    """
    entry_ts = pd.to_datetime(row['entry_ts'])
    exit_ts  = pd.to_datetime(row['exit_ts'])
    instrument = row.get('instrument', 'nifty')

    log_dir = ARTEMIS_NIFTY_DIR if instrument == 'nifty' else ARTEMIS_SENEX_DIR

    # Match by first-bar timestamp date — the filename contains the expiry date,
    # not the entry date. We search by entry date in the log's first bar.
    entry_date = entry_ts.strftime('%Y-%m-%d')
    path = _find_log_by_date(log_dir, entry_date)

    # If not found by entry date, try expiry date in filename
    # (entry is Monday of expiry week, so expiry is Thursday of same week)
    if path is None:
        # Try finding by matching first bar timestamp
        if not os.path.isdir(log_dir):
            return None
        for f in os.listdir(log_dir):
            if not f.endswith('.csv'):
                continue
            fpath = os.path.join(log_dir, f)
            try:
                first = pd.read_csv(fpath, nrows=1)
                first_ts = pd.to_datetime(first['time_stamp'].iloc[0])
                if first_ts.date() == entry_ts.date():
                    path = fpath
                    break
            except Exception:
                continue

    if path is None:
        return None

    df = pd.read_csv(path)
    df['ts'] = pd.to_datetime(df['time_stamp'])

    # Per-bar MTM in points: (pe_pl + ce_pl) + (pe_add_pl + ce_add_pl) / 2
    df['mtm_raw'] = (df['pe_pl'] + df['ce_pl']) + (df['pe_add_pl'] + df['ce_add_pl']) / 2

    # Truncate at exit_ts — bars after exit are post-trade tracking
    df = df[df['ts'] <= exit_ts]

    df['exit_bar'] = df['ts'] == df['ts'].max()
    df['trade_id'] = trade_id
    strat_name = f'artemis_{instrument}'
    df['strategy'] = strat_name

    return df[['ts', 'trade_id', 'strategy', 'mtm_raw', 'exit_bar']]


# ---------------------------------------------------------------------------
# Iris extractor
# ---------------------------------------------------------------------------

def extract_iris(row: pd.Series, trade_id: str) -> pd.DataFrame | None:
    """
    Iris: unr_rs column (rupees). Already in rupees (unr_pts × LOT_SIZE).
    The log continues after exit_ts — truncate at exit.
    """
    entry_ts = pd.to_datetime(row['entry_ts'])
    exit_ts  = pd.to_datetime(row['exit_ts'])
    entry_date = entry_ts.strftime('%Y-%m-%d')
    entry_time = entry_ts.strftime('%H%M')

    path = _find_iris_log(entry_date, entry_time)
    if path is None:
        return None

    df = pd.read_csv(path)
    df['ts'] = pd.to_datetime(df['ts'])

    # Truncate at exit_ts — the log continues after exit
    df = df[df['ts'] <= exit_ts]

    df = df[['ts', 'unr_rs']].rename(columns={'unr_rs': 'mtm_raw'})
    df = df.dropna(subset=['mtm_raw'])

    df['exit_bar'] = df['ts'] == df['ts'].max()
    df['trade_id'] = trade_id
    df['strategy'] = 'iris'

    return df[['ts', 'trade_id', 'strategy', 'mtm_raw', 'exit_bar']]


# ---------------------------------------------------------------------------
# Dispatcher + auto-calibration
# ---------------------------------------------------------------------------

_EXTRACTORS = {
    'athena':         extract_athena,
    'artemis':        extract_artemis,
    'iris':           extract_iris,
}


def extract_trade_mtm(row: pd.Series, trade_id: str) -> pd.DataFrame | None:
    """Dispatch to the right extractor based on strategy name."""
    strategy = row['strategy']
    extractor = _EXTRACTORS.get(strategy)
    if extractor is None:
        return None
    return extractor(row, trade_id)


def calibrate_to_rupees(mtm_df: pd.DataFrame, pl_rs: float, lot_size: int,
                        exit_ts: pd.Timestamp, epsilon: float = 0.01):
    """
    Convert per-bar MTM from native units to rupees.

    Auto-calibration: factor = pl_rs / mtm_raw_at_exit_bar.
    If factor > 0 and |mtm_at_exit| > epsilon:
        mtm_rs = mtm_raw × factor  (curve terminates at pl_rs)
    Else (opposite signs, near-zero exit bar, or division issue):
        mtm_rs = mtm_raw × lot_size  (direct conversion)

    In both cases, a final exit point at exit_ts with value = pl_rs is
    appended so the curve always terminates exactly at the realised P&L.
    For most trades this is redundant (same value as the last bar); for
    Artemis trades with expiry settlement after the last logged bar, it
    captures the gap as a real P&L event.

    Returns (mtm_df_with_mtm_rs, calibration_info_dict)
    """
    exit_bars = mtm_df[mtm_df['exit_bar']]
    if len(exit_bars) == 0:
        exit_val = mtm_df['mtm_raw'].iloc[-1] if len(mtm_df) > 0 else 0.0
    else:
        exit_val = exit_bars['mtm_raw'].iloc[-1]

    info = {
        'exit_bar_mtm_raw': exit_val,
        'pl_rs': pl_rs,
        'method': '',
        'factor': None,
        'synthetic_exit': False,
    }

    if abs(exit_val) > epsilon and (pl_rs * exit_val > 0 or (pl_rs == 0 and exit_val == 0)):
        # Same sign — auto-calibrate
        factor = pl_rs / exit_val if exit_val != 0 else 0.0
        mtm_df = mtm_df.copy()
        mtm_df['mtm_rs'] = mtm_df['mtm_raw'] * factor
        info['method'] = 'auto_calibrate'
        info['factor'] = factor
    else:
        # Opposite sign or near-zero — LOT_SIZE + synthetic exit point
        mtm_df = mtm_df.copy()
        mtm_df['mtm_rs'] = mtm_df['mtm_raw'] * lot_size
        info['method'] = 'lot_size_synthetic'
        info['factor'] = float(lot_size)
        info['synthetic_exit'] = True

    # Always append a final exit point at exit_ts with value = pl_rs.
    # For auto-calibrated trades where the last bar ≈ pl_rs, this is
    # redundant (same value). For trades with a gap, it captures the
    # expiry/exit effect as a real P&L event.
    last_ts = mtm_df['ts'].iloc[-1] if len(mtm_df) > 0 else exit_ts
    if last_ts < exit_ts:
        # Gap between last logged bar and exit — insert exit point
        synth_row = pd.DataFrame([{
            'ts': exit_ts,
            'trade_id': mtm_df['trade_id'].iloc[0],
            'strategy': mtm_df['strategy'].iloc[0],
            'mtm_raw': pl_rs / lot_size if lot_size else 0,
            'exit_bar': True,
            'mtm_rs': pl_rs,
        }])
        mtm_df = pd.concat([mtm_df, synth_row], ignore_index=True)
    else:
        # Last bar is at or after exit_ts — replace its mtm_rs with pl_rs
        mtm_df.loc[mtm_df.index[-1], 'mtm_rs'] = pl_rs
        mtm_df.loc[mtm_df.index[-1], 'exit_bar'] = True

    return mtm_df, info
