"""EMA_CROSS — Fast/slow EMA crossover on 3-min bars."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from configs import EMA_CROSS_TF, EMA_FAST_PERIOD, EMA_SLOW_PERIOD
from utils import resample_ohlcv

SIGNAL_NAME        = 'EMA_CROSS'
BAR_PERIOD_MINUTES = 3


def detect(df_1min: pd.DataFrame) -> pd.DataFrame:
    df = resample_ohlcv(df_1min, EMA_CROSS_TF).copy()
    df['ema_fast'] = df['close'].ewm(span=EMA_FAST_PERIOD, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=EMA_SLOW_PERIOD, adjust=False).mean()
    df['above']    = df['ema_fast'] > df['ema_slow']
    df['flip']     = df['above'] != df['above'].shift(1)
    df.iloc[0, df.columns.get_loc('flip')] = False

    flips = df[df['flip']]
    if flips.empty:
        return pd.DataFrame(columns=['timestamp', 'direction'])

    return pd.DataFrame({
        'timestamp': flips.index,
        'direction': flips['above'].map({True: 'bullish', False: 'bearish'}),
    }).reset_index(drop=True)
