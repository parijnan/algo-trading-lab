"""RANGE_BREAK — ADX-gated daily range breakout detected on 1-min bars."""
import sys
from pathlib import Path
_IRIS_ROOT = Path(__file__).parent.parent
_REPO_ROOT = _IRIS_ROOT.parent
sys.path.insert(0, str(_IRIS_ROOT))
sys.path.insert(0, str(_REPO_ROOT / 'research' / 'range_detection'))

import pandas as pd
import range_detector
from configs import (RANGE_BREAK_ADX_THRESHOLD, RANGE_BREAK_ADX_PERIOD,
                     RANGE_BREAK_SWING_STRENGTH)
from utils import load_nifty_daily

SIGNAL_NAME        = 'RANGE_BREAK'
BAR_PERIOD_MINUTES = 1


def detect(df_1min: pd.DataFrame) -> pd.DataFrame:
    """
    Uses daily Nifty OHLC to identify ranging episodes (ADX < threshold) and
    compute range_high/range_low bounds. Fires on the first intraday 1-min close
    that breaks those bounds — one signal per day maximum.
    Only fires on days classified as ranging by the ADX regime filter.
    """
    daily  = load_nifty_daily()
    adx    = range_detector.compute_adx(daily, RANGE_BREAK_ADX_PERIOD)
    sh, sl = range_detector.find_swings(daily, RANGE_BREAK_SWING_STRENGTH)
    ranges = range_detector.compute_ranges(daily, adx, sh, sl, RANGE_BREAK_ADX_THRESHOLD)

    ranges.index = pd.to_datetime(ranges.index).normalize()

    signals = []
    for date, day in df_1min.groupby(df_1min.index.date):
        date_key = pd.Timestamp(date)
        if date_key not in ranges.index:
            continue
        rrow = ranges.loc[date_key]
        if not rrow['is_ranging']:
            continue
        range_high = rrow['range_high']
        range_low  = rrow['range_low']
        if pd.isna(range_high) or pd.isna(range_low):
            continue

        fired = False
        for ts, bar in day.iterrows():
            if not fired and bar['close'] > range_high:
                signals.append({'timestamp': ts, 'direction': 'bullish'})
                fired = True
            elif not fired and bar['close'] < range_low:
                signals.append({'timestamp': ts, 'direction': 'bearish'})
                fired = True

    return pd.DataFrame(signals) if signals else pd.DataFrame(columns=['timestamp', 'direction'])
