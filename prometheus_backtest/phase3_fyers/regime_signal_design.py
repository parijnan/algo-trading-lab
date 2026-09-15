"""
Prometheus - Phase 3 (Fyers track): can a regime shift be detected causally,
not just confirmed in hindsight?

Direct follow-up to the user's question (2026-09-15, "forget the trades
for now"): what technique would separate the pre-2026-03-02 regime from
the post-2026-03-02 one? That question splits into two genuinely
different problems, and conflating them produces a circular answer:

  (a) RETROSPECTIVE: did a structural break happen? Any breakpoint
      algorithm run on the full series will "find" 2026-03-02 -- but the
      user already pointed at that date from the chart, so a retrospective
      test confirming it proves the algorithm can find a break it was
      shown, not that the technique is useful going forward. Used here
      only as a one-line sanity check, not the main deliverable.

  (b) CAUSAL: could Prometheus have known in real time, using only a
      trailing window of already-closed bars? This is what actually
      matters for #4/#5 (can Prometheus itself become regime-aware) --
      and it's the only one of the two that can't cheat by looking at
      the future. This script answers (b).

Two measures compared for (b), since Prometheus is a Supertrend
(trend-following) system, not a pure volatility strategy -- what should
matter is whether moves PERSIST, not just how big they are:
  - ATR(14)/price (already the strategy's own vocabulary -- Supertrend is
    built on ATR), aggregated to one value per trading day.
  - Kaufman's Efficiency Ratio (ER), a standard trend-persistence measure:
    |close[t] - close[t-N]| / sum(|close[i]-close[i-1]|) over a rolling
    N-day window -- 1.0 means every day's move was in the same direction
    (a pure trend), near 0 means moves cancelled out (chop), independent
    of how large those moves were.

For each measure and a range of trailing smoothing windows, reports:
  1. the date in 2026 a causal "elevated regime" flag first turns on
     (percentile threshold computed ONLY from data available up to that
     point -- an expanding/trailing lookback, never the future)
  2. how many times that flag flips across 2023-2025 -- the whipsaw cost
     of a given window length, not just its detection speed

**The n=1 caveat, stated explicitly rather than glossed over**: this
dataset has exactly ONE regime transition. Any detector's design choices
here (window length, threshold percentile) are being picked with
foreknowledge of where the one transition is -- so this script's output
is a design exploration, not a validated detector. The stronger existing
evidence for "volatility regime matters" is the pre-2026 top-decile-ATR%
result from regime_gate_walkforward.py's own groundwork (80 trades
spanning multiple separate 2023-2025 episodes, ~6x expectancy) -- that
spans several transitions, not one. CRUDEOIL's own Fyers-sourced history
(back to 2021-09-20, includes the 2022 oil-vol regime) is a real path to
a genuine second transition for out-of-sample detector validation, not
started here.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configs_p3 as configs  # noqa: E402
from data_loader_fyers import load_futures_1min, resample_ohlcv  # noqa: E402

CUTOFF = pd.Timestamp('2026-03-02')
ATR_PERIOD = 14
ER_WINDOW_DAYS = 20          # Kaufman's own standard default
PCTILE_LOOKBACK_DAYS = 250   # ~1 trading year, causal/expanding until it fills
FLAG_PERCENTILE = 0.80
SMOOTHING_WINDOWS = [5, 10, 20, 30, 40]   # trading days


def _daily_atr_pct(df_15m: pd.DataFrame) -> pd.Series:
    high, low, close = df_15m['high'], df_15m['low'], df_15m['close']
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr_pct_15m = (tr.rolling(ATR_PERIOD).mean() / close * 100)
    return atr_pct_15m.groupby(atr_pct_15m.index.date).last().rename('atr_pct_daily')


def _daily_efficiency_ratio(daily_close: pd.Series, window: int) -> pd.Series:
    direction = (daily_close - daily_close.shift(window)).abs()
    volatility = daily_close.diff().abs().rolling(window).sum()
    er = (direction / volatility).replace([np.inf, -np.inf], np.nan)
    return er.rename('efficiency_ratio')


def _causal_flag_stats(series: pd.Series, smoothing_days: int) -> dict:
    """A day's flag is ON if its own trailing `smoothing_days`-mean of
    `series` exceeds the FLAG_PERCENTILE of the series' own trailing
    PCTILE_LOOKBACK_DAYS window -- both computed using only data up to and
    including that day (shift(1)-free by construction: pandas rolling/
    expanding windows ending at index i use only i and earlier)."""
    smoothed = series.rolling(smoothing_days).mean()
    threshold = smoothed.rolling(PCTILE_LOOKBACK_DAYS, min_periods=60).quantile(FLAG_PERCENTILE)
    flag = smoothed >= threshold
    flag = flag.dropna()

    flips = int((flag != flag.shift(1)).sum())
    on_2026 = flag[flag.index >= pd.Timestamp('2026-01-01')]
    first_on = on_2026[on_2026].index.min() if on_2026.any() else None
    return {'smoothing_days': smoothing_days, 'first_flag_on_2026': first_on,
            'days_late_vs_2026_03_02': (pd.Timestamp(first_on) - CUTOFF).days if first_on else None,
            'total_flips_full_history': flips}


def main():
    print('Loading 1m data, resampling to 15m and daily...')
    df_1m = load_futures_1min(configs.SYMBOL)
    df_15m = resample_ohlcv(df_1m, '15min')
    daily_close = df_1m['close'].groupby(df_1m.index.date).last()
    daily_close.index = pd.to_datetime(daily_close.index)

    atr_daily = _daily_atr_pct(df_15m)
    atr_daily.index = pd.to_datetime(atr_daily.index)
    er_daily = _daily_efficiency_ratio(daily_close, ER_WINDOW_DAYS)

    # --- (a) one-line retrospective sanity check ---
    pre = atr_daily[atr_daily.index < CUTOFF]
    post = atr_daily[atr_daily.index >= CUTOFF]
    print(f'\n(a) Retrospective sanity check only (not the main answer -- see module docstring):')
    print(f'    ATR%/price daily mean: pre-cutoff {pre.mean():.3f}%  ->  post-cutoff {post.mean():.3f}%  '
          f'({post.mean()/pre.mean():.1f}x)')

    # --- which measure separates the two windows more cleanly? ---
    er_pre = er_daily[er_daily.index < CUTOFF].dropna()
    er_post = er_daily[er_daily.index >= CUTOFF].dropna()
    atr_post_pctile_in_pre = (pre < post.mean()).mean() * 100
    er_post_pctile_in_pre = (er_pre < er_post.mean()).mean() * 100
    print(f'\nWhich measure separates pre- vs post-2026-03-02 more cleanly?')
    print(f'    ATR%/price: post-window mean sits at the {atr_post_pctile_in_pre:.1f}th percentile of the pre-window distribution')
    print(f'    Efficiency Ratio ({ER_WINDOW_DAYS}d): post-window mean sits at the {er_post_pctile_in_pre:.1f}th percentile of the pre-window distribution')
    print(f'    (100th = the post-window mean exceeds every pre-window observation -- perfect separation)')

    # --- (b) causal detection: the actual answer ---
    print(f'\n(b) Causal detection -- flag ON when trailing N-day mean exceeds the '
          f'{FLAG_PERCENTILE:.0%}ile of its own trailing {PCTILE_LOOKBACK_DAYS}-day history (no future data used):\n')
    for name, series in [('ATR%/price', atr_daily), ('Efficiency Ratio', er_daily)]:
        print(f'  --- {name} ---')
        rows = [_causal_flag_stats(series, w) for w in SMOOTHING_WINDOWS]
        print(pd.DataFrame(rows).to_string(index=False))
        print()


if __name__ == '__main__':
    main()
