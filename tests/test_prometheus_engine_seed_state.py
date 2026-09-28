"""prometheus_engine.seed_state: standalone Prometheus state to the Hestia engine's state, at cutover."""
import json
import os

import pytest

import hestia_config
from prometheus_engine import seed_state as ss
from prometheus_engine.state import EngineState

IDLE = {'status': 'idle', 'last_processed_boundary': None}
OPEN = {'status': 'in_trade', 'direction': 'bearish', 'units': 5, 'entry_price': 9149.95, 'recalibration_basis_price': None,
        'entry_ts': '2026-09-28T17:45:00.974069', 'signal_ts': '2026-09-28T17:30:00', 'signal_close': 9152.0,
        'contract_expiry': '2026-10-19', 'symbol': 'CRUDEOILM19OCT26FUT', 'token': 569901, 'sl_price': 9351.25,
        'lot1_target': 8948.65, 'lot1_lots': 5, 'lot1_status': 'booked', 'lot1_exit_price': 8948.0, 'lot1_exit_ts': '2026-09-29T10:00:00',
        'lot1_exit_reason': 'target1', 'lot2_target': 8692.45, 'lot2_target_source': 'flat_pct', 'lot2_lots': 5, 'lot2_status': 'open',
        'last_processed_boundary': '2026-09-28T17:45:00'}


def test_a_flat_standalone_state_becomes_a_blank_engine_that_keeps_the_trade_counter():
    st = ss.convert(IDLE, 51)
    assert (st.status, st.trade_counter, st.pending, st.pending_flip) == ('watching', 51, {}, None)


def test_an_open_position_converts_field_for_field_and_round_trips():
    st = ss.convert(OPEN, 51)
    assert (st.status, st.direction, st.units, st.contract_token, st.trade_counter) == ('in_trade', 'bearish', 5, '569901', 51)
    assert (st.lot1_status, st.lot1_lots, st.lot2_status, st.lot2_lots) == ('booked', 5, 'open', 5)
    assert (st.sl_price, st.lot1_target, st.lot2_target, st.lot2_source) == (9351.25, 8948.65, 8692.45, 'flat_pct')
    assert st.open_lots() == 5 and st.last_processed_boundary == '2026-09-28T17:45:00'
    assert st.trade_row['lot1_exit_reason'] == 'target1' and 'lot2_exit_reason' not in st.trade_row      # a booked lot keeps its exit
    assert st.trade_row['lot1_pnl_points'] == 201.95 and st.trade_row['lot1_pnl_rs'] == round(201.95 * 5 * 10, 2)   # bearish: entry minus exit
    assert EngineState.from_json(st.to_json()) == st


def test_a_rolled_position_keeps_its_basis_and_the_rollover_direction_label():
    st = ss.convert(dict(OPEN, recalibration_basis_price=9200.0, lot1_status='never_opened', lot1_lots=0), 7)
    assert st.basis_price == 9200.0 and st.trade_row['direction'] == 'bearish-rollover'


def test_write_refuses_to_replace_an_existing_state_without_force(tmp_path, monkeypatch):
    src = tmp_path / 'standalone'
    src.mkdir()
    (src / 'prometheus_state.csv').write_text('status,last_processed_boundary\nidle,\n')
    (src / 'trade_counter.txt').write_text('51')
    monkeypatch.setattr(hestia_config, 'STATE_DIR', tmp_path / 'state')
    assert ss.main(['x', str(src), '--write']) == 0
    written = json.loads(json.loads((tmp_path / 'state' / 'prometheus_state.json').read_text())['blob'])
    assert written['trade_counter'] == 51 and written['status'] == 'watching'
    assert ss.main(['x', str(src), '--write']) == 1                                    # already there
    assert ss.main(['x', str(src), '--write', '--force']) == 0


def test_refuses_while_the_standalone_process_is_alive(tmp_path):
    (tmp_path / 'prometheus.pid').write_text(str(os.getpid()))
    (tmp_path / 'prometheus_state.csv').write_text('status\nidle\n')
    (tmp_path / 'trade_counter.txt').write_text('1')
    assert ss.main(['x', str(tmp_path)]) == 1
