"""
Prometheus - Phase 3 (Fyers track): does the underlying's own price
drawdown-from-peak correlate with realized volatility?

Direct follow-up to the user's question (2026-09-15): are there "micro
regimes" -- stretches where the mult-2.0 bespoke combo did well even
inside the broader calm period, followed by deep drawdown stretches --
and do these correlate with volatility?

Deliberately NOT the first step toward an ML regime classifier. Labeling
"micro regime" periods by how the STRATEGY's own P&L behaved and then
training a classifier on those labels would be circular: the exit
parameters (SL/T1/T2) were fit in-sample on a ~7-month window, so those
labels encode where that specific fitted combo happened to work, not a
property of the market. A classifier trained on them would learn the
calibration's idiosyncrasies and score beautifully in cross-validation
for exactly the wrong reason.

This script instead tests the underlying hypothesis directly, using only
the underlying's own PRICE (never strategy P&L) for both sides of the
comparison -- no labels, no model, no overfitting surface:
  - drawdown_pct: the underlying's own daily close vs. its running peak
    (a pure price-series measure of "how far into a decline are we")
  - atr_pct: realized ATR(14)/price, daily (same measure as
    regime_signal_design.py)

Reports the contemporaneous correlation, a bucketed table (matching the
"scatter or bucketed table" the hypothesis needs, not a p-value), and a
lead-lag check (does today's volatility predict FUTURE drawdown, or does
volatility just rise alongside/after a decline that's already underway --
relevant if this is ever meant to be used prospectively, not just
described in hindsight).

If a real correlation exists here, it's evidence worth building on. If it
doesn't, no amount of gradient boosting on engineered features will
manufacture one -- that's the whole reason to check this first.
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configs_p3 as configs  # noqa: E402
from data_loader_fyers import load_futures_1min, resample_ohlcv  # noqa: E402

ATR_PERIOD = 14
LEAD_LAG_DAYS = [-10, -5, -1, 0, 1, 5, 10]   # negative = past ATR% vs today's drawdown
ROLLING_PEAK_WINDOWS = [20, 60]   # trailing trading days -- local/"micro" peak, not all-time-high


def _daily_atr_pct(df_15m: pd.DataFrame) -> pd.Series:
    high, low, close = df_15m['high'], df_15m['low'], df_15m['close']
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr_pct_15m = tr.rolling(ATR_PERIOD).mean() / close * 100
    return atr_pct_15m.groupby(atr_pct_15m.index.date).last().rename('atr_pct')


def main():
    print('Loading 1m data, resampling to 15m and daily...')
    df_1m = load_futures_1min(configs.SYMBOL)
    df_15m = resample_ohlcv(df_1m, '15min')

    daily_close = df_1m['close'].groupby(df_1m.index.date).last()
    daily_close.index = pd.to_datetime(daily_close.index)
    atr_pct = _daily_atr_pct(df_15m)
    atr_pct.index = pd.to_datetime(atr_pct.index)

    running_peak = daily_close.cummax()
    drawdown_pct = (daily_close - running_peak) / running_peak * 100   # <= 0

    df = pd.DataFrame({'close': daily_close, 'drawdown_pct': drawdown_pct, 'atr_pct': atr_pct}).dropna()
    print(f'{len(df)} trading days, {df.index.min().date()} -> {df.index.max().date()}\n')

    # --- contemporaneous relationship, price-only, no strategy P&L anywhere ---
    corr = df['drawdown_pct'].corr(df['atr_pct'])
    corr_abs = df['drawdown_pct'].abs().corr(df['atr_pct'])
    print(f'Correlation(drawdown_pct, atr_pct):       {corr:.3f}')
    print(f'Correlation(|drawdown_pct|, atr_pct):     {corr_abs:.3f}  (does depth-of-drawdown scale with vol, regardless of direction)\n')

    df['dd_bucket'] = pd.qcut(df['drawdown_pct'], 5, labels=['Q1(deepest DD)', 'Q2', 'Q3', 'Q4', 'Q5(at/near peak)'])
    bucketed = df.groupby('dd_bucket', observed=True).agg(
        n=('close', 'count'), mean_drawdown_pct=('drawdown_pct', 'mean'),
        mean_atr_pct=('atr_pct', 'mean'), median_atr_pct=('atr_pct', 'median'),
    )
    print('Bucketed by drawdown depth (Q1 = deepest drawdowns):')
    print(bucketed.to_string())
    print()

    # --- lead-lag: does past/future ATR% relate to TODAY's drawdown? ---
    print('Lead-lag correlation: atr_pct shifted by N trading days vs. drawdown_pct on day 0')
    print('  (negative N = ATR% from N days BEFORE today; positive N = ATR% N days AFTER today)')
    for n in LEAD_LAG_DAYS:
        shifted = df['atr_pct'].shift(n)   # shift(-5) pulls a FUTURE value back to align with today's row... invert below
        # shift(n) with positive n moves values FORWARD in time (i.e. row t gets value from t-n).
        # To get "ATR% from n days before today" aligned with today's drawdown, use shift(n) for n>0 (past),
        # and shift(n) for n<0 pulls a future value back -- pandas shift(-n) does exactly that.
        c = df['drawdown_pct'].corr(shifted)
        label = f'{abs(n)}d before' if n > 0 else (f'{abs(n)}d after' if n < 0 else 'same day')
        print(f'  ATR% {label:>10}: corr = {c:.3f}' if pd.notna(c) else f'  ATR% {label:>10}: n/a')

    # --- local/"micro" peaks instead of the all-time high ---
    # The all-time-running-peak drawdown above is dominated by whatever the single
    # largest multi-year excursion happens to be -- it can't see short recover-then-
    # pull-back swings, which is what "micro regimes" actually means. A trailing
    # N-day high resets locally and picks those up.
    print('\n--- Local (rolling trailing-N-day-high) drawdown, instead of all-time-high ---')
    for window in ROLLING_PEAK_WINDOWS:
        local_peak = daily_close.rolling(window, min_periods=window).max()
        local_dd_pct = (daily_close - local_peak) / local_peak * 100
        ldf = pd.DataFrame({'local_drawdown_pct': local_dd_pct, 'atr_pct': atr_pct}).dropna()
        c = ldf['local_drawdown_pct'].corr(ldf['atr_pct'])
        c_abs = ldf['local_drawdown_pct'].abs().corr(ldf['atr_pct'])
        print(f'\n  Window = {window}d trailing high ({len(ldf)} days):')
        print(f'    Correlation(local_drawdown_pct, atr_pct):   {c:.3f}')
        print(f'    Correlation(|local_drawdown_pct|, atr_pct): {c_abs:.3f}')
        ldf['dd_bucket'] = pd.qcut(ldf['local_drawdown_pct'], 5, labels=['Q1(deepest)', 'Q2', 'Q3', 'Q4', 'Q5(at/near local high)'])
        bucketed = ldf.groupby('dd_bucket', observed=True).agg(
            n=('atr_pct', 'count'), mean_local_dd_pct=('local_drawdown_pct', 'mean'), mean_atr_pct=('atr_pct', 'mean'),
        )
        print(bucketed.to_string())


if __name__ == '__main__':
    main()
