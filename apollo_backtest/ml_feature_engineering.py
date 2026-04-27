"""
ml_feature_engineering.py — Leto Phase 2 ML Feature Generator
Implementation of "Solo Quant" ML framework features:
- Spatial Coordinates (DTEMA 20, Trend Strength)
- Risk Signals (Price-VIX Interaction)
- Multi-Timeframe Structural Features (15m, 75m)
- Future-Looking Labeling (30m Fwd Z-Score)
"""

import os
import sys
import logging
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))

from configs_debit_phase2 import (
    NIFTY_INDEX_FILE, VIX_INDEX_FILE,
    PRECOMPUTED_DIR,
)

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
MARKET_OPEN  = "09:15"
MARKET_CLOSE = "15:30"
ML_MASTER_FILE = os.path.join(PRECOMPUTED_DIR, "ml_features_master.csv")

# ---------------------------------------------------------------------------
# Core Logic
# ---------------------------------------------------------------------------

def load_data():
    """Load and align Nifty and VIX 1-min data."""
    logger.info("Loading 1-min Nifty and VIX data...")
    nifty = pd.read_csv(NIFTY_INDEX_FILE, parse_dates=['time_stamp'])
    vix = pd.read_csv(VIX_INDEX_FILE, parse_dates=['time_stamp'])
    
    # Strip timezone if present
    nifty['time_stamp'] = nifty['time_stamp'].dt.tz_localize(None)
    vix['time_stamp'] = vix['time_stamp'].dt.tz_localize(None)
    
    # Merge on timestamp
    df = pd.merge(nifty, vix[['time_stamp', 'close']], on='time_stamp', suffixes=('', '_vix'))
    df = df.rename(columns={'close_vix': 'vix'})
    df = df.sort_values('time_stamp').reset_index(drop=True)
    
    # Filter market hours
    df = df[
        (df['time_stamp'].dt.time >= pd.Timestamp(MARKET_OPEN).time()) &
        (df['time_stamp'].dt.time <= pd.Timestamp(MARKET_CLOSE).time())
    ].copy()
    
    return df

def add_1min_features(df):
    """Add 1-minute spatial and interaction features."""
    logger.info("Generating 1-min spatial and interaction features...")
    
    # Symmetrical Log Returns
    df['log_ret'] = np.log(df['close'] / df['close'].shift(1))
    df['vix_log_ret'] = np.log(df['vix'] / df['vix'].shift(1))
    
    # Interaction: Risk Signal (Price Return * -VIX Return)
    # High positive = Price UP + VIX DOWN (Stable Bull)
    # High negative = Price DOWN + VIX UP (Panic Sell)
    df['risk_signal'] = df['log_ret'] * (-df['vix_log_ret'])
    
    # Spatial Coordinates: EMA-based
    ema_20 = df['close'].ewm(span=20, adjust=False).mean()
    ema_50 = df['close'].ewm(span=50, adjust=False).mean()
    
    # DTEMA 20: Distance from 20-EMA / Close (Standardized rubber band tension)
    df['dtema_20'] = (df['close'] - ema_20) / df['close']
    
    # Trend Strength: (EMA 20 - EMA 50) / Close (Standardized momentum coordinate)
    df['trend_strength'] = (ema_20 - ema_50) / df['close']
    
    # Rolling Volatility (standardized window)
    df['vol_60'] = df['log_ret'].rolling(window=60).std()
    df['vol_120'] = df['log_ret'].rolling(window=120).std()
    
    return df

def add_mtf_features(df):
    """Add 15m and 75m structural context."""
    logger.info("Adding MTF structural context (15m, 75m)...")
    
    # We use daily anchors to avoid boundary contamination (as per Phase 2 logic)
    # To keep it simple for the feature set, we map the resampled values back to 1-min
    
    for tf in [15, 75]:
        # Compute volatility at MTF
        # Note: We compute these on the 1-min df to maintain alignment
        df[f'vol_{tf}'] = df['log_ret'].rolling(window=tf).std()
        
        # Trend strength at MTF (smoothed)
        ema_short = df['close'].ewm(span=tf, adjust=False).mean()
        ema_long = df['close'].ewm(span=tf*2.5, adjust=False).mean()
        df[f'trend_strength_{tf}'] = (ema_short - ema_long) / df['close']

    return df

def add_labels(df):
    """Add forward-looking ground truth labels for ML training."""
    logger.info("Generating future-looking labels (30-min Fwd Z-Score)...")
    
    # Forward return (30 minutes ahead)
    df['fwd_ret_30'] = np.log(df['close'].shift(-30) / df['close'])
    
    # Regime-scale volatility (Max of 1h/2h windows)
    df['sigma_t'] = df[['vol_60', 'vol_120']].max(axis=1)
    
    # Z-Score (normalized future move)
    df['z_score'] = df['fwd_ret_30'] / df['sigma_t']
    
    # Labels: 1 (Up), -1 (Down), 0 (Range)
    # Thresholds: 1.5 Z-score and must be persistent (persistence handled by user in training)
    df['label'] = 0
    df.loc[df['z_score'] > 1.5, 'label'] = 1
    df.loc[df['z_score'] < -1.5, 'label'] = -1
    
    return df

def main():
    logger.info("=== Leto Phase 2 Feature Engineering starting ===")
    
    os.makedirs(PRECOMPUTED_DIR, exist_ok=True)
    
    df = load_data()
    df = add_1min_features(df)
    df = add_mtf_features(df)
    df = add_labels(df)
    
    # Drop rows with NaN from rolling/shift (warmup and tail)
    df_clean = df.dropna(subset=['vol_120', 'fwd_ret_30']).copy()
    
    logger.info(f"Saving {len(df_clean):,} rows to {ML_MASTER_FILE}...")
    df_clean.to_csv(ML_MASTER_FILE, index=False)
    
    # Summary
    counts = df_clean['label'].value_counts()
    logger.info("Label Distribution:")
    for lbl, count in counts.items():
        name = {1: "UP", -1: "DOWN", 0: "RANGE"}[lbl]
        logger.info(f"  {name}: {count:,} ({count/len(df_clean):.1%})")
    
    logger.info("=== Feature Engineering Complete ===")

if __name__ == "__main__":
    main()
