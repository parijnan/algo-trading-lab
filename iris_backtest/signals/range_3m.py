"""RANGE_3M — Rolling K-bar range breakout on 3-min bars (K=6, 18-min range window)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from configs import RANGE_3M_TF, RANGE_3M_SETTER_BARS
from utils import resample_ohlcv, intraday_kbar_range

SIGNAL_NAME        = 'RANGE_3M'
BAR_PERIOD_MINUTES = 3


def detect(df_1min: pd.DataFrame) -> pd.DataFrame:
    return _detect_range(df_1min, RANGE_3M_TF, RANGE_3M_SETTER_BARS)


def _detect_range(df_1min: pd.DataFrame, tf: str, k: int) -> pd.DataFrame:
    """
    Resample to tf, compute rolling K-bar high/low (previous K bars, per-day reset).
    Signal fires on each transition from inside → outside the rolling range.
    Multiple signals per day possible as range updates each bar.
    """
    df = intraday_kbar_range(resample_ohlcv(df_1min, tf), k)

    bull = df['close'] > df['range_high']
    bear = df['close'] < df['range_low']

    bull_start = bull & ~bull.shift(1, fill_value=False) & df['range_high'].notna()
    bear_start = bear & ~bear.shift(1, fill_value=False) & df['range_low'].notna()

    signals = pd.concat([
        pd.DataFrame({'timestamp': df.index[bull_start], 'direction': 'bullish'}),
        pd.DataFrame({'timestamp': df.index[bear_start], 'direction': 'bearish'}),
    ]).sort_values('timestamp').reset_index(drop=True)

    return signals if not signals.empty else pd.DataFrame(columns=['timestamp', 'direction'])
