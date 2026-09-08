"""
Prometheus — WTI 5-minute side project: loader for the Kaggle WTI 5-min
OHLC CSV. Cannot reuse prometheus_backtest/data_loader.py's
load_futures_1min — that function assumes MCX's per-contract-expiry file
layout and front-month de-duplication, which a Kaggle continuous-series
file has no equivalent of. This loader instead produces a dataframe with
the same OUTPUT SHAPE that function does (tz-naive sorted DatetimeIndex,
lowercase open/high/low/close/volume + a contract_expiry column), so
resample_ohlcv/compute_st (both instrument-agnostic) work unchanged.

Real file schema (confirmed 2026-09-08, not assumed):
  time,date,instrument,granularity,open,high,low,close
  2026-04-29T09:55:00.000Z,2026-04-29,OIL,5Min,103.46,103.635,103.36,103.54
No volume column exists in the source — a dummy 0 column is added so
resample_ohlcv's own volume=('volume','sum') aggregation doesn't error;
compute_st never reads volume, so this has no effect on the signal.
Rows arrive newest-first in the raw file; sorted ascending here.

Real data-quality finding (2026-09-08, confirmed by direct inspection, not
assumed): this is NOT a genuine gapped exchange session — the closed
market is filled with flat (open==high==low==close, repeating the last
real price) synthetic bars instead of simply being absent. Saturday is
100% flat, Sunday 93.8% flat; real trading days (Mon-Fri) sit at ~9-11%
flat (Friday elevated to 17.6% from the week's close, Monday to 10.8% from
the reopen ramp). Same root problem load_futures_1min already solves for
MCX (its own "Drop Sat/Sun bars" step) — applying the identical fix here:
drop dayofweek>=5 rows entirely. The residual ~9-11% weekday flat rate
(illiquid real bars + Fri-close/Mon-open transition artifacts) is left
alone — not clearly distinguishable from genuine quiet trading without a
much finer per-bar heuristic, and not attempted here.
"""

import pandas as pd


def load_wti_5min(filepath: str) -> pd.DataFrame:
    df = pd.read_csv(filepath, usecols=['time', 'open', 'high', 'low', 'close'])
    df['time_stamp'] = pd.to_datetime(df['time'], utc=True).dt.tz_localize(None)
    df = df.drop(columns=['time']).set_index('time_stamp').sort_index()

    n_raw = len(df)
    dupes = df.index.duplicated(keep='first')
    if dupes.any():
        df = df[~dupes]

    df = df[df['close'].notna() & (df['close'] > 0)]

    # Weekend rows are near-entirely synthetic flat-fill (Sat 100%, Sun
    # 93.8% open==high==low==close) — same contamination load_futures_1min
    # already drops Sat/Sun bars for, on the same reasoning.
    n_before_weekend_drop = len(df)
    df = df[df.index.dayofweek < 5]
    n_weekend_dropped = n_before_weekend_drop - len(df)

    df['volume'] = 0
    df['contract_expiry'] = 'continuous'  # no MCX-style expiry — constant placeholder

    print(f"WTI 5-min data: {len(df):,} bars ({n_raw - n_before_weekend_drop:,} dropped: "
          f"{int(dupes.sum())} duplicate timestamp(s), rest bad/missing close; "
          f"{n_weekend_dropped:,} further dropped as Sat/Sun synthetic flat-fill). "
          f"Range: {df.index.min()} to {df.index.max()}.")

    flat = (df['open'] == df['high']) & (df['high'] == df['low']) & (df['low'] == df['close'])
    print(f"  Residual weekday flat-bar rate: {flat.mean():.1%} (illiquid real bars + "
          f"Fri-close/Mon-open transition artifacts — not filtered further).")

    return df


if __name__ == '__main__':
    import configs_wti as configs
    load_wti_5min(configs.WTI_DATA_FILE)
