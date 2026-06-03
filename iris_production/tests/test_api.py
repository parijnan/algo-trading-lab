"""
API validation script for Iris paper trading.

Tests everything except actual order placement:
  1. Login (generateSession)
  2. getCandleData — FIVE_MINUTE interval (unverified — critical test)
  3. getCandleData — FIFTEEN_MINUTE interval (known to work)
  4. getScripMaster — Nifty options scrip master
  5. WebSocket LTP feed — Nifty index ticks

Usage (from repo root):
    python iris_production/tests/test_api.py

Place user_credentials.csv in iris_production/data/ first.
Columns: api_key, client_id, password, totp_token
"""
import sys, time, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
from datetime import datetime, timedelta
from configs import CREDS_FILE, NIFTY_TOKEN, INDEX_EXCHANGE

PASS = '✓'
FAIL = '✗'
results = []

def report(label, ok, detail=''):
    sym = PASS if ok else FAIL
    print(f'  {sym}  {label}' + (f'  →  {detail}' if detail else ''))
    results.append(ok)


# ── 1. Login ─────────────────────────────────────────────────────────────────
print('\n[1] Login')
try:
    import pyotp
    from SmartApi import SmartConnect

    creds = pd.read_csv(CREDS_FILE)
    row   = creds.iloc[0]
    obj   = SmartConnect(api_key=str(row['api_key']))
    totp  = pyotp.TOTP(str(row['totp_token'])).now()
    resp  = obj.generateSession(str(row['client_id']), str(row['password']), totp)

    if resp.get('status'):
        auth_token = resp['data']['jwtToken']
        client_id  = str(row['client_id'])
        report('generateSession', True, f'client={client_id}')
    else:
        report('generateSession', False, str(resp))
        print('\nLogin failed — cannot continue.')
        sys.exit(1)
except Exception as e:
    report('generateSession', False, str(e))
    sys.exit(1)


# Brief pause — Angel One rate limiter needs ~3s after a new session
# before accepting getCandleData. Production code has natural delay via
# scrip master download; test skips that so we add an explicit wait.
time.sleep(3)

# ── 2. getCandleData — FIVE_MINUTE (the critical unverified test) ─────────────
print('\n[2] getCandleData — FIVE_MINUTE')
try:
    now   = datetime.now().replace(second=0, microsecond=0)
    start = (now - timedelta(hours=2)).strftime('%Y-%m-%d %H:%M')
    end   = now.strftime('%Y-%m-%d %H:%M')
    resp  = obj.getCandleData({
        'exchange':    INDEX_EXCHANGE,
        'symboltoken': NIFTY_TOKEN,
        'interval':    'FIVE_MINUTE',
        'fromdate':    start,
        'todate':      end,
    })
    data = resp.get('data', [])
    if data:
        last = data[-1]
        report('FIVE_MINUTE interval', True,
               f'{len(data)} candles  last={last[0]}  close={last[4]}')
    else:
        report('FIVE_MINUTE interval', False,
               f'no data returned — msg: {resp.get("message","")}')
except Exception as e:
    report('FIVE_MINUTE interval', False, str(e))


# ── 3. getCandleData — FIFTEEN_MINUTE (baseline) ─────────────────────────────
print('\n[3] getCandleData — FIFTEEN_MINUTE')
try:
    resp = obj.getCandleData({
        'exchange':    INDEX_EXCHANGE,
        'symboltoken': NIFTY_TOKEN,
        'interval':    'FIFTEEN_MINUTE',
        'fromdate':    start,
        'todate':      end,
    })
    data = resp.get('data', [])
    if data:
        report('FIFTEEN_MINUTE interval', True,
               f'{len(data)} candles  last close={data[-1][4]}')
    else:
        report('FIFTEEN_MINUTE interval', False, resp.get('message', ''))
except Exception as e:
    report('FIFTEEN_MINUTE interval', False, str(e))


# ── 4. Scrip master (public URL — same as Leto) ───────────────────────────────
print('\n[4] Scrip master download')
try:
    from urllib.request import urlopen
    from io import StringIO
    SCRIP_URL = ('https://margincalculator.angelbroking.com/'
                 'OpenAPI_File/files/OpenAPIScripMaster.json')
    df    = pd.read_json(StringIO(urlopen(SCRIP_URL).read().decode()))
    df.columns = [c.lower() for c in df.columns]
    nifty = df[(df['exch_seg'] == 'NFO') & (df['name'] == 'NIFTY')]
    if len(nifty) > 0:
        report('Scrip master (public URL)', True,
               f'{len(df):,} total rows  {len(nifty):,} Nifty NFO rows')
    else:
        report('Scrip master (public URL)', False, 'no Nifty rows found')
except Exception as e:
    report('Scrip master (public URL)', False, str(e))


# ── 5. WebSocket LTP feed ─────────────────────────────────────────────────────
print('\n[5] WebSocket LTP feed')
try:
    from websocket_feed import SharedFeed, EXCHANGE_NSE_CM

    feed_token = obj.getfeedToken()
    feed = SharedFeed()
    feed.start(
        auth_token     = auth_token,
        api_key        = str(creds.iloc[0]['api_key']),
        client_code    = client_id,
        feed_token     = feed_token,
        startup_tokens = [(EXCHANGE_NSE_CM, NIFTY_TOKEN)],
        alert_callback = lambda m: print(f'     feed alert: {m}'),
    )

    print('     Waiting for Nifty LTP ticks (10s)...')
    ticks_received = []
    for _ in range(20):
        ltp = feed.get_ltp(NIFTY_TOKEN)
        if ltp:
            ticks_received.append(ltp)
        time.sleep(0.5)

    feed.stop()

    if ticks_received:
        report('WebSocket LTP', True,
               f'Nifty LTP={ticks_received[-1]:.2f}  '
               f'({len(ticks_received)} readings in 10s)')
    else:
        report('WebSocket LTP', False, 'no ticks received in 10s — '
               'outside market hours or key has no WS access')
except Exception as e:
    report('WebSocket LTP', False, str(e))


# ── Summary ───────────────────────────────────────────────────────────────────
print(f'\n{"─"*50}')
passed = sum(results)
total  = len(results)
print(f'Result: {passed}/{total} checks passed')

if passed == total:
    print('All checks passed — API key is suitable for paper trading.')
else:
    print('Some checks failed — review above before paper trading.')

try:
    obj.terminateSession(client_id)
    print('Session terminated.')
except Exception:
    pass
