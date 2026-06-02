"""RANGE_75M — Rolling K-bar range breakout on 75-min bars (K=1, 75-min range window)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from configs import RANGE_75M_TF, RANGE_75M_SETTER_BARS
from signals.range_3m import _detect_range

SIGNAL_NAME        = 'RANGE_75M'
BAR_PERIOD_MINUTES = 75

def detect(df_1min: pd.DataFrame) -> pd.DataFrame:
    return _detect_range(df_1min, RANGE_75M_TF, RANGE_75M_SETTER_BARS)
