from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
IRIS_ROOT = Path(__file__).parent

NIFTY_1MIN_FILE  = REPO_ROOT / 'data_pipeline' / 'data' / 'indices' / 'nifty.csv'
NIFTY_DAILY_FILE = REPO_ROOT / 'data_pipeline' / 'data' / 'indices' / 'nifty_daily.csv'
VIX_1MIN_FILE    = REPO_ROOT / 'data_pipeline' / 'data' / 'indices' / 'india_vix.csv'
OUTPUT_DIR       = IRIS_ROOT / 'data'

# Excursion analysis horizons (1-min bars after entry)
HORIZONS = [5, 10, 15, 30, 60, 120]

# ── ST_FAST: Dual supertrend 5m entry + 15m regime ──────────────────────────
ST_FAST_ENTRY_TF   = '5min'
ST_FAST_REGIME_TF  = '15min'
ST_FAST_PERIOD     = 10
ST_FAST_MULTIPLIER = 3.0

# ── ST_RAPID: Dual supertrend 3m entry + 9m regime ──────────────────────────
ST_RAPID_ENTRY_TF   = '3min'
ST_RAPID_REGIME_TF  = '9min'
ST_RAPID_PERIOD     = 10
ST_RAPID_MULTIPLIER = 3.0

# ── EMA_CROSS: Fast/slow EMA crossover on 3-min bars ────────────────────────
EMA_CROSS_TF   = '3min'
EMA_FAST_PERIOD = 9
EMA_SLOW_PERIOD = 21

# ── BB_SQUEEZE: Bollinger Band squeeze → breakout on 5-min bars ─────────────
BB_SQUEEZE_TF      = '5min'
BB_PERIOD          = 20
BB_STD_DEV         = 2.0
BB_SQUEEZE_LOOKBACK = 20   # rolling window to measure "typical" bandwidth
BB_SQUEEZE_FACTOR  = 0.75  # squeeze when bandwidth < 75% of its lookback average
BB_LOOKBACK_BARS   = 5     # recent-squeeze window: breakout must follow squeeze within N bars

# ── ORB family: Opening Range Breakout on 1-min bars ────────────────────────
# ORB_15 uses ORB_MINUTES; ORB_30/45/60/75 are hardcoded in their files
ORB_MINUTES = 15           # minutes from 09:15 that define the opening range

# ── ATR_BURST: ATR expansion + directional candle on 3-min bars ─────────────
ATR_BURST_TF       = '3min'
ATR_FAST_PERIOD    = 5
ATR_SLOW_PERIOD    = 20
ATR_EXPANSION_MULT = 1.5   # ATR_fast > ATR_slow × this → burst state

# ── ROC_BURST: Rate-of-change burst on 1-min bars ───────────────────────────
ROC_PERIOD    = 5          # bars over which ROC is computed
ROC_THRESHOLD = 0.2        # % move in ROC_PERIOD bars to qualify as a burst

# ── Intraday K-bar rolling range breakout ────────────────────────────────────
# Range = high/low of previous K bars on the given TF (per-day reset).
# Signal fires when close crosses that rolling range boundary.
# K controls the range-formation window; larger K = wider, slower-moving range.
#
# 3-min  K=6  → 18-min range window; first signal eligible after 09:36
# 5-min  K=6  → 30-min range window; first signal eligible after 09:50
# 15-min K=3  → 45-min range window; first signal eligible after 10:15
# 75-min K=1  → 75-min range window; first signal eligible after 10:30
RANGE_3M_TF          = '3min'
RANGE_3M_SETTER_BARS = 6

RANGE_5M_TF          = '5min'
RANGE_5M_SETTER_BARS = 6

RANGE_15M_TF          = '15min'
RANGE_15M_SETTER_BARS = 3

RANGE_75M_TF          = '75min'
RANGE_75M_SETTER_BARS = 1
