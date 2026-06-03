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


# ── 4. getScripMaster ─────────────────────────────────────────────────────────
print('\n[4] getScripMaster (NFO)')
try:
    raw = obj.getScripMaster('NFO')
    if isinstance(raw, str):
        raw = json.loads(raw)
    data = raw if isinstance(raw, list) else raw.get('data', [])
    df   = pd.DataFrame(data)
    df.columns = [c.lower() for c in df.columns]
    nifty = df[(df.get('name', pd.Series()) == 'NIFTY') &
               (df.get('instrumenttype', pd.Series()) == 'OPTIDX')]
    if len(nifty) > 0:
        report('getScripMaster NFO', True,
               f'{len(df):,} total rows  {len(nifty):,} Nifty options')
    else:
        report('getScripMaster NFO', False, 'no Nifty options found')
except Exception as e:
    report('getScripMaster NFO', False, str(e))


# ── 5. WebSocket LTP feed ─────────────────────────────────────────────────────
print('\n[5] WebSocket LTP feed')
try:
    from websocket_feed import SharedFeed, EXCHANGE_NSE_CM

    ticks_received = []

    def on_alert(msg):
        print(f'     feed alert: {msg}')

    feed = SharedFeed(
        obj            = obj,
        auth_token     = auth_token,
        startup_tokens = [(EXCHANGE_NSE_CM, NIFTY_TOKEN)],
        alert_callback = on_alert,
    )

    print('     Waiting for Nifty LTP ticks (10s)...')
    for _ in range(20):
        ltp = feed.get_ltp(NIFTY_TOKEN)
        if ltp:
            ticks_received.append(ltp)
        time.sleep(0.5)

    feed.close()

    if ticks_received:
        report('WebSocket LTP', True,
               f'Nifty LTP={ticks_received[-1]:.2f}  '
               f'({len(ticks_received)} readings in 10s)')
    else:
        report('WebSocket LTP', False, 'no ticks received in 10s')
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
