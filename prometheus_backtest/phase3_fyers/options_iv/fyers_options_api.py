"""
prometheus_backtest/phase3_fyers/options_iv/fyers_options_api.py

Thin, options-specific siblings of data_pipeline/data_downloader_fyers_mcx.py's
own Fyers expired-contract helpers. NOT a modification of that module --
its own get_expiry_dates()/get_expired_contract_symbol() are deliberately
scoped to the 'futures' key of each response (correct for their purpose,
the MCX futures downloader), so this file adds the 'options' counterparts
instead of repurposing them. Reuses that module's auth/rate-limiting/
fatal-error handling (_get, _rate_limiter, FyersAuthExpiredError,
FyersRateLimitError) and its fully-generic get_expired_historical_data
(works for any symbol string, futures or options alike) unchanged.

Confirmed directly against the live API, 2026-09-17 (see session research):
Get Expiry Dates returns SEPARATE 'futures'/'options' lists (options expire
~2-4 days before the futures they're paired with -- devolvement requires
the future to still be alive); Get Expired Contracts, when queried with an
OPTIONS expiry date (not the paired futures one), resolves the full CE/PE
strike ladder for that cycle.
"""
import sys
from pathlib import Path

_DATA_PIPELINE_DIR = Path(__file__).resolve().parents[3] / 'data_pipeline'
if str(_DATA_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_PIPELINE_DIR))

from data_downloader_fyers_mcx import _get, EXPIRY_DATES_CHUNK_DAYS  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402


def get_expiry_dates_paired(anchor_symbol: str, range_from: str, range_to: str) -> tuple:
    """Returns (futures_expiries, options_expiries), both sorted lists of
    'YYYY-MM-DD' strings, chunked at EXPIRY_DATES_CHUNK_DAYS same as the
    futures-only original -- the 366-day API cap applies here identically."""
    start = datetime.strptime(range_from, '%Y-%m-%d')
    end = datetime.strptime(range_to, '%Y-%m-%d')
    all_fut, all_opt = set(), set()
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=EXPIRY_DATES_CHUNK_DAYS - 1), end)
        r = _get('https://api-t1.fyers.in/data/history/fno/expired/expiry-dates', {
            'symbol': anchor_symbol, 'range_from': chunk_start.strftime('%Y-%m-%d'),
            'range_to': chunk_end.strftime('%Y-%m-%d'), 'date_format': 1,
        })
        if r.get('s') == 'ok':
            ed = r.get('data', {}).get('expiry_dates', {})
            all_fut.update(ed.get('futures', []))
            all_opt.update(ed.get('options', []))
        chunk_start = chunk_end + timedelta(days=1)
    return sorted(all_fut), sorted(all_opt)


def get_expired_option_contracts(anchor_symbol: str, options_expiry_date: str) -> list:
    """Full CE+PE symbol list for one options expiry cycle -- must be queried
    with the OPTIONS expiry date, not the paired futures one (confirmed:
    querying with the futures date returns an empty 'options' list)."""
    r = _get('https://api-t1.fyers.in/data/history/fno/expired/underlying-symbols', {
        'symbol': anchor_symbol, 'expiry_date': options_expiry_date,
    })
    if r.get('s') != 'ok':
        return []
    return r.get('data', {}).get('contracts', {}).get('options', [])
