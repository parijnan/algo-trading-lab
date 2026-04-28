"""
leto_phase2_simulation.py — Leto Phase 2 Research Lab
Simulates ML-driven routing and risk orchestration.
Merges Spatial Features (Price/VIX) with Structural Features (OI/PCR).

Goal: Demonstrate "Next Level" signal quality improvement.
"""

import os
import sys
import logging
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from configs_debit_phase2 import PRECOMPUTED_DIR

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
ML_MASTER_FILE = os.path.join(PRECOMPUTED_DIR, "ml_features_master.csv")
OI_MASTER_FILE = os.path.join(PRECOMPUTED_DIR, "oi_dynamics_master.csv")

def load_merged_dataset():
    """Merge ML and OI features."""
    logger.info("Loading ML and OI master files...")
    ml_df = pd.read_csv(ML_MASTER_FILE, parse_dates=['time_stamp'])
    oi_df = pd.read_csv(OI_MASTER_FILE, parse_dates=['datetime'])
    
    # Coverage Fix: Use Left Merge to keep all 5 years of ML features
    # even if OI data (institutional intent) only starts in late 2025.
    df = pd.merge(ml_df, oi_df, left_on='time_stamp', right_on='datetime', how='left')
    df = df.drop(columns=['datetime'])
    
    # Fill missing OI values with neutral defaults
    df['pcr_velocity'] = df['pcr_velocity'].fillna(0.0)
    df['oi_velocity']  = df['oi_velocity'].fillna(0.0)
    
    df = df.sort_values('time_stamp').reset_index(drop=True)
    
    logger.info(f"Merged dataset created: {len(df):,} rows (Coverage: {df['time_stamp'].min()} to {df['time_stamp'].max()})")
    return df

def simulate_signals(df):
    """
    Simulate the "Signal Ensemble" logic from the Solo Quant framework.
    """
    logger.info("Simulating signal ensemble...")
    
    # 1. Trend Confidence (Probability of Trend)
    # Using Z-score and Trend Strength as proxies for ML confidence
    df['trend_conf'] = (df['z_score'].abs() / 2.0).clip(0, 1)
    
    # 2. Institutional Alignment (OI Delta)
    # Price rising + PCR rising = Strong Bullish Intent
    # Price falling + PCR falling = Strong Bearish Intent
    df['oi_alignment'] = 0
    df.loc[(df['log_ret'] > 0) & (df['pcr_velocity'] > 0), 'oi_alignment'] = 1
    df.loc[(df['log_ret'] < 0) & (df['pcr_velocity'] < 0), 'oi_alignment'] = -1
    
    # 3. Regime Triage
    df['sim_regime'] = 'RANGE'
    # Trend triage (Stage 1)
    df.loc[df['trend_conf'] > 0.6, 'sim_regime'] = 'TREND'
    
    # 4. Final Handoff Signal (Leto Phase 2)
    df['target_strategy'] = 'ATHENA/ARTEMIS'
    df.loc[(df['sim_regime'] == 'TREND') & (df['label'] == 1) & (df['oi_alignment'] == 1), 'target_strategy'] = 'APOLLO_BULL'
    df.loc[(df['sim_regime'] == 'TREND') & (df['label'] == -1) & (df['oi_alignment'] == -1), 'target_strategy'] = 'APOLLO_BEAR'
    
    # 5. Kelly Sizing Proxy
    # Confidence * Alignment
    df['sizing_factor'] = df['trend_conf'] * df['oi_alignment'].abs()
    
    return df

def report_analysis(df):
    """Analyze the simulated signals."""
    logger.info("=== Leto Phase 2 Simulation Results ===")
    
    # Strategy Distribution
    dist = df['target_strategy'].value_counts()
    for strat, count in dist.items():
        logger.info(f"  {strat:15}: {count:6,} mins ({count/len(df):.1%})")
    
    # Quality Check: Does the target strategy match the future label?
    # For Apollo, we want to see if our Bull/Bear signals actually preceded a move
    correct_bull = ((df['target_strategy'] == 'APOLLO_BULL') & (df['label'] == 1)).sum()
    total_bull = (df['target_strategy'] == 'APOLLO_BULL').sum()
    logger.info(f"  Bullish Precision: {correct_bull/total_bull:.1%}" if total_bull > 0 else "  Bullish Precision: N/A")
    
    correct_bear = ((df['target_strategy'] == 'APOLLO_BEAR') & (df['label'] == -1)).sum()
    total_bear = (df['target_strategy'] == 'APOLLO_BEAR').sum()
    logger.info(f"  Bearish Precision: {correct_bear/total_bear:.1%}" if total_bear > 0 else "  Bearish Precision: N/A")
    
    # VIX Interaction
    avg_vix = df.groupby('target_strategy')['vix'].mean()
    logger.info("  Avg VIX per Signal:")
    for strat, v in avg_vix.items():
        logger.info(f"    {strat:15}: {v:.2f}")

def main():
    try:
        df = load_merged_dataset()
        df = simulate_signals(df)
        report_analysis(df)
        
        output_file = os.path.join(PRECOMPUTED_DIR, "leto_phase2_sim_results.csv")
        df.to_csv(output_file, index=False)
        logger.info(f"Full simulation results saved to {output_file}")
        
    except Exception as e:
        logger.error(f"Simulation failed: {e}")

if __name__ == "__main__":
    main()
