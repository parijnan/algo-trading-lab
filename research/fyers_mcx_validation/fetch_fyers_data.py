"""
Plan §1.1 step 2: pull one fully-expired, fully-overlapping MCX contract from
Fyers via the documented 3-step expired-contract workflow, and save it in the
same schema as our existing Angel One files, for a direct bar-by-bar
comparison (done separately in compare_with_angelone.py).

Endpoints (confirmed directly against myapi.fyers.in/docsv3, 2026-09-15):
  GET https://api-t1.fyers.in/data/history/fno/expired/expiry-dates
  GET https://api-t1.fyers.in/data/history/fno/expired/underlying-symbols
  GET https://api-t1.fyers.in/data/history/fno/expired/historical-data
"1-min resolution, up to 100 days per request".

2026-09-15 finding: Get Expiry Dates' `symbol` param does NOT accept a bare
underlying like "MCX:CRUDEOILM" (tried first, per the docs' own equity-index
examples like NSE:NIFTY50-INDEX -- got {'code':-50,'symbol':'Invalid symbol
provided'}). MCX commodities have no standalone "-INDEX"-style quote symbol.
It needs ANY real, currently-tradeable contract symbol for that underlying
(Fyers resolves the underlying internally and returns its full expiry history
regardless of which specific contract you passed) -- confirmed against
Fyers's own live MCX_COM_sym_master.json. Also corrects the plan's §0
assumption that Fyers's MCX symbol format matches ours exactly: it doesn't --
Fyers uses "CRUDEOILM26OCTFUT" (month+2-digit-year, no day), Angel One uses
"CRUDEOILM19OCT26FUT" (day+month+year).

Usage (run from repo root):
  python research/fyers_mcx_validation/fetch_fyers_data.py \\
      --underlying MCX:CRUDEOILM26OCTFUT --expiry 2026-08-19 \\
      --hist-from 2026-07-21 --hist-to 2026-08-18
"""
import argparse
import csv
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = Path(__file__).parent / 'data'
OUT_DIR.mkdir(exist_ok=True)

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')


def _creds():
    with open(REPO_ROOT / 'data' / 'user_credentials.csv', newline='') as f:
        return next(csv.DictReader(f))


def _get(url: str, params: dict, auth: str) -> dict:
    full_url = f'{url}?{urllib.parse.urlencode(params)}'
    req = urllib.request.Request(full_url, headers={'Authorization': auth, 'User-Agent': _UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f'  HTTP {e.code} for {full_url}')
        print(f'  body: {body}')
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise


def fetch(underlying: str, target_expiry: str, range_from: str, range_to: str,
          hist_from: str, hist_to: str) -> Path:
    creds = _creds()
    auth = f"{creds['fyers_app_id']}:{creds['fyers_access_token']}"

    print(f'[1/3] Get Expiry Dates for {underlying}, {range_from} -> {range_to} ...')
    r1 = _get('https://api-t1.fyers.in/data/history/fno/expired/expiry-dates', {
        'symbol': underlying, 'range_from': range_from, 'range_to': range_to, 'date_format': 1,
    }, auth)
    print('  response status:', r1.get('s'), r1.get('message'))
    if r1.get('s') != 'ok':
        print('  FULL RESPONSE:', json.dumps(r1, indent=2))
        sys.exit(1)
    futures_expiries = r1.get('data', {}).get('expiry_dates', {}).get('futures', [])
    print('  futures expiry dates found:', futures_expiries)

    if target_expiry not in futures_expiries:
        print(f'  WARNING: {target_expiry} not found exactly in the returned list -- '
              f'check date format / nearby dates above before proceeding.')
        sys.exit(1)

    print(f'\n[2/3] Get Expired Contracts for {underlying}, expiry={target_expiry} ...')
    r2 = _get('https://api-t1.fyers.in/data/history/fno/expired/underlying-symbols', {
        'symbol': underlying, 'expiry_date': target_expiry,
    }, auth)
    print('  response status:', r2.get('s'), r2.get('message'))
    if r2.get('s') != 'ok':
        print('  FULL RESPONSE:', json.dumps(r2, indent=2))
        sys.exit(1)
    futures_contracts = r2.get('data', {}).get('contracts', {}).get('futures', [])
    print('  futures contracts found:', futures_contracts)

    if len(futures_contracts) != 1:
        print(f'  WARNING: expected exactly 1 futures contract for this expiry, got '
              f'{len(futures_contracts)}: {futures_contracts} -- picking the first, verify manually.')
    if not futures_contracts:
        sys.exit(1)
    contract_symbol = futures_contracts[0]

    print(f'\n[3/3] Get Expired F&O Data for {contract_symbol}, '
          f'{hist_from} -> {hist_to}, resolution=1 ...')
    r3 = _get('https://api-t1.fyers.in/data/history/fno/expired/historical-data', {
        'symbol': contract_symbol, 'resolution': '1', 'date_format': 1,
        'range_from': hist_from, 'range_to': hist_to,
    }, auth)
    print('  response status:', r3.get('s'), r3.get('message'))
    if r3.get('s') not in ('ok',):
        print('  FULL RESPONSE:', json.dumps(r3, indent=2))
        sys.exit(1)

    columns = r3.get('columns', [])
    candles = r3.get('candles', [])
    print(f'  columns: {columns}')
    print(f'  candle rows returned: {len(candles)}')

    if not candles:
        print('  No candles returned -- nothing to save.')
        sys.exit(1)

    IST = timezone(timedelta(hours=5, minutes=30))
    out_path = OUT_DIR / f'fyers_{contract_symbol.replace(":", "_")}_1min.csv'
    with open(out_path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['time_stamp', 'open', 'high', 'low', 'close', 'volume'])
        for row in candles:
            ts_epoch = row[0]
            ts_ist = datetime.fromtimestamp(ts_epoch, tz=IST)
            w.writerow([ts_ist.strftime('%Y-%m-%d %H:%M:%S+05:30'), row[1], row[2], row[3], row[4], row[5]])

    print(f'\nSaved {len(candles)} rows to {out_path}')
    print(f'Contract symbol used: {contract_symbol}')
    print(f'First candle (IST): {datetime.fromtimestamp(candles[0][0], tz=IST)}')
    print(f'Last candle (IST):  {datetime.fromtimestamp(candles[-1][0], tz=IST)}')
    return out_path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--underlying', required=True, help='Any live Fyers contract symbol for the underlying, e.g. MCX:CRUDEOILM26OCTFUT')
    p.add_argument('--expiry', required=True, help='Target expiry date, yyyy-mm-dd')
    p.add_argument('--range-from', default=None, help='Get Expiry Dates search window start (default: 60 days before --expiry)')
    p.add_argument('--range-to', default=None, help='Get Expiry Dates search window end (default: --expiry)')
    p.add_argument('--hist-from', required=True, help='Historical data range start, yyyy-mm-dd')
    p.add_argument('--hist-to', required=True, help='Historical data range end, yyyy-mm-dd')
    args = p.parse_args()

    range_to = args.range_to or args.expiry
    if args.range_from:
        range_from = args.range_from
    else:
        d = datetime.strptime(args.expiry, '%Y-%m-%d') - timedelta(days=60)
        range_from = d.strftime('%Y-%m-%d')

    fetch(args.underlying, args.expiry, range_from, range_to, args.hist_from, args.hist_to)


if __name__ == '__main__':
    main()
