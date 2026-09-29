"""Rolling on the fake Hestia: the engine orchestrates hestia_core.roll_policy, with Helios's OWN 14-minute rollover buffer (not
Prometheus's 15). OLD expires 2026-09-10, 6 trading days from the session date (2026-09-03: armed tonight) or, expiring 2026-09-09,
5 (already inside the roll window: a missed roll if the position is still on it)."""
import pytest

from datetime import date

from helios_engine_helpers import (CFG, FLIP_PATH, FRONT, HOLD_PATH, NEXT, SESSION_DATE, SESSION_OPEN, alert_texts, hm,
                                   next_contract, scripted_world, trades)
from hestia_core.interface import ContractRef
from helios_engine.state import EngineState

SL = 1 + CFG.sl_pct / 100   # Helios's own 1.6% stop (Selene's own port used 3.0%'s literal 1.03 -- ported as a factor instead)
OLD = ContractRef('XX', 'R1', 'XX10SEP26FUT', date(2026, 9, 10))
OLD_IN_WINDOW = ContractRef('XX', 'R1', 'XX09SEP26FUT', date(2026, 9, 9))
DOWN_THEN_UP = [(9, 0, 105), (10, 0, 105), (11, 0, 108), (23, 29, 108)]
FLAT_NEXT = [(9, 0, 105), (23, 29, 105)]


def world(path, next_path=None, old=OLD, with_next=True, **kw):
    made = []
    extra = [next_contract(next_path or path, ref=FRONT)] if with_next else []
    return scripted_world(path, made=made, front=old, extra_next=extra, **kw), made


def seed_bearish(h, ref=OLD, entry=99.5, counter=1):
    st = EngineState(status='in_trade', direction='bearish', units=1, entry_price=entry, entry_ts='2026-09-03T10:00:00',
                     signal_ts=None, contract_token=ref.token, contract_symbol=ref.symbol, contract_expiry=ref.expiry.isoformat(),
                     sl_price=entry * SL, lots=1, trade_counter=counter,
                     trade_row={'trade_id': counter, 'entry_price': entry, 'units': 1, 'direction': 'bearish'})
    h._saved_state['helios'] = st.to_json()
    h.seed_position('helios', ref, -1, entry)


def run(h, until):
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(until)


def held(h, ref):
    return h._ledger.get(('helios', ref.token), [0])[0]


ROLLOVER_AT = hm(23, 16)                                        # 23:30 close - 14 minutes, Helios's own buffer


def test_own_rollover_buffer_is_14_minutes_not_prometheus_15():
    h, made = world(HOLD_PATH, with_next=True)
    seed_bearish(h)
    run(h, hm(11, 30))
    assert made[-1].rollover_at == ROLLOVER_AT


def test_flat_on_a_roll_eve_switches_at_once_and_trades_the_new_contract():
    h, made = world(FLIP_PATH)
    run(h, hm(10, 40))
    e = made[-1]
    assert e.contract.token == FRONT.token and e.state.roll_executed_date == '2026-09-03'
    assert h.orders and all(o.contract.token == FRONT.token for o in h.orders)


def test_fallback_roll_carries_the_position_when_the_new_contract_agrees():
    h, made = world(HOLD_PATH)
    seed_bearish(h)
    run(h, hm(23, 40))
    e, s = made[-1], made[-1].state
    reqs = [o.request_id.split('-', 2)[-1] for o in h.orders]
    assert reqs == ['t1-roll_close-1', 't1-roll_open-1']
    close, reopen = h.orders[0], h.orders[1]
    assert (close.contract.token, close.side, close.lots) == (OLD.token, 'BUY', 1)
    assert (reopen.contract.token, reopen.side, reopen.lots) == (FRONT.token, 'SELL', 1)
    old_row = trades(h)[0]
    assert old_row['lot1_exit_reason'] == 'rollover'
    assert s.status == 'in_trade' and s.contract_token == FRONT.token and s.trade_counter == 2 and e.contract.token == FRONT.token
    assert s.basis_price == pytest.approx(105.0, abs=0.3)
    assert s.sl_price == pytest.approx(s.basis_price * SL)
    assert s.trade_row['direction'] == 'bearish-rollover' and s.trade_row['parent_trade_id'] == 1
    assert held(h, OLD) == 0 and held(h, FRONT) == -1


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
    assert (h.orders[0].contract.token, h.orders[0].side, h.orders[0].lots) == (OLD.token, 'BUY', 1)
    assert (h.orders[1].contract.token, h.orders[1].side, h.orders[1].lots) == (FRONT.token, 'BUY', 1)
    s = made[-1].state
    assert s.status == 'in_trade' and s.direction == 'bullish' and s.contract_token == FRONT.token
    assert s.basis_price is None and s.sl_price == pytest.approx(s.entry_price * (1 - CFG.sl_pct / 100))
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
    h, made = world(HOLD_PATH, old=OLD_IN_WINDOW)
    sign = 1 if position == 'bullish' else -1
    seed_bearish(h, ref=OLD_IN_WINDOW, entry=100.0, counter=7)
    st = EngineState.from_json(h._saved_state['helios'])
    st.direction = position
    st.sl_price = 100 - sign * 3.0
    st.entry_ts = '2026-09-02T14:00:00'
    h._saved_state['helios'] = st.to_json()
    h._ledger[('helios', OLD_IN_WINDOW.token)][0] = sign * 1
    h._sim.seed(OLD_IN_WINDOW.token, sign * 1, 100.0)
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
                     contract_token=NEXT.token, contract_symbol=NEXT.symbol, contract_expiry=NEXT.expiry.isoformat(), sl_price=108.15,
                     lots=1, trade_counter=3)
    h._saved_state['helios'] = st.to_json()
    h.seed_position('helios', NEXT, -1, 105.0)
    run(h, hm(9, 30))
    assert made[-1].contract.token == NEXT.token and made[-1].state.status == 'in_trade' and not h.orders


def test_rejected_fallback_roll_close_is_retried_and_never_switches_contracts_early():
    from hestia_core.replay import BrokerReply
    h, made = world(HOLD_PATH, broker=lambda c: BrokerReply('reject') if c.request.request_id.endswith('roll_close-1') else BrokerReply('fill'))
    seed_bearish(h)
    run(h, hm(23, 40))
    e, s = made[-1], made[-1].state
    ids = [o.request_id.split('-', 2)[-1] for o in h.orders]
    assert 't1-roll_close-2' in ids and 't1-roll_open-1' not in ids
    assert held(h, OLD) == 0 and held(h, FRONT) == -1
    assert s.status == 'in_trade' and s.contract_token == FRONT.token and e.contract.token == FRONT.token


def test_rejected_coincident_flip_close_is_retried_as_an_exit_at_once():
    from hestia_core.replay import BrokerReply
    h, made = world(FLIP_PATH, broker=lambda c: BrokerReply('reject') if c.request.request_id.endswith('coin_close-1') else BrokerReply('fill'))
    seed_bearish(h)
    run(h, hm(13, 30))
    ids = [o.request_id.split('-', 2)[-1] for o in h.orders]
    assert ids[0] == 't1-flip_exit-1' and held(h, OLD) == 0
    assert made[-1].state.status == 'watching' and 't1-coin_open-1' not in ids


def test_rejected_missed_roll_close_is_retried_by_the_tick():
    from hestia_core.replay import BrokerReply
    h, made = world(HOLD_PATH, old=OLD_IN_WINDOW, broker=lambda c: BrokerReply('reject') if c.request.request_id.endswith('roll_close-1') else BrokerReply('fill'))
    seed_bearish(h, ref=OLD_IN_WINDOW, entry=100.0, counter=7)
    run(h, hm(9, 30))
    ids = [o.request_id.split('-', 2)[-1] for o in h.orders]
    assert 't7-roll_close-2' in ids and held(h, OLD_IN_WINDOW) == 0
    assert made[-1].state.roll_target is None
