"""
data_pipeline/fyers_auth.py -- headless daily Fyers re-authentication.

Fyers's access_token expires at midnight IST regardless of issue time, and
refresh tokens are being discontinued under the April 2026 SEBI algo-trading
framework (plan §2.3, plans/fyers-mcx-data-integration.md) -- so a
cron-safe design needs a fully headless login, not a token refresh. Fyers's
own "External 2FA TOTP" feature (Profile -> Others -> External 2FA TOTP ->
Enable, on the Fyers app or web) exposes a raw, copyable TOTP secret -- the
same RFC 6238 mechanism this project already uses for Angel One via pyotp
(leto.py, data_downloader_angelone.py) -- letting this script replicate the
full browser login sequence with zero human interaction. Only a single
one-time manual browser visit was ever needed to grant the app's OAuth
consent (already done, 2026-09-15).

Sequence (all four `vagator`/`api.fyers.in` endpoints below are
UNOFFICIAL -- not in Fyers's public API docs, reverse-engineered from
their own web app, stable enough that multiple independent community
tools rely on them today, but could change without notice; re-check this
file first if daily auth ever silently starts failing):
  1. POST api-t2.fyers.in/vagator/v2/send_login_otp_v2  (fy_id -> request_key)
  2. POST api-t2.fyers.in/vagator/v2/verify_otp          (request_key + current
     TOTP code -> new request_key)
  3. POST api-t2.fyers.in/vagator/v2/verify_pin_v2       (request_key + PIN ->
     a short-lived session access_token)
  4. POST api.fyers.in/api/v2/token                      (session token + app
     details -> a redirect URL containing `auth_code`, replicating the
     one-time browser OAuth consent already granted)
  5. POST api-t1.fyers.in/api/v3/validate-authcode        (auth_code +
     appIdHash=sha256(app_id:app_secret) -> the real API access_token +
     refresh_token)
Confirmed 2026-09-15 against Fyers's own community-maintained sample
implementations and this plan's own already-completed manual OAuth
exchange (same appIdHash formula, same validate-authcode endpoint).

A real browser User-Agent is required on every call -- Cloudflare silently
403s bare-urllib requests otherwise (found 2026-09-15 during the manual
OAuth exchange, plan §2.3).

**Never logs a raw token/OTP/PIN/TOTP code at any level.** See
project_smartapi_logger_credential_exposure and the AB1007 diagnostic's
own credential-leak fix (this session) for why that's a hard rule here,
not a nice-to-have -- every error path below explicitly strips token/data
fields before logging a failed response body.

**This app (fyers_app_id ending -200) has order-placement scope, not just
data scope** (plan §2.1 addendum -- the account owner's own deliberate
choice, not accidental). This script and its resulting access_token are
used ONLY for data (fyers_st_probe.py, data_downloader_fyers_mcx.py).
Never use the resulting token to place an order.

Requires these columns in data/user_credentials.csv (gitignored):
  fyers_app_id, fyers_app_secret   -- already populated (plan §2.1/§2.2)
  fyers_client_id                  -- Fyers login username / fy_id, e.g. "XY01234"
  fyers_pin                        -- 4-digit account PIN
  fyers_totp_key                   -- TOTP secret from External 2FA TOTP setup
fyers_access_token/fyers_refresh_token are written by this script, not
read from as input.

Usage:
  python data_pipeline/fyers_auth.py            # refresh + write to user_credentials.csv
  from data_pipeline.fyers_auth import ensure_fresh_token
  ensure_fresh_token()                          # library use, e.g. fyers_st_probe.py's own startup
"""
import base64
import csv
import hashlib
import json
import logging
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pyotp

REPO_ROOT = Path(__file__).resolve().parent.parent
CREDS_FILE = REPO_ROOT / 'data' / 'user_credentials.csv'

APP_TYPE = '2'          # Fyers web-app type id, per community convention (send_login_otp_v2)
REDIRECT_URI = 'https://quant-grow.com'   # registered redirect URL, plan §2.1 addendum

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')

logger = logging.getLogger('fyers_auth')
if not logger.handlers:
    logger.setLevel(logging.INFO)
    logger.propagate = False
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
                                      datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(_h)


def _read_creds() -> dict:
    with open(CREDS_FILE, newline='') as f:
        return next(csv.DictReader(f))


def _write_tokens(access_token: str, refresh_token: str) -> None:
    """Rewrites only fyers_access_token/fyers_refresh_token in place,
    preserving every other column and row exactly -- this file holds
    credentials for every broker/service in the repo, not just Fyers."""
    with open(CREDS_FILE, newline='') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    rows[0]['fyers_access_token'] = access_token
    rows[0]['fyers_refresh_token'] = refresh_token
    with open(CREDS_FILE, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _post(url: str, payload: dict, headers: dict = None) -> dict:
    req_headers = {'Content-Type': 'application/json', 'User-Agent': _UA}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=req_headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            raise RuntimeError(f'HTTP {e.code} from {url} (non-JSON body)')


def _redact(d: dict, drop_keys=('data', 'access_token', 'refresh_token', 'request_key')) -> dict:
    return {k: v for k, v in d.items() if k not in drop_keys}


def login_headless() -> tuple:
    """Runs the full 5-step headless login. Returns (access_token, refresh_token).
    Raises on any step's failure with a redacted error body -- callers
    (this module's own main(), or fyers_st_probe.py's startup) should
    treat a raised exception as 'cannot start today', the same severity
    as any other unrecoverable startup failure."""
    creds = _read_creds()
    fy_id = creds.get('fyers_client_id', '').strip()
    pin = creds.get('fyers_pin', '').strip()
    totp_key = creds.get('fyers_totp_key', '').strip()
    app_id = creds.get('fyers_app_id', '').strip()
    app_secret = creds.get('fyers_app_secret', '').strip()
    missing = [n for n, v in [('fyers_client_id', fy_id), ('fyers_pin', pin),
                              ('fyers_totp_key', totp_key), ('fyers_app_id', app_id),
                              ('fyers_app_secret', app_secret)] if not v]
    if missing:
        raise RuntimeError(f'login_headless: missing required credential column(s) in '
                           f'{CREDS_FILE}: {missing}. External 2FA TOTP must be enabled on the '
                           f'Fyers account first (Profile -> Others -> External 2FA TOTP -> Enable) '
                           f'to obtain fyers_totp_key.')

    # Step 1: send login OTP. fy_id only -- despite the endpoint name, no
    # SMS/email OTP is actually sent once External 2FA TOTP is enabled;
    # this just issues the challenge that step 2's locally-computed TOTP answers.
    r1 = _post('https://api-t2.fyers.in/vagator/v2/send_login_otp_v2',
              {'fy_id': base64.b64encode(fy_id.encode()).decode(), 'app_id': APP_TYPE})
    if 'request_key' not in r1:
        raise RuntimeError(f'login_headless step 1 (send_login_otp_v2) failed: {_redact(r1)}')
    request_key = r1['request_key']
    logger.info('Step 1/5 (send_login_otp_v2): OK')

    # Step 2: verify TOTP
    totp_code = pyotp.TOTP(totp_key).now()
    r2 = _post('https://api-t2.fyers.in/vagator/v2/verify_otp',
              {'request_key': request_key, 'otp': totp_code})
    if r2.get('s') != 'ok' or 'request_key' not in r2:
        raise RuntimeError(f'login_headless step 2 (verify_otp) failed: {_redact(r2)}')
    request_key = r2['request_key']
    logger.info('Step 2/5 (verify_otp / TOTP): OK')

    # Step 3: verify PIN -> short-lived session token
    r3 = _post('https://api-t2.fyers.in/vagator/v2/verify_pin_v2',
              {'request_key': request_key, 'identity_type': 'pin',
               'identifier': base64.b64encode(pin.encode()).decode()})
    if r3.get('s') != 'ok' or 'data' not in r3 or 'access_token' not in r3.get('data', {}):
        raise RuntimeError(f'login_headless step 3 (verify_pin_v2) failed: {_redact(r3)}')
    session_token = r3['data']['access_token']
    logger.info('Step 3/5 (verify_pin_v2): OK')

    # Step 4: exchange session token for an auth_code (replicates the
    # one-time browser OAuth consent already granted 2026-09-15)
    r4 = _post('https://api.fyers.in/api/v2/token',
              {'fyers_id': fy_id, 'app_id': app_id[:-4], 'redirect_uri': REDIRECT_URI,
               'appType': '100', 'code_challenge': '', 'state': 'fyers_st_probe',
               'scope': '', 'nonce': '', 'response_type': 'code', 'create_cookie': True},
              headers={'Authorization': f'Bearer {session_token}'})
    redirect_url = r4.get('Url') or r4.get('url')
    if not redirect_url:
        raise RuntimeError(f'login_headless step 4 (api/v2/token) failed: {_redact(r4)}')
    parsed = urllib.parse.urlparse(redirect_url)
    auth_code = urllib.parse.parse_qs(parsed.query).get('auth_code', [None])[0]
    if not auth_code:
        raise RuntimeError('login_headless step 4: no auth_code in redirect URL '
                           f'(host={parsed.netloc}, path={parsed.path}).')
    logger.info('Step 4/5 (api/v2/token -> auth_code): OK')

    # Step 5: exchange auth_code for the real API access_token + refresh_token
    app_id_hash = hashlib.sha256(f'{app_id}:{app_secret}'.encode()).hexdigest()
    r5 = _post('https://api-t1.fyers.in/api/v3/validate-authcode',
              {'grant_type': 'authorization_code', 'appIdHash': app_id_hash, 'code': auth_code})
    if r5.get('s') != 'ok' or 'access_token' not in r5:
        raise RuntimeError(f'login_headless step 5 (validate-authcode) failed: {_redact(r5)}')
    logger.info('Step 5/5 (validate-authcode): OK -- fresh access_token obtained.')
    return r5['access_token'], r5.get('refresh_token', '')


def ensure_fresh_token() -> str:
    """Convenience entry point for other scripts (fyers_st_probe.py etc.):
    always performs a fresh headless login and writes the result to
    user_credentials.csv, then returns the new access_token. Does NOT try
    to detect whether the existing token is 'still valid' first -- Fyers's
    own midnight-IST expiry means any cron-started process should just
    refresh unconditionally at startup rather than guessing."""
    access_token, refresh_token = login_headless()
    _write_tokens(access_token, refresh_token)
    logger.info('user_credentials.csv updated with fresh Fyers tokens.')
    return access_token


def main():
    try:
        ensure_fresh_token()
        print('Fyers headless login succeeded -- user_credentials.csv updated.')
    except Exception as e:
        logger.error(f'Headless login failed: {e}')
        sys.exit(1)


if __name__ == '__main__':
    main()
