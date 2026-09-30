"""Typhon's unit is 2 lots (DEFAULT.lots_per_unit), not 1 (owner's decision 2026-09-30, brings its margin per unit level with
Selene's and Helios's). The engine was ported from Selene, which hardcoded `lots = units` in four places; each now reads
`units * self.cfg.lots_per_unit`, plus the ledger-adopt reverse division and the margin_per_unit() scaling factor. Every other
Typhon test file overrides lots_per_unit to 1 so Selene's numeric assertions carry over unchanged -- this file is where the
2-lot scaling itself is exercised end to end."""
import dataclasses

import pytest

from typhon_engine_helpers import CFG, FLIP_PATH, FRONT, SESSION_DATE, SESSION_OPEN, ZIGZAG, hm, next_contract, scripted_world
from typhon_engine.engine_configs import DEFAULT
from typhon_engine.levels import margin_per_unit
from typhon_engine.state import EngineState

CFG2 = dataclasses.replace(DEFAULT, instrument='XX')      # DEFAULT.lots_per_unit == 2, the real production shape
LPU = DEFAULT.lots_per_unit


def run(h, until):
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(until)


def test_the_production_unit_is_two_lots():
    assert LPU == 2


def test_entry_requests_lots_per_unit_lots_for_one_unit():
    made = []
    h = scripted_world(ZIGZAG, cfg=CFG2, made=made, lots_per_unit=LPU)
    run(h, hm(10, 30))
    assert made[-1].state.status == 'in_trade' and made[-1].state.lots == 2 and made[-1].state.units == 1
    assert h.orders[0].lots == 2


def test_flip_requests_lots_per_unit_lots_for_the_new_side():
    made = []
    h = scripted_world(FLIP_PATH, cfg=CFG2, made=made, lots_per_unit=LPU)
    run(h, hm(13, 1))
    assert [(o.side, o.lots) for o in h.orders[:2]] == [('SELL', 2), ('BUY', 4)]      # Rule 7: close 2 + open 2, netted
    assert made[-1].state.status == 'in_trade' and made[-1].state.lots == 2


def test_coincident_flip_reentry_requests_lots_per_unit_lots():
    from datetime import date
    from hestia_core.interface import ContractRef
    old = ContractRef('XX', 'R1', 'XX10SEP26FUT', date(2026, 9, 10))
    made = []
    h = scripted_world(FLIP_PATH, cfg=CFG2, made=made, front=old, lots_per_unit=LPU,
                       extra_next=[next_contract(FLIP_PATH, ref=FRONT)])
    st = EngineState(status='in_trade', direction='bearish', units=1, entry_price=99.5, entry_ts='2026-09-03T10:00:00',
                     contract_token=old.token, contract_symbol=old.symbol, contract_expiry=old.expiry.isoformat(),
                     sl_price=99.5 * (1 + CFG2.sl_pct / 100), target_price=99.5 * (1 - CFG2.target_pct / 100), lots=LPU,
                     trade_counter=1, trade_row={'trade_id': 1, 'entry_price': 99.5, 'units': 1, 'direction': 'bearish'})
    h._saved_state['typhon'] = st.to_json()
    h.seed_position('typhon', old, -LPU, 99.5)
    run(h, hm(13, 30))
    assert (h.orders[0].contract.token, h.orders[0].side, h.orders[0].lots) == (old.token, 'BUY', LPU)
    assert (h.orders[1].contract.token, h.orders[1].side, h.orders[1].lots) == (FRONT.token, 'BUY', LPU)
    assert made[-1].state.status == 'in_trade' and made[-1].state.lots == LPU


def test_ledger_adopt_divides_an_unexpected_position_by_lots_per_unit():
    made = []
    h = scripted_world(FLIP_PATH, cfg=CFG2, made=made, lots_per_unit=LPU)
    h.seed_position('typhon', FRONT, -4, 99.5)         # two clean units' worth the engine never placed itself
    run(h, hm(9, 30))
    s = made[-1].state
    assert s.status == 'in_trade' and s.direction == 'bearish' and s.units == 2 and s.lots == 4
    assert s.sl_price == pytest.approx(99.5 * (1 + CFG2.sl_pct / 100))


def test_ledger_adopt_floors_a_lot_count_that_is_not_a_clean_multiple():
    """An odd lot count (e.g. a manual partial close leaving 3 lots): floor to a sane minimum-units estimate rather than
    the pre-change `units = max(1, lots)`, which would overstate units."""
    made = []
    h = scripted_world(FLIP_PATH, cfg=CFG2, made=made, lots_per_unit=LPU)
    h.seed_position('typhon', FRONT, -3, 99.5)
    run(h, hm(9, 30))
    s = made[-1].state
    assert s.status == 'in_trade' and s.units == 1 and s.lots == 3


def test_margin_per_unit_scales_with_lots_per_unit():
    one_lot = dataclasses.replace(DEFAULT, lots_per_unit=1)
    assert margin_per_unit(300.0, 250, DEFAULT) == pytest.approx(margin_per_unit(300.0, 250, one_lot) * LPU)


def test_the_engine_and_the_hestia_registry_agree_on_lots_per_unit():
    import hestia_config as hc
    import types
    base = hc.resolve_for_host(hc.ENGINES, {}, 'nowhere')[0]        # the committed registry, no host overrides
    assert base['typhon'].lots_per_unit == DEFAULT.lots_per_unit
