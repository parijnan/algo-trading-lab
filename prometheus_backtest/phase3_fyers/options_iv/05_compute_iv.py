"""
Step 5: resample every downloaded option contract to 15-min bars (same
DST-safe resample as the futures series), then compute Black-76 IV per
boundary for both CE and PE via mibian's Merton-with-q=r trick (see this
session's own research/confirmation -- Me's pricing formula collapses to
Black-76 exactly when dividendYield == interestRate and the futures price
is used in place of spot).

Output: one row per matched 15-min boundary with ce_iv, pe_iv, and their
average (atm_iv) -- keeping both legs rather than only the average lets a
later sanity check catch a mispriced/stale leg (e.g. one side illiquid)
rather than silently blending it into a single number.
"""
import sys
from pathlib import Path

import mibian
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

import configs_iv as cfg
from resample_utils import resample_1m_to_Nmin_historical

schedule = pd.read_csv(cfg.ATM_SCHEDULE_FILE, parse_dates=['time_stamp'])
matched = schedule[schedule['atm_strike'].notna()].copy()

# MIN_DAYS_TO_EXPIRY floor -- see configs_iv.py's own comment for why.
matched['days_to_expiry'] = (
    pd.to_datetime(matched['options_expiry']) - matched['time_stamp']
).dt.total_seconds() / 86400
before = len(matched)
matched = matched[matched['days_to_expiry'] >= cfg.MIN_DAYS_TO_EXPIRY]
print(f'{before:,} matched boundaries, {before - len(matched):,} dropped under the '
      f'{cfg.MIN_DAYS_TO_EXPIRY}-day-to-expiry floor -> {len(matched):,} to price.')

# Cache: (options_expiry, symbol) -> resampled 15-min df, so each contract's
# file is only read+resampled once even though it's referenced by many
# consecutive boundaries.
_resampled_cache = {}


def get_resampled(expiry: str, symbol: str) -> pd.DataFrame:
    key = (expiry, symbol)
    if key in _resampled_cache:
        return _resampled_cache[key]
    fname = symbol.split(':', 1)[1] + '.csv'
    path = Path(cfg.OPTIONS_STAGING_DIR) / expiry / fname
    if not path.exists():
        _resampled_cache[key] = None
        return None
    df_1m = pd.read_csv(path, parse_dates=['time_stamp'])
    df_1m['time_stamp'] = df_1m['time_stamp'].dt.tz_localize(None)
    df_15m = resample_1m_to_Nmin_historical(df_1m, minutes=cfg.GRANULARITY_MINUTES)
    df_15m = df_15m.set_index('time_stamp')
    _resampled_cache[key] = df_15m
    return df_15m


def black76_iv(F: float, K: float, days_to_expiry: float, price: float, is_call: bool):
    """mibian.Me collapses to Black-76 when dividendYield == interestRate
    (i.e. annualDividends = F * r/100) and F is passed as underlyingPrice."""
    if days_to_expiry <= 0 or price <= 0:
        return None
    r = cfg.RISK_FREE_RATE_PCT
    div = F * r / 100
    try:
        if is_call:
            m = mibian.Me([F, K, r, div, days_to_expiry], callPrice=price)
        else:
            m = mibian.Me([F, K, r, div, days_to_expiry], putPrice=price)
        iv = m.impliedVolatility
    except Exception:
        return None
    # mibian's bisection returns its `high`/`low` midpoint bound even when it
    # never actually converges (e.g. price outside the model's achievable
    # range for that F/K/T) -- a result pinned at the search bounds (0.00001
    # or ~500) is that failure mode, not a real answer.
    if iv is None or iv <= 0.001 or iv >= 499:
        return None
    return iv


results = []
n = len(matched)
for i, (_, row) in enumerate(matched.iterrows()):
    if i % 5000 == 0:
        print(f'  {i}/{n}...')
    expiry = row['options_expiry']
    K = row['atm_strike']
    F = row['close']
    boundary = row['time_stamp']
    days_to_expiry = row['days_to_expiry']

    ce_df = get_resampled(expiry, row['ce_symbol']) if pd.notna(row['ce_symbol']) else None
    pe_df = get_resampled(expiry, row['pe_symbol']) if pd.notna(row['pe_symbol']) else None

    ce_price = ce_df.loc[boundary, 'close'] if ce_df is not None and boundary in ce_df.index else None
    pe_price = pe_df.loc[boundary, 'close'] if pe_df is not None and boundary in pe_df.index else None

    ce_iv = black76_iv(F, K, days_to_expiry, ce_price, is_call=True) if ce_price else None
    pe_iv = black76_iv(F, K, days_to_expiry, pe_price, is_call=False) if pe_price else None

    if ce_iv is None and pe_iv is None:
        continue
    atm_iv = pd.Series([ce_iv, pe_iv]).dropna().mean()
    results.append({
        'time_stamp': boundary, 'futures_price': F, 'atm_strike': K,
        'options_expiry': expiry, 'days_to_expiry': round(days_to_expiry, 3),
        'ce_price': ce_price, 'pe_price': pe_price,
        'ce_iv': ce_iv, 'pe_iv': pe_iv, 'atm_iv': atm_iv,
    })

out = pd.DataFrame(results)
out.to_csv(cfg.IV_SERIES_FILE, index=False)
print(f'\n{len(out):,} / {n:,} boundaries priced successfully -> {cfg.IV_SERIES_FILE}')
if len(out):
    print(f'atm_iv range: {out["atm_iv"].min():.1f}% -> {out["atm_iv"].max():.1f}%, '
          f'median {out["atm_iv"].median():.1f}%')
