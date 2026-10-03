"""The host's wiring of the Fyers shadow (plans/hestia-fyers-candle-source.md, Phase 1): off by default, an error for an unknown mode, closed at the
end of the session without waiting, and resolvable per host."""
import types
from datetime import datetime

import pytest

import hestia_config
from hestia_config import EngineEntry
from hestia_core import fyers_shadow as fs
from hestia_host_helpers import HostRun, wait_until
from hestia_fake_helpers import RecEngine

ENGINES = {'a': EngineEntry(instrument='XX', factory='unused:unused', enabled=True)}


def test_the_committed_default_is_angel_only_and_builds_no_fyers_code():
    assert hestia_config.CANDLE_SOURCE['mode'] == 'angel'
    assert fs.build_shadow(types.SimpleNamespace(CANDLE_SOURCE=dict(mode='angel'))) is None
    assert fs.build_shadow(types.SimpleNamespace()) is None, 'a config with no CANDLE_SOURCE at all means angel'


def test_an_unknown_mode_is_an_error_not_a_silent_fallback():
    for mode in ('rescue', 'smart', 'Shadow', ''):
        with pytest.raises(ValueError, match='not one of'):
            fs.build_shadow(types.SimpleNamespace(CANDLE_SOURCE=dict(mode=mode)))


def test_shadow_mode_builds_a_recorder_over_the_configured_paths(tmp_path):
    cfg = types.SimpleNamespace(CANDLE_SOURCE=dict(mode='shadow', instruments=('CRUDEOILM',), timeout_s=2.0, retry_s=0.5, max_wait_s=4.0),
                                SHADOW_DIR=tmp_path / 'shadow', FYERS_TOKEN_FILE=tmp_path / 't.json', FYERS_OFF_FLAG=tmp_path / 'off.flag')
    rec = fs.build_shadow(cfg)
    try:
        assert rec.cfg.instruments == ('CRUDEOILM',) and rec.cfg.timeout_s == 2.0 and rec.client.timeout_s == 2.0
        assert rec.gate.token_file == tmp_path / 't.json' and rec.gate.off_flag == tmp_path / 'off.flag' and rec.dir == tmp_path / 'shadow'
    finally:
        rec.close()


def test_a_host_or_local_file_overrides_candle_source_key_by_key_without_touching_the_base():
    base = dict(hestia_config.CANDLE_SOURCE)
    hosts = {'delos': {'CANDLE_SOURCE': {'mode': 'shadow'}}}
    out = hestia_config.resolve_candle_source(base, hosts, 'delos')
    assert out['mode'] == 'shadow' and out['instruments'] == base['instruments'] and base['mode'] == 'angel'
    assert hestia_config.resolve_candle_source(base, hosts, 'laptop')['mode'] == 'angel'
    local = types.SimpleNamespace(CANDLE_SOURCE={'mode': 'angel', 'timeout_s': 1.0})
    both = hestia_config.resolve_candle_source(base, hosts, 'delos', local)
    assert both['mode'] == 'angel' and both['timeout_s'] == 1.0, 'the machine-local file wins'


class Recorder:
    def __init__(self):
        self.begins, self.closed = [], False

    def begin(self, ref, tick, win_from, win_to):
        self.begins.append(tick)

    def angel_result(self, *a):
        pass

    def close(self):
        self.closed = True


def test_a_session_in_shadow_mode_feeds_the_recorder_announces_it_and_closes_it_at_the_end(tmp_path):
    rec = Recorder()
    run = HostRun(tmp_path, ENGINES, {'a': lambda: RecEngine('a')}, CANDLE_SOURCE=dict(mode='shadow', instruments=('XX',)))
    run.deps.make_shadow = lambda cfg: rec
    run.start()
    wait_until(lambda: rec.begins, what='the first shadow tick')
    (run.cfg.FLAG_DIR / 'hestia_active.flag').unlink()
    run.join()
    assert rec.closed, 'the recorder is closed when the session ends'
    assert any('Fyers candle shadow ON for XX' in t and 'no decision uses it' in t for t in run.texts())


def test_in_angel_mode_no_shadow_announcement_is_made(tmp_path):
    run = HostRun(tmp_path, ENGINES, {'a': lambda: RecEngine('a')})
    run.start()
    wait_until(lambda: run.login_calls() == 1, what='the login')
    (run.cfg.FLAG_DIR / 'hestia_active.flag').unlink()
    run.join()
    assert not any('Fyers' in t for t in run.texts())
