"""
Fake Hestia, account side: paper versus live per engine, separate pools, and the ledger versus the broker's position book
(startup adoption of an overnight position, periodic mismatch alerts, paper ledgers excluded).
"""
from datetime import datetime, timedelta

import pytest

from hestia_fake_helpers import (FRONT, NEXT, OTHER_FRONT, SESSION_DATE, SPEC_YY, RecEngine, events_of, factory, minutes_frame,
                                 world)
from hestia_core.fake import ContractSpec, FakeConfig
from hestia_core.interface import (CloseRequest, Direction, ExitReason, OpenRequest, OutcomeStatus, RequestOutcome,
                                   SessionStart)

T0 = datetime(2026, 9, 3, 9, 0)


@pytest.fixture
def hestias():
    made = []

    def build(engines, **k):
        extra = [ContractSpec(OTHER_FRONT, lot_size=10, minutes=minutes_frame(50.0, 3))]
        h = world(engines, extra=extra, **k)
        h.set_price('T1', 100.0)
        h.set_price('T2', 101.0)
        h.set_price('U1', 50.0)
        made.append(h)
        return h
    yield build
    for h in made:
        h.close()


def go(h, seconds=120):
    h.start_session(SESSION_DATE)
    h.run_until(T0 + timedelta(seconds=seconds))


def opener(contract, lots=3, rid='o1'):
    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.submit(OpenRequest(rid, contract, Direction.BULLISH, lots, trade_ref=1))
    return act


def live_and_paper(log_a=None, log_b=None, act_a=None, act_b=None):
    return [('a', lambda: RecEngine('a', act=act_a, log=log_a)),
            ('b', lambda: RecEngine('b', spec=SPEC_YY, trade=OTHER_FRONT, act=act_b, log=log_b), {'paper': True})]


def test_a_paper_engine_fills_on_the_paper_side_and_a_live_engine_on_the_live_side(hestias):
    log_a, log_b = [], []
    h = hestias(live_and_paper(log_a, log_b, opener(FRONT), opener(OTHER_FRONT, 2)))
    go(h)
    assert [(o.engine, o.lots) for o in h.orders] == [('a', 3)]
    assert [(spec.engine, spec.lots) for _, _, spec in h.paper_orders] == [('b', 2)]
    assert h.held('a', FRONT) == 3 and h.held('b', OTHER_FRONT) == 2
    assert [o.status for o in events_of(log_b, RequestOutcome)] == [OutcomeStatus.FILLED]
    assert events_of(log_b, RequestOutcome)[0].opened.avg_price == 50.0, 'paper fills at the live price'
    assert 'U1' not in h._sim.book, 'the paper position never reaches the account book'


def test_reconciliation_leaves_a_paper_ledger_alone(hestias):
    h = hestias(live_and_paper(act_a=opener(FRONT), act_b=opener(OTHER_FRONT, 2)))
    go(h)
    h.run_until(T0 + timedelta(seconds=130))              # let any read that started before the fill finish and be discarded
    h.reconcile_ledger()
    h.run_for(10)
    assert h.ledger_mismatches == {} and h.alerts_for('critical') == []
    assert h.held('b', OTHER_FRONT) == 2, 'a periodic read must not correct a paper position to the empty account book'
    assert h.last_reconciled is not None


def test_a_live_ledger_that_disagrees_with_the_broker_book_raises_a_critical_alert(hestias):
    h = hestias(live_and_paper(act_a=opener(FRONT)))
    go(h)
    h._sim.seed('T1', 5, 100.0)                          # the broker now shows 5 lots: someone traded by hand
    h.reconcile_ledger()
    h.run_for(10)
    assert h.ledger_mismatches == {'T1': (3, 5)}
    (a,) = [x for x in h.alerts_for('critical') if 'ledger mismatch' in x.text]
    assert '+3' in a.text and '+5' in a.text and 'XX30OCT26FUT' in a.text
    assert h.held('a', FRONT) == 3, 'alert only: never auto-corrected'


def test_a_token_with_a_request_in_flight_is_skipped_by_reconciliation(hestias):
    from hestia_core.fake import BrokerReply
    h = hestias(live_and_paper(act_a=opener(FRONT)), broker=lambda c: BrokerReply('fill', latency=20.0))
    h.start_session(SESSION_DATE)
    h.run_until(T0 + timedelta(seconds=5))
    h.reconcile_ledger()
    h.run_for(5)
    assert h.alerts_for('critical') == [], 'the order is still being worked; ledger and book legitimately differ'


def test_periodic_reconciliation_finds_a_mismatch_on_its_own(hestias):
    h = hestias(live_and_paper(act_a=opener(FRONT)), config=FakeConfig(ledger_reconcile_interval_s=60.0))
    h.start_session(SESSION_DATE)
    h.kernel.at(T0 + timedelta(seconds=90), lambda: h._sim.seed('T1', 1, 100.0))
    h.run_until(T0 + timedelta(seconds=200))
    assert any('ledger mismatch' in a.text for a in h.alerts_for('critical'))
    assert all(a.ts > T0 + timedelta(seconds=90) for a in h.alerts_for('critical'))


def test_startup_adopts_an_overnight_broker_position_into_the_owning_live_engine(hestias):
    h = hestias(live_and_paper())
    h._sim.seed('T1', -4, 99.5)                          # carried overnight, ledger empty after a restart
    h.bootstrap_ledger()
    h.run_for(5)
    assert h.held('a', FRONT) == -4 and h._ledger[('a', 'T1')][1] == 99.5
    assert any('adopted' in a.text and a.engine == 'a' for a in h.alerts)
    h.reconcile_ledger()
    h.run_for(5)
    assert h.alerts_for('critical') == []


def test_startup_flags_a_broker_position_no_live_engine_owns(hestias):
    h = hestias(live_and_paper())
    h._sim.seed('U1', 2, 50.0)                           # YY belongs to a PAPER engine: not adoptable from the live book
    h.bootstrap_ledger()
    h.run_for(5)
    assert h.held('b', OTHER_FRONT) == 0
    assert any('no live engine owns' in a.text for a in h.alerts_for('critical'))


def test_paper_and_live_pools_are_separate(hestias):
    """500 of paper cash cannot fund three lots of margin 500 each, while the live account is unaffected."""
    log_a, log_b = [], []
    cfg = FakeConfig(paper_cash=500.0)
    h = hestias(live_and_paper(log_a, log_b, opener(FRONT, 3), opener(OTHER_FRONT, 3)), config=cfg)
    go(h)
    assert [o.status for o in events_of(log_a, RequestOutcome)] == [OutcomeStatus.FILLED]
    assert [o.status for o in events_of(log_b, RequestOutcome)] == [OutcomeStatus.MARGIN_REFUSED]
    assert h.paper_orders == [] and h.held('a', FRONT) == 3


def test_paper_margin_reservations_do_not_shrink_the_live_pool(hestias):
    """A paper entry in flight must not reserve against the live account's cash."""
    from hestia_core.fake import BrokerReply
    cfg = FakeConfig(available_cash=1600.0)                # live: room for 3 lots at 500 each, once
    h = hestias(live_and_paper(act_a=opener(FRONT, 3), act_b=opener(OTHER_FRONT, 3)), config=cfg)
    log = []
    go(h)
    assert h.held('a', FRONT) == 3 and h.held('b', OTHER_FRONT) == 3
    assert h.available_cash('a') == pytest.approx(1600.0 - 3 * 500.0)


def test_a_book_read_that_started_before_a_fill_is_not_compared_with_the_newer_ledger(hestias):
    """The snapshot is older than the ledger, so comparing them would raise a false mismatch: the read is discarded."""
    h = hestias(live_and_paper(act_a=opener(FRONT)), config=FakeConfig(ledger_reconcile_interval_s=None))
    h.start_session(SESSION_DATE)
    h.kernel.at(T0 - timedelta(milliseconds=200), h.reconcile_ledger)     # read starts, order fills 0.25 s after the open
    h.kernel.at(T0 + timedelta(milliseconds=10), h.reconcile_ledger)
    h.run_until(T0 + timedelta(seconds=10))
    assert h.alerts_for('critical') == [] and h.held('a', FRONT) == 3
