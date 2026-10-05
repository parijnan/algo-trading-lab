"""hestia_core/fyers_shadow.py: the Phase 1 shadow recorder. The properties that matter: it can never change what an engine sees, it never leaks the
token, it measures when the just-closed minute becomes available for BOTH sources, and a slow or failing Fyers costs nothing."""
import csv
import json
import logging
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'data_pipeline'))
import fyers_token_refresh as ftr                                            # noqa: E402
from hestia_core import fyers_shadow as fs                                   # noqa: E402

IST = fs.IST
NOW = datetime(2026, 10, 5, 10, 17, 3, tzinfo=IST)
TOKEN = 'SECRET-ACCESS-TOKEN-ABC123'
REF = SimpleNamespace(token='569901', instrument='CRUDEOILM', expiry=date(2026, 10, 19), symbol='CRUDEOILM19OCT26FUT')
TICK = datetime(2026, 10, 5, 10, 15, 0)                       # a 15-minute boundary tick (tz-naive IST, as Hestia's kernel time)


def make_token(tmp_path, issued=NOW.replace(hour=6, minute=35), mtime=None, mode=0o600, name='fyers_token.json'):
    p = tmp_path / name
    ftr.write_token_file(p, ftr.token_record('APP-100', TOKEN, issued))
    os.chmod(p, mode)
    m = (mtime or issued).timestamp()
    os.utime(p, (m, m))
    return p


class FakeTime:
    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


class InlinePool:
    def __init__(self):
        self.closed = False

    def submit(self, fn, *args):
        fn(*args)

    def close(self):
        self.closed = True


def frame(minutes, base=100.0):
    return pd.DataFrame([{'time_stamp': pd.Timestamp(m), 'open': base, 'high': base + 1, 'low': base - 1, 'close': base + 0.5, 'volume': 10}
                         for m in minutes])


class ScriptedClient:
    """Returns the scripted results in order; the last one repeats. Records every call."""

    def __init__(self, results):
        self.results, self.calls = list(results), []

    def minutes(self, symbol, start, end, auth):
        self.calls.append((symbol, start, end, auth))
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return r


def ok_result(minutes):
    return fs.FetchResult('ok', frame(minutes), 120.0)


def recorder(tmp_path, client, token_file=None, flag=None, clock=None, **cfg):
    t = clock or FakeTime(TICK + timedelta(seconds=0.2))
    gate = fs.TokenGate(token_file or make_token(tmp_path), flag, clock=lambda: NOW)
    rec = fs.ShadowRecorder(tmp_path / 'shadow', gate, client, fs.ShadowConfig(**cfg), pool=InlinePool(), clock=t.now, sleep=t.sleep)
    return rec, gate, t


def polls(tmp_path):
    files = list((tmp_path / 'shadow').glob('polls_*.csv'))
    return [] if not files else list(csv.DictReader(open(files[0])))


M = lambda hh, mm: datetime(2026, 10, 5, hh, mm)             # noqa: E731
EXPECTED = M(10, 14)                                          # the latest closed minute at a 10:15 tick


# ---- the gate ------------------------------------------------------------------------------------------------------------------

def test_a_fresh_token_opens_the_gate_and_never_shows_in_a_repr(tmp_path):
    st = fs.TokenGate(make_token(tmp_path), clock=lambda: NOW).check()
    assert st.ok and st.auth == f'APP-100:{TOKEN}' and TOKEN not in repr(st) and st.fingerprint == ftr.fingerprint(TOKEN)


@pytest.mark.parametrize('case', ['missing', 'mode', 'stale_mtime', 'stale_issued', 'expired', 'malformed', 'flag'])
def test_every_way_the_token_can_be_unusable_closes_the_gate(tmp_path, case):
    flag = tmp_path / 'fyers_off.flag'
    p = make_token(tmp_path)
    if case == 'missing':
        p.unlink()
    elif case == 'mode':
        os.chmod(p, 0o644)
    elif case == 'stale_mtime':
        old = (NOW - timedelta(days=1)).timestamp()
        os.utime(p, (old, old))
    elif case == 'stale_issued':
        p = make_token(tmp_path, issued=NOW - timedelta(days=1), mtime=NOW)
    elif case == 'expired':
        p = make_token(tmp_path, issued=NOW.replace(hour=6, minute=0))      # issued 06:00, expired 06:30; now is 10:17
    elif case == 'malformed':
        p.write_text('{nope')
        os.chmod(p, 0o600)
    elif case == 'flag':
        flag.write_text('')
    gate = fs.TokenGate(p, flag, clock=lambda: NOW)
    st = gate.check()
    assert not st.ok and st.auth is None and st.reason


def test_the_breaker_holds_until_the_token_file_changes(tmp_path):
    p = make_token(tmp_path)
    gate = fs.TokenGate(p, clock=lambda: NOW)
    assert gate.check().ok
    gate.trip('refused')
    assert not gate.check().ok and 'breaker' in gate.check().reason
    time.sleep(0.01)
    make_token(tmp_path)                                         # the 06:35 job (or a manual refresh) rewrites the file
    assert gate.check().ok


def test_the_gate_agrees_with_the_data_pipeline_freshness_check_on_every_case(tmp_path):
    """Two implementations of one rule must not drift: fyers_token_refresh.check_token_file is the one the skill and the 06:35 job use."""
    cases = {'fresh': dict(), 'mode': dict(mode=0o644), 'stale_issued': dict(issued=NOW - timedelta(days=1), mtime=NOW),
             'stale_mtime': dict(mtime=NOW - timedelta(days=1)), 'expired': dict(issued=NOW.replace(hour=6, minute=0))}
    for name, kw in cases.items():
        p = make_token(tmp_path, name=f'{name}.json', **kw)
        pipeline_ok = not ftr.check_token_file(p, NOW)[0]
        assert fs.TokenGate(p, clock=lambda: NOW).check().ok == pipeline_ok, name


def test_the_gate_logs_one_line_per_state_change_and_never_the_token(tmp_path, caplog):
    p = make_token(tmp_path)
    gate = fs.TokenGate(p, clock=lambda: NOW)
    with caplog.at_level(logging.INFO, logger='hestia_fyers'):
        for _ in range(5):
            gate.check()
        gate.trip('refused')
        for _ in range(5):
            gate.check()
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2 and not any(TOKEN in l for l in lines)


# ---- the client ----------------------------------------------------------------------------------------------------------------

def client_with(status, body=None, raises=None):
    def get(url, params, auth, timeout):
        if raises:
            raise raises
        return status, body
    return fs.FyersClient(get)


@pytest.mark.parametrize('status,body,kind', [
    (429, {}, 'rate'), (200, {'code': 429}, 'rate'), (401, {}, 'auth'), (403, None, 'auth'), (200, {'code': -16}, 'auth'),
    (503, None, 'http'), (200, {'s': 'no_data'}, 'empty'), (200, {'s': 'error', 'code': -300, 'message': 'Please provide a valid symbol'}, 'symbol'),
    (200, {'s': 'error', 'code': -1, 'message': 'something'}, 'error')])
def test_responses_are_classified(status, body, kind):
    assert client_with(status, body).minutes('MCX:X', M(10, 0), M(10, 5), 'A:T').kind == kind


def test_exceptions_are_classified_and_carry_no_secret():
    import socket
    assert client_with(0, raises=socket.timeout()).minutes('S', M(10, 0), M(10, 5), f'A:{TOKEN}').kind == 'timeout'
    r = client_with(0, raises=ValueError(f'boom {TOKEN}')).minutes('S', M(10, 0), M(10, 5), f'A:{TOKEN}')
    assert r.kind == 'error' and TOKEN not in r.detail


def test_a_good_response_becomes_a_tz_naive_ist_frame():
    epoch = int(datetime(2026, 10, 5, 10, 14, tzinfo=IST).timestamp())
    r = client_with(200, {'s': 'ok', 'candles': [[epoch, 1, 2, 0.5, 1.5, 7]]}).minutes('S', M(10, 10), M(10, 15), 'A:T')
    assert r.kind == 'ok' and r.frame['time_stamp'].iloc[0] == pd.Timestamp('2026-10-05 10:14:00') and r.frame['volume'].iloc[0] == 7


def test_the_request_asks_for_one_minute_candles_with_epoch_range_and_the_auth_header():
    seen = {}

    def get(url, params, auth, timeout):
        seen.update(url=url, params=params, auth=auth, timeout=timeout)
        return 200, {'s': 'no_data'}
    fs.FyersClient(get, timeout_s=2.5).minutes('MCX:CRUDEOILM26OCTFUT', M(10, 10), M(10, 15), 'APP:TOK')
    assert seen['url'] == fs.HISTORY_URL and seen['auth'] == 'APP:TOK' and seen['timeout'] == 2.5
    assert seen['params']['resolution'] == '1' and seen['params']['symbol'] == 'MCX:CRUDEOILM26OCTFUT'
    assert seen['params']['range_to'] - seen['params']['range_from'] == 300


def test_symbols_follow_the_verified_mapping():
    assert fs.fyers_symbol('SILVERMIC', date(2026, 11, 30)) == 'MCX:SILVERMIC26NOVFUT'
    assert fs.fyers_symbol('NATGASMINI', date(2026, 10, 27)) == 'MCX:NATGASMINI26OCTFUT'


# ---- the pool ------------------------------------------------------------------------------------------------------------------

def test_the_daemon_pool_runs_jobs_survives_a_failing_one_and_closes_without_waiting():
    pool, done = fs.DaemonPool(2, 'test'), threading.Event()
    pool.submit(lambda: 1 / 0)
    pool.submit(done.set)
    assert done.wait(2)
    assert all(t.daemon for t in pool._threads)
    blocker = threading.Event()
    pool.submit(blocker.wait, 30)                                 # a hung call
    t0 = time.monotonic()
    pool.close()
    assert time.monotonic() - t0 < 0.5, 'close() must not wait for a hung job'
    blocker.set()


# ---- the recorder: Fyers side --------------------------------------------------------------------------------------------------------

def test_the_latency_is_measured_from_the_tick_to_the_first_response_holding_the_just_closed_minute(tmp_path):
    client = ScriptedClient([ok_result([M(10, 12), M(10, 13)]), ok_result([M(10, 12), M(10, 13)]),
                             ok_result([M(10, 12), M(10, 13), EXPECTED])])
    rec, _, t = recorder(tmp_path, client)
    rec.begin(REF, TICK, M(10, 10), M(10, 15))
    row = polls(tmp_path)[0]
    assert row['side'] == 'fyers' and row['ok'] == '1' and row['attempts'] == '3' and row['boundary'] == '1'
    assert float(row['after_s']) == pytest.approx(2.2, abs=0.01)                   # polled 0.2s after the tick, then 1s retries
    assert len(client.calls) == 3 and client.calls[0][0] == 'MCX:CRUDEOILM26OCTFUT' and client.calls[0][3] == f'APP-100:{TOKEN}'


def test_a_minute_that_never_appears_is_recorded_as_not_ok_after_the_wait_limit(tmp_path):
    client = ScriptedClient([ok_result([M(10, 12), M(10, 13)])])
    rec, _, _ = recorder(tmp_path, client, max_wait_s=3.0, retry_s=1.0)
    rec.begin(REF, TICK, M(10, 10), M(10, 15))
    row = polls(tmp_path)[0]
    assert row['ok'] == '0' and row['expected_present'] == '0' and row['after_s'] == '' and int(row['attempts']) <= 4


def test_the_forming_minute_is_never_recorded(tmp_path):
    forming = M(10, 15)                                          # the clock is 10:15:00.2, so the 10:15 minute is still forming
    client = ScriptedClient([ok_result([EXPECTED, forming])])
    rec, _, _ = recorder(tmp_path, client)
    rec.begin(REF, TICK, M(10, 10), M(10, 15))
    minutes = list(csv.DictReader(open(next((tmp_path / 'shadow').glob('*_fyers_1m_*.csv')))))
    assert [r['time_stamp'] for r in minutes] == [EXPECTED.isoformat()] and minutes[0]['seen_at']


def test_a_minute_is_written_once_however_many_polls_return_it(tmp_path):
    rec, _, _ = recorder(tmp_path, ScriptedClient([ok_result([EXPECTED])]))
    rec.begin(REF, TICK, M(10, 10), M(10, 15))
    rec.begin(REF, TICK + timedelta(minutes=1), M(10, 11), M(10, 16))
    minutes = list(csv.DictReader(open(next((tmp_path / 'shadow').glob('*_fyers_1m_*.csv')))))
    assert len(minutes) == 1


def test_a_minute_is_written_again_when_a_later_poll_shows_different_values_and_the_file_keeps_every_version(tmp_path):
    """Fyers's first-seen value of a minute is provisional (2026-10-05): the file must keep the first-seen AND the settled value."""
    first = frame([EXPECTED], base=100.0)
    settled = frame([EXPECTED], base=100.0)
    settled.loc[:, 'close'] = 107.0
    settled.loc[:, 'volume'] = 99
    rec, _, _ = recorder(tmp_path, ScriptedClient([fs.FetchResult('ok', first, 90.0), fs.FetchResult('ok', settled, 90.0), fs.FetchResult('ok', settled, 90.0)]))
    rec.begin(REF, TICK, M(10, 10), M(10, 15))
    rec.begin(REF, TICK + timedelta(minutes=1), M(10, 11), M(10, 16))
    rec.begin(REF, TICK + timedelta(minutes=2), M(10, 12), M(10, 17))
    minutes = list(csv.DictReader(open(next((tmp_path / 'shadow').glob('*_fyers_1m_*.csv')))))
    assert [float(r['close']) for r in minutes] == [100.5, 107.0], 'first-seen, then the changed value once; the unchanged repeat is not written'
    assert [int(float(r['volume'])) for r in minutes] == [10, 99] and all(r['seen_at'] for r in minutes)


def test_an_authentication_refusal_stops_at_once_and_trips_the_breaker_until_the_file_changes(tmp_path):
    client = ScriptedClient([fs.FetchResult('auth', detail='http 401 code -16')])
    rec, gate, _ = recorder(tmp_path, client)
    rec.begin(REF, TICK, M(10, 10), M(10, 15))
    assert len(client.calls) == 1 and polls(tmp_path)[0]['kind'] == 'auth' and not gate.check().ok
    rec.begin(REF, TICK + timedelta(minutes=1), M(10, 11), M(10, 16))
    assert len(client.calls) == 1, 'no further Fyers call while the breaker is tripped'
    time.sleep(0.01)
    make_token(tmp_path)
    rec.begin(REF, TICK + timedelta(minutes=2), M(10, 12), M(10, 17))
    assert len(client.calls) == 2


def test_a_rate_limit_is_never_retried(tmp_path):
    client = ScriptedClient([fs.FetchResult('rate')])
    rec, gate, _ = recorder(tmp_path, client)
    rec.begin(REF, TICK, M(10, 10), M(10, 15))
    assert len(client.calls) == 1 and polls(tmp_path)[0]['kind'] == 'rate' and gate.check().ok


def test_with_the_gate_closed_nothing_is_called_and_nothing_is_written(tmp_path):
    client = ScriptedClient([ok_result([EXPECTED])])
    flag = tmp_path / 'fyers_off.flag'
    flag.write_text('')
    rec, _, _ = recorder(tmp_path, client, flag=flag)
    rec.begin(REF, TICK, M(10, 10), M(10, 15))
    assert client.calls == [] and list((tmp_path / 'shadow').iterdir()) == []


def test_other_instruments_are_ignored(tmp_path):
    client = ScriptedClient([ok_result([EXPECTED])])
    rec, _, _ = recorder(tmp_path, client, instruments=('SILVERMIC',))
    rec.begin(REF, TICK, M(10, 10), M(10, 15))
    assert client.calls == []


def test_a_slow_fyers_skips_that_tokens_next_minute_instead_of_queueing(tmp_path):
    release, entered = threading.Event(), threading.Event()

    class SlowClient:
        calls = 0

        def minutes(self, *a):
            self.calls += 1
            entered.set()
            release.wait(5)
            return ok_result([EXPECTED])
    client = SlowClient()
    gate = fs.TokenGate(make_token(tmp_path), clock=lambda: NOW)
    rec = fs.ShadowRecorder(tmp_path / 'shadow', gate, client, fs.ShadowConfig(), pool=fs.DaemonPool(2, 'slow'))
    rec.begin(REF, TICK, M(10, 10), M(10, 15))
    assert entered.wait(2)
    rec.begin(REF, TICK + timedelta(minutes=1), M(10, 11), M(10, 16))
    assert rec.skipped_inflight == 1 and client.calls == 1
    release.set()
    rec.close()


def test_a_failing_client_or_pool_never_raises_into_the_caller(tmp_path):
    class Boom:
        def minutes(self, *a):
            raise RuntimeError('boom')
    rec, _, _ = recorder(tmp_path, Boom())
    rec.begin(REF, TICK, M(10, 10), M(10, 15))                    # swallowed inside the job
    rec2, _, _ = recorder(tmp_path, ScriptedClient([ok_result([EXPECTED])]))
    rec2.pool = SimpleNamespace(submit=lambda *a: (_ for _ in ()).throw(RuntimeError('pool gone')), close=lambda: None)
    rec2.begin(REF, TICK, M(10, 10), M(10, 15))
    rec2.angel_result(REF, TICK, [None], [{}], datetime.now())
    assert rec2._inflight == set(), 'a job that never started must not leave the token marked in flight'


# ---- the recorder: Angel One side ---------------------------------------------------------------------------------------------------

def test_the_angel_side_records_success_latency_attempts_and_minutes(tmp_path):
    rec, _, _ = recorder(tmp_path, ScriptedClient([ok_result([EXPECTED])]))
    returned = TICK + timedelta(seconds=1.4)
    rec.angel_result(REF, TICK, [frame([M(10, 13), EXPECTED])], [{'attempts': 2, 'exhausted': False}], returned)
    row = polls(tmp_path)[0]
    assert (row['side'], row['ok'], row['attempts'], row['expected_present'], row['exhausted'], row['boundary']) == ('angel', '1', '2', '1', '0', '1')
    assert float(row['after_s']) == pytest.approx(1.4)
    assert len(list(csv.DictReader(open(next((tmp_path / 'shadow').glob('*_angel_1m_*.csv')))))) == 2


def test_a_rescued_angel_window_is_recorded_as_rescued_not_as_an_angel_frame(tmp_path):
    rec, _, _ = recorder(tmp_path, ScriptedClient([ok_result([EXPECTED])]))
    rec.angel_result(REF, TICK, [None], [{'attempts': 5, 'exhausted': True, 'rescued': True}], TICK + timedelta(seconds=8))
    row = [r for r in polls(tmp_path) if r['side'] == 'angel'][0]
    assert (row['ok'], row['kind'], row['exhausted'], row['attempts'], row['after_s']) == ('0', 'rescued', '1', '5', '')
    assert not list((tmp_path / 'shadow').glob('*_angel_1m_*.csv')), 'no minute is attributed to Angel One that Fyers supplied'


def test_an_exhausted_angel_burst_is_recorded_as_exhausted_with_no_latency(tmp_path):
    rec, _, _ = recorder(tmp_path, ScriptedClient([ok_result([EXPECTED])]))
    rec.angel_result(REF, TICK, [None], [{'attempts': 5, 'exhausted': True}], TICK + timedelta(seconds=9))
    row = polls(tmp_path)[0]
    assert (row['ok'], row['kind'], row['exhausted'], row['attempts'], row['after_s']) == ('0', 'exhausted', '1', '5', '')


def test_the_angel_frames_are_copied_so_later_changes_cannot_alter_the_record(tmp_path):
    rec, _, _ = recorder(tmp_path, ScriptedClient([ok_result([EXPECTED])]))
    pool_calls = []
    rec.pool = SimpleNamespace(submit=lambda fn, *a: pool_calls.append((fn, a)), close=lambda: None)
    f = frame([EXPECTED])
    rec.angel_result(REF, TICK, [f], [{}], TICK)
    f.loc[:, 'close'] = -1.0                                      # the live data mutates its frame after handing it over
    fn, args = pool_calls[0]
    fn(*args)
    written = list(csv.DictReader(open(next((tmp_path / 'shadow').glob('*_angel_1m_*.csv')))))
    assert float(written[0]['close']) == 100.5


# ---- no secret anywhere ---------------------------------------------------------------------------------------------------------------

def test_the_token_never_reaches_a_file_or_a_log_line(tmp_path, caplog):
    client = ScriptedClient([ok_result([EXPECTED])])
    with caplog.at_level(logging.DEBUG):
        rec, _, _ = recorder(tmp_path, client)
        rec.begin(REF, TICK, M(10, 10), M(10, 15))
        rec.angel_result(REF, TICK, [frame([EXPECTED])], [{'attempts': 1}], TICK)
    blob = ' '.join(open(p).read() for p in (tmp_path / 'shadow').iterdir()) + ' '.join(r.getMessage() for r in caplog.records)
    assert TOKEN not in blob and 'APP-100' not in blob
