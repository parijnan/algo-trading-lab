import os
import sys
import glob
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT / 'apollo_production'))
from technical_indicators import SupertrendIndicator  # noqa: E402

import configs  # noqa: E402


def load_futures_1min(symbol: str) -> pd.DataFrame:
    """
    Concatenate every per-contract CSV for `symbol` under
    data_pipeline/data/mcx/<symbol>/, tagging each row with the contract's
    expiry date, then keep only the genuinely-front-month row for each
    calendar date. No back-adjustment needed: the strategy is pure intraday
    (Phase 1/2) or the ST-splicing-across-rolls question is a separate,
    already-accepted issue (Phase 3) — no position ever spans a contract
    roll in a way this function needs to handle differently.

    The front-month de-duplication step (added 2026-09-01) is NOT
    optional: a contract's own file can now carry data well before its own
    front-month tenure (data_downloader_mcx.py's own backfills for live
    seeding purposes deliberately extend a newly-effective contract's file
    backward — confirmed live this session, e.g. 2026-09-21_futures.csv now
    starts 2026-02-11, months before it was ever front-month). Without this
    step, dates covered by more than one contract's file get silently
    blended — the resample step downstream just picks whichever row sorts
    first for a given minute, mixing two different instruments' prices.
    Caught via a real anomaly: a trade entered and exited entirely within
    2026-02-10/11 showed exit_contract_expiry='2026-09-21' before this fix.
    """
    contract_dir = os.path.join(configs.MCX_DATA_DIR, symbol)
    files = sorted(glob.glob(os.path.join(contract_dir, '*_futures.csv')))
    if not files:
        raise FileNotFoundError(f"No contract CSVs found under {contract_dir}")

    frames = []
    for f in files:
        df = pd.read_csv(f, parse_dates=['time_stamp'])
        df['time_stamp'] = pd.to_datetime(df['time_stamp']).dt.tz_localize(None)
        expiry_str = os.path.basename(f).replace('_futures.csv', '')
        df['contract_expiry'] = expiry_str
        df['expiry_date'] = pd.Timestamp(expiry_str)
        frames.append(df)

    full = pd.concat(frames, ignore_index=True)
    full = full[(full['close'].notna()) & (full['close'] > 0)]

    # Drop Sat/Sun bars (2026-09-01, real incident): the production cron is
    # Mon-Fri only ("15 9 * * 1-5", CLAUDE.md), so a live Prometheus process
    # never runs and never observes price action on a weekend special
    # session -- confirmed exactly one such session exists in this dataset,
    # MCX's 2026-02-01 Union Budget session (WTI itself wasn't trading).
    # Left in, that session's thin, WTI-disconnected price action could
    # (and did) drive both raw signal flips and SL/target fills a live
    # Mon-Fri cron could never have reacted to -- e.g. mult 2.0/2.5's own
    # bespoke exit-calibration each had 2-3 trades whose stop/target fired
    # on that Sunday, net understating true achievable P&L by ~Rs 957-4,902.
    full = full[full['time_stamp'].dt.dayofweek < 5]

    # For each calendar date, the genuine front-month contract is the one
    # with the smallest expiry_date >= that date (matches
    # data_downloader_mcx.py's own select_front_month_contracts() rule) --
    # keep only rows matching that contract, dropping any "extra" history a
    # file carries beyond its own real front-month tenure.
    full['date'] = full['time_stamp'].dt.normalize()
    all_dates = pd.DataFrame({'date': sorted(full['date'].unique())})
    expiry_calendar = pd.DataFrame({'expiry_date': sorted(full['expiry_date'].unique())})
    front_month_by_date = pd.merge_asof(
        all_dates, expiry_calendar, left_on='date', right_on='expiry_date', direction='forward'
    ).rename(columns={'expiry_date': 'front_month_expiry'})

    full = full.merge(front_month_by_date, on='date', how='left')
    full = full[full['expiry_date'] == full['front_month_expiry']]
    full = full.drop(columns=['date', 'expiry_date', 'front_month_expiry'])

    full = full.set_index('time_stamp').sort_index()
    return full


def resample_ohlcv(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Resample per trading day, anchored to each day's first bar."""
    dfs = []
    for _, day in df.groupby(df.index.date):
        resampled = day.resample(freq, origin=day.index[0]).agg(
            open=('open', 'first'),
            high=('high', 'max'),
            low=('low', 'min'),
            close=('close', 'last'),
            volume=('volume', 'sum'),
            contract_expiry=('contract_expiry', 'first'),
        ).dropna(subset=['close'])
        dfs.append(resampled)
    return pd.concat(dfs) if dfs else pd.DataFrame()


def compute_st(df: pd.DataFrame, period: int, multiplier: float) -> pd.DataFrame:
    """
    Add supertrend, trend (bool), trend_flip (bool) to df.
    df must have lowercase ohlc columns and a DatetimeIndex.
    Computed continuously across the whole series (not reset per day),
    matching iris_production's live behaviour — the Supertrend ratchet is
    history-dependent.
    """
    df_up = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'})
    result = SupertrendIndicator(period=period, multiplier=multiplier).calculate(df_up)
    result = result.rename(columns={
        'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close',
        'Supertrend': 'supertrend',
    })
    result['trend'] = (result['close'] > result['supertrend']).astype(object)
    result.loc[result['supertrend'].isna(), 'trend'] = pd.NA
    result['trend_flip'] = result['trend'] != result['trend'].shift(1)
    result.loc[result['supertrend'].isna(), 'trend_flip'] = False
    # The first bar where supertrend becomes valid reads as a flip (its
    # shifted-back trend is NA, so `!=` is True) — spurious, not a real
    # regime change. Guard it out.
    first_valid_idx = result['supertrend'].first_valid_index()
    if first_valid_idx is not None:
        result.loc[first_valid_idx, 'trend_flip'] = False
    return result
