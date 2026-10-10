"""hestia_core/fyers_shadow.FyersRescue (Phase 2 of plans/hestia-fyers-candle-source.md): what it may return, when it may be silent, and that it can never raise,
overwrite an Angel One minute, trust an unsettled minute or retry a rate limit."""
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
NOW_IST = datetime(2026, 10, 5, 10, 17, 3, tzinfo=IST)
TOKEN = 'SECRET-ACCESS-TOKEN-ABC123'
REF = SimpleNamespace(token='569901', instrument='CRUDEOILM', expiry=date(2026, 10, 19), symbol='CRUDEOILM19OCT26FUT')
WIN_TO = datetime(2026, 10, 5, 10, 15, 0)                 # the poll tick; the just-closed minute is 10:14, which closed at 10:15:00
WIN_FROM = datetime(2026, 10, 5, 10, 10, 0)


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


M = lambda mm: datetime(2026, 10, 5, 10, mm)             # noqa: E731


class Client:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def minutes(self, symbol, start, end, auth):
        self.calls.append((symbol, start, end, auth))
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def ok(minutes, vol=10.0):
    return fs.FetchResult('ok', minute_rows(minutes, vol), 90.0)


def rescue(tmp_path, client, since_close_s=8.0, **cfg):
    clock = Clock(WIN_TO + timedelta(seconds=since_close_s))
    gate = fs.TokenGate(make_token(tmp_path), tmp_path / 'off.flag', clock=lambda: NOW_IST)
    return fs.FyersRescue(tmp_path / 'shadow', gate, client, fs.RescueConfig(**cfg), clock=clock.now, sleep=clock.sleep), gate, clock


def rows(tmp_path):
    files = list((tmp_path / 'shadow').glob('rescues_*.csv'))
    return [] if not files else list(csv.DictReader(open(files[0])))


def test_it_returns_only_the_minutes_the_engine_does_not_have_and_never_the_forming_one(tmp_path):
    client = Client(ok([M(10), M(11), M(12), M(13), M(14), M(15)]))             # 10:15 is the forming minute
    r, _, _ = rescue(tmp_path, client)
    known = {pd.Timestamp(M(10)), pd.Timestamp(M(11)), pd.Timestamp(M(12))}
    out = r.fetch_window(REF, WIN_FROM, WIN_TO, known)
    assert list(out['time_stamp']) == [pd.Timestamp(M(13)), pd.Timestamp(M(14))]
    assert list(out.columns) == fs.MINUTE_COLS and r.summary() == (1, 0)


def test_it_never_overwrites_a_minute_angel_one_already_gave(tmp_path):
    r, _, _ = rescue(tmp_path, Client(ok([M(12), M(13), M(14)])))
    known = {pd.Timestamp(M(12)), pd.Timestamp(M(13)), pd.Timestamp(M(14))}
    out = r.fetch_window(REF, WIN_FROM, WIN_TO, known)
    assert out is not None and out.empty and r.summary() == (1, 0), 'a fetch that worked with nothing new is an empty frame, not a failure'


def test_zero_volume_placeholder_minutes_are_dropped_but_still_prove_the_answer_is_current(tmp_path):
    frame = minute_rows([M(12), M(13)])
    frame = pd.concat([frame, minute_rows([M(14)], vol=0.0)], ignore_index=True)       # the just-closed minute had no trades: Fyers pads it
    r, _, _ = rescue(tmp_path, Client(fs.FetchResult('ok', frame, 90.0)))
    out = r.fetch_window(REF, WIN_FROM, WIN_TO, set())
    assert list(out['time_stamp']) == [pd.Timestamp(M(12)), pd.Timestamp(M(13))]


def test_an_answer_without_the_just_closed_minute_is_refused(tmp_path):
    r, _, _ = rescue(tmp_path, Client(ok([M(11), M(12), M(13)])))                      # 10:14 missing: Fyers is not current yet
    assert r.fetch_window(REF, WIN_FROM, WIN_TO, set()) is None
    assert r.summary() == (0, 1) and rows(tmp_path)[0]['kind'] == 'stale'


def test_a_minute_that_has_not_settled_is_waited_for_before_fyers_is_asked(tmp_path):
    client = Client(ok([M(13), M(14)]))
    r, _, clock = rescue(tmp_path, client, since_close_s=1.5, settle_s=5.0)
    r.fetch_window(REF, WIN_FROM, WIN_TO, set())
    assert clock.slept == [pytest.approx(3.5)] and len(client.calls) == 1


def test_no_wait_when_the_minute_is_already_old_enough(tmp_path):
    r, _, clock = rescue(tmp_path, Client(ok([M(13), M(14)])), since_close_s=9.0, settle_s=5.0)
    r.fetch_window(REF, WIN_FROM, WIN_TO, set())
    assert clock.slept == []


def test_an_old_recovery_window_is_not_delayed_by_the_settle_wait(tmp_path):
    r, _, clock = rescue(tmp_path, Client(ok([M(13), M(14)])), since_close_s=600.0)
    r.fetch_window(REF, WIN_FROM, WIN_TO, set())
    assert clock.slept == []


def test_an_authentication_refusal_trips_the_shared_breaker_and_returns_none(tmp_path):
    client = Client(fs.FetchResult('auth', detail='http 401 code -16'))
    r, gate, _ = rescue(tmp_path, client)
    assert r.fetch_window(REF, WIN_FROM, WIN_TO, set()) is None
    assert not gate.check().ok and r.summary() == (0, 1)
    assert r.fetch_window(REF, WIN_FROM, WIN_TO, set()) is None and len(client.calls) == 1, 'no second call while the breaker holds'


def test_a_rate_limit_is_answered_once_and_never_retried(tmp_path):
    client = Client(fs.FetchResult('rate'))
    r, gate, _ = rescue(tmp_path, client)
    assert r.fetch_window(REF, WIN_FROM, WIN_TO, set()) is None
    assert len(client.calls) == 1 and gate.check().ok


def test_a_closed_gate_means_no_call_at_all(tmp_path):
    client = Client(ok([M(13), M(14)]))
    (tmp_path / 'off.flag').write_text('')
    r, _, _ = rescue(tmp_path, client)
    assert r.fetch_window(REF, WIN_FROM, WIN_TO, set()) is None and client.calls == [] and r.summary() == (0, 0)


def test_other_instruments_are_never_asked(tmp_path):
    client = Client(ok([M(13), M(14)]))
    r, _, _ = rescue(tmp_path, client, instruments=('SILVERMIC',))
    assert r.fetch_window(REF, WIN_FROM, WIN_TO, set()) is None and client.calls == []


def test_it_never_raises_whatever_the_client_does(tmp_path):
    class Boom:
        def minutes(self, *a):
            raise RuntimeError('boom')
    r, _, _ = rescue(tmp_path, Boom())
    assert r.fetch_window(REF, WIN_FROM, WIN_TO, set()) is None and r.summary() == (0, 1)


def test_every_rescue_is_recorded_and_logged_and_the_token_is_nowhere(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger='hestia_fyers'):
        r, _, _ = rescue(tmp_path, Client(ok([M(12), M(13), M(14)])))
        r.fetch_window(REF, WIN_FROM, WIN_TO, {pd.Timestamp(M(12))})
    row = rows(tmp_path)[0]
    assert (row['kind'], row['minutes_returned'], row['minutes_used'], row['symbol']) == ('ok', '3', '2', 'MCX:CRUDEOILM26OCTFUT')
    assert row['expected_minute'].startswith('2026-10-05T10:14') and row['win_to'].startswith('2026-10-05T10:15')
    text = ' '.join(r_.getMessage() for r_ in caplog.records) + open(next((tmp_path / 'shadow').glob('rescues_*.csv'))).read()
    assert 'filled 2 minute(s) from Fyers' in text and TOKEN not in text and 'APP-100' not in text


def test_the_symbol_asked_for_is_the_verified_fyers_mapping_over_the_polled_window(tmp_path):
    client = Client(ok([M(13), M(14)]))
    r, _, _ = rescue(tmp_path, client)
    r.fetch_window(REF, WIN_FROM, WIN_TO, set())
    assert client.calls[0][:3] == ('MCX:CRUDEOILM26OCTFUT', WIN_FROM, WIN_TO) and client.calls[0][3] == f'APP-100:{TOKEN}'


# ---- the builders --------------------------------------------------------------------------------------------------------------------

def cfg_for(tmp_path, mode, **extra):
    return SimpleNamespace(CANDLE_SOURCE=dict(mode=mode, instruments=('CRUDEOILM',), **extra), SHADOW_DIR=tmp_path / 'shadow',
                           FYERS_TOKEN_FILE=tmp_path / 't.json', FYERS_OFF_FLAG=tmp_path / 'off.flag')


def test_rescue_is_built_only_in_rescue_mode_and_shares_the_recorders_gate_and_client(tmp_path):
    assert fs.build_rescue(cfg_for(tmp_path, 'angel'), None) is None
    sh = fs.build_shadow(cfg_for(tmp_path, 'shadow'))
    try:
        assert fs.build_rescue(cfg_for(tmp_path, 'shadow'), sh) is None
    finally:
        sh.close()
    cfg = cfg_for(tmp_path, 'rescue', rescue_after_attempts=2, settle_s=7.5)
    sh = fs.build_shadow(cfg)
    try:
        r = fs.build_rescue(cfg, sh)
        assert r.gate is sh.gate and r.client is sh.client and r.after_attempts == 2 and r.cfg.settle_s == 7.5 and r.cfg.instruments == ('CRUDEOILM',)
    finally:
        sh.close()


def test_rescue_mode_also_records_and_an_unknown_mode_is_still_an_error(tmp_path):
    sh = fs.build_shadow(cfg_for(tmp_path, 'rescue'))
    try:
        assert sh is not None
    finally:
        sh.close()
    with pytest.raises(ValueError, match='not one of'):
        fs.build_shadow(cfg_for(tmp_path, 'bogus'))
