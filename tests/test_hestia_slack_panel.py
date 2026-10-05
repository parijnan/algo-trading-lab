"""The per-engine and whole-host Slack control panel (2026-10-05): every button acts on its OWN flag file only; Hestia refuses to start
while hestia_disabled.flag exists; the engines' sizing buttons cover all four engines. No Slack, no broker."""
import importlib.util
import json
import logging
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

import hestia_config
from hestia_core import slack_bridge as sb
from hestia_core.flags import FlagFiles
from hestia_host_helpers import HostRun
from hestia_config import EngineEntry

ROOT = Path(__file__).resolve().parents[1]
ENGINES = ('prometheus', 'selene', 'helios', 'typhon')


def cfg(tmp, hosted=True):
    flags, state = Path(tmp) / 'flags', Path(tmp) / 'state'
    flags.mkdir(exist_ok=True), state.mkdir(exist_ok=True)
    entries = {n: EngineEntry(instrument='XX', factory='x:y', lots_per_unit=lots) for n, lots in
               zip(ENGINES, (2, 1, 20, 2))}
    return types.SimpleNamespace(SLACK_PROMETHEUS_VIA_HESTIA=hosted, FLAG_DIR=flags, STATE_DIR=state, ENGINES=entries)


# ---- engine buttons: Exit, Kill/Disable, Clear, each on its own flag ----------------------------------------------------------------

def test_kill_writes_kill_and_clear_removes_only_that_engines_flag(tmp_path):
    c = cfg(tmp_path)
    (c.FLAG_DIR / 'typhon_command.flag').write_text('KILL')
    (c.FLAG_DIR / 'hestia_active.flag').touch()
    (c.FLAG_DIR / 'hestia_disabled.flag').touch()
    ok, _ = sb.apply_engine_action(c, '/r', 'selene', 'kill', True)
    assert ok and (c.FLAG_DIR / 'selene_command.flag').read_text() == 'KILL'
    ok, msg = sb.apply_engine_action(c, '/r', 'selene', 'clear', True)
    assert ok and not (c.FLAG_DIR / 'selene_command.flag').exists() and 'Selene' in msg
    assert (c.FLAG_DIR / 'typhon_command.flag').read_text() == 'KILL', "another engine's flag is never touched"
    assert (c.FLAG_DIR / 'hestia_active.flag').exists() and (c.FLAG_DIR / 'hestia_disabled.flag').exists(), 'nor the host flags'


def test_exit_writes_exit_only_while_hestia_runs_and_the_engine_is_not_killed(tmp_path):
    c = cfg(tmp_path)
    ok, msg = sb.apply_engine_action(c, '/r', 'prometheus', 'exit', False)
    assert not ok and 'not running' in msg and not (c.FLAG_DIR / 'prometheus_command.flag').exists()
    ok, _ = sb.apply_engine_action(c, '/r', 'prometheus', 'exit', True)
    assert ok and (c.FLAG_DIR / 'prometheus_command.flag').read_text() == 'EXIT'
    (c.FLAG_DIR / 'helios_command.flag').write_text('KILL')
    ok, msg = sb.apply_engine_action(c, '/r', 'helios', 'exit', True)
    assert not ok and 'killed' in msg and (c.FLAG_DIR / 'helios_command.flag').read_text() == 'KILL', 'an EXIT never replaces a KILL gate'


def test_kill_over_a_pending_exit_wins_and_says_so(tmp_path):
    c = cfg(tmp_path)
    (c.FLAG_DIR / 'typhon_command.flag').write_text('EXIT')
    ok, msg = sb.apply_engine_action(c, '/r', 'typhon', 'kill', True)
    assert ok and (c.FLAG_DIR / 'typhon_command.flag').read_text() == 'KILL' and 'EXIT still in progress' in msg


def test_clear_reports_what_it_removed_and_a_missing_flag_is_not_an_error(tmp_path):
    c = cfg(tmp_path)
    ok, msg = sb.apply_engine_action(c, '/r', 'selene', 'clear', False)
    assert ok and 'No Selene flag' in msg
    (c.FLAG_DIR / 'selene_command.flag').write_text('KILL')
    ok, msg = sb.apply_engine_action(c, '/r', 'selene', 'clear', False)
    assert ok and 'was KILL' in msg


def test_a_killed_flag_really_gates_the_next_start_the_same_way_disable_does(tmp_path):
    """One flag word serves both uses: the host's own startup gate treats KILL like DISABLE."""
    f = FlagFiles(tmp_path)
    f.command_path('selene').write_text('KILL')
    assert f.read_command('selene') in ('DISABLE', 'KILL')
    assert "('DISABLE', 'KILL')" in (ROOT / 'hestia_core' / 'host.py').read_text()


def test_the_standalone_world_only_knows_prometheus(tmp_path):
    c = cfg(tmp_path, hosted=False)
    assert sb.panel_engines(c) == ['prometheus']
    with pytest.raises(ValueError):
        sb.engine_command_flag_path(c, '/r', 'selene')


def test_unit_labels_follow_the_registrys_lots_per_unit(tmp_path):
    c = cfg(tmp_path)
    assert sb.engine_units_label(c, 'prometheus') == 'Units (1 unit = 2 lots)'
    assert sb.engine_units_label(c, 'selene') == 'Units (1 unit = 1 lot)'
    assert sb.engine_units_label(c, 'helios') == 'Units (1 unit = 20 lots)'


# ---- Hestia buttons ---------------------------------------------------------------------------------------------------------------

def test_stop_sets_the_gate_removes_the_active_flag_and_leaves_every_engine_flag_alone(tmp_path):
    c = cfg(tmp_path)
    (c.FLAG_DIR / 'hestia_active.flag').touch()
    (c.FLAG_DIR / 'selene_command.flag').write_text('KILL')
    ok, msg = sb.apply_hestia_action(c, 'stop')
    assert ok and (c.FLAG_DIR / 'hestia_disabled.flag').exists() and not (c.FLAG_DIR / 'hestia_active.flag').exists()
    assert (c.FLAG_DIR / 'selene_command.flag').read_text() == 'KILL' and 'OPEN' in msg
    assert sb.apply_hestia_action(c, 'stop')[0], 'stopping a Hestia that is already down just sets the gate'


def test_hestia_clear_removes_only_the_gate(tmp_path):
    c = cfg(tmp_path)
    sb.apply_hestia_action(c, 'stop')
    (c.FLAG_DIR / 'selene_command.flag').write_text('KILL')
    (c.FLAG_DIR / 'hestia_active.flag').touch()
    ok, _ = sb.apply_hestia_action(c, 'clear')
    assert ok and not (c.FLAG_DIR / 'hestia_disabled.flag').exists()
    assert (c.FLAG_DIR / 'selene_command.flag').read_text() == 'KILL' and (c.FLAG_DIR / 'hestia_active.flag').exists()
    assert 'No Hestia disable flag' in sb.apply_hestia_action(c, 'clear')[1]


def test_start_is_refused_while_gated_or_already_running(tmp_path):
    c = cfg(tmp_path)
    assert sb.start_refusal(c, False) is None
    assert 'already running' in sb.start_refusal(c, True)
    sb.apply_hestia_action(c, 'stop')
    assert 'disable flag' in sb.start_refusal(c, False)
    assert sb.start_refusal(cfg(tmp_path, hosted=False), False) is None, 'the standalone world has no Hestia gate'


# ---- the host itself honours the gate ---------------------------------------------------------------------------------------------

def test_hestia_exits_before_logging_in_while_the_disable_flag_is_present(tmp_path):
    run = HostRun(tmp_path, {'a': EngineEntry(instrument='XX', factory='unused:unused', enabled=True)}, {})
    run.cfg.FLAG_DIR.mkdir(parents=True, exist_ok=True)
    (run.cfg.FLAG_DIR / 'hestia_disabled.flag').touch()
    r = run.start().join()
    assert not r.started and r.reason == 'disabled by flag' and run.login_calls() == 0
    assert not (run.cfg.FLAG_DIR / 'hestia_active.flag').exists() and not run.cfg.SESSION_LOCK_FILE.exists()
    assert (run.cfg.FLAG_DIR / 'hestia_disabled.flag').exists(), 'the gate stays until the operator clears it'
    assert any('hestia_disabled.flag' in t for t in run.texts())


# ---- the listener: panel layout and handlers (Slack stubbed, decorators transparent) ---------------------------------------------------

def load_listener(monkeypatch, tmp_path, hosted):
    from unittest.mock import MagicMock
    app = MagicMock()
    app.action = lambda *a, **k: (lambda f: f)
    app.view = lambda *a, **k: (lambda f: f)
    bolt = types.ModuleType('slack_bolt')
    bolt.App = lambda token=None: app
    monkeypatch.setitem(sys.modules, 'slack_bolt', bolt)
    monkeypatch.setitem(sys.modules, 'slack_bolt.adapter', types.ModuleType('slack_bolt.adapter'))
    sock = types.ModuleType('slack_bolt.adapter.socket_mode')
    sock.SocketModeHandler = object
    monkeypatch.setitem(sys.modules, 'slack_bolt.adapter.socket_mode', sock)
    monkeypatch.setattr(pd, 'read_csv', lambda *a, **k: pd.DataFrame([{'slack_token': 'x', 'slack_app_token': 'y'}]))
    monkeypatch.setattr(logging, 'FileHandler', lambda *a, **k: logging.NullHandler())
    monkeypatch.setattr(hestia_config, 'SLACK_PROMETHEUS_VIA_HESTIA', hosted)
    monkeypatch.setattr(hestia_config, 'FLAG_DIR', tmp_path / 'flags')
    monkeypatch.setattr(hestia_config, 'STATE_DIR', tmp_path / 'state')
    (tmp_path / 'flags').mkdir(exist_ok=True), (tmp_path / 'state').mkdir(exist_ok=True)
    spec = importlib.util.spec_from_file_location('slack_listener_panel_test', ROOT / 'slack_listener.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def action_ids(blocks):
    return [e['action_id'] for b in blocks if b['type'] == 'actions' for e in b['elements']]


def test_the_hosted_panel_has_the_three_buttons_for_every_engine_and_for_hestia(monkeypatch, tmp_path):
    mod = load_listener(monkeypatch, tmp_path, True)
    ids = action_ids(mod.CONTROL_PANEL_BLOCKS)
    for e in ENGINES:
        assert {f'btn_eng_{e}_exit', f'btn_eng_{e}_kill', f'btn_eng_{e}_clear'} <= set(ids), e
    assert {'btn_hestia_start', 'btn_hestia_stop', 'btn_hestia_clear'} <= set(ids)
    assert 'btn_prometheus_instrument' not in ids and not any(i.startswith('btn_prometheus_') for i in ids)
    assert len(mod.CONTROL_PANEL_BLOCKS) <= 50, 'Slack allows at most 50 blocks in a message'
    assert len(ids) == len(set(ids)), 'action ids are unique'
    assert {'btn_pos_sizing', 'btn_clear_sizing', 'btn_kill_switch'} <= set(ids), 'the other panel sections are still there'
    text = json.dumps(mod.CONTROL_PANEL_BLOCKS)
    assert 'standalone cron' not in text and 'Switch Instrument' not in text


def test_the_standalone_panel_only_shows_prometheus_and_a_start_button(monkeypatch, tmp_path):
    mod = load_listener(monkeypatch, tmp_path, False)
    ids = action_ids(mod.CONTROL_PANEL_BLOCKS)
    assert 'btn_eng_prometheus_exit' in ids and 'btn_eng_selene_exit' not in ids and 'btn_hestia_stop' not in ids
    assert 'btn_hestia_start' in ids


def test_destructive_buttons_ask_for_confirmation(monkeypatch, tmp_path):
    mod = load_listener(monkeypatch, tmp_path, True)
    by_id = {e['action_id']: e for b in mod.CONTROL_PANEL_BLOCKS if b['type'] == 'actions' for e in b['elements']}
    for e in ENGINES:
        assert 'confirm' in by_id[f'btn_eng_{e}_exit'] and 'confirm' in by_id[f'btn_eng_{e}_kill']
    assert 'confirm' in by_id['btn_hestia_stop']


def fake_body(action_id):
    return {'user': {'id': 'U1'}, 'actions': [{'action_id': action_id}]}


def test_the_engine_handler_writes_only_that_engines_flag_and_announces_it(monkeypatch, tmp_path):
    mod = load_listener(monkeypatch, tmp_path, True)
    said = []
    say = lambda **kw: said.append(kw)               # noqa: E731
    mod.handle_engine_button(lambda: None, fake_body('btn_eng_selene_kill'), say)
    assert (tmp_path / 'flags' / 'selene_command.flag').read_text() == 'KILL'
    assert not list((tmp_path / 'flags').glob('prometheus*')) and 'Selene' in said[-1]['text'] and '<@U1>' in said[-1]['text']
    mod.handle_engine_button(lambda: None, fake_body('btn_eng_selene_clear'), say)
    assert not (tmp_path / 'flags' / 'selene_command.flag').exists()


def test_the_exit_handler_refuses_when_hestia_is_not_running(monkeypatch, tmp_path):
    mod = load_listener(monkeypatch, tmp_path, True)
    monkeypatch.setattr(mod, '_hestia_running', lambda: False)
    said = []
    mod.handle_engine_button(lambda: None, fake_body('btn_eng_typhon_exit'), lambda **kw: said.append(kw))
    assert not (tmp_path / 'flags' / 'typhon_command.flag').exists() and 'Cannot exit Typhon' in said[-1]['text']
    monkeypatch.setattr(mod, '_hestia_running', lambda: True)
    mod.handle_engine_button(lambda: None, fake_body('btn_eng_typhon_exit'), lambda **kw: said.append(kw))
    assert (tmp_path / 'flags' / 'typhon_command.flag').read_text() == 'EXIT'


def test_an_unknown_engine_id_is_reported_not_written(monkeypatch, tmp_path):
    mod = load_listener(monkeypatch, tmp_path, True)
    said = []
    mod.handle_engine_button(lambda: None, fake_body('btn_eng_ghost_kill'), lambda **kw: said.append(kw))
    assert not list((tmp_path / 'flags').iterdir()) and 'Unknown engine' in said[-1]['text']


def test_the_hestia_handlers_stop_clear_and_refuse_a_gated_start(monkeypatch, tmp_path):
    mod = load_listener(monkeypatch, tmp_path, True)
    monkeypatch.setattr(mod, '_hestia_running', lambda: False)
    launched = []
    monkeypatch.setattr(mod.subprocess, 'Popen', lambda *a, **k: launched.append(a))
    said = []
    say = lambda **kw: said.append(kw)               # noqa: E731
    mod.handle_hestia_stop(lambda: None, fake_body('btn_hestia_stop'), say)
    assert (tmp_path / 'flags' / 'hestia_disabled.flag').exists()
    mod.handle_hestia_start(lambda: None, fake_body('btn_hestia_start'), say)
    assert not launched and 'Cannot start Hestia' in said[-1]['text']
    mod.handle_hestia_clear(lambda: None, fake_body('btn_hestia_clear'), say)
    assert not (tmp_path / 'flags' / 'hestia_disabled.flag').exists()


def test_the_sizing_modals_list_every_engine_and_label_the_units_from_the_registry(monkeypatch, tmp_path):
    mod = load_listener(monkeypatch, tmp_path, True)
    values = [o['value'] for o in mod._strategy_options()]
    assert values == ['Artemis', 'Athena', 'Iris', 'Prometheus', 'Selene', 'Helios', 'Typhon']
    assert mod._lots_label('Helios') == f'Units (1 unit = {hestia_config.ENGINES["helios"].lots_per_unit} lots)'
    assert mod._lots_label('Athena') == 'Lot Count'
    assert mod.SIZING_OVERRIDE_PATHS['Typhon'] == str(tmp_path / 'state' / 'typhon_sizing.json')
    assert mod.write_sizing_override('Selene', False, 3)
    assert json.loads((tmp_path / 'state' / 'selene_sizing.json').read_text()) == {'dynamic': False, 'static_units': 3}
    assert mod.clear_sizing_override('Selene') == (True, True) and not (tmp_path / 'state' / 'selene_sizing.json').exists()
