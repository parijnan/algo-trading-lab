"""hestia_core.slack_bridge: where the Slack listener writes Prometheus's operator files, standalone and hosted by Hestia."""
import ast
import types
from pathlib import Path

import hestia_config
from hestia_core import slack_bridge as sb
from hestia_core.sizing import SizingStore
from hestia_core.interface import SizingConfig

BASE = '/repo'


def cfg(on, tmp=None):
    return types.SimpleNamespace(SLACK_PROMETHEUS_VIA_HESTIA=on, FLAG_DIR=Path(tmp or '/repo/hestia_data/flags'),
                                 STATE_DIR=Path(tmp or '/repo/hestia_data/state'))


def test_the_switch_is_off_in_the_committed_configuration():
    assert hestia_config.SLACK_PROMETHEUS_VIA_HESTIA is False or hestia_config.LOCAL_OVERRIDES_PRESENT


def test_standalone_paths_and_payload_are_exactly_what_the_listener_always_wrote():
    c = cfg(False)
    assert sb.command_flag_path(c, BASE) == '/repo/prometheus_production/data/prometheus_command.flag'
    assert sb.sizing_override_path(c, BASE) == '/repo/prometheus_production/data/sizing_override.json'
    assert sb.sizing_override_payload(c, True, 5) == {'lot_calc': True, 'lot_count': 5}
    argv, pattern, prefix = sb.start_command(c, 'py')
    assert argv == ['py', 'prometheus_production/prometheus.py'] and 'prometheus_production/prometheus.py' in pattern and prefix == 'prometheus'


def test_hosted_paths_point_at_hestia_and_the_start_button_starts_hestia():
    c = cfg(True)
    assert sb.command_flag_path(c, BASE) == '/repo/hestia_data/flags/prometheus_command.flag'
    assert sb.sizing_override_path(c, BASE) == '/repo/hestia_data/state/prometheus_sizing.json'
    argv, pattern, prefix = sb.start_command(c, 'py')
    assert argv == ['py', 'hestia.py'] and pattern.endswith('hestia.py') and prefix == 'hestia'


def test_a_hosted_override_is_read_back_by_hestias_sizing_store(tmp_path):
    c = cfg(True, tmp_path)
    import json
    Path(sb.sizing_override_path(c, BASE)).write_text(json.dumps(sb.sizing_override_payload(c, False, 3)))
    store = SizingStore(tmp_path, {'prometheus': SizingConfig(False, 5, 50)})
    got = store.get('prometheus')
    assert (got.dynamic, got.static_units, got.unit_cap) == (False, 3, 50)


def test_the_listener_uses_the_bridge_for_every_prometheus_path():
    src = Path(__file__).resolve().parents[1].joinpath('slack_listener.py').read_text()
    ast.parse(src)
    for needle in ('slack_bridge.command_flag_path', 'slack_bridge.engine_sizing_path', 'slack_bridge.sizing_override_payload',
                   'slack_bridge.start_command', 'PROMETHEUS_VIA_HESTIA'):
        assert needle in src, needle


# ---- the real listener module still loads, in both worlds --------------------------------------------------------------------

def _load_listener(monkeypatch, tmp_path, via_hestia):
    """Import slack_listener.py with Slack, the credentials file, its log file and the sizing/flag directories stubbed out."""
    import importlib.util
    import logging
    import sys

    import pandas as pd
    bolt = types.ModuleType('slack_bolt')
    bolt.App = lambda token=None: __import__('unittest.mock', fromlist=['MagicMock']).MagicMock()
    adapter = types.ModuleType('slack_bolt.adapter')
    sock = types.ModuleType('slack_bolt.adapter.socket_mode')
    sock.SocketModeHandler = object
    monkeypatch.setitem(sys.modules, 'slack_bolt', bolt)
    monkeypatch.setitem(sys.modules, 'slack_bolt.adapter', adapter)
    monkeypatch.setitem(sys.modules, 'slack_bolt.adapter.socket_mode', sock)
    fake = pd.DataFrame([{'slack_token': 'xoxb-x', 'slack_app_token': 'xapp-x'}])
    monkeypatch.setattr(pd, 'read_csv', lambda *a, **k: fake)
    monkeypatch.setattr(logging, 'FileHandler', lambda *a, **k: logging.NullHandler())
    monkeypatch.setattr(hestia_config, 'SLACK_PROMETHEUS_VIA_HESTIA', via_hestia)
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('slack_listener_under_test', root / 'slack_listener.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, root


def test_the_listener_loads_with_the_switch_off_and_keeps_the_old_standalone_paths(monkeypatch, tmp_path):
    mod, root = _load_listener(monkeypatch, tmp_path, False)
    assert mod.PROMETHEUS_COMMAND_FLAG == str(root / 'prometheus_production' / 'data' / 'prometheus_command.flag')
    assert mod.SIZING_OVERRIDE_PATHS['Prometheus'] == str(root / 'prometheus_production' / 'data' / 'sizing_override.json')
    assert not hasattr(mod, 'PROMETHEUS_INSTRUMENT_OVERRIDE'), 'the Switch Instrument button and its override file were removed 2026-10-05'
    assert mod.PROMETHEUS_STATE == str(root / 'prometheus_production' / 'data' / 'prometheus_state.csv')
    assert mod.PROMETHEUS_VIA_HESTIA is False
    for other in ('Artemis', 'Athena', 'Iris'):                                    # the other strategies' overrides are untouched
        assert mod.SIZING_OVERRIDE_PATHS[other].endswith(f'{other.lower()}_production/data/sizing_override.json')


def test_the_listener_loads_with_the_switch_on_and_writes_hestias_files(monkeypatch, tmp_path):
    mod, root = _load_listener(monkeypatch, tmp_path, True)
    assert mod.PROMETHEUS_COMMAND_FLAG == str(hestia_config.FLAG_DIR / 'prometheus_command.flag')
    assert mod.SIZING_OVERRIDE_PATHS['Prometheus'] == str(hestia_config.STATE_DIR / 'prometheus_sizing.json')
    assert mod.PROMETHEUS_VIA_HESTIA is True
