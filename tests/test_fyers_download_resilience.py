"""data_pipeline/data_downloader_fyers_mcx._get retries transient network failures (a socket read timeout crashed a Sensex expiry download on 2026-10-10)
and data_downloader_fyers_equities treats a failure that survives the retries as a failure, never as 'no data'."""
import http.client
import io
import json
import socket
import sys
import urllib.error
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'data_pipeline'))
import data_downloader_fyers_equities as eq          # noqa: E402
import data_downloader_fyers_mcx as mcx              # noqa: E402


class Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.body).encode()


@pytest.fixture
def net(monkeypatch):
    """_get with no real waiting, no credentials file, and a scripted urlopen: a list of outcomes (an exception to raise, or a body to return)."""
    calls, slept = [], []
    state = {'script': []}

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        out = state['script'].pop(0) if len(state['script']) > 1 else state['script'][0]
        if isinstance(out, BaseException):
            raise out
        return Resp(out)
    monkeypatch.setattr(mcx.urllib.request, 'urlopen', urlopen)
    monkeypatch.setattr(mcx.time, 'sleep', lambda s: slept.append(s))
    monkeypatch.setattr(mcx._rate_limiter, 'wait', lambda: None)
    monkeypatch.setattr(mcx, '_auth_header', lambda: 'APP:TOKEN')

    def run(*script):
        state['script'] = list(script)
        calls.clear(); slept.clear()
        return calls, slept
    return run


def http_error(code, body=b'{}'):
    return urllib.error.HTTPError('http://x', code, 'msg', {}, io.BytesIO(body))


@pytest.mark.parametrize('exc', [TimeoutError('read timed out'), socket.timeout('x'), ConnectionResetError('reset'), http.client.IncompleteRead(b'ab'),
                                 urllib.error.URLError('dns'), http_error(503)])
def test_a_transient_failure_is_retried_and_the_answer_returned(net, exc):
    calls, slept = net(exc, exc, {'s': 'ok', 'candles': [[1, 2, 3, 4, 5, 6]]})
    assert mcx._get('http://x', {'a': 1}) == {'s': 'ok', 'candles': [[1, 2, 3, 4, 5, 6]]}
    assert len(calls) == 3 and slept == [3.0, 6.0], 'two retries with a doubling backoff, no wait before the first attempt'


def test_a_failure_that_survives_every_retry_raises_the_network_error_not_a_bare_timeout(net):
    calls, slept = net(TimeoutError('read timed out'))
    with pytest.raises(mcx.FyersNetworkError, match='TimeoutError after 4 attempts'):
        mcx._get('http://x', {})
    assert len(calls) == 4 and slept == [3.0, 6.0, 12.0]


def test_a_rate_limit_is_never_retried(net):
    calls, slept = net(http_error(429))
    with pytest.raises(mcx.FyersRateLimitError):
        mcx._get('http://x', {})
    assert len(calls) == 1 and slept == []


def test_an_authentication_refusal_in_a_200_body_is_raised_at_once_not_retried(net):
    calls, slept = net({'s': 'error', 'code': -16, 'message': 'Could not authenticate the user'})
    with pytest.raises(mcx.FyersAuthExpiredError):
        mcx._get('http://x', {})
    assert len(calls) == 1 and slept == []


def test_a_client_error_with_a_json_body_is_returned_as_before_and_not_retried(net):
    calls, slept = net(http_error(400, b'{"s": "error", "code": 400, "message": "bad symbol"}'))
    assert mcx._get('http://x', {}) == {'s': 'error', 'code': 400, 'message': 'bad symbol'}
    assert len(calls) == 1 and slept == []


def test_a_non_json_client_error_still_raises_as_before(net):
    net(http_error(404, b'not json'))
    with pytest.raises(json.JSONDecodeError):            # unchanged: the old code re-raised the decode error
        mcx._get('http://x', {})


# ---- the equities options loop ------------------------------------------------------------------------------------------------

def tracking(tmp_path, status=False):
    p = tmp_path / 'options_list.csv'
    pd.DataFrame({'expiry_date': ['2026-10-01T07:00:00.000Z'], 'start_date': ['2026-09-10T09:15:00.000Z'], 'end_date': ['2026-10-01T15:30:00.000Z'],
                  'download_status': [status]}).to_csv(p, index=False)
    return p


def candles(n=3):
    return pd.DataFrame({'time_stamp': pd.date_range('2026-09-30 09:15', periods=n, freq='min'), 'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0, 'volume': 1})


def run_loop(monkeypatch, tmp_path, symbols, fetch):
    monkeypatch.setattr(eq, 'resolve_options_anchor', lambda u, m: 'NSE:ANCHOR')
    monkeypatch.setattr(eq, 'get_expired_option_contracts', lambda a, e: symbols)
    monkeypatch.setattr(eq, 'get_expired_historical_data', fetch)
    monkeypatch.setattr(eq, '_strike_and_type', lambda s: (int(s[-7:-2]), s[-2:].lower()))
    t = tracking(tmp_path)
    eq.download_options_for_symbol('Nifty', 'NIFTY', 'NSE_FO', tmp_path / 'opt', t)
    return t


SYMS = [f'NSE:NIFTY2610{k}CE' for k in (20000, 20050, 20100, 20150)]


def test_a_network_failure_leaves_the_expiry_pending_even_when_the_hit_rate_would_pass(monkeypatch, tmp_path):
    def fetch(symbol, start, end):
        if symbol.endswith('20100CE'):
            raise mcx.FyersNetworkError('TimeoutError after 4 attempts')
        return candles()
    t = run_loop(monkeypatch, tmp_path, SYMS, fetch)
    assert not pd.read_csv(t)['download_status'].iloc[0], '3 of 4 contracts saved is above the 50% floor, but one failed on the network: not complete'
    assert sorted(p.name for p in (tmp_path / 'opt' / '2026-10-01').iterdir()) == ['20000ce.csv', '20050ce.csv', '20150ce.csv']


def test_the_re_run_downloads_only_the_failed_contract_and_then_completes(monkeypatch, tmp_path):
    fails = {'on': True}
    asked = []

    def fetch(symbol, start, end):
        asked.append(symbol)
        if fails['on'] and symbol.endswith('20100CE'):
            raise mcx.FyersNetworkError('x')
        return candles()
    t = run_loop(monkeypatch, tmp_path, SYMS, fetch)
    fails['on'] = False
    asked.clear()
    eq.download_options_for_symbol('Nifty', 'NIFTY', 'NSE_FO', tmp_path / 'opt', t)
    assert asked == ['NSE:NIFTY2610' + '20100CE'], 'files already saved are skipped'
    assert pd.read_csv(t)['download_status'].iloc[0]


def test_repeated_network_failures_stop_the_instrument_and_keep_what_was_completed(monkeypatch, tmp_path):
    def fetch(symbol, start, end):
        raise mcx.FyersNetworkError('x')
    syms = [f'NSE:NIFTY2610{k}CE' for k in range(20000, 20000 + 50 * 8, 50)]
    asked = []

    def counting(symbol, start, end):
        asked.append(symbol)
        return fetch(symbol, start, end)
    run_loop(monkeypatch, tmp_path, syms, counting)
    assert len(asked) == eq.MAX_NETWORK_FAILURES_PER_EXPIRY, 'it stops once the connection looks down instead of failing every remaining contract'


def test_a_clean_run_still_completes_the_expiry(monkeypatch, tmp_path):
    t = run_loop(monkeypatch, tmp_path, SYMS, lambda s, a, b: candles())
    assert pd.read_csv(t)['download_status'].iloc[0]


def test_a_rate_limit_stops_the_instrument_but_keeps_the_expiry_already_completed_in_this_run(monkeypatch, tmp_path):
    t = tmp_path / 'options_list.csv'
    pd.DataFrame({'expiry_date': ['2026-10-01T07:00:00.000Z', '2026-10-08T07:00:00.000Z'], 'start_date': ['2026-09-10T09:15:00.000Z'] * 2,
                  'end_date': ['2026-10-01T15:30:00.000Z', '2026-10-08T15:30:00.000Z'], 'download_status': [False, False]}).to_csv(t, index=False)
    monkeypatch.setattr(eq, 'resolve_options_anchor', lambda u, m: 'NSE:ANCHOR')
    monkeypatch.setattr(eq, 'get_expired_option_contracts', lambda a, e: SYMS)
    monkeypatch.setattr(eq, '_strike_and_type', lambda s: (int(s[-7:-2]), s[-2:].lower()))

    def fetch(symbol, start, end):
        if end == '2026-10-08':
            raise mcx.FyersRateLimitError('429')
        return candles()
    monkeypatch.setattr(eq, 'get_expired_historical_data', fetch)
    eq.download_options_for_symbol('Nifty', 'NIFTY', 'NSE_FO', tmp_path / 'opt', t)
    assert list(pd.read_csv(t)['download_status']) == [True, False], 'the first expiry completed before the rate limit, and that survives it'
