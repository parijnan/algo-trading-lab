"""ORB_60 — Opening Range Breakout, 60-min window."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from signals.orb import _detect_orb

SIGNAL_NAME        = 'ORB_60'
BAR_PERIOD_MINUTES = 1

def detect(df_1min):
    return _detect_orb(df_1min, 60)
