"""
Step 4: download 1-min historical data for every contract in
needed_contracts.csv (built by step 3), via data_downloader_fyers_mcx.py's
own get_expired_historical_data() -- fully generic on symbol string,
unchanged, already validated against options in this session's own probe.

Resumable: skips any contract whose staging file already exists. Stops the
whole run (not just this contract) on FyersAuthExpiredError/
FyersRateLimitError, same failure-handling convention as
data_downloader_fyers_mcx.py's own main() -- re-running later with a fresh
token picks up exactly where it left off.

**A transient socket TimeoutError crashed the first real run at contract
~205/1308** (confirmed benign -- a one-off network hiccup, not a rate-limit
or auth condition; get_expired_historical_data's own retry only catches
the "exceeding access rate" string match, not a raw socket timeout). Each
contract's own fetch now gets a short retry burst for exactly this class
of transient failure, and any OTHER unexpected exception marks just that
one contract failed and continues -- rather than the whole 1,100+ remaining
run dying on one bad contract the way this first run did.
"""
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'data_pipeline'))

import configs_iv as cfg
from data_downloader_fyers_mcx import (
    get_expired_historical_data, FyersAuthExpiredError, FyersRateLimitError, logger,
)

TRANSIENT_RETRY_ATTEMPTS = 3
TRANSIENT_RETRY_SLEEP_SEC = 5

needed = pd.read_csv(Path(cfg.DATA_DIR) / 'needed_contracts.csv')
print(f'{len(needed)} contracts to fetch.')

failed = []
skipped = 0
fetched = 0
for i, row in needed.iterrows():
    expiry = row['options_expiry']
    symbol = row['symbol']
    out_dir = Path(cfg.OPTIONS_STAGING_DIR) / expiry
    out_dir.mkdir(parents=True, exist_ok=True)
    # symbol like 'MCX:CRUDEOILM24JUN5350CE' -> filename 'CRUDEOILM24JUN5350CE.csv'
    fname = symbol.split(':', 1)[1] + '.csv'
    out_path = out_dir / fname
    if out_path.exists():
        skipped += 1
        continue

    # Fetch a window comfortably covering the contract's real ~4-week life:
    # 45 days before its own expiry through the expiry date itself.
    expiry_ts = pd.Timestamp(expiry)
    hist_from = (expiry_ts - pd.Timedelta(days=45)).strftime('%Y-%m-%d')
    hist_to = expiry

    df = None
    for attempt in range(1, TRANSIENT_RETRY_ATTEMPTS + 1):
        try:
            df = get_expired_historical_data(symbol, hist_from, hist_to)
            break
        except (FyersAuthExpiredError, FyersRateLimitError) as e:
            logger.error(f'{type(e).__name__}: {e}')
            remaining = len(needed) - i
            logger.error(f'Stopping -- {remaining} contract(s) not yet attempted. '
                         f'Re-run this script (with a fresh token if auth expired) to resume.')
            raise SystemExit(0)
        except Exception as e:
            if attempt < TRANSIENT_RETRY_ATTEMPTS:
                logger.warning(f'{symbol}: attempt {attempt}/{TRANSIENT_RETRY_ATTEMPTS} failed '
                               f'({type(e).__name__}: {e}) -- retrying in {TRANSIENT_RETRY_SLEEP_SEC}s')
                time.sleep(TRANSIENT_RETRY_SLEEP_SEC)
            else:
                logger.error(f'{symbol}: gave up after {TRANSIENT_RETRY_ATTEMPTS} attempts '
                             f'({type(e).__name__}: {e}) -- skipping this contract, continuing.')
                failed.append(symbol)

    if df is None:
        continue

    if df.empty:
        logger.warning(f'{symbol}: no data returned.')
        failed.append(symbol)
        continue

    df.to_csv(out_path, index=False)
    fetched += 1
    if fetched % 50 == 0:
        print(f'  ... {fetched} fetched, {skipped} skipped (already cached), {len(failed)} empty')

print(f'\nDone. {fetched} fetched, {skipped} already cached, {len(failed)} returned no data.')
if failed:
    print(f'No-data symbols: {failed[:20]}{" ..." if len(failed) > 20 else ""}')
