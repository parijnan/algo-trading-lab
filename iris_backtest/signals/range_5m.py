"""RANGE_5M — Rolling K-bar range breakout on 5-min bars (K=6, 30-min range window)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from configs import RANGE_5M_TF, RANGE_5M_SETTER_BARS
from signals.range_3m import _detect_range

SIGNAL_NAME        = 'RANGE_5M'
BAR_PERIOD_MINUTES = 5

def detect(df_1min: pd.DataFrame) -> pd.DataFrame:
    return _detect_range(df_1min, RANGE_5M_TF, RANGE_5M_SETTER_BARS)
