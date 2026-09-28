"""hestia_core.preflight: the no-login start report, on a scratch configuration."""
import dataclasses
import json
import os
import types
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

import hestia_config as real
from hestia_core import preflight as pf

NOW = datetime(2026, 9, 29, 8, 0)


def minute_file(path: Path, start: datetime, end: datetime):
    ts = pd.date_range(start, end, freq='1min')
    pd.DataFrame({'time_stamp': ts, 'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0, 'volume': 1}).to_csv(path, index=False)


@pytest.fixture
def cfg(tmp_path):
    data = tmp_path / 'pipeline'
    (data / 'mcx' / 'CRUDEOILM').mkdir(parents=True)
    pd.DataFrame([
        {'name': 'CRUDEOILM', 'token': '1', 'symbol': 'CRUDEOILM19OCT26FUT', 'expiry': '19OCT2026', 'lotsize': 10, 'tick_size': 100, 'freeze_qty': 1000},
        {'name': 'CRUDEOILM', 'token': '2', 'symbol': 'CRUDEOILM19NOV26FUT', 'expiry': '19NOV2026', 'lotsize': 10, 'tick_size': 100, 'freeze_qty': 1000},
    ]).to_csv(data / 'master.csv', index=False)
    minute_file(data / 'mcx' / 'CRUDEOILM' / '2026-10-19_futures.csv', datetime(2026, 8, 1, 9), datetime(2026, 9, 28, 23, 29))
    minute_file(data / 'mcx' / 'CRUDEOILM' / '2026-11-19_futures.csv', datetime(2026, 8, 1, 9), datetime(2026, 9, 28, 23, 29))
    pd.DataFrame({'date': ['2026-12-25'], 'morning_session_closed': [True], 'evening_session_closed': [True], 'holiday_name': ['x']}
                 ).to_csv(data / 'holidays.csv', index=False)
    creds = tmp_path / 'creds.csv'
    pd.DataFrame([{'api_key': 'a', 'user_name': 'u', 'password': 'p', 'qr_code': 'q', 'slack_token': 's'}]).to_csv(creds, index=False)
    ns = types.SimpleNamespace(**{k: getattr(real, k) for k in dir(real) if k.isupper()})
    ns.STATE_DIR, ns.FLAG_DIR = tmp_path / 'state', tmp_path / 'flags'
    ns.STATE_DIR.mkdir()
    ns.SESSION_LOCK_FILE = tmp_path / 'lock'
    ns.LEGACY_PID_FILES = {'standalone Prometheus': tmp_path / 'prometheus.pid'}
    ns.MCX_DATA_DIR, ns.INSTRUMENT_MASTER_FILE, ns.MCX_HOLIDAYS_FILE, ns.CREDS_FILE = data / 'mcx', data / 'master.csv', data / 'holidays.csv', creds
    ns.ENGINES = {'prometheus': dataclasses.replace(real.ENGINES['prometheus'], enabled=True, static_units=5)}
    return ns


def by_name(checks):
    return {c.name: c for c in checks}


def test_a_healthy_configuration_has_no_failures(cfg):
    checks = pf.run_checks(cfg, NOW)
    assert not [c for c in checks if c.status == pf.FAIL], pf.format_report(checks)
    assert by_name(checks)['CRUDEOILM19OCT26FUT data'].status == pf.OK


def test_no_enabled_engine_fails(cfg):
    cfg.ENGINES = {'prometheus': dataclasses.replace(real.ENGINES['prometheus'], enabled=False)}
    assert by_name(pf.run_checks(cfg, NOW))['engines'].status == pf.FAIL


def test_units_over_the_cap_fail(cfg):
    cfg.ENGINES = {'prometheus': dataclasses.replace(cfg.ENGINES['prometheus'], static_units=60)}
    assert by_name(pf.run_checks(cfg, NOW))['engine prometheus'].status == pf.FAIL


def test_stale_front_month_data_fails_but_a_stale_next_month_only_warns(cfg):
    d = Path(cfg.MCX_DATA_DIR) / 'CRUDEOILM'
    minute_file(d / '2026-11-19_futures.csv', datetime(2026, 8, 1, 9), datetime(2026, 9, 1, 23, 29))
    st = by_name(pf.run_checks(cfg, NOW))
    assert st['CRUDEOILM19NOV26FUT data'].status == pf.WARN and st['CRUDEOILM19OCT26FUT data'].status == pf.OK
    minute_file(d / '2026-10-19_futures.csv', datetime(2026, 8, 1, 9), datetime(2026, 9, 1, 23, 29))
    assert by_name(pf.run_checks(cfg, NOW))['CRUDEOILM19OCT26FUT data'].status == pf.FAIL


def test_short_history_fails_for_the_front_month(cfg):
    minute_file(Path(cfg.MCX_DATA_DIR) / 'CRUDEOILM' / '2026-10-19_futures.csv', datetime(2026, 9, 25, 9), datetime(2026, 9, 28, 23, 29))
    c = by_name(pf.run_checks(cfg, NOW))['CRUDEOILM19OCT26FUT data']
    assert c.status == pf.FAIL and 'shorter than' in c.detail


def test_a_kill_or_disable_flag_fails_and_exit_only_warns(cfg):
    Path(cfg.FLAG_DIR).mkdir(exist_ok=True)
    flag = Path(cfg.FLAG_DIR) / 'prometheus_command.flag'
    for word, want in (('KILL', pf.FAIL), ('DISABLE', pf.FAIL), ('EXIT', pf.WARN)):
        flag.write_text(word)
        assert by_name(pf.run_checks(cfg, NOW))['prometheus flag'].status == want
    flag.unlink()
    assert 'prometheus flag' not in by_name(pf.run_checks(cfg, NOW))


def test_a_running_standalone_prometheus_fails_and_a_stale_pid_file_warns(cfg):
    pidfile = cfg.LEGACY_PID_FILES['standalone Prometheus']
    pidfile.write_text(str(os.getpid()))
    assert by_name(pf.run_checks(cfg, NOW, alive=lambda p: True))['standalone Prometheus'].status == pf.FAIL
    assert by_name(pf.run_checks(cfg, NOW, alive=lambda p: False))['standalone Prometheus'].status == pf.WARN


def test_saved_engine_state_is_summarised_and_a_broken_file_fails(cfg):
    p = Path(cfg.STATE_DIR) / 'prometheus_state.json'
    p.write_text(json.dumps({'saved': 'x', 'blob': json.dumps({'status': 'in_trade', 'direction': 'bearish', 'trade_counter': 51,
                                                               'last_processed_boundary': None})}))
    c = by_name(pf.run_checks(cfg, NOW))['prometheus state']
    assert c.status == pf.OK and 'in_trade bearish' in c.detail and '51' in c.detail
    p.write_text('{not json')
    assert by_name(pf.run_checks(cfg, NOW))['prometheus state'].status == pf.FAIL


def test_a_missing_credentials_column_fails_without_reading_values(cfg):
    pd.DataFrame([{'api_key': 'a'}]).to_csv(cfg.CREDS_FILE, index=False)
    c = by_name(pf.run_checks(cfg, NOW))['credentials']
    assert c.status == pf.FAIL and 'password' in c.detail


def test_the_report_never_logs_in(cfg):
    import hestia
    src = open(pf.__file__).read()
    assert 'generateSession' not in src and 'SmartApi' not in src and 'requests' not in src
    assert hestia.check.__doc__.startswith('`python hestia.py --check`')


def test_local_overrides_enable_an_engine_only_by_naming_it_and_its_fields():
    import hestia_config as hc
    got = hc.apply_local_overrides(hc.ENGINES, types.SimpleNamespace(ENGINES={'prometheus': dict(enabled=True, static_units=5, unit_cap=10)}))
    assert got['prometheus'].enabled and got['prometheus'].static_units == 5 and got['prometheus'].unit_cap == 10
    assert not got['selene'].enabled and not hc.ENGINES['prometheus'].enabled               # the original registry is not touched
    assert hc.apply_local_overrides(hc.ENGINES, None) == hc.ENGINES
    with pytest.raises(KeyError):
        hc.apply_local_overrides(hc.ENGINES, types.SimpleNamespace(ENGINES={'typhon': dict(enabled=True)}))
    with pytest.raises(TypeError):
        hc.apply_local_overrides(hc.ENGINES, types.SimpleNamespace(ENGINES={'prometheus': dict(enabeld=True)}))


def test_the_committed_configuration_enables_nothing_and_ignores_the_local_file():
    import hestia_config as hc
    assert not any(e.enabled for e in hc.ENGINES.values()) and hc.SLACK_PROMETHEUS_VIA_HESTIA is False
    assert 'hestia_local.py' in (Path(hc.__file__).parent / '.gitignore').read_text()
