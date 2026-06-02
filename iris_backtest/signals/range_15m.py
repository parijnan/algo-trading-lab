"""RANGE_15M — Rolling K-bar range breakout on 15-min bars (K=3, 45-min range window)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from configs import RANGE_15M_TF, RANGE_15M_SETTER_BARS
from signals.range_3m import _detect_range

SIGNAL_NAME        = 'RANGE_15M'
BAR_PERIOD_MINUTES = 15

def detect(df_1min: pd.DataFrame) -> pd.DataFrame:
    return _detect_range(df_1min, RANGE_15M_TF, RANGE_15M_SETTER_BARS)
