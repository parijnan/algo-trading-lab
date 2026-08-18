"""
regime_signal.py — Kronos decision-tree state series (Decision E, plan §10)

Pure annotation, no trading. Computes the four-state regime series over the
full Nifty daily history so the tree's shape can be checked — state coverage,
transition frequency, how often CONFLICTED actually fires — before any engine
code depends on it. Same discipline Phase 0 applied to the calendar: validate
the signal before building on top of it.

Two axes, both already validated elsewhere in the repo, neither of them the
VIX-direction axis that plans/vix-router-research.md closed:

  Containment — research/range_detection/range_detector_pa.py::compute_pa_ranges.
      Gate passed 2026-05-26 (plans/range-detection-research.md §7). Per-bar:
      established vs transient, and a `direction` ('up'/'down'/'initial') set
      by the breakout that created the current range episode.

  Trend — apollo_backtest/technical_indicators.py::SupertrendIndicator, the
      same indicator live-validated in Iris and Apollo, run on 15m and 75m
      bars and combined the way apollo_production/apollo.py::_resolve_direction
      does: agreement across both timeframes is a signal, disagreement isn't.

State assignment (mutually exclusive and total by construction):

  1. TRENDING       no established range; Supertrend 15m+75m agree
  2. CONFLICTED     no established range; Supertrend timeframes disagree
  3. RANGE_ALIGNED  established range; range direction agrees with Supertrend
  4. RANGE_NEUTRAL  established range; no clear bias, or bias disagrees
"""

import os
import sys
import logging

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, 'research', 'range_detection'))
sys.path.insert(0, REPO_ROOT)

from range_detector_pa import compute_pa_ranges          # noqa: E402
from apollo_backtest.technical_indicators import SupertrendIndicator  # noqa: E402

from configs import (                                      # noqa: E402
    NIFTY_INDEX_FILE, OUTPUT_DIR,
    REGIME_MIN_RANGE_BARS as MIN_RANGE_BARS,
    REGIME_BREAKOUT_CONFIRM as BREAKOUT_CONFIRM,
    REGIME_ST_PERIOD as ST_PERIOD,
    REGIME_ST_MULTIPLIER as ST_MULTIPLIER,
    REGIME_CONFIRMATION_DAYS as CONFIRMATION_DAYS,
)

logger = logging.getLogger(__name__)

REGIME_SIGNAL_FILE = os.path.join(OUTPUT_DIR, "regime_signal.csv")


# ---------------------------------------------------------------------------
# Loading — daily bars for the range detector, 15m/75m for Supertrend
# ---------------------------------------------------------------------------

def _load_1min() -> pd.DataFrame:
    df = pd.read_csv(NIFTY_INDEX_FILE, parse_dates=['time_stamp'])
    df['time_stamp'] = pd.to_datetime(df['time_stamp'], utc=False).dt.tz_localize(None)
    return df.set_index('time_stamp').sort_index()


def _resample(df_1m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Day-anchored N-minute OHLC bars from 1-min data, session starting 09:15."""
    agg = df_1m.resample(f'{minutes}min', origin='start_day', offset='9h15min').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'})
    return agg.dropna(subset=['open'])


def _daily(df_1m: pd.DataFrame) -> pd.DataFrame:
    daily = df_1m.resample('1D').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'})
    return daily.dropna(subset=['open'])


# ---------------------------------------------------------------------------
# Containment — daily PA ranges
# ---------------------------------------------------------------------------

def compute_range_state(daily: pd.DataFrame) -> pd.DataFrame:
    """
    Per-day established/transient flag and range direction, aligned to `daily`'s
    index. No lookahead: compute_pa_ranges is causal by construction (each bar's
    range state depends only on bars up to and including it).
    """
    result, episodes = compute_pa_ranges(
        daily.rename(columns=str.lower), start_idx=0,
        min_range_bars=MIN_RANGE_BARS, breakout_confirm=BREAKOUT_CONFIRM)

    ep_by_id = {e['episode_id']: e for e in episodes}
    counts = result.groupby('episode_id').cumcount() + 1
    direction = result['episode_id'].map(lambda eid: ep_by_id.get(eid, {}).get('direction'))

    out = pd.DataFrame(index=result.index)
    out['established'] = counts >= MIN_RANGE_BARS
    out['range_direction'] = direction.where(out['established'])
    out['range_high'] = result['range_high']
    out['range_low'] = result['range_low']
    return out


# ---------------------------------------------------------------------------
# Trend — Supertrend on 15m and 75m, Apollo's agreement rule
# ---------------------------------------------------------------------------

def _st_trend(bars: pd.DataFrame) -> pd.Series:
    """True = bullish (close > Supertrend line), False = bearish, NaN = warmup."""
    st = SupertrendIndicator(period=ST_PERIOD, multiplier=ST_MULTIPLIER).calculate(
        bars.rename(columns=str.title))
    return st['Close'] > st['Supertrend']


def compute_trend_state(df_1m: pd.DataFrame, daily_index: pd.DatetimeIndex) -> pd.Series:
    """
    Apollo's agreement rule (apollo.py::_resolve_direction) applied to the last
    completed 15m and 75m candle as of each daily bar's close. 'bullish',
    'bearish', or None (misaligned — the CONFLICTED-vs-TRENDING split).
    """
    bars_15 = _resample(df_1m, 15)
    bars_75 = _resample(df_1m, 75)
    trend_15 = _st_trend(bars_15).reindex(daily_index, method='ffill')
    trend_75 = _st_trend(bars_75).reindex(daily_index, method='ffill')

    def resolve(row):
        t15, t75 = row['t15'], row['t75']
        if pd.isna(t15) or pd.isna(t75):
            return None
        if t75 and t15:
            return 'bullish'
        if (not t75) and (not t15):
            return 'bearish'
        return None

    frame = pd.DataFrame({'t15': trend_15, 't75': trend_75})
    return frame.apply(resolve, axis=1)


# ---------------------------------------------------------------------------
# Combine into the four-state tree
# ---------------------------------------------------------------------------

def classify(range_state: pd.DataFrame, trend: pd.Series) -> pd.Series:
    established = range_state['established']
    direction = range_state['range_direction']

    aligned = established & (
        ((direction == 'up') & (trend == 'bullish'))
        | ((direction == 'down') & (trend == 'bearish'))
    )
    neutral = established & ~aligned
    trending = (~established) & trend.notna()
    conflicted = (~established) & trend.isna()

    state = pd.Series(index=range_state.index, dtype=object)
    state[aligned] = 'RANGE_ALIGNED'
    state[neutral] = 'RANGE_NEUTRAL'
    state[trending] = 'TRENDING'
    state[conflicted] = 'CONFLICTED'
    return state


def confirm(raw: pd.Series, min_days: int = None) -> pd.Series:
    """
    Sticky, slow-moving version of a raw per-bar series.

    A run of `min_days` (default CONFIRMATION_DAYS) identical raw values is
    required before the candidate replaces the confirmed value; until then the
    PREVIOUS confirmed value carries forward.

    Applied to `trend` alone, not to the combined four-way state label. The
    range side of the tree (`established`, `direction`) is already persistent
    by construction — compute_pa_ranges requires MIN_RANGE_BARS before it
    calls a range established — so re-confirming the whole blob would silently
    re-filter on that already-stable signal too and mostly measure how often
    the noisier trend axis happens to agree for 3 straight days, which is rare
    even when the underlying trend is real. Confirming trend on its own axis
    and combining it with the (already stable) range state avoids that.
    """
    if min_days is None:
        min_days = CONFIRMATION_DAYS
    run_id = (raw != raw.shift(1)).cumsum()
    run_len = raw.groupby(run_id).cumcount() + 1
    candidate = raw.where(run_len >= min_days)

    confirmed = pd.Series(index=raw.index, dtype=object)
    current = None
    for ts, cand in candidate.items():
        if pd.notna(cand):
            current = cand
        confirmed[ts] = current
    return confirmed


def build(refresh: bool = False) -> pd.DataFrame:
    if os.path.exists(REGIME_SIGNAL_FILE) and not refresh:
        logger.info(f"(cached: {REGIME_SIGNAL_FILE} — pass --refresh to rebuild)")
        return pd.read_csv(REGIME_SIGNAL_FILE, parse_dates=['date'], index_col='date')

    df_1m = _load_1min()
    daily = _daily(df_1m)

    range_state = compute_range_state(daily)
    trend = compute_trend_state(df_1m, daily.index)
    state = classify(range_state, trend)

    confirmed_trend = confirm(trend)
    confirmed_state = classify(range_state, confirmed_trend)

    out = pd.DataFrame({
        'state': state,
        'confirmed_state': confirmed_state,
        'established': range_state['established'],
        'range_direction': range_state['range_direction'],
        'range_high': range_state['range_high'],
        'range_low': range_state['range_low'],
        'trend': trend,
        'confirmed_trend': confirmed_trend,
        'close': daily['close'],
    })
    out.index.name = 'date'
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out.to_csv(REGIME_SIGNAL_FILE)
    return out


# ---------------------------------------------------------------------------
# Validation report
# ---------------------------------------------------------------------------

def _report_one(out: pd.DataFrame, col: str, label: str) -> None:
    n = len(out)
    logger.info(f"{label} coverage:")
    counts = out[col].value_counts()
    for state in ('RANGE_ALIGNED', 'RANGE_NEUTRAL', 'TRENDING', 'CONFLICTED'):
        c = int(counts.get(state, 0))
        logger.info(f"  {state:<14} {c:>5}  ({c/n:>5.1%})")
    unclassified = out[col].isna().sum()
    if unclassified:
        logger.info(f"  {'(unclassified)':<14} {unclassified:>5}  ({unclassified/n:>5.1%})")

    changed = (out[col] != out[col].shift(1)) & out[col].shift(1).notna()
    logger.info(f"  {int(changed.sum())} transitions over {n} days ({changed.mean():.1%} of days)")

    run_id = (out[col] != out[col].shift(1)).cumsum()
    run_lengths = out.groupby(run_id)[col].agg(['first', 'size'])
    logger.info(f"  median run length by state:")
    for state in ('RANGE_ALIGNED', 'RANGE_NEUTRAL', 'TRENDING', 'CONFLICTED'):
        lens = run_lengths.loc[run_lengths['first'] == state, 'size']
        if len(lens):
            logger.info(f"    {state:<14} median {lens.median():>4.0f} days, n={len(lens)} episodes")


def report(out: pd.DataFrame) -> None:
    n = len(out)
    logger.info(f"Kronos regime signal — {n} daily bars "
                f"({out.index.min().date()} -> {out.index.max().date()})")
    logger.info("")
    _report_one(out, 'state', 'Raw state (fast-exit trigger — reacts same-day)')
    logger.info("")
    _report_one(out, 'confirmed_state',
               f'Confirmed state ({CONFIRMATION_DAYS}-day persistence — entry/reinforcement)')

    logger.info("")
    logger.info("Year by year confirmed-state share:")
    yearly = out.groupby(out.index.year)['confirmed_state'].value_counts(
        normalize=True).unstack(fill_value=0)
    for state in ('RANGE_ALIGNED', 'RANGE_NEUTRAL', 'TRENDING', 'CONFLICTED'):
        if state not in yearly.columns:
            continue
        row = "  ".join(f"{y}:{yearly.loc[y, state]:.0%}" for y in yearly.index)
        logger.info(f"  {state:<14} {row}")


def run(refresh: bool = False) -> pd.DataFrame:
    out = build(refresh=refresh)
    report(out)
    return out
