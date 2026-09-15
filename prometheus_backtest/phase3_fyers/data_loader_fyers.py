"""
prometheus_backtest/phase3_fyers/data_loader_fyers.py

Fyers-sourced equivalent of ../data_loader.py's own load_futures_1min(),
for the independent Fyers-vs-Angel-One Phase 3 validation track (the
user's own 2026-09-15 instruction: replicate production's rollover logic
and the already-decided mult 2.0 + bespoke exit parameters, CRUDEOILM
only for now, kept fully separate from the existing published Phase 3
results until validated).

One real difference from a plain Fyers-only series, found and verified
directly against the live Fyers API this session (not inferred from the
downloaded files alone):

**Angel-One gap-fill, 2026-03-13 through 2026-06-29 inclusive.** Fyers's
own expired-contract data has a genuine ~3.5-month void here. Confirmed
directly: a live query for the April-2026 contract
(MCX:CRUDEOILM26APRFUT, requested range 2026-03-10 -> 2026-04-20) returns
real candles only through 2026-03-12; a live query for the July-2026
contract (MCX:CRUDEOILM26JULFUT, requested range 2026-05-20 -> 2026-07-01)
returns real candles only from 2026-06-30 onward. Fyers has nothing for
this window under EITHER contract -- not a downloader bug (the
downloader's own hist_from/hist_to already requested the full window in
both cases; Fyers simply returned nothing for the missing stretch). This
is wider than the "May and June 2026 expiries return no_data" finding
already in plans/fyers-mcx-data-integration.md §2.4a -- it also eats the
back ~5 weeks of the April contract's real life and the front ~6 weeks of
July's.

User's own decision (2026-09-15, asked directly): splice in the existing
Angel-One-sourced dataset (data_pipeline/data/mcx/CRUDEOILM/) for JUST
this window, rather than letting the front-month merge_asof silently
substitute the July contract's own price series for the missing March-
June stretch (which would trade the wrong contract for ~3.5 months and
mislabel it as genuinely front-month), or leaving a hard flat gap.
Angel-One's own data has no gap here and reflects the genuinely-correct
front-month sequence (confirmed directly: March->April->May->June->July
contracts, 76 trading days, 63,245 1-min rows, zero gap) -- so this is
the more accurate choice for this specific window, not just a convenient
stopgap.

_CRUDEOILM_OPENING_BAR_CORRECTIONS (../data_loader.py) is inherited
automatically for the spliced-in rows, since they come straight from
../data_loader.py's own load_futures_1min() -- correct, since that table
fixes a confirmed Angel-One-specific thin-liquidity artifact, and five of
its six correction dates (04-06, 05-11, 05-19, 05-25, 06-03) fall inside
this gap-fill window. It is never applied to native Fyers rows -- there is
no evidence Fyers's own feed shares that defect, and doing so would be an
uninvited bug of its own.

Every returned row carries a `data_source` column ('fyers' or
'angelone_gap_fill') so any trade whose hold period touches the spliced
window is identifiable downstream, not silently blended in.
"""
import os
import sys
import glob
from pathlib import Path

import pandas as pd

_PHASE3_FYERS_DIR = Path(__file__).parent
_PROMETHEUS_BACKTEST_DIR = _PHASE3_FYERS_DIR.parent
_REPO_ROOT = _PROMETHEUS_BACKTEST_DIR.parent

sys.path.insert(0, str(_PROMETHEUS_BACKTEST_DIR))
import data_loader as _angelone_loader  # noqa: E402

# Reused verbatim -- pure functions, no data-source dependency.
resample_ohlcv = _angelone_loader.resample_ohlcv
compute_st = _angelone_loader.compute_st

FYERS_DATA_DIR = os.path.join(_REPO_ROOT, 'data_pipeline', 'data', 'mcx_fyers')

# Confirmed directly against the live Fyers API, 2026-09-15 (see module
# docstring). Inclusive on both ends -- Fyers's own last good day is
# 2026-03-12, its next good day is 2026-06-30.
GAP_FILL_START = pd.Timestamp('2026-03-13')
GAP_FILL_END   = pd.Timestamp('2026-06-29 23:59:59')


def _load_fyers_front_month(symbol: str) -> pd.DataFrame:
    """Same front-month de-duplication logic as ../data_loader.py's own
    load_futures_1min, pointed at the Fyers staging tree instead of
    data_pipeline/data/mcx/. Kept as its own copy rather than
    parameterizing the shared function -- that one is load-bearing for
    every existing published Phase 2/3 result and must not change
    behavior as a side effect of this new track."""
    contract_dir = os.path.join(FYERS_DATA_DIR, symbol)
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
    full = full[full['time_stamp'].dt.dayofweek < 5]

    full['date'] = full['time_stamp'].dt.normalize()
    all_dates = pd.DataFrame({'date': sorted(full['date'].unique())})
    expiry_calendar = pd.DataFrame({'expiry_date': sorted(full['expiry_date'].unique())})
    front_month_by_date = pd.merge_asof(
        all_dates, expiry_calendar, left_on='date', right_on='expiry_date', direction='forward'
    ).rename(columns={'expiry_date': 'front_month_expiry'})

    full = full.merge(front_month_by_date, on='date', how='left')
    full = full[full['expiry_date'] == full['front_month_expiry']]
    full = full.drop(columns=['date', 'expiry_date', 'front_month_expiry'])
    full['data_source'] = 'fyers'
    return full.set_index('time_stamp').sort_index()


def load_futures_1min(symbol: str) -> pd.DataFrame:
    if symbol != 'CRUDEOILM':
        raise NotImplementedError(
            f"phase3_fyers is scoped to CRUDEOILM only for now (user's own 2026-09-15 "
            f"instruction: 'Build it for the mini contract for now') -- got {symbol!r}.")

    fyers_df = _load_fyers_front_month(symbol).reset_index()
    fyers_df = fyers_df[(fyers_df['time_stamp'] < GAP_FILL_START) | (fyers_df['time_stamp'] > GAP_FILL_END)]

    angelone_df = _angelone_loader.load_futures_1min(symbol).reset_index()
    angelone_gap = angelone_df[(angelone_df['time_stamp'] >= GAP_FILL_START) &
                               (angelone_df['time_stamp'] <= GAP_FILL_END)].copy()
    angelone_gap['data_source'] = 'angelone_gap_fill'

    combined = pd.concat([fyers_df, angelone_gap], ignore_index=True)
    combined = combined.sort_values('time_stamp').reset_index(drop=True)
    return combined.set_index('time_stamp')
