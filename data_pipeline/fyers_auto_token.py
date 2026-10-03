"""
data_pipeline/fyers_auto_token.py -- fully-automated daily Fyers token.

Fyers access tokens die daily at ~06:30 IST (docs: 6:30 AM) and SEBI's retail
algo framework killed the refresh-token path for good (live-probed 2026-10-02:
-16 "Refresh token API is currently disabled to comply with SEBI
regulations", staff-confirmed permanent). Raw-HTTP login replication is also
dead: the vagator login endpoints now require a Cloudflare Turnstile token in
a `fy_captcha_token` header (-1025 without it). But TOTP auto-login is the
officially endorsed automation -- Fyers staff themselves hand out TOTP
auto-login scripts for daily cron use (community threads 23636/22904/13699,
2026) -- and in a real Chrome browser Turnstile auto-solves (proven
2026-10-02, ~1s) and every remaining step is scriptable.

Two paths, both live-proven end-to-end 2026-10-02:

  1. STRAIGHT-THROUGH (~10s, most days): the persistent Chrome profile at
     data_pipeline/data/fyers_browser_profile/ holds a live Fyers web
     session cookie, so generate-authcode redirects straight to the
     registered redirect URI with a fresh auth_code -- no login form at all.

  2. FULL AUTO-LOGIN (~34s, after session expiry): client ID -> Turnstile
     auto-solves -> TOTP from fyers_totp_key via pyotp (same mechanism as
     Angel One in leto.py; account must have External 2FA TOTP enabled at
     myaccount.fyers.in -> ManageAccount, Profile API `totp: true`) -> PIN
     -> redirect with auth_code.

Gotchas found live on 2026-10-02, all handled here:
  - The 6-box TOTP form AUTO-SUBMITS on the 6th digit -- do not click the
    (now hidden) Confirm button afterwards; poll for the PIN form instead.
  - The registered redirect URI (quant-grow.com) is deliberately unhosted
    and has NO DNS A record -- Chrome's navigation raises ERR_NAME_NOT_RESOLVED
    but the address bar still carries the full redirect URL with the
    auth_code. Treat a navigation error as a possible SUCCESS: check
    d.current_url for the redirect prefix before retrying.
  - Chrome's own DNS intermittently fails under Tailscale MagicDNS while the
    system resolver always succeeds -- pre-resolve the Fyers hosts and pin
    them via --host-resolver-rules.
  - A real browser User-Agent is required on every direct HTTP call or
    Cloudflare 403s (same as fyers_auth.py / fyers_token_refresh.py).

Never logs a raw token/auth_code/TOTP/PIN at any level. Tokens are written
atomically: data/user_credentials.csv (both Fyers token columns) and
hestia_data/fyers_token.json (same format fyers_token_refresh.py defines --
{app_id, access_token, issued_at, expires_at}, mode 600), reusing that
module's helpers so the format stays single-source. The token is for data
only: this app has order scope, and nothing here places an order.

The push to Delos (hestia_data/fyers_token.json over ssh stdin, atomic,
mode 600, then remote `verify`) lives in run_fyers_auto_token.sh, matching
the fyers-token skill's own mechanics -- this script only produces and
verifies the token locally.

Usage:
  python data_pipeline/fyers_auto_token.py          # cron entry (wrapper: run_fyers_auto_token.sh)
  Exit codes: 0 success; 2..10 specific failure stages (see main()).
"""

import csv
import json
import secrets
import socket
import sys
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyotp
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

# Reuse the deterministic half's helpers (same directory when run as a
# script) -- token file format, creds update, auth-code exchange, live check.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fyers_token_refresh import (  # noqa: E402  (path-based import, same package dir)
    DEFAULT_TOKEN_FILE,
    exchange,
    fingerprint,
    live_check,
    now_ist,
    parse_redirect,
    read_creds,
    token_record,
    update_creds,
    write_token_file,
)

IST = ZoneInfo('Asia/Kolkata')
REPO_ROOT = Path(__file__).resolve().parent.parent
CREDS_FILE = REPO_ROOT / 'data' / 'user_credentials.csv'
PROFILE_DIR = Path(__file__).resolve().parent / 'data' / 'fyers_browser_profile'
REDIRECT_URI = 'https://quant-grow.com'
CHROMEDRIVER_PATH = '/home/parijnan/anaconda3/bin/chromedriver'
SLACK_DATA_CHANNEL = '#data-alerts'
SLACK_ERROR_CHANNEL = '#error-alerts'
SLACK_POST_URL = 'https://slack.com/api/chat.postMessage'

FYERS_HOSTS = ['api-t1.fyers.in', 'api-t2.fyers.in', 'api.fyers.in',
               'login.fyers.in', 'trade.fyers.in', 'myapi.fyers.in']

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')


def is_redirect(url):
    """Exact-host redirect predicate. A plain startswith(REDIRECT_URI) would
    also accept lookalike hosts (quant-grow.com.evil.example); the address
    bar must be on the registered redirect host itself."""
    try:
        return urllib.parse.urlparse(url).netloc == urllib.parse.urlparse(REDIRECT_URI).netloc \
            and url.startswith('https://')
    except Exception:  # noqa: BLE001
        return False


def log(msg):
    print(f'[{datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")}] {msg}', flush=True)


# ---------------------------------------------------------------------------
# Slack (same pattern as data_downloader_mcx.py; token read from creds file,
# nothing secret in the message)
# ---------------------------------------------------------------------------

def slack_send(msg, channel, creds):
    import urllib.request
    try:
        req = urllib.request.Request(
            SLACK_POST_URL,
            data=json.dumps({'channel': channel, 'text': msg}).encode(),
            headers={'Authorization': f"Bearer {creds['slack_token'].strip()}",
                     'Content-Type': 'application/json'},
            method='POST')
        with urllib.request.urlopen(req, timeout=8) as resp:
            json.loads(resp.read().decode())
    except Exception as e:  # noqa: BLE001
        log(f'(slack send failed: {e})')


# ---------------------------------------------------------------------------
# Browser plumbing
# ---------------------------------------------------------------------------

def resolver_rules():
    """Pin Fyers hosts to system-resolved IPs: Chrome's own DNS lookups
    intermittently fail under Tailscale MagicDNS while getaddrinfo always
    succeeds (found 2026-10-02)."""
    rules = []
    for h in FYERS_HOSTS:
        try:
            ip = socket.getaddrinfo(h, 443, socket.AF_INET)[0][4][0]
            rules.append(f'MAP {h} {ip}')
        except Exception as e:  # noqa: BLE001
            log(f'(could not pre-resolve {h}: {e})')
    return ','.join(rules)


def launch_chrome(headless=False):
    """Persistent-profile, automation-flag-hidden Chrome. headless=True uses
    headless=new (a last resort if no display is available -- Turnstile's
    tolerance of it is unproven on this IP; the headed path is the proven one)."""
    rules = resolver_rules()
    args = [f'--user-data-dir={PROFILE_DIR}', '--no-first-run',
            '--no-default-browser-check', '--disable-blink-features=AutomationControlled',
            '--window-size=1000,950']
    if headless:
        args.append('--headless=new')
    if rules:
        args.append(f'--host-resolver-rules={rules}')
    opts = Options()
    for a in args:
        opts.add_argument(a)
    opts.add_experimental_option('excludeSwitches', ['enable-automation'])
    opts.add_experimental_option('useAutomationExtension', False)
    try:
        d = webdriver.Chrome(service=Service(CHROMEDRIVER_PATH), options=opts)
    except WebDriverException:
        d = webdriver.Chrome(options=opts)  # fall back to Selenium Manager
    d.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument',
                      {'source': "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"})
    return d


def wait_visible(d, elem_id, timeout=45, redirect_check=None):
    end = time.time() + timeout
    while time.time() < end:
        cur = d.current_url
        if redirect_check and is_redirect(cur):
            return 'redirect'
        try:
            if d.find_element(By.ID, elem_id).is_displayed():
                return elem_id
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    raise TimeoutException(f'{elem_id} never appeared')


def wait_clickable(d, elem_id, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            b = d.find_element(By.ID, elem_id)
            if b.is_displayed() and d.execute_script('return !arguments[0].disabled', b):
                return b
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.5)
    raise TimeoutException(f'{elem_id} never became clickable (Turnstile or flow stuck)')


def fill_digit_boxes(d, form_id, value):
    """The TOTP/PIN forms use one input per digit and AUTO-SUBMIT when the
    last box is filled -- type slowly enough for the per-box input events to
    register (found 2026-10-02: instant send_keys left the form dead)."""
    inputs = [i for i in d.find_elements(By.CSS_SELECTOR, f'#{form_id} input')
              if i.is_displayed() and (i.get_attribute('type') or 'text').lower()
              in ('text', 'number', 'password', 'tel')]
    if not inputs:
        raise RuntimeError(f'no visible inputs in {form_id}')
    if len(inputs) == 1:
        inputs[0].clear()
        inputs[0].send_keys(value)
    else:
        for box, ch in zip(inputs, value):
            box.send_keys(ch)
            time.sleep(0.4)
    log(f'typed {len(value)} digits into {form_id} ({len(inputs)} box'
        f'{"es" if len(inputs) > 1 else ""})')


def fresh_totp(totp_key):
    """TOTP with a guard against the last seconds of the 30s window."""
    rem = 30 - (int(time.time()) % 30)
    if rem < 4:
        log(f'{rem}s left in TOTP window; waiting for the next one')
        time.sleep(rem + 0.5)
    return pyotp.TOTP(totp_key).now()


def get_auth_url(app_id, state):
    from urllib.parse import urlencode
    q = urlencode({'client_id': app_id, 'redirect_uri': REDIRECT_URI,
                  'response_type': 'code', 'state': state})
    return f'https://api-t1.fyers.in/api/v3/generate-authcode?{q}'


def navigate_capturing_redirect(d, auth_url, state):
    """Open the auth URL; return the redirect URL on success.

    The final redirect target (quant-grow.com) is deliberately unhosted with
    no DNS record, so the navigation that CARRIES the auth_code often raises
    ERR_NAME_NOT_RESOLVED -- that is success, not failure. Any navigation
    error is checked against d.current_url before being retrreated as
    retryable."""
    for attempt in range(3):
        try:
            d.get(auth_url)
            if is_redirect(d.current_url):
                log('straight-through redirect: live browser session reused')
                return d.current_url
            return None  # login page showing -- not an error
        except WebDriverException as e:
            if is_redirect(d.current_url):
                log('redirect URL captured from address bar (unhosted redirect domain)')
                return d.current_url
            if 'net::' in str(e):
                log(f'navigation error attempt {attempt + 1}: {str(e).splitlines()[0][:90]}')
                time.sleep(5)
                if is_redirect(d.current_url):
                    return d.current_url
            else:
                raise
    raise RuntimeError('page never loaded after retries')


def do_login(d, creds):
    """Full client-ID -> TOTP -> PIN login. Returns nothing; on success the
    browser has been redirected to the redirect URI."""
    fy_id = creds['fyers_client_id'].strip()
    totp_key = creds['fyers_totp_key'].strip()
    pin = creds['fyers_pin'].strip()

    # Client-ID tab of the login page (mobile-number is the default tab)
    try:
        rb = d.find_element(By.ID, 'clientId_rb')
        if rb.is_displayed():
            d.execute_script('arguments[0].click()', rb)
            time.sleep(1)
    except Exception:  # noqa: BLE001
        pass
    d.find_element(By.ID, 'fy_client_id').send_keys(fy_id)
    wait_clickable(d, 'clientIdSubmit').click()
    log('client ID submitted')

    r = wait_visible(d, 'confirmOtpForm', timeout=45, redirect_check=True)
    if r != 'redirect':
        fill_digit_boxes(d, 'confirmOtpForm', fresh_totp(totp_key))
        r = wait_visible(d, 'verifyPinForm', timeout=30, redirect_check=True)
    if r != 'redirect':
        log('TOTP accepted (form auto-submitted)')
        fill_digit_boxes(d, 'verifyPinForm', pin)
        # PIN may auto-submit; click only while the button is present+enabled
        end = time.time() + 60
        while time.time() < end:
            if is_redirect(d.current_url):
                return
            try:
                b = d.find_element(By.ID, 'verifyPinSubmit')
                if b.is_displayed() and d.execute_script('return !arguments[0].disabled', b):
                    b.click()
                    time.sleep(2)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.5)
        raise RuntimeError('no redirect after PIN (wrong PIN? attempt lockout?)')
    # PIN not needed this time (rare) -- fall through


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    exit_code = 0
    stage = 'startup'
    details = ''
    creds = read_creds(CREDS_FILE)
    app_id = creds['fyers_app_id'].strip()
    state = secrets.token_urlsafe(16)

    try:
        missing = [k for k in ('fyers_app_id', 'fyers_app_secret', 'fyers_client_id',
                               'fyers_pin', 'fyers_totp_key') if not creds.get(k, '').strip()]
        if missing:
            raise RuntimeError(f'missing credential columns in user_credentials.csv: {missing}')

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        for stale in PROFILE_DIR.glob('Singleton*'):
            stale.unlink()

        stage = 'browser'
        d = launch_chrome()
        try:
            stage = 'navigate'
            auth_url = get_auth_url(app_id, state)
            redirect_url = navigate_capturing_redirect(d, auth_url, state)

            stage = 'login'
            if redirect_url is None:
                log('login page showing -- full auto-login (session expired)')
                do_login(d, creds)
                redirect_url = d.current_url
                if not is_redirect(redirect_url):
                    raise RuntimeError(f'login completed but no redirect (url host='
                                       f'{urllib.parse.urlparse(redirect_url).netloc})')

            stage = 'exchange'
            auth_code = parse_redirect(redirect_url, state)  # raises TokenError on state/host mismatch
            access, refresh = exchange(auth_code, creds)
            record = token_record(app_id, access)
            write_token_file(DEFAULT_TOKEN_FILE, record)  # local hestia_data/, mode 600, atomic
            update_creds(access, refresh, CREDS_FILE)
            log(f'token exchanged and written: issued {record["issued_at"]}, '
                f'expires {record["expires_at"]}, fingerprint {fingerprint(access)}')

            stage = 'verify'
            ok, detail = live_check(record)
            if not ok:
                raise RuntimeError(f'fresh token rejected live: {detail}')
            log(f'live verification ok ({detail})')

            details = (f':white_check_mark: *Fyers auto-token* succeeded: issued '
                       f'{record["issued_at"]}, expires {record["expires_at"]}, '
                       f'fingerprint {fingerprint(access)}. Local creds + '
                       f'hestia_data/fyers_token.json updated; Delos push follows.')
        finally:
            try:
                d.quit()
            except Exception:  # noqa: BLE001
                pass

    except Exception as e:  # noqa: BLE001
        exit_code = 1
        stage_detail = f'at stage [{stage}]: {str(e).splitlines()[0][:220]}'
        log(f'FAILED {stage_detail}')
        details = (f':rotating_light: *Fyers auto-token* FAILED {stage_detail} '
                   f'-- today\'s Fyers token is NOT refreshed. Fallback: run the '
                   f'`fyers-token` skill (needs a manual Fyers login in Chrome). '
                   f'Log: data_pipeline/fyers_auto_token.log')
    slack_send(details, SLACK_DATA_CHANNEL if exit_code == 0 else SLACK_ERROR_CHANNEL, creds)
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
