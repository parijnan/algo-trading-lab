"""Rolling on the fake Hestia: the engine orchestrates hestia_core.roll_policy. OLD expires 2026-09-10, which is 6 trading days from
the session date (2026-09-03, so the roll is armed for tonight) or, expiring 2026-09-09, 5 (already inside the roll window: a
missed roll if the position is still on it)."""
from datetime import date

import pytest

from prometheus_engine_helpers import (FLIP_PATH, FRONT, HOLD_PATH, NEXT, SESSION_DATE, SESSION_OPEN, alert_texts, hm, next_contract,
                                       scripted_world, trades)
from hestia_core.interface import ContractRef
from prometheus_engine.state import EngineState

OLD = ContractRef('XX', 'R1', 'XX10SEP26FUT', date(2026, 9, 10))           # armed tonight
OLD_IN_WINDOW = ContractRef('XX', 'R1', 'XX09SEP26FUT', date(2026, 9, 9))  # already inside the window today
DOWN_THEN_UP = [(9, 0, 105), (10, 0, 105), (11, 0, 108), (23, 29, 108)]    # the next contract turns bullish
FLAT_NEXT = [(9, 0, 105), (23, 29, 105)]


def world(path, next_path=None, old=OLD, with_next=True, **kw):
    made = []
    extra = [next_contract(next_path or path, ref=FRONT)] if with_next else []
    return scripted_world(path, made=made, front=old, extra_next=extra, **kw), made


def seed_bearish(h, ref=OLD, entry=99.5, counter=1):
    """A bearish 1-unit position on `ref` that the engine's own state and Hestia's ledger both know about (taken before today's start)."""
    st = EngineState(status='in_trade', direction='bearish', units=1, entry_price=entry, entry_ts='2026-09-03T10:00:00', signal_ts=None,
                     contract_token=ref.token, contract_symbol=ref.symbol, contract_expiry=ref.expiry.isoformat(),
                     sl_price=entry * 1.022, lot1_target=entry * 0.978, lot1_lots=1, lot1_status='open', lot2_target=entry * 0.95,
                     lot2_lots=1, lot2_status='open', lot2_source='flat_pct', trade_counter=counter,
                     trade_row={'trade_id': counter, 'entry_price': entry, 'units': 1, 'direction': 'bearish'})
    h._saved_state['prometheus'] = st.to_json()
    h.seed_position('prometheus', ref, -2, entry)


def run(h, until):
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(until)


def held(h, ref):
    return h._ledger.get(('prometheus', ref.token), [0])[0]


def test_flat_on_a_roll_eve_switches_at_once_and_trades_the_new_contract():
    h, made = world(FLIP_PATH)
    run(h, hm(10, 40))
    e = made[-1]
    assert e.contract.token == FRONT.token and e.state.roll_executed_date == '2026-09-03'
    assert h.orders and all(o.contract.token == FRONT.token for o in h.orders)
    assert e.state.contract_token == FRONT.token and e.state.status == 'in_trade'


def test_in_trade_on_a_roll_eve_tracks_the_new_contract_and_arms_the_evening():
    h, made = world(HOLD_PATH, with_next=True)
    seed_bearish(h)
    run(h, hm(11, 30))
    e = made[-1]
    assert e.state.status == 'in_trade' and e.state.contract_token == OLD.token
    assert e.state.roll_target['token'] == FRONT.token and not e.state.roll_target['flatten_only']
    assert e.contract.token == OLD.token                                       # still trading the old one until tonight
    assert any('rolling to' in t for t in alert_texts(h))


def test_fallback_roll_carries_the_position_when_the_new_contract_agrees():
    h, made = world(HOLD_PATH)
    seed_bearish(h)
    run(h, hm(23, 40))
    e, s = made[-1], made[-1].state
    reqs = [o.request_id.split('-', 2)[-1] for o in h.orders]
    assert reqs == ['t1-roll_close-1', 't1-roll_open-1']
    close, reopen = h.orders[0], h.orders[1]
    assert (close.contract.token, close.side, close.lots) == (OLD.token, 'BUY', 2)
    assert (reopen.contract.token, reopen.side, reopen.lots) == (FRONT.token, 'SELL', 2)
    old_row = trades(h)[0]
    assert old_row['lot1_exit_reason'] == 'rollover' and old_row['lot2_exit_reason'] == 'rollover'
    assert s.status == 'in_trade' and s.contract_token == FRONT.token and s.trade_counter == 2 and e.contract.token == FRONT.token
    assert s.basis_price == pytest.approx(105.0, abs=0.3)                          # the new contract's price near the old entry (10:00)
    assert s.sl_price == pytest.approx(s.basis_price * 1.022)                       # recalibrated off the basis, not the new fill
    assert s.trade_row['direction'] == 'bearish-rollover' and s.trade_row['parent_trade_id'] == 1
    assert held(h, OLD) == 0 and held(h, FRONT) == -2


def test_fallback_roll_vetoed_when_the_new_contract_disagrees():
    h, made = world(HOLD_PATH, next_path=DOWN_THEN_UP)
    seed_bearish(h)
    run(h, hm(23, 40))
    s = made[-1].state
    assert [o.request_id.split('-', 2)[-1] for o in h.orders] == ['t1-roll_close-1']
    assert s.status == 'watching' and made[-1].contract.token == FRONT.token
    assert trades(h)[0]['lot1_exit_reason'] == 'rollover' and held(h, OLD) == 0 and held(h, FRONT) == 0
    assert any('no-go' in t for t in alert_texts(h))


def test_no_next_contract_flattens_at_the_rollover_time_and_stays_out():
    h, made = world(HOLD_PATH, with_next=False)
    seed_bearish(h)
    run(h, hm(23, 40))
    e = made[-1]
    assert e.state.roll_target is None and e.state.status == 'watching' and e.flatten_done
    assert [o.request_id.split('-', 2)[-1] for o in h.orders] == ['t1-roll_close-1']
    assert any('FLATTENED' in t for t in alert_texts(h))


def test_coincident_flip_exits_the_old_contract_and_enters_the_new_one_fresh():
    h, made = world(FLIP_PATH)
    seed_bearish(h)
    run(h, hm(13, 30))
    reqs = [o.request_id.split('-', 2)[-1] for o in h.orders]
    assert reqs == ['t1-coin_close-1', 't1-coin_open-1']
    assert (h.orders[0].contract.token, h.orders[0].side, h.orders[0].lots) == (OLD.token, 'BUY', 2)
    assert (h.orders[1].contract.token, h.orders[1].side, h.orders[1].lots) == (FRONT.token, 'BUY', 2)
    s = made[-1].state
    assert s.status == 'in_trade' and s.direction == 'bullish' and s.contract_token == FRONT.token
    assert s.basis_price is None and s.sl_price == pytest.approx(s.entry_price * (1 - 0.022))      # a fresh entry, off its own fill
    assert trades(h)[0]['lot1_exit_reason'] == 'trend_flip'


def test_flip_without_a_coincident_new_contract_flip_exits_only():
    h, made = world(FLIP_PATH, next_path=FLAT_NEXT)
    seed_bearish(h)
    run(h, hm(13, 30))
    reqs = [o.request_id.split('-', 2)[-1] for o in h.orders]
    assert reqs == ['t1-coin_close-1']
    s = made[-1].state
    assert s.status == 'watching' and made[-1].contract.token == FRONT.token and held(h, OLD) == 0


@pytest.mark.parametrize('position', ['bullish', 'bearish'])
def test_restart_after_a_missed_roll_rolls_at_once(position):
    """The position is still on a contract that is already inside the roll window: roll it now, carry it only if the new contract's
    supertrend agrees."""
    h, made = world(HOLD_PATH, old=OLD_IN_WINDOW)
    sign = 1 if position == 'bullish' else -1
    seed_bearish(h, ref=OLD_IN_WINDOW, entry=100.0, counter=7)
    st = EngineState.from_json(h._saved_state['prometheus'])
    st.direction = position
    st.sl_price, st.lot1_target, st.lot2_target = 100 - sign * 2.2, 100 + sign * 2.2, 100 + sign * 5
    st.entry_ts = '2026-09-02T14:00:00'
    h._saved_state['prometheus'] = st.to_json()
    h._ledger[('prometheus', OLD_IN_WINDOW.token)][0] = sign * 2
    h._sim.seed(OLD_IN_WINDOW.token, sign * 2, 100.0)
    run(h, hm(9, 30))
    e, s = made[-1], made[-1].state
    assert any('MISSED ROLLOVER' in t for t in alert_texts(h))
    new_dir = e.ctx.latest_bar(FRONT)[1].trend.value
    ids = [o.request_id.split('-', 2)[-1] for o in h.orders]
    assert ids[0] == 't7-roll_close-1' and held(h, OLD_IN_WINDOW) == 0
    if new_dir == position:
        assert ids[1] == 't7-roll_open-1' and s.status == 'in_trade' and s.contract_token == FRONT.token
    else:
        assert len(ids) == 1 and s.status == 'watching'


def test_restart_on_a_later_contract_than_today_adopts_it():
    made = []
    h = scripted_world(HOLD_PATH, made=made, front=FRONT, extra_next=[next_contract(HOLD_PATH, ref=NEXT, shift=5.0)])
    st = EngineState(status='in_trade', direction='bearish', units=1, entry_price=105.0, entry_ts='2026-09-02T14:00:00',
                     contract_token=NEXT.token, contract_symbol=NEXT.symbol, contract_expiry=NEXT.expiry.isoformat(), sl_price=107.3,
                     lot1_target=102.7, lot1_lots=1, lot1_status='open', lot2_target=99.75, lot2_lots=1, lot2_status='open',
                     lot2_source='flat_pct', trade_counter=3)
    h._saved_state['prometheus'] = st.to_json()
    h.seed_position('prometheus', NEXT, -2, 105.0)
    run(h, hm(9, 30))
    assert made[-1].contract.token == NEXT.token and made[-1].state.status == 'in_trade' and not h.orders


# ---------------------------------------------------------------------------------------------------------------------
# Failure paths: a rejected close must never move the engine to the new contract with the old position still open
# ---------------------------------------------------------------------------------------------------------------------

def _rejecting(*suffixes):
    from hestia_core.replay import BrokerReply
    return lambda call: BrokerReply('reject') if any(call.request.request_id.endswith(x) for x in suffixes) else BrokerReply('fill')


def test_rejected_fallback_roll_close_is_retried_and_never_switches_contracts_early():
    h, made = world(HOLD_PATH, broker=_rejecting('roll_close-1'))
    seed_bearish(h)
    run(h, hm(23, 40))
    e, s = made[-1], made[-1].state
    ids = [o.request_id.split('-', 2)[-1] for o in h.orders]
    assert 't1-roll_close-2' in ids and 't1-roll_open-1' not in ids                       # the first pair never reached the broker
    assert held(h, OLD) == 0 and held(h, FRONT) == -2                                     # the retry rolled the position across
    assert s.status == 'in_trade' and s.contract_token == FRONT.token and e.contract.token == FRONT.token


def test_rejected_missed_roll_close_is_retried_by_the_tick():
    h, made = world(HOLD_PATH, old=OLD_IN_WINDOW, broker=_rejecting('roll_close-1'))
    seed_bearish(h, ref=OLD_IN_WINDOW, entry=100.0, counter=7)
    run(h, hm(9, 30))
    ids = [o.request_id.split('-', 2)[-1] for o in h.orders]
    assert 't7-roll_close-2' in ids and held(h, OLD_IN_WINDOW) == 0
    assert made[-1].state.roll_target is None                                             # done: the marker is cleared


def test_rejected_coincident_flip_close_is_retried_as_an_exit_at_once_not_at_the_rollover():
    h, made = world(FLIP_PATH, broker=_rejecting('coin_close-1'))
    seed_bearish(h)
    run(h, hm(13, 30))
    ids = [o.request_id.split('-', 2)[-1] for o in h.orders]
    assert ids[0] == 't1-flip_exit-1' and held(h, OLD) == 0                              # long before 23:14
    assert made[-1].state.status == 'watching' and 't1-coin_open-1' not in ids


def test_trade_rows_carry_only_the_shared_columns_and_production_reason_strings():
    from hestia_core.interface import TRADE_RECORD_COLUMNS
    h, made = world(HOLD_PATH)
    seed_bearish(h)
    from hestia_core.interface import CommandKind
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(10, 30))
    h.send_command('prometheus', CommandKind.EXIT)
    h.run_until(hm(10, 35))
    row = trades(h)[0]
    assert set(row) <= set(TRADE_RECORD_COLUMNS) and set(row) == set(TRADE_RECORD_COLUMNS)
    assert row['lot1_exit_reason'] == 'slack_exit'                                        # production's string for the command
    # points x lots x lot size, the pulled trades file's convention (5 units: -106.25 pts -> -5,312.5 Rs at lot size 10)
    assert row['lot1_pnl_rs'] == pytest.approx(row['lot1_pnl_points'] * 1 * 10, abs=0.1)
