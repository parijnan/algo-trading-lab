"""ORB_45 — Opening Range Breakout, 45-min window."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from signals.orb import _detect_orb

SIGNAL_NAME        = 'ORB_45'
BAR_PERIOD_MINUTES = 1

def detect(df_1min):
    return _detect_orb(df_1min, 45)
