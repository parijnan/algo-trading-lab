"""
Prometheus - Phase 4: 1h/15m ST alignment entry filter (plan §17 preview,
mechanism built into prometheus_production/ 2026-09-04, gated off pending
this backtest). Sole source of truth for Phase 4's own parameters.

Design (user-specified, 2026-09-04):
  1. ST_15 held FIXED at Phase 3's decided mult-2.0 candidate -- the signal
     and exits already live in prometheus_production/. Phase 4 only varies
     the 1h filter's own ST_PERIOD/ST_MULTIPLIER on top of that, one
     variable changed per experiment (CLAUDE.md convention).
  2. The filter can only REMOVE entries a flip would otherwise take, never
     add one -- so it's evaluated by filtering the already-generated raw
     ST_15 mult-2.0 trade list (prometheus_backtest/phase3/data_sweep/
     mult_2.0/), not a second parallel backtest engine. Valid because ST_15
     flips are price-only, position-independent: blocking one entry never
     moves any other trade's own signal_ts/exit_ts.
  3. No lookahead: alignment at a flip is checked against the LAST FULLY
     CLOSED 1h bar as of that flip's own decision time (signal_ts + 15min,
     the moment the 15m bar itself closes and the flip becomes known) --
     never the in-progress 1h bucket. Same bug class as the look-ahead
     found and fixed in Phase 1's 15-min regime filter, just a 6x larger
     window (60min vs 15min) if it were to leak.
  4. 1h resample reuses prometheus_backtest/data_loader.py's resample_ohlcv
     UNCHANGED (verified 2026-09-04: origin=day.index[0] already builds a
     genuine trailing partial bucket rather than dropping it -- confirmed
     against a 23:29-close day (30 real minutes) and a 23:54-close day (55
     real minutes), and already anchors each day at ITS OWN first bar,
     which the user confirmed is correct behaviour for the 7/153 days that
     are evening-only special sessions starting 17:00, not 09:00 -- their
     chart shows the first candle of those sessions at 17:00 too).
"""

import os

import pandas as pd

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
PROMETHEUS_DIR = os.path.dirname(BASE_DIR)
REPO_ROOT      = os.path.dirname(PROMETHEUS_DIR)
PHASE3_DIR     = os.path.join(PROMETHEUS_DIR, 'phase3')

MCX_DATA_DIR           = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'mcx')
INSTRUMENT_MASTER_FILE = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'mcx_instrument_master.csv')

SYMBOL = 'CRUDEOILM'

DATA_DIR       = os.path.join(BASE_DIR, 'data')
DATA_SWEEP_DIR = os.path.join(BASE_DIR, 'data_sweep')

# Phase 3's own sweep output this reuses as the unfiltered baseline --
# NOT regenerated here, read as-is (re-run prometheus_backtest/phase3/
# sweep_p3.py first if the underlying 1-min data has changed).
PHASE3_SWEEP_DIR = os.path.join(PHASE3_DIR, 'data_sweep')


def _lookup_lot_size(symbol: str) -> int:
    df = pd.read_csv(INSTRUMENT_MASTER_FILE)
    rows = df[df['name'] == symbol]
    if rows.empty:
        raise ValueError(f"No instrument master rows found for '{symbol}' in {INSTRUMENT_MASTER_FILE}")
    return int(rows.iloc[0]['lotsize'])


LOT_SIZE = _lookup_lot_size(SYMBOL)

# ---------------------------------------------------------------------------
# ST_15 -- FIXED at Phase 3's DECIDED mult-2.0 candidate, live in
# prometheus_production/ since 2026-09-04. Phase 4 never re-derives this.
# ---------------------------------------------------------------------------
ST_15_PERIOD     = 10
ST_15_MULTIPLIER = 2.0

# Bespoke exits -- Phase 3's mult-2.0 calibrated combo (prometheus_backtest/
# phase3/README.md's Phase 3 section), also held fixed.
SL_PCT            = 2.2
TARGET1_PCT       = 2.0
TARGET2_FLAT_PCT  = 5.0

# ---------------------------------------------------------------------------
# 1h alignment filter -- the thing actually under test this phase. The
# single-cell (10, 2.0) run (2026-09-04) showed anti-selection: blocked-set
# Calmar (5.15) beat kept-set Calmar (2.05), i.e. the filter preferentially
# passed the WORSE trades. advisor() diagnosed this as likely a
# responsiveness mismatch -- 60-min bars at period 10 look back ~10 hours
# (~0.7 trading days), far slower than ST_15's own period-10 (~2.5 hours),
# so the 1h trend stays pinned across many 15m flips and isn't really
# "regime confirmation" so much as a near-random long-lived gate. Grid
# widened to test faster 1h settings, per advisor guidance -- period 3 is
# the responsiveness floor worth testing (below that the 60-min ATR is too
# noisy to mean anything); multiplier lowered in step since a smaller
# multiplier also tightens the band and increases flip frequency.
# ---------------------------------------------------------------------------
ST_1H_PERIOD_GRID     = [3, 4, 5, 7, 10]
ST_1H_MULTIPLIER_GRID = [1.0, 1.5, 2.0, 2.5, 3.0]

# Decision-time offset: a 15m flip's signal_ts is the flip BAR's own
# start-label timestamp (backtest_p3.py's df_15m.iterrows() ts); the real
# decision only happens once that bar's own window has closed, +15min
# later -- matching when prometheus.py's _handle_new_15m_bar actually runs
# in production. NOT entry_ts (the fill bar), which can be +15min OR a full
# overnight gap away depending on whether the flip landed on a day's last bar.
DECISION_OFFSET_MIN = 15
