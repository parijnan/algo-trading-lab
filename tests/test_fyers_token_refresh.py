"""data_pipeline/fyers_token_refresh.py: the auth URL, the redirect parse, the exchange, the token file and its freshness check, with no
network. The property that matters most is the last one: a token or an auth code never appears in anything printed."""
import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'data_pipeline'))
import fyers_token_refresh as ft                                       # noqa: E402

IST = ft.IST
NOW = datetime(2026, 10, 5, 9, 41, 7, tzinfo=IST)
ACCESS, REFRESH, CODE = 'ACCESS-TOKEN-SECRET-123', 'REFRESH-TOKEN-SECRET-456', 'AUTHCODE-SECRET-789'
CREDS = {'fyers_app_id': 'ABCDE12345-100', 'fyers_app_secret': 'APPSECRET', 'fyers_pin': '1234'}


def redirect(state='STATE1', **extra):
    q = '&'.join(f'{k}={v}' for k, v in {'s': 'ok', 'code': '200', 'auth_code': CODE, 'state': state, **extra}.items() if v is not None)
    return f'https://quant-grow.com/?{q}'


@pytest.fixture
def creds_file(tmp_path):
    p = tmp_path / 'user_credentials.csv'
    with open(p, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['angel_key', 'fyers_app_id', 'fyers_app_secret', 'fyers_access_token',
                                          'fyers_refresh_token', 'fyers_pin'])
        w.writeheader()
        w.writerow({'angel_key': 'ANGEL', 'fyers_app_id': 'ABCDE12345-100', 'fyers_app_secret': 'APPSECRET',
                    'fyers_access_token': 'old', 'fyers_refresh_token': 'oldr', 'fyers_pin': '1234'})
    return p


# ---- the URL and the redirect -------------------------------------------------------------------------------------------------

def test_the_auth_url_names_our_app_redirect_and_state():
    url = ft.build_auth_url('ABCDE12345-100', 'STATE1')
    assert url.startswith('https://api-t1.fyers.in/api/v3/generate-authcode?')
    assert 'client_id=ABCDE12345-100' in url and 'state=STATE1' in url and 'response_type=code' in url
    assert 'redirect_uri=https%3A%2F%2Fquant-grow.com' in url


def test_the_auth_code_is_read_from_our_own_redirect():
    assert ft.parse_redirect(redirect(), 'STATE1') == CODE


def test_a_different_state_is_refused():
    with pytest.raises(ft.TokenError, match='different state'):
        ft.parse_redirect(redirect(state='OTHER'), 'STATE1')


def test_a_page_that_is_not_our_redirect_is_refused_even_with_an_auth_code_in_it():
    with pytest.raises(ft.TokenError, match='not the registered redirect'):
        ft.parse_redirect(f'https://evil.example/?auth_code={CODE}&state=STATE1', 'STATE1')
    with pytest.raises(ft.TokenError, match='not the registered redirect'):
        ft.parse_redirect('https://api-t1.fyers.in/api/v3/generate-authcode?client_id=X', 'STATE1')


def test_a_redirect_without_an_auth_code_says_why_and_never_echoes_the_url():
    with pytest.raises(ft.TokenError, match='no auth code') as e:
        ft.parse_redirect(redirect(auth_code=None, message='User denied'), 'STATE1')
    assert 'User denied' in str(e.value)


# ---- the exchange ---------------------------------------------------------------------------------------------------------------

def test_the_exchange_sends_the_hash_and_the_code_and_returns_both_tokens():
    seen = {}

    def post(url, payload):
        seen.update(url=url, payload=payload)
        return {'s': 'ok', 'access_token': ACCESS, 'refresh_token': REFRESH}
    assert ft.exchange(CODE, CREDS, post) == (ACCESS, REFRESH)
    assert seen['url'] == ft.TOKEN_URL
    assert seen['payload'] == {'grant_type': 'authorization_code', 'code': CODE,
                               'appIdHash': hashlib.sha256(b'ABCDE12345-100:APPSECRET').hexdigest()}


def test_a_refusal_carries_fyers_s_message_and_neither_the_code_nor_the_secret():
    with pytest.raises(ft.TokenError) as e:
        ft.exchange(CODE, CREDS, lambda u, p: {'s': 'error', 'code': -501, 'message': 'invalid auth code'})
    text = str(e.value)
    assert 'invalid auth code' in text and CODE not in text and 'APPSECRET' not in text


def test_missing_app_credentials_are_reported():
    with pytest.raises(ft.TokenError, match='missing'):
        ft.exchange(CODE, {'fyers_app_id': 'X'}, lambda u, p: {})


# ---- the token file -----------------------------------------------------------------------------------------------------------------

def test_the_token_file_is_mode_600_atomic_and_complete(tmp_path):
    p = tmp_path / 'sub' / 'fyers_token.json'
    ft.write_token_file(p, ft.token_record('ABCDE12345-100', ACCESS, NOW))
    assert p.stat().st_mode & 0o777 == 0o600
    assert not list(p.parent.glob('*.tmp'))
    rec = json.loads(p.read_text())
    assert rec['access_token'] == ACCESS and rec['app_id'] == 'ABCDE12345-100'
    assert rec['issued_at'] == '2026-10-05T09:41:07+05:30' and rec['expires_at'] == '2026-10-06T00:00:00+05:30'


def make_file(tmp_path, issued=NOW, mtime=NOW, mode=0o600):
    p = tmp_path / 'fyers_token.json'
    ft.write_token_file(p, ft.token_record('ABCDE12345-100', ACCESS, issued))
    os.chmod(p, mode)
    os.utime(p, (mtime.timestamp(), mtime.timestamp()))
    return p


def test_a_token_issued_and_written_today_is_fresh(tmp_path):
    problems, rec = ft.check_token_file(make_file(tmp_path), NOW + timedelta(hours=3))
    assert problems == [] and rec['access_token'] == ACCESS


def test_a_stale_issued_at_is_caught_even_when_the_file_was_touched_today(tmp_path):
    p = make_file(tmp_path, issued=NOW - timedelta(days=1))
    assert any('issued_at' in x for x in ft.check_token_file(p, NOW)[0])


def test_a_stale_mtime_is_caught_even_when_issued_at_says_today(tmp_path):
    p = make_file(tmp_path, mtime=NOW - timedelta(days=1))
    assert any('modified' in x for x in ft.check_token_file(p, NOW)[0])


def test_a_group_readable_token_file_is_flagged(tmp_path):
    assert any('mode' in x for x in ft.check_token_file(make_file(tmp_path, mode=0o644), NOW)[0])


def test_freshness_flips_at_midnight_ist_not_utc(tmp_path):
    p = make_file(tmp_path, issued=datetime(2026, 10, 5, 23, 59, tzinfo=IST), mtime=datetime(2026, 10, 5, 23, 59, tzinfo=IST))
    assert ft.check_token_file(p, datetime(2026, 10, 5, 23, 59, 30, tzinfo=IST))[0] == []
    after = ft.check_token_file(p, datetime(2026, 10, 6, 0, 1, tzinfo=IST))[0]
    assert any('issued_at' in x for x in after) and any('modified' in x for x in after)


def test_missing_and_malformed_files_are_reported_not_raised(tmp_path):
    assert 'does not exist' in ft.check_token_file(tmp_path / 'nope.json', NOW)[0][0]
    bad = tmp_path / 'bad.json'
    bad.write_text('{not json')
    os.chmod(bad, 0o600)
    assert any('malformed' in x for x in ft.check_token_file(bad, NOW)[0])


def test_the_live_check_accepts_ok_and_no_data_and_rejects_an_auth_failure():
    rec = {'app_id': 'A-100', 'access_token': ACCESS}
    seen = {}

    def get(url, params, auth):
        seen['auth'] = auth
        return {'s': 'ok', 'candles': [[1, 2, 3, 4, 5, 6]]}
    assert ft.live_check(rec, get, NOW)[0] and seen['auth'] == f'A-100:{ACCESS}'
    assert ft.live_check(rec, lambda u, p, a: {'s': 'no_data'}, NOW)[0]
    good, detail = ft.live_check(rec, lambda u, p, a: {'s': 'error', 'code': -16, 'message': 'Could not authenticate the user'}, NOW)
    assert not good and '-16' in detail and ACCESS not in detail


# ---- credentials ---------------------------------------------------------------------------------------------------------------------

def test_updating_the_credentials_file_changes_only_the_two_token_columns(creds_file):
    ft.update_creds('NEW-A', 'NEW-R', creds_file)
    row = next(csv.DictReader(open(creds_file)))
    assert row['fyers_access_token'] == 'NEW-A' and row['fyers_refresh_token'] == 'NEW-R'
    assert row['angel_key'] == 'ANGEL' and row['fyers_app_secret'] == 'APPSECRET' and row['fyers_pin'] == '1234'
    assert creds_file.stat().st_mode & 0o777 == 0o600


# ---- the command line: nothing sensitive is ever printed --------------------------------------------------------------------------

def run(monkeypatch, capsys, argv, post=None):
    if post is not None:
        monkeypatch.setattr(ft, '_post', post)
    rc = ft.main(argv)
    out = capsys.readouterr()
    return rc, out.out + out.err


def test_the_auth_url_command_prints_a_url_and_a_fresh_state(monkeypatch, capsys, creds_file):
    rc, out = run(monkeypatch, capsys, ['--creds', str(creds_file), 'auth-url'])
    data = json.loads(out)
    assert rc == 0 and data['url'].startswith(ft.AUTH_URL) and data['state'] in data['url']
    assert 'APPSECRET' not in out


def test_a_full_exchange_writes_the_files_deletes_the_redirect_and_prints_no_secret(monkeypatch, capsys, tmp_path, creds_file):
    url_file = tmp_path / 'redirect.txt'
    url_file.write_text(redirect())
    out_path = tmp_path / 'fyers_token.json'
    rc, out = run(monkeypatch, capsys, ['--creds', str(creds_file), 'exchange', '--redirect-url-file', str(url_file),
                                        '--state', 'STATE1', '--out', str(out_path), '--update-creds'],
                  post=lambda u, p: {'s': 'ok', 'access_token': ACCESS, 'refresh_token': REFRESH})
    assert rc == 0 and not url_file.exists(), 'the redirect URL holds a one-time code and must not be left behind'
    for secret in (ACCESS, REFRESH, CODE, 'APPSECRET'):
        assert secret not in out, f'{secret} leaked into the output'
    assert ft.fingerprint(ACCESS) in out
    assert json.loads(out_path.read_text())['access_token'] == ACCESS
    assert next(csv.DictReader(open(creds_file)))['fyers_access_token'] == ACCESS


def test_a_failed_exchange_writes_nothing_prints_no_secret_and_still_deletes_the_redirect(monkeypatch, capsys, tmp_path, creds_file):
    url_file = tmp_path / 'redirect.txt'
    url_file.write_text(redirect())
    out_path = tmp_path / 'fyers_token.json'
    rc, out = run(monkeypatch, capsys, ['--creds', str(creds_file), 'exchange', '--redirect-url-file', str(url_file),
                                        '--state', 'STATE1', '--out', str(out_path), '--update-creds'],
                  post=lambda u, p: {'s': 'error', 'code': -501, 'message': 'invalid auth code'})
    assert rc == 1 and 'FAILED' in out and CODE not in out
    assert not out_path.exists() and not url_file.exists()
    assert next(csv.DictReader(open(creds_file)))['fyers_access_token'] == 'old'


def test_a_wrong_state_never_reaches_the_exchange(monkeypatch, capsys, tmp_path, creds_file):
    url_file = tmp_path / 'redirect.txt'
    url_file.write_text(redirect(state='ATTACKER'))
    called = []
    rc, out = run(monkeypatch, capsys, ['--creds', str(creds_file), 'exchange', '--redirect-url-file', str(url_file),
                                        '--state', 'STATE1', '--out', str(tmp_path / 't.json')],
                  post=lambda u, p: called.append(1) or {'s': 'ok', 'access_token': ACCESS})
    assert rc == 1 and not called and 'different state' in out


def test_verify_prints_the_fingerprint_never_the_token(monkeypatch, capsys, tmp_path):
    p = tmp_path / 'fyers_token.json'
    ft.write_token_file(p, ft.token_record('A-100', ACCESS))
    monkeypatch.setattr(ft, '_get', lambda url, params, auth: {'s': 'ok', 'candles': [[1]]})
    rc, out = run(monkeypatch, capsys, ['verify', '--token-file', str(p)])
    assert rc == 0 and 'token is fresh and works' in out and ft.fingerprint(ACCESS) in out and ACCESS not in out


def test_verify_fails_on_a_stale_token_and_on_a_rejected_one(monkeypatch, capsys, tmp_path):
    # cmd_verify() checks freshness against real now_ist() (no now injection on the CLI path),
    # so the stale fixture must be stale in REAL time -- deriving it from the hardcoded NOW
    # made this test fail on 2026-10-03, the one real-world date its "stale" (NOW - 2d) landed on.
    real_days_ago = datetime.now(IST) - timedelta(days=2)
    stale = make_file(tmp_path, issued=real_days_ago, mtime=real_days_ago)
    rc, out = run(monkeypatch, capsys, ['verify', '--token-file', str(stale), '--no-live'])
    assert rc == 1 and 'NOT OK' in out
    fresh = tmp_path / 'fresh.json'
    ft.write_token_file(fresh, ft.token_record('A-100', ACCESS))
    monkeypatch.setattr(ft, '_get', lambda url, params, auth: {'s': 'error', 'code': -16, 'message': 'authenticate'})
    rc, out = run(monkeypatch, capsys, ['verify', '--token-file', str(fresh)])
    assert rc == 1 and 'rejected' in out
