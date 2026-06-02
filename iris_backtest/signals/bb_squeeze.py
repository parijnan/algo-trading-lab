"""BB_SQUEEZE — Bollinger Band squeeze followed by band breakout on 5-min bars."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from configs import (BB_SQUEEZE_TF, BB_PERIOD, BB_STD_DEV,
                     BB_SQUEEZE_LOOKBACK, BB_SQUEEZE_FACTOR, BB_LOOKBACK_BARS)
from utils import resample_ohlcv

SIGNAL_NAME        = 'BB_SQUEEZE'
BAR_PERIOD_MINUTES = 5


def detect(df_1min: pd.DataFrame) -> pd.DataFrame:
    """
    Squeeze: bandwidth falls below BB_SQUEEZE_FACTOR × its BB_SQUEEZE_LOOKBACK-bar average.
    Signal: first close outside the upper/lower band within BB_LOOKBACK_BARS of a squeeze.
    One signal per breakout episode — resets when price returns inside the bands.
    """
    df = resample_ohlcv(df_1min, BB_SQUEEZE_TF).copy()

    df['bb_mid']    = df['close'].rolling(BB_PERIOD).mean()
    df['bb_std']    = df['close'].rolling(BB_PERIOD).std()
    df['bb_upper']  = df['bb_mid'] + BB_STD_DEV * df['bb_std']
    df['bb_lower']  = df['bb_mid'] - BB_STD_DEV * df['bb_std']
    df['bandwidth'] = (df['bb_upper'] - df['bb_lower']) / df['bb_mid']
    df['bw_avg']    = df['bandwidth'].rolling(BB_SQUEEZE_LOOKBACK).mean()
    df['squeeze']   = df['bandwidth'] < df['bw_avg'] * BB_SQUEEZE_FACTOR
    df['sq_recent'] = df['squeeze'].rolling(BB_LOOKBACK_BARS).max().fillna(0).astype(bool)

    warmup = BB_PERIOD + BB_SQUEEZE_LOOKBACK
    signals = []
    outside_side = None  # tracks current breakout episode to avoid re-firing

    for i in range(warmup, len(df)):
        row = df.iloc[i]
        if pd.isna(row['bb_upper']):
            continue

        above  = row['close'] > row['bb_upper']
        below  = row['close'] < row['bb_lower']
        inside = not above and not below

        if inside:
            outside_side = None
            continue

        if not row['sq_recent']:
            outside_side = None
            continue

        if above and outside_side != 'bull':
            signals.append({'timestamp': df.index[i], 'direction': 'bullish'})
            outside_side = 'bull'
        elif below and outside_side != 'bear':
            signals.append({'timestamp': df.index[i], 'direction': 'bearish'})
            outside_side = 'bear'

    return pd.DataFrame(signals) if signals else pd.DataFrame(columns=['timestamp', 'direction'])
