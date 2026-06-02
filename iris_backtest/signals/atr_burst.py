"""ATR_BURST — ATR expansion (fast > slow × multiplier) on 3-min bars."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from configs import ATR_BURST_TF, ATR_FAST_PERIOD, ATR_SLOW_PERIOD, ATR_EXPANSION_MULT
from utils import resample_ohlcv

SIGNAL_NAME        = 'ATR_BURST'
BAR_PERIOD_MINUTES = 3


def detect(df_1min: pd.DataFrame) -> pd.DataFrame:
    """
    Fires on the first bar where fast ATR crosses above slow ATR × multiplier
    (transition into burst — consecutive burst bars do not re-fire).
    Direction: bullish if close > open on the burst bar, bearish otherwise.
    """
    df = resample_ohlcv(df_1min, ATR_BURST_TF).copy()

    prev_close   = df['close'].shift(1)
    df['tr']     = pd.concat([
        df['high'] - df['low'],
        (df['high'] - prev_close).abs(),
        (df['low']  - prev_close).abs(),
    ], axis=1).max(axis=1)

    df['atr_fast'] = df['tr'].ewm(alpha=1 / ATR_FAST_PERIOD, adjust=False).mean()
    df['atr_slow'] = df['tr'].ewm(alpha=1 / ATR_SLOW_PERIOD, adjust=False).mean()

    df['burst']       = df['atr_fast'] > df['atr_slow'] * ATR_EXPANSION_MULT
    prev_burst        = df['burst'].shift(1).astype(object).fillna(False).astype(bool)
    df['burst_start'] = df['burst'] & ~prev_burst

    starts = df[df['burst_start'] & df['atr_slow'].notna()]
    if starts.empty:
        return pd.DataFrame(columns=['timestamp', 'direction'])

    return pd.DataFrame({
        'timestamp': starts.index,
        'direction': (starts['close'] > starts['open']).map({True: 'bullish', False: 'bearish'}),
    }).reset_index(drop=True)
