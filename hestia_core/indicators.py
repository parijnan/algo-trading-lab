"""
Hestia's single Supertrend implementation (plan section 2: the data service holds ONE).

Numerically identical to prometheus_functions.compute_st (the same loop, same in-place band ratchet, same trend derivation),
pinned by tests/test_supertrend_production_backtest_parity.py (12 seeded random-walk cases against production and, in the same
file, the backtest copy against production) and re-checked on 41,840 real SILVERMIC 15-minute bars for four parameter sets
(2026-09-28). Always computed from
scratch over the whole series passed in (the ratchet is history-dependent, so it is never resumed).

`df` needs lowercase open/high/low/close columns and a `time_stamp` column (kept as is). Adds `supertrend` (NaN during
warm-up), `trend` (True bullish, False bearish, NA during warm-up) and `trend_flip`.
"""

import pandas as pd


def compute_st(df: pd.DataFrame, period: int, multiplier: float) -> pd.DataFrame:
    d = df.copy().reset_index(drop=True)
    high, low, close = d['high'].astype(float), d['low'].astype(float), d['close'].astype(float)
    tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    hl2 = (high + low) / 2
    upper = (hl2 + multiplier * atr).tolist()
    lower = (hl2 - multiplier * atr).tolist()
    closes = close.tolist()

    trend = True
    st = []
    for i in range(len(d)):
        if i < period:
            st.append(float('nan'))
            continue
        prev_upper, prev_lower = upper[i - 1], lower[i - 1]
        if closes[i] > prev_upper:
            trend = True
        elif closes[i] < prev_lower:
            trend = False
        if trend and lower[i] < prev_lower:
            lower[i] = prev_lower
        if not trend and upper[i] > prev_upper:
            upper[i] = prev_upper
        st.append(lower[i] if trend else upper[i])

    d['supertrend'] = pd.to_numeric(pd.Series(st), errors='coerce')
    d['trend'] = (d['close'] > d['supertrend']).astype(object)
    d.loc[d['supertrend'].isna(), 'trend'] = pd.NA
    d['trend_flip'] = d['trend'] != d['trend'].shift(1)
    d.loc[d['supertrend'].isna(), 'trend_flip'] = False
    first_valid = d['supertrend'].first_valid_index()
    if first_valid is not None:
        d.loc[first_valid, 'trend_flip'] = False
    return d
