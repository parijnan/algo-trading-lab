"""ROC_BURST — Rate-of-change momentum burst on 1-min bars."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from configs import ROC_PERIOD, ROC_THRESHOLD

SIGNAL_NAME        = 'ROC_BURST'
BAR_PERIOD_MINUTES = 1


def detect(df_1min: pd.DataFrame) -> pd.DataFrame:
    """
    ROC = (close - close[N bars ago]) / close[N bars ago] × 100.
    Signal fires on the first bar where ROC crosses above ROC_THRESHOLD (bullish)
    or below -ROC_THRESHOLD (bearish) — i.e., transition into burst only.
    """
    df      = df_1min.copy()
    df['roc']      = (df['close'] - df['close'].shift(ROC_PERIOD)) / df['close'].shift(ROC_PERIOD) * 100
    df['prev_roc'] = df['roc'].shift(1)

    bull = (df['roc'] >  ROC_THRESHOLD) & (df['prev_roc'] <=  ROC_THRESHOLD)
    bear = (df['roc'] < -ROC_THRESHOLD) & (df['prev_roc'] >= -ROC_THRESHOLD)

    bull_df = df[bull & df['prev_roc'].notna()].copy()
    bear_df = df[bear & df['prev_roc'].notna()].copy()

    signals = pd.concat([
        pd.DataFrame({'timestamp': bull_df.index, 'direction': 'bullish'}),
        pd.DataFrame({'timestamp': bear_df.index, 'direction': 'bearish'}),
    ]).sort_values('timestamp').reset_index(drop=True)

    return signals if not signals.empty else pd.DataFrame(columns=['timestamp', 'direction'])
