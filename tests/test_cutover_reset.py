"""hestia_core.cutover_reset: reset an engine to flat for the paper->live cutover, on a scratch state directory."""
import json
from datetime import datetime

import pytest

from hestia_core.state_store import StateStore
from hestia_core import cutover_reset as cr
from selene_engine.state import EngineState

NOW = datetime(2026, 9, 30, 23, 45)


def in_trade(**kw):
    base = dict(status='in_trade', direction='bearish', units=1, lots=1, entry_price=228000.0, sl_price=234840.0,
                contract_token='111', contract_symbol='SILVERMIC30NOV26FUT', contract_expiry='2026-11-30',
                trade_counter=4, last_processed_boundary='2026-09-30T23:15:00', roll_executed_date='2026-09-20',
                attempts={'4:entry': 1}, trade_row={'trade_id': 4, 'entry_price': 228000.0})
    base.update(kw)
    return EngineState(**base)


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path)
    s.save_engine_state('selene', in_trade().to_json())
    s.save_engine_state('helios', in_trade(contract_token='222', trade_counter=2).to_json())
    s.save_ledger({('selene', '111'): [-1, 228000.0, NOW], ('helios', '222'): [20, 15000.0, NOW],
                   ('prometheus', '333'): [2, 8600.0, NOW]})
    return s


def test_an_open_paper_position_is_reset_to_flat_and_everything_else_is_untouched(store, tmp_path):
    helios_before = (tmp_path / 'helios_state.json').read_text()
    res = cr.reset(tmp_path, 'selene', now=NOW)
    assert res.ok and res.changed, res.messages
    st = EngineState.from_json(store.load_engine_state('selene'))
    assert st.status == 'watching' and st.direction is None and st.entry_price is None and st.lots is None
    assert st.sl_price is None and st.trade_row is None and st.contract_token is None
    ledger = store.load_ledger()
    assert not any(k[0] == 'selene' for k in ledger)
    assert ledger[('helios', '222')][:2] == [20, 15000.0] and ledger[('prometheus', '333')][:2] == [2, 8600.0]
    assert (tmp_path / 'helios_state.json').read_text() == helios_before


def test_the_counters_that_must_survive_are_kept(store, tmp_path):
    cr.reset(tmp_path, 'selene', now=NOW)
    st = EngineState.from_json(store.load_engine_state('selene'))
    assert st.trade_counter == 4 and st.last_processed_boundary == '2026-09-30T23:15:00'
    assert st.roll_executed_date == '2026-09-20' and st.attempts == {'4:entry': 1}


def test_the_paper_trade_is_saved_for_the_manual_record_and_backups_are_written(store, tmp_path):
    cr.reset(tmp_path, 'selene', now=NOW)
    saved = json.loads((tmp_path / 'selene_paper_trade_20260930_234500.json').read_text())
    assert saved['state']['entry_price'] == 228000.0 and saved['state']['direction'] == 'bearish'
    assert saved['state']['trade_row']['trade_id'] == 4
    assert saved['ledger_rows'] == [{'engine': 'selene', 'token': '111', 'net': -1, 'avg': 228000.0, 'ts': NOW.isoformat()}]
    bak_state = json.loads((tmp_path / 'selene_state.json.bak_cutover_20260930_234500').read_text())
    assert EngineState.from_json(bak_state['blob']).status == 'in_trade'
    assert (tmp_path / 'ledger.json.bak_cutover_20260930_234500').exists()


def test_an_already_flat_engine_is_left_alone(tmp_path):
    s = StateStore(tmp_path)
    s.save_engine_state('selene', EngineState(trade_counter=7).to_json())
    s.save_ledger({('helios', '222'): [20, 15000.0, NOW]})
    before = (tmp_path / 'selene_state.json').read_text()
    res = cr.reset(tmp_path, 'selene', now=NOW)
    assert res.ok and not res.changed
    assert (tmp_path / 'selene_state.json').read_text() == before
    assert not list(tmp_path.glob('*bak_cutover*'))


@pytest.mark.parametrize('kw', [dict(frozen=True), dict(pending={'r1': {'purpose': 'exit'}}),
                                dict(pending_flip={'x': 1}), dict(pending_missed_flip={'x': 1})])
def test_an_unexpected_state_is_refused_and_nothing_is_written(store, tmp_path, kw):
    store.save_engine_state('selene', in_trade(**kw).to_json())
    before = (tmp_path / 'selene_state.json').read_text(), (tmp_path / 'ledger.json').read_text()
    res = cr.reset(tmp_path, 'selene', now=NOW)
    assert not res.ok and not res.changed
    assert ((tmp_path / 'selene_state.json').read_text(), (tmp_path / 'ledger.json').read_text()) == before
    assert not list(tmp_path.glob('*bak_cutover*'))


def test_a_ledger_position_with_a_flat_state_is_still_cleared(store, tmp_path):
    store.save_engine_state('selene', EngineState(trade_counter=4).to_json())
    res = cr.reset(tmp_path, 'selene', now=NOW)
    assert res.ok and res.changed
    assert not any(k[0] == 'selene' for k in store.load_ledger())


def test_dry_run_writes_nothing(store, tmp_path):
    before = (tmp_path / 'selene_state.json').read_text(), (tmp_path / 'ledger.json').read_text()
    res = cr.reset(tmp_path, 'selene', now=NOW, dry_run=True)
    assert res.ok and not res.changed
    assert ((tmp_path / 'selene_state.json').read_text(), (tmp_path / 'ledger.json').read_text()) == before
    assert not list(tmp_path.glob('*bak_cutover*')) and not list(tmp_path.glob('*paper_trade*'))


def test_no_saved_state_is_a_no_op(tmp_path):
    res = cr.reset(tmp_path, 'selene', now=NOW)
    assert res.ok and not res.changed


def _main_env(monkeypatch, tmp_path, paper, engine='selene'):
    import dataclasses
    import hestia_config as hc
    eng = dict(hc.ENGINES)
    eng[engine] = dataclasses.replace(hc.ENGINES[engine], enabled=True, paper=paper)
    monkeypatch.setattr(hc, 'ENGINES', eng)
    monkeypatch.setattr(hc, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(hc, 'FLAG_DIR', tmp_path / 'flags')
    monkeypatch.setattr(hc, 'REPO_ROOT', tmp_path)
    monkeypatch.setattr(cr, 'hestia_running', lambda flag_dir: False)


def test_main_changes_nothing_while_the_config_still_says_paper(store, tmp_path, monkeypatch):
    _main_env(monkeypatch, tmp_path, paper=True)
    before = (tmp_path / 'selene_state.json').read_text(), (tmp_path / 'ledger.json').read_text()
    assert cr.main(['--engine', 'selene']) == 1
    assert ((tmp_path / 'selene_state.json').read_text(), (tmp_path / 'ledger.json').read_text()) == before
    assert not list(tmp_path.glob('*bak_cutover*'))


def test_main_resets_once_the_config_says_live(store, tmp_path, monkeypatch):
    _main_env(monkeypatch, tmp_path, paper=False)
    assert cr.main(['--engine', 'selene']) == 0
    assert EngineState.from_json(store.load_engine_state('selene')).status == 'watching'


def test_main_does_nothing_on_a_different_date_than_only_date(store, tmp_path, monkeypatch):
    _main_env(monkeypatch, tmp_path, paper=False)
    before = (tmp_path / 'selene_state.json').read_text(), (tmp_path / 'ledger.json').read_text()
    assert cr.main(['--engine', 'selene', '--only-date', '2000-01-01']) == 1
    assert ((tmp_path / 'selene_state.json').read_text(), (tmp_path / 'ledger.json').read_text()) == before
    assert cr.main(['--engine', 'selene', '--only-date', datetime.now().date().isoformat()]) == 0


# ---- Helios (2026-10-02): the same reset on an engine whose unit is 20 lots, leaving Selene's own rows alone ----------------------

def test_helios_is_reset_to_flat_with_its_twenty_lot_position_and_selene_is_untouched(tmp_path):
    from helios_engine.state import EngineState as HeliosState
    s = StateStore(tmp_path)
    s.save_engine_state('helios', HeliosState(status='in_trade', direction='bearish', units=1, lots=20, entry_price=14956.0,
                                              sl_price=15195.3, contract_token='571306', contract_symbol='GOLDPETAL30OCT26FUT',
                                              contract_expiry='2026-10-30', trade_counter=3,
                                              last_processed_boundary='2026-10-01T23:00:00', attempts={'3:entry': 1},
                                              trade_row={'trade_id': 3, 'entry_price': 14956.0}).to_json())
    s.save_engine_state('selene', in_trade().to_json())
    s.save_ledger({('helios', '571306'): [-20, 14956.0, NOW], ('selene', '111'): [-1, 228000.0, NOW]})
    selene_before = (tmp_path / 'selene_state.json').read_text()
    res = cr.reset(tmp_path, 'helios', now=NOW)
    assert res.ok and res.changed, res.messages
    st = HeliosState.from_json(s.load_engine_state('helios'))
    assert st.status == 'watching' and st.lots is None and st.entry_price is None and st.trade_counter == 3
    ledger = s.load_ledger()
    assert not any(k[0] == 'helios' for k in ledger) and ledger[('selene', '111')][:2] == [-1, 228000.0]
    assert (tmp_path / 'selene_state.json').read_text() == selene_before
    saved = json.loads((tmp_path / 'helios_paper_trade_20260930_234500.json').read_text())
    assert saved['engine'] == 'helios' and saved['state']['lots'] == 20
    assert saved['ledger_rows'][0]['net'] == -20


def test_main_resets_the_named_engine_once_its_config_says_live(tmp_path, monkeypatch):
    from helios_engine.state import EngineState as HeliosState
    s = StateStore(tmp_path)
    s.save_engine_state('helios', HeliosState(status='in_trade', direction='bullish', units=1, lots=20, entry_price=15000.0,
                                              trade_counter=5).to_json())
    s.save_ledger({('helios', '571306'): [20, 15000.0, NOW]})
    _main_env(monkeypatch, tmp_path, paper=True, engine='helios')
    assert cr.main(['--engine', 'helios']) == 1                                  # still paper in config: nothing changes
    assert HeliosState.from_json(s.load_engine_state('helios')).status == 'in_trade'
    _main_env(monkeypatch, tmp_path, paper=False, engine='helios')
    assert cr.main(['--engine', 'helios']) == 0
    assert HeliosState.from_json(s.load_engine_state('helios')).status == 'watching'
    assert not any(k[0] == 'helios' for k in s.load_ledger())


def test_typhon_is_reset_to_flat_and_its_target_is_saved_with_the_paper_trade(tmp_path):
    from typhon_engine.state import EngineState as TyphonState
    s = StateStore(tmp_path)
    s.save_engine_state('typhon', TyphonState(status='in_trade', direction='bearish', units=1, lots=2, entry_price=287.6,
                                              sl_price=289.9008, target_price=244.46, contract_token='570751',
                                              contract_symbol='NATGASMINI27OCT26FUT', contract_expiry='2026-10-27',
                                              trade_counter=2, last_processed_boundary='2026-10-01T22:15:00',
                                              trade_row={'trade_id': 2, 'entry_price': 287.6, 'lot1_target': 244.46}).to_json())
    s.save_ledger({('typhon', '570751'): [-2, 287.6, NOW], ('helios', '571306'): [-20, 14933.0, NOW]})
    res = cr.reset(tmp_path, 'typhon', now=NOW)
    assert res.ok and res.changed, res.messages
    st = TyphonState.from_json(s.load_engine_state('typhon'))
    assert st.status == 'watching' and st.lots is None and st.target_price is None and st.sl_price is None
    assert st.trade_counter == 2 and st.last_processed_boundary == '2026-10-01T22:15:00'
    assert not any(k[0] == 'typhon' for k in s.load_ledger()) and s.load_ledger()[('helios', '571306')][:2] == [-20, 14933.0]
    saved = json.loads((tmp_path / 'typhon_paper_trade_20260930_234500.json').read_text())
    assert saved['state']['target_price'] == 244.46 and saved['state']['lots'] == 2
