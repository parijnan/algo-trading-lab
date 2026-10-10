"""hestia_core/fyers_shadow.FyersSmart (Phase 3 of plans/hestia-fyers-candle-source.md, Fyers first): what it may return, when it must hand the poll back to
Angel One (return None), that it waits for a minute to settle, never retries a rate limit, never raises, and never leaks the token."""
import csv
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'data_pipeline'))
import fyers_token_refresh as ftr                                            # noqa: E402
from hestia_core import fyers_shadow as fs                                   # noqa: E402

IST = fs.IST
NOW_IST = datetime(2026, 10, 12, 10, 17, 3, tzinfo=IST)
TOKEN = 'SECRET-ACCESS-TOKEN-ABC123'
REF = SimpleNamespace(token='569901', instrument='CRUDEOILM', expiry=date(2026, 10, 19), symbol='CRUDEOILM19OCT26FUT')
OTHER = SimpleNamespace(token='562058', instrument='SILVERMIC', expiry=date(2026, 11, 30), symbol='SILVERMIC30NOV26FUT')
WIN_TO = datetime(2026, 10, 12, 10, 15, 0)                # the poll tick; the just-closed minute is 10:14, which closed at 10:15:00
WIN_FROM = datetime(2026, 10, 12, 10, 10, 0)
M = lambda mm: datetime(2026, 10, 12, 10, mm)             # noqa: E731


def make_token(tmp_path):
    p = tmp_path / 'fyers_token.json'
    issued = NOW_IST.replace(hour=6, minute=35)
    ftr.write_token_file(p, ftr.token_record('APP-100', TOKEN, issued))
    os.chmod(p, 0o600)
    os.utime(p, (issued.timestamp(), issued.timestamp()))
    return p


class Clock:
    def __init__(self, t):
        self.t, self.slept = t, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += timedelta(seconds=s)


def minute_rows(minutes, vol=10.0):
    return pd.DataFrame([{'time_stamp': pd.Timestamp(m), 'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.5, 'volume': vol} for m in minutes])


class Client:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def minutes(self, symbol, start, end, auth):
        self.calls.append((symbol, start, end, auth))
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def ok(minutes, vol=10.0):
    return fs.FetchResult('ok', minute_rows(minutes, vol), 90.0)


def smart(tmp_path, client, since_close_s=0.1, **cfg):
    clock = Clock(WIN_TO + timedelta(seconds=since_close_s))
    gate = fs.TokenGate(make_token(tmp_path), tmp_path / 'off.flag', clock=lambda: NOW_IST)
    return fs.FyersSmart(tmp_path / 'shadow', gate, client, fs.SmartConfig(**cfg), clock=clock.now, sleep=clock.sleep), gate, clock


def rows(tmp_path):
    files = list((tmp_path / 'shadow').glob('smart_*.csv'))
    return [] if not files else list(csv.DictReader(open(files[0])))


FULL = [M(10), M(11), M(12), M(13), M(14)]


def test_a_complete_answer_returns_only_minutes_the_engine_lacks_and_is_recorded_as_served(tmp_path):
    s, _, _ = smart(tmp_path, Client(ok(FULL + [M(15)])))                    # 10:15 is the forming minute
    out = s.fetch_window(REF, WIN_FROM, WIN_TO, {pd.Timestamp(M(10)), pd.Timestamp(M(11)), pd.Timestamp(M(12))})
    assert list(out['time_stamp']) == [pd.Timestamp(M(13)), pd.Timestamp(M(14))] and list(out.columns) == fs.MINUTE_COLS
    assert s.summary() == (1, 0)
    r = rows(tmp_path)
    assert len(r) == 1 and r[0]['kind'] == 'ok' and r[0]['served'] == '1' and r[0]['minutes_used'] == '2' and r[0]['symbol'] == 'MCX:CRUDEOILM26OCTFUT'


def test_nothing_new_is_an_empty_frame_and_still_counts_as_served(tmp_path):
    s, _, _ = smart(tmp_path, Client(ok(FULL)))
    out = s.fetch_window(REF, WIN_FROM, WIN_TO, {pd.Timestamp(m) for m in FULL})
    assert out is not None and out.empty and s.summary() == (1, 0)


def test_a_zero_volume_placeholder_minute_is_dropped_but_still_proves_the_answer_is_current(tmp_path):
    frame = minute_rows(FULL)
    frame.loc[frame['time_stamp'] == pd.Timestamp(M(14)), 'volume'] = 0.0
    s, _, _ = smart(tmp_path, Client(fs.FetchResult('ok', frame, 50.0)))
    out = s.fetch_window(REF, WIN_FROM, WIN_TO, set())
    assert pd.Timestamp(M(14)) not in set(out['time_stamp']) and len(out) == 4 and s.summary() == (1, 0)


def test_it_waits_for_the_minute_to_settle_before_the_first_pull(tmp_path):
    s, _, clock = smart(tmp_path, Client(ok(FULL)), since_close_s=0.1)                    # the default settle is 0.5 s
    s.fetch_window(REF, WIN_FROM, WIN_TO, set())
    assert clock.slept and clock.slept[0] == pytest.approx(0.4), 'the first pull is made 0.5 s after the minute closed, not 0.1 s'
    s2, _, clock2 = smart(tmp_path / 'b', Client(ok(FULL)), since_close_s=0.2, settle_s=1.0)
    s2.fetch_window(REF, WIN_FROM, WIN_TO, set())
    assert clock2.slept[0] == pytest.approx(0.8), 'and the offset is configurable'


def test_a_window_already_old_enough_is_pulled_at_once(tmp_path):
    s, _, clock = smart(tmp_path, Client(ok(FULL)), since_close_s=20.0)          # a recovery window from earlier
    s.fetch_window(REF, WIN_FROM, WIN_TO, set())
    assert clock.slept == []


def test_a_lagging_answer_is_retried_briefly_then_handed_to_angel_one(tmp_path):
    client = Client(ok(FULL[:-1]))                                              # the just-closed 10:14 never appears
    s, _, clock = smart(tmp_path, client, since_close_s=0.1, settle_s=1.0, retry_s=0.5, max_wait_s=3.0)
    assert s.fetch_window(REF, WIN_FROM, WIN_TO, set()) is None
    assert len(client.calls) == 5 and s.summary() == (0, 1), 'pulls at +1.0, 1.5, 2.0, 2.5 and 3.0 s after the minute closed'
    assert (clock.t - WIN_TO).total_seconds() <= 3.0 + 1e-6, 'it gives up at max_wait_s, so Angel One is not delayed beyond its own typical 2 to 3 s'
    assert rows(tmp_path)[0]['kind'] == 'stale' and 'not in the Fyers answer' in rows(tmp_path)[0]['note']


def test_a_lagging_answer_that_catches_up_on_the_retry_is_served(tmp_path):
    client = Client(ok(FULL[:-1]), ok(FULL))
    s, _, _ = smart(tmp_path, client)
    out = s.fetch_window(REF, WIN_FROM, WIN_TO, set())
    assert out is not None and len(out) == 5 and len(client.calls) == 2 and rows(tmp_path)[0]['attempts'] == '2'


@pytest.mark.parametrize('kind', ['timeout', 'http', 'error', 'symbol'])
def test_a_failing_fyers_hands_the_window_to_angel_one_at_once_without_retrying(tmp_path, kind):
    client = Client(fs.FetchResult(kind, latency_ms=3000.0, detail='x'))
    s, _, _ = smart(tmp_path, client)
    assert s.fetch_window(REF, WIN_FROM, WIN_TO, set()) is None
    assert len(client.calls) == 1 and s.summary() == (0, 1) and rows(tmp_path)[0]['kind'] == kind and rows(tmp_path)[0]['served'] == '0'


def test_a_rate_limit_is_never_retried(tmp_path):
    client = Client(fs.FetchResult('rate', latency_ms=10.0))
    s, gate, _ = smart(tmp_path, client)
    assert s.fetch_window(REF, WIN_FROM, WIN_TO, set()) is None and len(client.calls) == 1 and gate.check(NOW_IST).ok, 'a rate limit does not trip the breaker'


def test_an_authentication_refusal_trips_the_shared_breaker_until_the_token_file_changes(tmp_path):
    client = Client(fs.FetchResult('auth', latency_ms=10.0, detail='http 401 code -16'))
    s, gate, _ = smart(tmp_path, client)
    assert s.fetch_window(REF, WIN_FROM, WIN_TO, set()) is None and not gate.check(NOW_IST).ok
    assert s.fetch_window(REF, WIN_FROM, WIN_TO, set()) is None and len(client.calls) == 1, 'the second window makes no call: the gate is shut'
    assert rows(tmp_path)[-1]['kind'] == 'gate'


def test_no_token_means_angel_one_only_with_no_fyers_call(tmp_path):
    client = Client(ok(FULL))
    s, _, _ = smart(tmp_path, client)
    (tmp_path / 'fyers_token.json').unlink()
    assert s.fetch_window(REF, WIN_FROM, WIN_TO, set()) is None and client.calls == [] and rows(tmp_path)[0]['kind'] == 'gate'


def test_an_instrument_outside_the_smart_set_is_not_touched(tmp_path):
    client = Client(ok(FULL))
    s, _, _ = smart(tmp_path, client)
    assert s.handles('CRUDEOILM') and not s.handles('SILVERMIC')
    assert s.fetch_window(OTHER, WIN_FROM, WIN_TO, set()) is None and client.calls == [] and s.summary() == (0, 0) and rows(tmp_path) == []


def test_it_never_raises_even_when_the_client_does(tmp_path):
    class Boom:
        def minutes(self, *a):
            raise RuntimeError('boom')
    s, _, _ = smart(tmp_path, Boom())
    assert s.fetch_window(REF, WIN_FROM, WIN_TO, set()) is None and s.summary() == (0, 1)


def test_the_token_never_appears_in_the_log_or_the_record(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    s, _, _ = smart(tmp_path, Client(fs.FetchResult('timeout', latency_ms=3000.0)))
    s.fetch_window(REF, WIN_FROM, WIN_TO, set())
    assert TOKEN not in caplog.text and TOKEN not in ''.join(p.read_text() for p in (tmp_path / 'shadow').glob('*.csv'))


def test_the_authorization_header_carries_the_token_to_the_client_only(tmp_path):
    client = Client(ok(FULL))
    s, _, _ = smart(tmp_path, client)
    s.fetch_window(REF, WIN_FROM, WIN_TO, set())
    assert client.calls[0][3] == f'APP-100:{TOKEN}' and client.calls[0][0] == 'MCX:CRUDEOILM26OCTFUT'


def cfg_for(tmp_path, mode, **extra):
    return SimpleNamespace(CANDLE_SOURCE=dict(mode=mode, instruments=('CRUDEOILM', 'SILVERMIC'), **extra), SHADOW_DIR=tmp_path / 'shadow',
                           FYERS_TOKEN_FILE=tmp_path / 't.json', FYERS_OFF_FLAG=tmp_path / 'off.flag')


def test_smart_is_built_only_in_smart_mode_and_shares_the_recorders_gate_and_client(tmp_path):
    assert fs.build_smart(cfg_for(tmp_path, 'angel'), None) is None
    for mode in ('shadow', 'rescue'):
        sh = fs.build_shadow(cfg_for(tmp_path, mode))
        try:
            assert fs.build_smart(cfg_for(tmp_path, mode), sh) is None
        finally:
            sh.close()
    cfg = cfg_for(tmp_path, 'smart', smart_instruments=('SILVERMIC',), smart_settle_s=0.8, smart_max_wait_s=2.5)
    sh = fs.build_shadow(cfg)
    try:
        sm, rescue = fs.build_smart(cfg, sh), fs.build_rescue(cfg, sh)
        assert sm.gate is sh.gate and sm.client is sh.client and sm.cfg.instruments == ('SILVERMIC',) and sm.cfg.settle_s == 0.8 and sm.cfg.max_wait_s == 2.5
        assert rescue is not None and rescue.gate is sh.gate, 'smart mode keeps the rescue as the second chance behind the Angel One burst'
    finally:
        sh.close()


def test_the_default_pilot_is_crudeoilm_only(tmp_path):
    cfg = cfg_for(tmp_path, 'smart')
    sh = fs.build_shadow(cfg)
    try:
        assert fs.build_smart(cfg, sh).cfg.instruments == ('CRUDEOILM',) and fs.build_smart(cfg, sh).cfg.settle_s == 0.5
    finally:
        sh.close()
