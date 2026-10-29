"""
Hestia's single Supertrend implementation (plan section 2: the data service holds ONE).

The standard (Zerodha / TradingView) Supertrend: the final bands keep their ratchet until the PREVIOUS close breaks through the
band, and the line follows the final band of the side it sits on. Reproduces Zerodha's 15-minute Supertrend(10, 2.5) column on
CRUDEOILM exactly (tests/test_supertrend_zerodha_fixture.py). Until 2026-10-28 this module used a different ratchet (it reset the
band on every bar of the opposite phase), which on 28 Oct 2026 delayed a Prometheus entry by 45 minutes (Zerodha flipped at the
19:45 bar, Hestia at 20:30).

Numerically identical to prometheus_functions.compute_st and the backtest copy (prometheus_backtest/data_loader.compute_st),
pinned by tests/test_supertrend_production_backtest_parity.py. Always computed from
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
    ub = (hl2 + multiplier * atr).tolist()
    lb = (hl2 - multiplier * atr).tolist()
    closes = close.tolist()

    # final bands: a band moves to the raw value when that tightens it or when the previous close broke through it
    n = len(d)
    fub, flb, line = list(ub), list(lb), [float('nan')] * n
    for i in range(1, n):
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or closes[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or closes[i - 1] < flb[i - 1]) else flb[i - 1]
        if i == 1:
            line[i] = fub[i]
        elif line[i - 1] == fub[i - 1]:
            line[i] = fub[i] if closes[i] <= fub[i] else flb[i]
        else:
            line[i] = flb[i] if closes[i] >= flb[i] else fub[i]
    st = [float('nan') if i < period else line[i] for i in range(n)]

    d['supertrend'] = pd.to_numeric(pd.Series(st), errors='coerce')
    d['trend'] = (d['close'] > d['supertrend']).astype(object)
    d.loc[d['supertrend'].isna(), 'trend'] = pd.NA
    d['trend_flip'] = d['trend'] != d['trend'].shift(1)
    d.loc[d['supertrend'].isna(), 'trend_flip'] = False
    first_valid = d['supertrend'].first_valid_index()
    if first_valid is not None:
        d.loc[first_valid, 'trend_flip'] = False
    return d
