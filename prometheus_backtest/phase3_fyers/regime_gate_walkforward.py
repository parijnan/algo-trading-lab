"""
Prometheus - Phase 3 (Fyers track): walk-forward test of a realized-
volatility regime gate.

Follows directly from the 2026-09-15 finding (user's own comparison of
phase3_fyers's results against the existing Angel-One-sourced Phase 3
numbers): the mult-2.0/bespoke-exit combo's edge concentrates heavily in
high-realized-volatility conditions. An in-sample-only bucketing pass
(ad-hoc, this session) found that trades in the top ATR(14)/price decile
earned ~6x the average trade's expectancy even using ONLY pre-2026-01-01
data — so the volatility effect predates the 2026-03 Strait-of-Hormuz-
driven regime and isn't hindsight on that one event.

This script turns that into a genuine walk-forward test rather than
another in-sample bucketing exercise, since Phase 3's own calibration
caveat #5 (prometheus_backtest/README.md) already flags in-sample fitting
as an open problem this project keeps re-committing if it isn't careful:

  1. In-sample window: entries before CUTOFF. The gate THRESHOLD (a
     fixed ATR(14)/price percentage, not a percentile re-derived on the
     fly) is chosen here, using nothing after CUTOFF.
  2. Out-of-sample window: entries on/after CUTOFF. The fixed threshold
     from step 1 is applied MECHANICALLY -- trades are split into
     "gate open" (would have been taken) vs "gate closed" (would have
     been skipped), with no further tuning against this window's own
     results.

The gate is evaluated, not re-fit, in the out-of-sample window -- that is
the entire point of the exercise. If a threshold search over the OOS
window's own P&L were used to pick the "best" cut, this would just be a
second in-sample fit wearing a walk-forward costume.

Threshold choice: the 80th percentile of in-sample entry-time ATR(14)/
price (i.e. "top quintile" -- same framing already discussed with the
user, robust with more sample size than the top decile alone, while the
ad-hoc decile check found the edge already visible from decile 8 (80th
percentile) onward). Computed purely from in-sample 15m bars' ATR% values,
not peeked from the combined or OOS series.

CRUDEOILM only, mult 2.0, bespoke exits (SL/T1/T2 = 2.2%/2.2%/5.0%),
same data as phase3_fyers/data_sweep/mult_2.0/bespoke_trade_summary.csv
(Fyers-sourced + Angel-One gap-fill, 2026-03-13 to 2026-06-29 -- see
data_loader_fyers.py). Not a new backtest engine -- reuses that already-
computed trade set and the same 15m OHLC series for the ATR calc.

Output: data_sweep/regime_gate_walkforward_summary.csv
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configs_p3 as configs  # noqa: E402
from data_loader_fyers import load_futures_1min, resample_ohlcv  # noqa: E402

CUTOFF = pd.Timestamp('2026-01-01')
ATR_PERIOD = 14
GATE_PERCENTILE = float(sys.argv[1]) if len(sys.argv) > 1 else 0.80   # top quintile, in-sample-determined


def _compute_atr_pct(df_15m: pd.DataFrame) -> pd.Series:
    high, low, close = df_15m['high'], df_15m['low'], df_15m['close']
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(ATR_PERIOD).mean()
    return (atr / close * 100).rename('atr_pct')


def _stats(label: str, d: pd.DataFrame) -> dict:
    n = len(d)
    if n == 0:
        return {'label': label, 'n_trades': 0, 'win_pct': float('nan'),
                'avg_pnl_pct_of_entry': float('nan'), 'avg_pnl_rs': float('nan'), 'total_pnl_rs': 0.0}
    wins = int((d['total_pnl_rs'] > 0).sum())
    return {
        'label': label, 'n_trades': n, 'win_pct': round(wins / n * 100, 1),
        'avg_pnl_pct_of_entry': round(d['pnl_pct_of_entry'].mean(), 4),
        'avg_pnl_rs': round(d['total_pnl_rs'].mean(), 0),
        'total_pnl_rs': round(d['total_pnl_rs'].sum(), 0),
    }


def main():
    trade_summary_path = os.path.join(configs.DATA_SWEEP_DIR, 'mult_2.0', 'bespoke_trade_summary.csv')
    if not os.path.exists(trade_summary_path):
        raise FileNotFoundError(
            f'{trade_summary_path} not found -- run sweep_p3.py then bespoke_2lot_p3.py first.')

    print('Loading 15m series for ATR(14)/price...')
    df_1m = load_futures_1min(configs.SYMBOL)
    df_15m = resample_ohlcv(df_1m, '15min')
    atr_pct = _compute_atr_pct(df_15m).dropna()

    trades = pd.read_csv(trade_summary_path, parse_dates=['entry_ts'])
    trades['pnl_pct_of_entry'] = trades['total_pnl_points'] / trades['entry_price'] * 100
    # asof: last ATR value at-or-before entry_ts -- no lookahead (ATR14 is
    # itself a trailing rolling mean over already-closed 15m bars).
    trades['atr_pct_at_entry'] = trades['entry_ts'].apply(
        lambda ts: atr_pct.asof(ts) if ts >= atr_pct.index.min() else float('nan'))
    trades = trades.dropna(subset=['atr_pct_at_entry']).reset_index(drop=True)

    is_trades = trades[trades['entry_ts'] < CUTOFF]
    oos_trades = trades[trades['entry_ts'] >= CUTOFF]

    threshold = is_trades['atr_pct_at_entry'].quantile(GATE_PERCENTILE)
    print(f'\nIn-sample window: {is_trades["entry_ts"].min()} -> {is_trades["entry_ts"].max()} '
          f'({len(is_trades)} trades)')
    print(f'Gate threshold (in-sample {GATE_PERCENTILE:.0%}ile of entry-time ATR14/price): {threshold:.4f}%')
    print(f'Out-of-sample window: {oos_trades["entry_ts"].min()} -> {oos_trades["entry_ts"].max()} '
          f'({len(oos_trades)} trades)\n')

    oos_gated = oos_trades[oos_trades['atr_pct_at_entry'] >= threshold]
    oos_skipped = oos_trades[oos_trades['atr_pct_at_entry'] < threshold]
    is_gated = is_trades[is_trades['atr_pct_at_entry'] >= threshold]   # descriptive only, not re-fit

    rows = [
        _stats('in_sample_all', is_trades),
        _stats('in_sample_gate_open_descriptive_only', is_gated),
        _stats('oos_ungated_baseline', oos_trades),
        _stats('oos_gate_open_taken', oos_gated),
        _stats('oos_gate_closed_skipped', oos_skipped),
    ]
    summary = pd.DataFrame(rows)
    out_path = os.path.join(configs.DATA_SWEEP_DIR, f'regime_gate_walkforward_summary_p{int(GATE_PERCENTILE*100)}.csv')
    summary.to_csv(out_path, index=False)

    print(summary.to_string(index=False))
    print(f'\nSaved to {out_path}')

    print('\n--- Read ---')
    print(f'OOS ungated (current behavior): {len(oos_trades)} trades, '
          f'{summary.loc[summary.label=="oos_ungated_baseline","total_pnl_rs"].iloc[0]:,.0f} Rs total.')
    print(f'OOS gated (only trade when ATR% >= {threshold:.3f}, fixed from in-sample): '
          f'{len(oos_gated)} trades, {summary.loc[summary.label=="oos_gate_open_taken","total_pnl_rs"].iloc[0]:,.0f} Rs total.')
    print(f'OOS skipped by the gate: {len(oos_skipped)} trades, '
          f'{summary.loc[summary.label=="oos_gate_closed_skipped","total_pnl_rs"].iloc[0]:,.0f} Rs total '
          f'(what the gate would have left on the table, or saved you from, depending on sign).')


if __name__ == '__main__':
    main()
