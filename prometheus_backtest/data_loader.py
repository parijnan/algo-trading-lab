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
    expiry date. No back-adjustment needed: the strategy is pure intraday,
    so no position ever spans a contract roll — each day's bars belong to
    whichever contract was genuinely front-month that day, which is exactly
    what the earlier expiry-boundary split already preserved.
    """
    contract_dir = os.path.join(configs.MCX_DATA_DIR, symbol)
    files = sorted(glob.glob(os.path.join(contract_dir, '*_futures.csv')))
    if not files:
        raise FileNotFoundError(f"No contract CSVs found under {contract_dir}")

    frames = []
    for f in files:
        df = pd.read_csv(f, parse_dates=['time_stamp'])
        df['time_stamp'] = pd.to_datetime(df['time_stamp']).dt.tz_localize(None)
        df['contract_expiry'] = os.path.basename(f).replace('_futures.csv', '')
        frames.append(df)

    full = pd.concat(frames, ignore_index=True)
    full = full[(full['close'].notna()) & (full['close'] > 0)]
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
