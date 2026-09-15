"""
Standalone, manual-run diagnostic for the 2026-09-15 AB1007 "Invalid Token" incident.

Context: Prometheus's live trend-flip Rule 7 combined order (4 lots,
CRUDEOILM19OCT26FUT, symboltoken=569901) was rejected repeatedly by Angel
One with errorcode AB1007 / message "Invalid Token", for over two minutes
straight, while market data (candles, LTP) kept flowing normally on the
same token throughout. Angel One's own live public scrip master was
checked directly and confirms 569901 is exactly correct for
CRUDEOILM19OCT26FUT -- so the instrument token itself is not wrong. This
script exists to get one clean, isolated data point outside of
Prometheus's own retry loop: does the identical order request succeed or
fail the same way when placed on its own, and if it fails, what does the
full raw response actually contain.

This places a REAL order with REAL capital when run with --confirm. It is
NOT meant to be automated, scheduled, or run by an agent -- a human reviews
the printed preview and explicitly opts in every time. Without --confirm,
it only prints what WOULD be sent and exits, no API call is made.

Usage:
    python research/ab1007_order_diagnostic/place_test_order.py            # preview only
    python research/ab1007_order_diagnostic/place_test_order.py --confirm  # actually places the order

Run from the repo root (imports prometheus_production.prometheus_configs
for the live CREDS_FILE/FO_EXCHANGE/LOT_SIZE values -- never hardcode
these separately, they must track the real deployed config).
"""
import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / 'prometheus_production'))

import pandas as pd  # noqa: E402
from prometheus_configs import CREDS_FILE, FO_EXCHANGE, LOT_SIZE  # noqa: E402

# The exact contract/token this incident was about -- see prometheus_state.csv
# on Delos and the 2026-09-15 16:30 log window for the original failing calls.
SYMBOL = 'CRUDEOILM19OCT26FUT'
TOKEN = '569901'
LOTS = 4  # matches the failed Rule 7 combined order's requested size exactly

LOG_DIR = Path(__file__).parent / 'logs'
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / f'ab1007_test_{datetime.now():%Y%m%d_%H%M%S}.log'

logger = logging.getLogger('ab1007_diagnostic')
logger.setLevel(logging.DEBUG)
_fmt = logging.Formatter('%(asctime)s  %(levelname)-8s  %(message)s')
_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(_fmt)
_file = logging.FileHandler(LOG_FILE)
_file.setFormatter(_fmt)
logger.addHandler(_console)
logger.addHandler(_file)


def login():
    """Mirrors prometheus.py's own _login() exactly -- same credential
    source, same TOTP mechanism, same SmartConnect construction -- so this
    test's session behaves identically to the one that actually failed."""
    import pyotp
    from SmartApi import SmartConnect

    logger.info('Reading credentials from %s', CREDS_FILE)
    creds = pd.read_csv(CREDS_FILE)
    row = creds.iloc[0]
    api_key = str(row['api_key'])
    client_code = str(row['user_name'])
    logger.debug('api_key=%s... client_code=%s', api_key[:4], client_code)

    obj = SmartConnect(api_key=api_key)
    logging.getLogger('logzero_default').setLevel(logging.CRITICAL)

    totp = pyotp.TOTP(str(row['qr_code'])).now()
    logger.info('Requesting session (TOTP computed, not logged)')
    resp = obj.generateSession(client_code, str(row['password']), totp)
    logger.debug('generateSession raw response: %s', json.dumps(resp, default=str))

    if not resp.get('status'):
        logger.critical('Login FAILED: %s', resp)
        raise RuntimeError(f'Angel One login failed: {resp}')

    logger.info('Logged in as %s', client_code)
    return obj


def build_order_params() -> dict:
    """Identical shape to place_order()'s own orderparams dict in
    prometheus_functions.py -- variety/ordertype/producttype/duration all
    match exactly, so this is a faithful, apples-to-apples reproduction of
    the request that got AB1007, not an approximation of it."""
    qty_shares = LOTS * LOT_SIZE
    return {
        'variety': 'NORMAL',
        'tradingsymbol': SYMBOL,
        'symboltoken': TOKEN,
        'transactiontype': 'BUY',
        'exchange': FO_EXCHANGE,
        'ordertype': 'MARKET',
        'producttype': 'CARRYFORWARD',
        'duration': 'DAY',
        'quantity': str(qty_shares),
        'price': '0',
        'triggerprice': '0',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm', action='store_true',
                        help='Actually place the order. Without this flag, only previews it.')
    args = parser.parse_args()

    logger.info('=' * 70)
    logger.info('AB1007 diagnostic run starting. Log file: %s', LOG_FILE)
    logger.info('=' * 70)

    orderparams = build_order_params()
    logger.info('Order to be placed:')
    for k, v in orderparams.items():
        logger.info('  %-15s = %s', k, v)
    logger.info('This is a REAL BUY order for %d lot(s) (%s shares) of %s if --confirm is set.',
               LOTS, orderparams['quantity'], SYMBOL)

    if not args.confirm:
        logger.info('--confirm not passed -- PREVIEW ONLY, no API call made. '
                    'Re-run with --confirm to actually place this order.')
        return

    logger.warning('--confirm passed -- proceeding to place a REAL order with REAL capital.')

    obj = login()

    logger.info('Submitting placeOrderFullResponse...')
    try:
        resp = obj.placeOrderFullResponse(orderparams)
    except Exception:
        logger.exception('placeOrderFullResponse RAISED an exception (not a returned error dict) — '
                         'full traceback above.')
        return

    logger.info('Raw response (full, unmodified):')
    logger.info(json.dumps(resp, indent=2, default=str))

    status = resp.get('status')
    message = resp.get('message')
    errorcode = resp.get('errorcode')
    data = resp.get('data') or {}
    orderid = data.get('orderid') if isinstance(data, dict) else None

    logger.info('-' * 70)
    logger.info('SUMMARY: status=%s message=%r errorcode=%s orderid=%s',
               status, message, errorcode, orderid)
    logger.info('-' * 70)

    if status and orderid:
        logger.info('Order appears to have been ACCEPTED (orderid=%s). Checking order book for '
                    'current status...', orderid)
        try:
            ob = obj.orderBook()
            logger.info('orderBook() raw response:')
            logger.info(json.dumps(ob, indent=2, default=str))
        except Exception:
            logger.exception('orderBook() call failed — see traceback above. '
                             'Check the order manually in the Angel One app/terminal.')
    else:
        logger.warning('Order was NOT accepted. See status/message/errorcode above for the exact '
                       'broker-side reason.')

    logger.info('Done. Full log saved to %s', LOG_FILE)
    logger.warning('Remember: if this order WAS placed, it is a real position — '
                   'exit it manually if you do not want to hold it.')


if __name__ == '__main__':
    main()
