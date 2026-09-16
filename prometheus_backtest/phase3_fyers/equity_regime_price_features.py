"""
Prometheus - Phase 3 (Fyers track): what was the underlying's own price
doing, at multiple timeframes, during the strategy's equity-curve
up-stretches vs. its down-stretches?

Direct follow-up to the user's question (2026-09-15): rather than
labeling "micro regimes" from strategy P&L and feeding those labels into
a classifier (circular -- see drawdown_vol_correlation.py's docstring),
use the equity curve only to LOCATE windows of interest, then ask a
purely descriptive question about what price looked like inside each
window. No model is trained here and no label is carried forward into
one; this is diagnostic, the same way regime_signal_design.py's causal
detector was a design exploration, not a validated system.

--- Step 1: segment the mult-2.0 bespoke equity curve ---
A peak-to-trough drawdown state machine, not a rolling-slope fit: 'up'
from a fresh equity high until the running drawdown breaches
DD_THRESHOLD_RS, then 'down' until equity sets a fresh all-time high
again. DD_THRESHOLD_RS=15000 was chosen by sweeping 8000-20000 (see
session scratchpad) -- it's the smallest threshold that collapses the
noisiest single/few-trade wiggles into their neighboring segment while
still cutting the curve into a small number of genuinely distinct
stretches (9 segments here), and its LAST segment boundary (2026-03-11)
lands within ~9 days of the already-confirmed 2026-03-02 regime shift
found independently via ATR% -- a useful sanity check that this
threshold isn't cutting somewhere arbitrary.

Caveat worth stating plainly: because this is a pure drawdown
state-machine (not a trend fit), an 'up' segment runs all the way to the
local peak just before a confirmed drawdown starts, and can therefore
include a topping/rolling-over stretch with net-negative P&L at its tail
end (e.g. the Sep 2024 and mid-2025 'up' segments below). That's a
known, accepted property of this segmentation, not a bug -- flagged in
the printed table rather than hidden.

--- Step 2: price features per segment, computed once over the whole
series then aggregated per segment (not recomputed per-segment) ---
For each of 15m (the signal's own timeframe) / 1h / daily:
  - atr_pct        : ATR(14)/close, rolling, %
  - efficiency_ratio: Kaufman ER over a 20-bar window (trend persistence,
                      independent of move size -- same measure
                      regime_signal_design.py already validated on daily)
  - return_autocorr : lag-1 autocorrelation of bar returns over a 20-bar
                      window (momentum vs. mean-reversion)
  - return_skew     : rolling 20-bar skew of returns
Plus two features specific to a Supertrend system's actual failure mode,
computed on the 15m ST(period, mult=2.0 -- the live production signal):
  - st_flips_per_day: raw flip frequency within the segment
  - st_fakeout_rate : fraction of flips that reverse again within
                      FAKEOUT_BARS bars (whipsaw, not real follow-through)
And one multi-timeframe alignment feature:
  - trend_alignment : fraction of 15m bars in the segment where the ST
                      direction agrees with a simple daily trend filter
                      (close vs. its own rolling 50-day SMA) -- catches
                      whether losing stretches are the entry timeframe
                      fighting the higher-timeframe trend.

Output: data_sweep/equity_regime_features.csv (one row per segment) plus
a printed up-vs-down group comparison.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configs_p3 as configs  # noqa: E402
from data_loader_fyers import load_futures_1min, resample_ohlcv, compute_st  # noqa: E402

DD_THRESHOLD_RS = 15000
BESPOKE_MULT = 2.0   # live production multiplier -- the only bespoke trade log we have
TRADE_SUMMARY_FILE = os.path.join(configs.DATA_SWEEP_DIR, f'mult_{BESPOKE_MULT:.1f}', 'bespoke_trade_summary.csv')

ATR_PERIOD = 14
ER_WINDOW_BARS = 20
FAKEOUT_BARS = 8          # ~2h on the 15m chart -- a flip reversing inside this is a whipsaw, not follow-through
DAILY_TREND_SMA_DAYS = 50

OUT_FILE = os.path.join(configs.DATA_SWEEP_DIR, 'equity_regime_features.csv')


# ---------------------------------------------------------------- segmentation

def _load_equity_curve() -> pd.DataFrame:
    trades = pd.read_csv(TRADE_SUMMARY_FILE, parse_dates=['entry_ts', 'lot1_exit_ts', 'lot2_exit_ts'])
    trades['exit_ts'] = trades['lot2_exit_ts'].fillna(trades['lot1_exit_ts'])
    trades = trades.sort_values('exit_ts').reset_index(drop=True)
    trades['equity'] = trades['total_pnl_rs'].cumsum()
    trades['peak'] = trades['equity'].cummax()
    trades['dd'] = trades['equity'] - trades['peak']
    return trades


def _segment_equity(trades: pd.DataFrame) -> list[dict]:
    state = 'up'
    seg_start = 0
    segments = []
    for i in range(len(trades)):
        dd = trades['dd'].iloc[i]
        if state == 'up' and dd <= -DD_THRESHOLD_RS:
            segments.append((state, seg_start, i - 1))
            state, seg_start = 'down', i
        elif state == 'down' and dd == 0:
            segments.append((state, seg_start, i - 1))
            state, seg_start = 'up', i
    segments.append((state, seg_start, len(trades) - 1))

    out = []
    for label, s, e in segments:
        if e < s:
            continue
        out.append({
            'label': label,
            'start_date': trades['exit_ts'].iloc[s].normalize(),
            'end_date': trades['exit_ts'].iloc[e].normalize(),
            'n_trades': e - s + 1,
            'segment_pnl_rs': trades['total_pnl_rs'].iloc[s:e + 1].sum(),
        })
    return out


# ---------------------------------------------------------------- per-bar features

def _bar_features(df: pd.DataFrame) -> pd.DataFrame:
    """ATR%, Kaufman ER, lag-1 return autocorr, return skew -- all as
    rolling per-bar series, computed once for a given timeframe's df."""
    high, low, close = df['high'], df['low'], df['close']
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr_pct = tr.rolling(ATR_PERIOD).mean() / close * 100

    direction = (close - close.shift(ER_WINDOW_BARS)).abs()
    volatility = close.diff().abs().rolling(ER_WINDOW_BARS).sum()
    er = (direction / volatility).replace([np.inf, -np.inf], np.nan)

    ret = close.pct_change()
    ret_lag1 = ret.shift(1)
    roll_cov = ret.rolling(ER_WINDOW_BARS).cov(ret_lag1)
    roll_var = ret.rolling(ER_WINDOW_BARS).var()
    autocorr = (roll_cov / roll_var).replace([np.inf, -np.inf], np.nan)

    skew = ret.rolling(ER_WINDOW_BARS).skew()

    return pd.DataFrame({'atr_pct': atr_pct, 'efficiency_ratio': er, 'return_autocorr': autocorr, 'return_skew': skew})


def _segment_bar_means(bar_df: pd.DataFrame, seg: dict, suffix: str) -> dict:
    window = bar_df[(bar_df.index.normalize() >= seg['start_date']) & (bar_df.index.normalize() <= seg['end_date'])]
    return {f'{col}_{suffix}': window[col].mean() for col in bar_df.columns}


# ---------------------------------------------------------------- ST whipsaw + multi-TF alignment

def _st_flip_events(df_15m_st: pd.DataFrame) -> pd.DataFrame:
    flips = df_15m_st[df_15m_st['trend_flip']].copy()
    flip_idx = flips.index
    flip_trend = flips['trend'].to_numpy()
    is_fakeout = np.zeros(len(flips), dtype=bool)
    all_idx = df_15m_st.index
    for pos, ts in enumerate(flip_idx):
        start_pos = all_idx.get_loc(ts)
        window = df_15m_st.iloc[start_pos + 1: start_pos + 1 + FAKEOUT_BARS]
        opposite = window[window['trend'] != flip_trend[pos]]
        if not opposite.empty and opposite['trend_flip'].any():
            is_fakeout[pos] = True
    flips['is_fakeout'] = is_fakeout
    return flips


def _segment_st_stats(flip_events: pd.DataFrame, df_15m_st: pd.DataFrame, daily_trend_up: pd.Series, seg: dict) -> dict:
    seg_flips = flip_events[(flip_events.index.normalize() >= seg['start_date']) & (flip_events.index.normalize() <= seg['end_date'])]
    n_days = max((seg['end_date'] - seg['start_date']).days, 1)
    flips_per_day = len(seg_flips) / n_days
    fakeout_rate = seg_flips['is_fakeout'].mean() if len(seg_flips) else float('nan')

    seg_bars = df_15m_st[(df_15m_st.index.normalize() >= seg['start_date']) & (df_15m_st.index.normalize() <= seg['end_date'])].copy()
    seg_bars = seg_bars[seg_bars['trend'].notna()]
    daily_up_for_bars = seg_bars.index.normalize().map(daily_trend_up)
    valid = ~pd.isna(daily_up_for_bars.to_numpy())
    st_trend = seg_bars['trend'].to_numpy(dtype=bool)[valid]
    daily_up = daily_up_for_bars.to_numpy(dtype=bool)[valid]
    trend_alignment = (st_trend == daily_up).mean() if valid.any() else float('nan')

    return {'st_flips_per_day': flips_per_day, 'st_fakeout_rate': fakeout_rate, 'trend_alignment': trend_alignment}


def main():
    print('Loading mult-2.0 bespoke trade log and building the equity curve...')
    trades = _load_equity_curve()
    segments = _segment_equity(trades)
    print(f'{len(segments)} segments (threshold={DD_THRESHOLD_RS} Rs):')
    for seg in segments:
        print(f"  {seg['label']:5s}  {seg['start_date'].date()} -> {seg['end_date'].date()}  "
              f"n={seg['n_trades']:4d}  pnl={seg['segment_pnl_rs']:>10,.0f}")

    print('\nLoading 1m price data and resampling to 15m/1h/daily...')
    df_1m = load_futures_1min(configs.SYMBOL)
    df_15m = resample_ohlcv(df_1m, '15min')
    df_1h = resample_ohlcv(df_1m, '1h')
    df_daily = resample_ohlcv(df_1m, '1D')

    feat_15m = _bar_features(df_15m)
    feat_1h = _bar_features(df_1h)
    feat_daily = _bar_features(df_daily)

    daily_close = df_daily['close']
    daily_trend_up = (daily_close > daily_close.rolling(DAILY_TREND_SMA_DAYS).mean())
    daily_trend_up.index = daily_trend_up.index.normalize()

    df_15m_st = compute_st(df_15m, configs.ST_PERIOD, BESPOKE_MULT)
    flip_events = _st_flip_events(df_15m_st)

    rows = []
    for seg in segments:
        row = dict(seg)
        row.update(_segment_bar_means(feat_15m, seg, '15m'))
        row.update(_segment_bar_means(feat_1h, seg, '1h'))
        row.update(_segment_bar_means(feat_daily, seg, 'daily'))
        row.update(_segment_st_stats(flip_events, df_15m_st, daily_trend_up, seg))
        rows.append(row)

    result = pd.DataFrame(rows)
    result.to_csv(OUT_FILE, index=False)
    print(f'\nSaved -> {OUT_FILE}\n')

    display_cols = ['label', 'start_date', 'end_date', 'n_trades', 'segment_pnl_rs',
                     'atr_pct_15m', 'atr_pct_1h', 'atr_pct_daily',
                     'efficiency_ratio_15m', 'efficiency_ratio_1h', 'efficiency_ratio_daily',
                     'return_autocorr_15m', 'return_autocorr_1h', 'return_autocorr_daily',
                     'return_skew_15m', 'return_skew_1h', 'return_skew_daily',
                     'st_flips_per_day', 'st_fakeout_rate', 'trend_alignment']
    with pd.option_context('display.max_columns', None, 'display.width', 220):
        print(result[display_cols].to_string(index=False))

    print('\n--- Up vs. down segment means ---')
    feature_cols = [c for c in display_cols if c not in ('label', 'start_date', 'end_date', 'n_trades', 'segment_pnl_rs')]
    grouped = result.groupby('label')[feature_cols].mean()
    with pd.option_context('display.max_columns', None, 'display.width', 220):
        print(grouped.to_string())


if __name__ == '__main__':
    main()
