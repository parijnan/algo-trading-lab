"""
data_pipeline/fyers_token_refresh.py -- the deterministic half of the `fyers-token` skill: build the Fyers login URL, turn the
redirect URL the owner's logged-in Chrome lands on into a fresh access token, write it, and verify a token file.

Fyers cannot be re-authenticated unattended (plans/fyers-mcx-data-integration.md section 3.6: headless login and refresh tokens are
both closed under the SEBI framework), and its access token expires at midnight IST whatever time it was issued. So the one
manual step is the owner logging in to Fyers in Chrome; the `fyers-token` skill then drives the OAuth redirect in that session and
calls this script. Nothing here ever prints, logs or passes on a command line a token or an auth code: tokens are read from and
written to files, and every message carries only a short sha256 fingerprint so two copies can be compared.

    python data_pipeline/fyers_token_refresh.py auth-url
    python data_pipeline/fyers_token_refresh.py exchange --redirect-url-file F --state S [--out PATH] [--update-creds]
    python data_pipeline/fyers_token_refresh.py verify [--token-file PATH] [--no-live]

The token file is `hestia_data/fyers_token.json` (plans/hestia-fyers-candle-source.md section 3.2): `{app_id, access_token,
issued_at, expires_at}`, mode 600, written atomically. `--update-creds` also writes fyers_access_token / fyers_refresh_token in
the local data/user_credentials.csv, which the laptop's Fyers downloaders already read. This app has order-placement scope (plan
section 2.1): the token is used for data only, and nothing in this file places an order.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
REPO_ROOT = Path(__file__).resolve().parent.parent
CREDS_FILE = REPO_ROOT / 'data' / 'user_credentials.csv'
DEFAULT_TOKEN_FILE = REPO_ROOT / 'hestia_data' / 'fyers_token.json'

REDIRECT_URI = 'https://quant-grow.com'                                   # the registered redirect URL (plan section 2.1)
AUTH_URL = 'https://api-t1.fyers.in/api/v3/generate-authcode'
TOKEN_URL = 'https://api-t1.fyers.in/api/v3/validate-authcode'
HISTORY_URL = 'https://api-t1.fyers.in/data/history'
VERIFY_SYMBOL = 'NSE:INDIAVIX-INDEX'                                      # an index that never expires; known-good History call
_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')


class TokenError(Exception):
    """A failure whose message is safe to show: it never contains a token or an auth code."""


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:8]


def now_ist() -> datetime:
    return datetime.now(IST)


# ---- credentials -----------------------------------------------------------------------------------------------------------

def read_creds(path: Path = CREDS_FILE) -> dict:
    with open(path, newline='') as f:
        return next(csv.DictReader(f))


def update_creds(access_token: str, refresh_token: str, path: Path = CREDS_FILE) -> None:
    """Rewrites only fyers_access_token and fyers_refresh_token, preserving every other column and row: this file holds the
    credentials of every broker in the repo."""
    with open(path, newline='') as f:
        reader = csv.DictReader(f)
        fieldnames, rows = reader.fieldnames, list(reader)
    rows[0]['fyers_access_token'] = access_token
    rows[0]['fyers_refresh_token'] = refresh_token
    tmp = Path(str(path) + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


# ---- the login URL and the redirect ----------------------------------------------------------------------------------------

def build_auth_url(app_id: str, state: str) -> str:
    q = urllib.parse.urlencode({'client_id': app_id, 'redirect_uri': REDIRECT_URI, 'response_type': 'code', 'state': state})
    return f'{AUTH_URL}?{q}'


def parse_redirect(url: str, expected_state: str) -> str:
    """The auth code out of the URL the browser landed on. Refuses anything that is not our own redirect carrying our own state
    (a mismatched state means the page was not the answer to the login we started)."""
    url = url.strip()
    if not url.startswith(REDIRECT_URI):
        raise TokenError(f'the page the browser landed on is not the registered redirect ({REDIRECT_URI}); '
                         f'is the Fyers login still valid, or is a consent or login page showing?')
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if params.get('state', [None])[0] != expected_state:
        raise TokenError('the redirect URL carries a different state than the login that was started; refusing to use it')
    code = params.get('auth_code', [None])[0]
    if not code:
        reason = params.get('message', params.get('error', ['no auth_code in the redirect']))[0]
        raise TokenError(f'Fyers returned no auth code ({reason})')
    return code


# ---- the exchange -----------------------------------------------------------------------------------------------------------

def _post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'User-Agent': _UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            raise TokenError(f'HTTP {e.code} from the Fyers token endpoint (non-JSON body)')


def exchange(auth_code: str, creds: dict, post: Optional[Callable] = None) -> Tuple[str, str]:
    """(access_token, refresh_token) for an auth code. A failure message carries Fyers's code and message only."""
    post = post or _post
    app_id, secret = (creds.get('fyers_app_id') or '').strip(), (creds.get('fyers_app_secret') or '').strip()
    if not app_id or not secret:
        raise TokenError('fyers_app_id / fyers_app_secret are missing from the credentials file')
    body = post(TOKEN_URL, {'grant_type': 'authorization_code', 'appIdHash': hashlib.sha256(f'{app_id}:{secret}'.encode()).hexdigest(),
                            'code': auth_code})
    if body.get('s') != 'ok' or not body.get('access_token'):
        raise TokenError(f"Fyers refused the auth code: code {body.get('code')}, {body.get('message')} "
                         f'(an auth code is single-use and short-lived: log in again and retry)')
    return body['access_token'], body.get('refresh_token', '')


# ---- the token file ----------------------------------------------------------------------------------------------------------

def token_record(app_id: str, access_token: str, now: Optional[datetime] = None) -> dict:
    now = (now or now_ist()).astimezone(IST)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return {'app_id': app_id, 'access_token': access_token, 'issued_at': now.isoformat(timespec='seconds'),
            'expires_at': midnight.isoformat(timespec='seconds')}


def write_token_file(path: Path, record: dict) -> None:
    """Atomic, mode 600 from the first byte (never a world-readable moment)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(record, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def check_token_file(path: Path, now: Optional[datetime] = None) -> Tuple[List[str], Optional[dict]]:
    """(problems, record). Fresh means BOTH the file's modification date and the embedded issued_at date are today in IST, so a
    copied file that kept a stale mtime, or a fresh copy of a stale token, is each caught. Mode must be 600."""
    now = (now or now_ist()).astimezone(IST)
    path = Path(path)
    if not path.exists():
        return [f'{path} does not exist'], None
    problems: List[str] = []
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        problems.append(f'mode is {oct(mode)}, expected 600 (group/other can read a token)')
    mtime = datetime.fromtimestamp(path.stat().st_mtime, IST)
    if mtime.date() != now.date():
        problems.append(f'file modified {mtime:%Y-%m-%d %H:%M}, not today ({now:%Y-%m-%d})')
    try:
        record = json.loads(path.read_text())
        issued = datetime.fromisoformat(record['issued_at']).astimezone(IST)
        if not record.get('access_token') or not record.get('app_id'):
            raise KeyError('access_token/app_id')
    except Exception as exc:                                                  # noqa: BLE001
        return problems + [f'unreadable or malformed ({type(exc).__name__})'], None
    if issued.date() != now.date():
        problems.append(f'issued_at {issued:%Y-%m-%d %H:%M} is not today ({now:%Y-%m-%d})')
    return problems, record


def _get(url: str, params: dict, auth: str) -> dict:
    req = urllib.request.Request(f'{url}?{urllib.parse.urlencode(params)}', headers={'Authorization': auth, 'User-Agent': _UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {'s': 'error', 'code': f'HTTP{e.code}', 'message': 'non-JSON body'}


def live_check(record: dict, get: Optional[Callable] = None, now: Optional[datetime] = None) -> Tuple[bool, str]:
    """One small, known-good History call with the token. no_data is fine (a holiday window): only an authentication refusal or
    an error fails it."""
    get = get or _get
    now = now or now_ist()
    start = (now - timedelta(days=10)).strftime('%Y-%m-%d')
    body = get(HISTORY_URL, {'symbol': VERIFY_SYMBOL, 'resolution': 'D', 'date_format': '1', 'range_from': start,
                             'range_to': now.strftime('%Y-%m-%d'), 'cont_flag': '1'}, f"{record['app_id']}:{record['access_token']}")
    if body.get('s') in ('ok', 'no_data'):
        return True, f"History call ok ({len(body.get('candles', []))} candles for {VERIFY_SYMBOL})"
    return False, f"Fyers rejected the token: code {body.get('code')}, {body.get('message')}"


# ---- the command line --------------------------------------------------------------------------------------------------------

def cmd_auth_url(args) -> int:
    app_id = (read_creds(Path(args.creds)).get('fyers_app_id') or '').strip()
    if not app_id:
        print('fyers_app_id is missing from the credentials file', file=sys.stderr)
        return 1
    state = secrets.token_urlsafe(16)
    print(json.dumps({'url': build_auth_url(app_id, state), 'state': state}))
    return 0


def cmd_exchange(args) -> int:
    path = Path(args.redirect_url_file)
    try:
        url = path.read_text()
    finally:
        if args.delete_input and path.exists():
            path.unlink()                                                     # the URL carries a one-time auth code
    try:
        code = parse_redirect(url, args.state)
        creds = read_creds(Path(args.creds))
        access, refresh = exchange(code, creds)
    except TokenError as exc:
        print(f'FAILED: {exc}', file=sys.stderr)
        return 1
    record = token_record(creds['fyers_app_id'].strip(), access)
    write_token_file(Path(args.out), record)
    if args.update_creds:
        update_creds(access, refresh, Path(args.creds))
    print(f"Token refreshed: issued {record['issued_at']}, expires {record['expires_at']}, fingerprint {fingerprint(access)}, "
          f"written to {args.out}" + ('; local credentials file updated' if args.update_creds else ''))
    return 0


def cmd_verify(args) -> int:
    problems, record = check_token_file(Path(args.token_file))
    ok = not problems
    for p in problems:
        print(f'FAIL  {p}')
    if record is not None:
        print(f"      issued_at {record['issued_at']}, fingerprint {fingerprint(record['access_token'])}")
    if record is not None and not args.no_live:
        good, detail = live_check(record)
        print(('OK    ' if good else 'FAIL  ') + detail)
        ok = ok and good
    print('RESULT: ' + ('token is fresh and works' if ok else 'NOT OK'))
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--creds', default=str(CREDS_FILE))
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('auth-url').set_defaults(fn=cmd_auth_url)
    ex = sub.add_parser('exchange')
    ex.add_argument('--redirect-url-file', required=True)
    ex.add_argument('--state', required=True)
    ex.add_argument('--out', default=str(DEFAULT_TOKEN_FILE))
    ex.add_argument('--update-creds', action='store_true')
    ex.add_argument('--keep-input', dest='delete_input', action='store_false')
    ex.set_defaults(fn=cmd_exchange)
    vf = sub.add_parser('verify')
    vf.add_argument('--token-file', default=str(DEFAULT_TOKEN_FILE))
    vf.add_argument('--no-live', action='store_true')
    vf.set_defaults(fn=cmd_verify)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == '__main__':
    sys.exit(main())
