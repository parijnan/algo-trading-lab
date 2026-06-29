"""
Load and normalise each strategy's trade summary to a common schema:

  strategy    : str   — 'artemis_nifty', 'artemis_sensex', 'athena', 'iris'
  entry_date  : date
  entry_ts    : datetime (tz-naive)
  exit_date   : date
  exit_ts     : datetime (tz-naive)
  pl_rs       : float  — 1-lot ₹ P&L
  exit_reason : str
  entry_vix   : float | NaN
"""

import pandas as pd
from configs import ERA_SPLIT_DATE


def _strip_tz(series: pd.Series) -> pd.Series:
    s = pd.to_datetime(series)
    if s.dt.tz is not None:
        s = s.dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    return s


def load_artemis(nifty_path: str, sensex_path: str) -> pd.DataFrame:
    era_split = pd.Timestamp(ERA_SPLIT_DATE)
    frames = []

    for path, strategy in [(nifty_path, 'artemis_nifty'), (sensex_path, 'artemis_sensex')]:
        raw = pd.read_csv(path)
        raw = raw.dropna(subset=['entry_time'])   # rows with no trade
        raw = raw.dropna(subset=['total_pl_rupees'])

        raw['entry_ts']   = _strip_tz(raw['entry_time'])
        raw['expiry_ts']  = _strip_tz(raw['expiry'])
        raw['pe_exit_ts'] = _strip_tz(raw['pe_exit_time'])
        raw['ce_exit_ts'] = _strip_tz(raw['ce_exit_time'])

        # Exit time: latest of pe/ce exits; null leg assumed to ride to expiry
        raw['exit_ts'] = raw.apply(
            lambda r: max(
                r['pe_exit_ts'] if pd.notna(r['pe_exit_ts']) else r['expiry_ts'],
                r['ce_exit_ts'] if pd.notna(r['ce_exit_ts']) else r['expiry_ts'],
            ),
            axis=1,
        )

        # Combined exit reason (informational)
        def _exit_reason(r):
            pe = r.get('pe_exit_reason')
            ce = r.get('ce_exit_reason')
            pe = None if pd.isna(pe) else pe
            ce = None if pd.isna(ce) else ce
            if pe and ce:
                return pe if pe == ce else f'{pe}|{ce}'
            return pe or ce or 'unknown'

        raw['exit_reason_combined'] = raw.apply(_exit_reason, axis=1)

        df = pd.DataFrame({
            'strategy':    strategy,
            'entry_date':  raw['entry_ts'].dt.date,
            'entry_ts':    raw['entry_ts'],
            'exit_date':   raw['exit_ts'].dt.date,
            'exit_ts':     raw['exit_ts'],
            'pl_rs':       raw['total_pl_rupees'],
            'exit_reason': raw['exit_reason_combined'],
            'entry_vix':   raw['entry_vix'],
        })
        frames.append(df)

    combined = pd.concat(frames, ignore_index=True)

    # Era split: Nifty only before split, Sensex only from split onward
    combined = combined[
        ((combined['strategy'] == 'artemis_nifty')   & (combined['entry_ts'] < era_split)) |
        ((combined['strategy'] == 'artemis_sensex') & (combined['entry_ts'] >= era_split))
    ].reset_index(drop=True)

    return combined


def load_athena(path: str) -> pd.DataFrame:
    raw = pd.read_csv(path)
    raw = raw.dropna(subset=['entry_time', 'exit_time', 'total_pl_rupees'])

    raw['entry_ts'] = _strip_tz(raw['entry_time'])
    raw['exit_ts']  = _strip_tz(raw['exit_time'])

    return pd.DataFrame({
        'strategy':    'athena',
        'entry_date':  raw['entry_ts'].dt.date,
        'entry_ts':    raw['entry_ts'],
        'exit_date':   raw['exit_ts'].dt.date,
        'exit_ts':     raw['exit_ts'],
        'pl_rs':       raw['total_pl_rupees'],
        'exit_reason': raw['exit_reason'],
        'entry_vix':   raw['entry_vix'],
    })


def load_iris(path: str) -> pd.DataFrame:
    raw = pd.read_csv(path)
    raw = raw.dropna(subset=['entry_ts', 'exit_ts', 'pnl_rs'])

    raw['entry_ts'] = _strip_tz(raw['entry_ts'])
    raw['exit_ts']  = _strip_tz(raw['exit_ts'])

    return pd.DataFrame({
        'strategy':    'iris',
        'entry_date':  raw['entry_ts'].dt.date,
        'entry_ts':    raw['entry_ts'],
        'exit_date':   raw['exit_ts'].dt.date,
        'exit_ts':     raw['exit_ts'],
        'pl_rs':       raw['pnl_rs'],
        'exit_reason': raw['exit_reason'],
        'entry_vix':   float('nan'),  # not in Iris summary; VIX gate applied by router
    })


def load_artemis_entry_dates(nifty_contracts_path: str, sensex_contracts_path: str):
    """Return (nifty_dates: set[date], sensex_dates: set[date])."""
    nifty_df  = pd.read_csv(nifty_contracts_path)
    sensex_df = pd.read_csv(sensex_contracts_path)

    nifty_dates  = set(pd.to_datetime(nifty_df['entry']).dt.date)
    sensex_dates = set(pd.to_datetime(sensex_df['entry']).dt.date)
    return nifty_dates, sensex_dates
