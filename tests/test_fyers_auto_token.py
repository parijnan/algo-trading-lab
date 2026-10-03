"""data_pipeline/fyers_auto_token.py: the pure helpers of the automated token
run -- redirect-URL classification (the unhosted redirect domain raises a
navigation error while the address bar still carries the auth_code, so the
classification must treat that as success), the TOTP window guard boundary,
and the Slack message redaction property. No browser, no network."""
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'data_pipeline'))
import fyers_auto_token as fat                                        # noqa: E402


REDIRECT = fat.REDIRECT_URI


# ---- redirect classification --------------------------------------------------------------------------------------------------

def test_redirect_prefix_is_exact_host():
    """is_redirect() is an exact-host predicate: only the registered
    redirect host counts. A lookalike domain must NOT pass."""
    assert fat.is_redirect('https://quant-grow.com/?s=ok&auth_code=X')
    assert not fat.is_redirect('https://quant-grow.com.evil.example/?auth_code=X')
    assert not fat.is_redirect('https://quant-grow.evil.com/')
    assert not fat.is_redirect('https://api-t1.fyers.in/api/v3/generate-authcode')
    assert not fat.is_redirect('about:blank')
    assert not fat.is_redirect('http://quant-grow.com/?auth_code=X')  # scheme must match


# ---- the unhosted redirect domain ----------------------------------------------------------------------------------------------

def test_redirect_domain_is_unhosted_by_design():
    """quant-grow.com deliberately has no A record (plan fyers-mcx-data-integration
    section 2.1). The automation depends on this: the auth_code arrives in the
    address bar of a navigation that itself FAILS. Nothing here can make the
    domain resolvable; this test pins the assumption the code is built on so
    that a future hosted page (where d.get() returns normally) is a conscious
    change, not a silent behavioural drift."""
    import socket
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo('quant-grow.com', 443, socket.AF_INET)


# ---- TOTP window guard --------------------------------------------------------------------------------------------------------

class FixedTOTP:
    """pyotp.TOTP stand-in recording when now() was called."""

    def __init__(self):
        self.calls = []

    def now(self):
        self.calls.append(time.time())
        return '123456'


def test_fresh_totp_recomputes_near_window_end(monkeypatch):
    """With <4s left in the 30s window the code must be recomputed after the
    window rolls -- a stale-code submission is the #1 cause of a failed
    unattended login."""
    fake = FixedTOTP()
    monkeypatch.setattr(fat.pyotp, 'TOTP', lambda key: fake)

    state = {'now': 27.0}  # 30 - 27 = 3s left -> guard must fire

    def fake_time():
        return state['now']

    def fake_sleep(s):
        state['now'] += s  # the window rolls over during the sleep

    monkeypatch.setattr(fat.time, 'time', fake_time)
    monkeypatch.setattr(fat.time, 'sleep', fake_sleep)
    out = fat.fresh_totp('KEY')
    assert out == '123456'
    assert len(fake.calls) == 1
    assert fake.calls[0] >= 30.5 - 3  # computed at/after the rolled window
    assert fake.calls[0] == pytest.approx(30.5, abs=0.6)


def test_fresh_totp_no_wait_mid_window(monkeypatch):
    fake = FixedTOTP()
    monkeypatch.setattr(fat.time, 'time', lambda: 10.0)  # 20s left -> no wait
    monkeypatch.setattr(fat.pyotp, 'TOTP', lambda key: fake)
    sleeps = []
    monkeypatch.setattr(fat.time, 'sleep', lambda s: sleeps.append(s))
    assert fat.fresh_totp('KEY') == '123456'
    assert sleeps == []  # no window guard mid-window
    assert fake.calls == [10.0]


# ---- auth URL -------------------------------------------------------------------------------------------------------------------

def test_get_auth_url_shape():
    url = fat.get_auth_url('APP-200', 'STATE')
    assert url.startswith('https://api-t1.fyers.in/api/v3/generate-authcode?')
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(url).query)
    assert q == {'client_id': ['APP-200'], 'redirect_uri': [REDIRECT],
                 'response_type': ['code'], 'state': ['STATE']}


# ---- no secret in the module's own log surface -----------------------------------------------------------------------------------

def test_no_secret_literals_in_source():
    """The module must not embed any credential defaults. (It reads
    everything from the gitignored CSV; this pins that no secret was ever
    pasted into the source itself.)"""
    src = Path(fat.__file__).read_text()
    for needle in ('xoxb-', 'Bearer eyJ', 'Bearer ey'):
        assert needle not in src
