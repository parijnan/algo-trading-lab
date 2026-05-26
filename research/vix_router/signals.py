"""
signals.py — pure signal functions for VIX-direction forecasting.

Each function takes daily DataFrames (indexed by date) and returns a date-indexed Series.
All signals are no-lookahead by construction: they use only data available at a 10:30 entry
(i.e. the previous completed daily close). The caller decides when to join signals to entry
dates — these functions produce the full time series.

Parameter budget (§10 guardrail): ≤ 2–3 params across the entire forecaster.
Currently: vrp_window ∈ {10, 20}, bb_window = 20 (fixed), zscore_window ∈ {20, 50}.
"""

import numpy as np
import pandas as pd


def vrp(vix_close: pd.Series, nifty_close: pd.Series, window: int = 10) -> pd.Series:
    """
    Variance Risk Premium: VIX_close(t) − annualised realised Nifty vol over trailing window.

    VIX is already annualised (%). Realised vol = std(log returns, window) * sqrt(252) * 100.
    Positive VRP → VIX is rich relative to realised → expected to fall (Artemis signal).
    Negative VRP → realised has caught up → VIX may rise (Athena signal).

    Both series must be indexed by the same dates. Returns NaN for the first `window` rows.
    """
    log_ret = np.log(nifty_close / nifty_close.shift(1))
    rv = log_ret.rolling(window).std() * np.sqrt(252) * 100
    # Align on the intersection of dates
    aligned_vix, aligned_rv = vix_close.align(rv, join='inner')
    return (aligned_vix - aligned_rv).rename(f'vrp_{window}')


def bb_pct(vix_close: pd.Series, window: int = 20, num_std: float = 2.0) -> pd.Series:
    """
    VIX Bollinger Band %B: position of VIX within its rolling Bollinger Bands.
    %B = (VIX − lower) / (upper − lower)
    0 = at lower band, 1 = at upper band. Values outside [0,1] indicate band breaches.

    Low %B → VIX near multi-day low → may signal calm/decaying regime (Artemis).
    High %B → VIX elevated within recent range → may signal rising pressure (Athena).

    Matches the vix_bb_pct computed in annotate_athena.py (window=20, std=2.0).
    """
    sma   = vix_close.rolling(window).mean()
    sigma = vix_close.rolling(window).std(ddof=1)
    upper = sma + num_std * sigma
    lower = sma - num_std * sigma
    band_width = upper - lower
    pct = (vix_close - lower) / band_width
    return pct.rename('bb_pct')


def zscore(vix_close: pd.Series, window: int = 20) -> pd.Series:
    """
    VIX z-score relative to its rolling mean/std.
    z(t) = (VIX(t) − SMA_m(t)) / STD_m(t)
    High z → VIX elevated; may mean-revert down. Low z → compressed; may stay low or rise.
    """
    sma   = vix_close.rolling(window).mean()
    sigma = vix_close.rolling(window).std(ddof=1)
    z = (vix_close - sma) / sigma
    return z.rename(f'zscore_{window}')


def bb_zone(pct: pd.Series) -> pd.Series:
    """
    Categorical zone from %B value (mirrors annotate_athena.py categorisation).
    above_upper: >1.0
    upper_zone:  0.7–1.0
    mid_zone:    0.3–0.7
    lower_zone:  0.0–0.3
    below_lower: <0.0
    """
    def _zone(v):
        if pd.isna(v):
            return None
        if v > 1.0:
            return 'above_upper'
        if v >= 0.7:
            return 'upper_zone'
        if v >= 0.3:
            return 'mid_zone'
        if v >= 0.0:
            return 'lower_zone'
        return 'below_lower'
    return pct.map(_zone).rename('bb_zone')
