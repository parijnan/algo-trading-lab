"""ORB_15 — Opening Range Breakout, 15-min window."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from configs import ORB_MINUTES

SIGNAL_NAME        = 'ORB_15'
BAR_PERIOD_MINUTES = 1


def _detect_orb(df_1min: pd.DataFrame, orb_minutes: int) -> pd.DataFrame:
    """
    Opening range = high/low of the first orb_minutes 1-min bars (09:15 to 09:15+N).
    Signal = first 1-min close that breaks above range_high (bullish) or
    below range_low (bearish). One signal per day maximum.
    """
    signals = []
    for date, day in df_1min.groupby(df_1min.index.date):
        open_ts = pd.Timestamp(f'{date} 09:15:00')
        orb_end = open_ts + pd.Timedelta(minutes=orb_minutes)

        orb_bars = day[(day.index >= open_ts) & (day.index < orb_end)]
        post_orb = day[day.index >= orb_end]

        if len(orb_bars) < orb_minutes - 2 or post_orb.empty:
            continue

        range_high = orb_bars['high'].max()
        range_low  = orb_bars['low'].min()

        fired = False
        for ts, row in post_orb.iterrows():
            if not fired and row['close'] > range_high:
                signals.append({'timestamp': ts, 'direction': 'bullish'})
                fired = True
            elif not fired and row['close'] < range_low:
                signals.append({'timestamp': ts, 'direction': 'bearish'})
                fired = True

    return pd.DataFrame(signals) if signals else pd.DataFrame(columns=['timestamp', 'direction'])


def detect(df_1min: pd.DataFrame) -> pd.DataFrame:
    return _detect_orb(df_1min, ORB_MINUTES)
