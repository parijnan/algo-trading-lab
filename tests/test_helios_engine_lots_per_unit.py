"""The one real behavioral difference from Selene's engine: 1 unit = 20 lots (DEFAULT.lots_per_unit), not 1. Selene's own
engine hardcoded `lots = units` in four places; Helios's port changed each to `lots = units * self.cfg.lots_per_unit`
(engine.py) plus the ledger-adopt reverse-division and the margin_per_unit() scaling factor (levels.py). Every other test
file in this directory overrides lots_per_unit to 1 (matching Selene's own shape) specifically so it can port Selene's
numeric assertions unchanged -- this file is where the 20x scaling itself is actually exercised end to end."""
import dataclasses

import pytest

from helios_engine_helpers import CFG, FLIP_PATH, FRONT, HOLD_PATH, SESSION_DATE, SESSION_OPEN, ZIGZAG, hm, next_contract, \
    scripted_world, trades
from helios_engine.engine_configs import DEFAULT
from helios_engine.state import EngineState

CFG20 = dataclasses.replace(DEFAULT, instrument='XX')   # DEFAULT.lots_per_unit == 20, real production shape


def run(h, until):
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(until)


def test_entry_requests_lots_per_unit_lots_for_one_unit():
    made = []
    h = scripted_world(ZIGZAG, cfg=CFG20, made=made, lots_per_unit=DEFAULT.lots_per_unit)
    run(h, hm(10, 30))
    assert made[-1].state.status == 'in_trade' and made[-1].state.lots == 20
    assert h.orders[0].lots == 20


def test_flip_requests_lots_per_unit_lots_for_the_new_side():
    made = []
    h = scripted_world(FLIP_PATH, cfg=CFG20, made=made, lots_per_unit=DEFAULT.lots_per_unit)
    run(h, hm(13, 1))
    assert [(o.side, o.lots) for o in h.orders[:2]] == [('SELL', 20), ('BUY', 40)]   # Rule 7: close 20 + open 20 netted
    assert made[-1].state.status == 'in_trade' and made[-1].state.lots == 20


def test_coincident_flip_reentry_requests_lots_per_unit_lots():
    from datetime import date
    from hestia_core.interface import ContractRef
    old = ContractRef('XX', 'R1', 'XX10SEP26FUT', date(2026, 9, 10))
    made = []
    h = scripted_world(FLIP_PATH, cfg=CFG20, made=made, front=old, lots_per_unit=DEFAULT.lots_per_unit,
                       extra_next=[next_contract(FLIP_PATH, ref=FRONT)])
    st = EngineState(status='in_trade', direction='bearish', units=1, entry_price=99.5, entry_ts='2026-09-03T10:00:00',
                     contract_token=old.token, contract_symbol=old.symbol, contract_expiry=old.expiry.isoformat(),
                     sl_price=99.5 * (1 + CFG20.sl_pct / 100), lots=20, trade_counter=1,
                     trade_row={'trade_id': 1, 'entry_price': 99.5, 'units': 1, 'direction': 'bearish'})
    h._saved_state['helios'] = st.to_json()
    h.seed_position('helios', old, -20, 99.5)
    run(h, hm(13, 30))
    reqs = [o.request_id.split('-', 2)[-1] for o in h.orders]
    assert reqs == ['t1-coin_close-1', 't1-coin_open-1']
    assert (h.orders[0].contract.token, h.orders[0].side, h.orders[0].lots) == (old.token, 'BUY', 20)
    assert (h.orders[1].contract.token, h.orders[1].side, h.orders[1].lots) == (FRONT.token, 'BUY', 20)
    assert made[-1].state.status == 'in_trade' and made[-1].state.lots == 20


def test_ledger_adopt_divides_an_unexpected_position_by_lots_per_unit():
    made = []
    h = scripted_world(FLIP_PATH, cfg=CFG20, made=made, lots_per_unit=DEFAULT.lots_per_unit)
    h.seed_position('helios', FRONT, -40, 99.0)   # 2 clean units' worth, the engine never placed this order itself
    run(h, hm(9, 30))
    s = made[-1].state
    assert s.status == 'in_trade' and s.direction == 'bearish' and s.units == 2 and s.lots == 40
    assert s.sl_price == pytest.approx(99.0 * (1 + CFG20.sl_pct / 100))


def test_ledger_adopt_floors_a_lot_count_that_is_not_a_clean_multiple():
    """A real anomaly (e.g. a manual partial close leaving 25 lots, not a multiple of 20) -- floor to a sane minimum-units
    estimate (1) rather than the pre-fix bug's `units = max(1, lots)`, which would have wildly overstated units 25x."""
    made = []
    h = scripted_world(FLIP_PATH, cfg=CFG20, made=made, lots_per_unit=DEFAULT.lots_per_unit)
    h.seed_position('helios', FRONT, -25, 99.0)
    run(h, hm(9, 30))
    s = made[-1].state
    assert s.status == 'in_trade' and s.units == 1 and s.lots == 25


# No "live" end-to-end version of the margin_per_unit x20 check: tried, and removed. Hestia's own host-level admission
# gate (hestia_core/core.py's _entry_limits, its own independent _margin_per_lot calculation) rejects an
# under-margined entry BEFORE the engine's own _margin_sufficient() pre-check result has any bearing on the outcome --
# confirmed directly (mutating margin_per_unit()'s x lots_per_unit factor away left "not h.orders" true either way,
# rejected instead by Hestia's own unrelated calculation reporting a completely different "needs" figure). The engine's
# own margin_per_unit() usage is precisely covered by test_helios_engine.py's test_margin_per_unit_is_20x_margin_per_lot
# (a direct call, no host interference); test_margin_refusal_skips_the_entry in that same file covers the engine's own
# _margin_sufficient() rejection path generally (at CFG's lots_per_unit=1, so it doesn't exercise the x20 factor
# specifically, but confirms the mechanism itself). Forcing a live version to isolate the engine's own check from
# Hestia's separate one is not worth the complexity for what would be redundant coverage.
