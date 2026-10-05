"""P5.4: the recorded live days replayed through the Prometheus engine on the fake Hestia (Tier 1), and the injected-fault cases on top
of the recorded days. Skips itself when the pulled days (hestia_data/replay_pull) or the pipeline price file are absent. 2026-09-15 is a
known-bad day (an expired token, not a fault case) and is not replayed."""
import dataclasses
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from hestia_core import recorded as rec  # noqa: E402
from hestia_core.interface import CommandKind  # noqa: E402
from hestia_core.replay import BrokerReply  # noqa: E402
from prometheus_engine import replay_check as rc  # noqa: E402
from prometheus_engine.engine import PrometheusEngine  # noqa: E402

PULL = REPO / 'hestia_data' / 'replay_pull'
PIPE = REPO / 'data_pipeline' / 'data' / 'mcx'
needs_pull = pytest.mark.skipif(not (PULL / 'logs').exists() or not (PIPE / 'CRUDEOILM' / '2026-10-19_futures.csv').exists(),
                                reason='the pulled recorded days (hestia_data/replay_pull) are not present')
DAYS = [date(2026, 9, d) for d in (16, 17, 18, 21, 22, 23, 24, 25, 28)]


@pytest.fixture(scope='module')
def world():
    sessions = rec.load_sessions(PULL)
    frames = rec.load_minute_files(PIPE, 'CRUDEOILM')
    trades = rec.load_trades(PULL / 'data' / 'prometheus_trades.csv')
    return sessions, frames, trades


from contextlib import contextmanager  # noqa: E402


@contextmanager
def replayed(world, day, **kw):
    """Replay `day`; yields (report, fake hestia, engine instances made) and closes the fake afterwards."""
    sessions, frames, trades = world
    h, made = rc.replay_day(day, sessions, frames, trades, **kw)
    try:
        yield rc.compare(day, rc.live_decisions(next(s for s in sessions if s.day == day)), rc.replay_decisions(h)), h, made
    finally:
        h.close()


def at(day, hms):
    return datetime.combine(day, datetime.strptime(hms, '%H:%M:%S').time())


# ---------------------------------------------------------------------------------------------------------------------
# Tier 1: decision for decision against live
# ---------------------------------------------------------------------------------------------------------------------

@needs_pull
@pytest.mark.parametrize('day', DAYS, ids=lambda d: d.isoformat())
def test_every_live_decision_of_the_day_is_reproduced_and_nothing_extra(world, day):
    sessions, frames, trades = world
    rep = rc.check_day(day, sessions, frames, trades)
    assert rep.exact, rep.differences
    assert rep.matched == len(rep.live) == len(rep.replay) > 0
    assert max(rep.price_gaps) < 12.0                       # fills differ by the 1-minute price model, never by a wild margin


@needs_pull
def test_the_recorded_window_covers_a_stop_a_target_scale_out_and_rule_7_flips(world):
    sessions, frames, trades = world
    reasons = {(d.lot, d.reason) for s in sessions if s.day in DAYS for d in rc.live_decisions(s) if d.kind == 'exit'}
    assert {(1, 'stop_loss'), (2, 'stop_loss'), (1, 'target1'), (2, 'target2_flat_pct'), (1, 'trend_flip'), (2, 'trend_flip')} <= reasons


@needs_pull
def test_the_comparison_is_not_trivially_permissive(world):
    """With the first target moved from 2.2% to 1.0% the replay must differ from live (an engine that ignored its levels would not)."""
    sessions, frames, trades = world
    rep = rc.check_day(date(2026, 9, 24), sessions, frames, trades, cfg=dataclasses.replace(rc.RECORDED_CONFIG, target1_pct=1.0))
    assert not rep.exact and any('target1' in d for d in rep.differences)


# ---------------------------------------------------------------------------------------------------------------------
# Injected faults on recorded days
# ---------------------------------------------------------------------------------------------------------------------

def _baseline_orders(world, day):
    sessions, frames, trades = world
    return rc.check_day(day, sessions, frames, trades).replay_orders


@needs_pull
def test_engine_crash_mid_trade_resumes_without_a_second_order(world):
    sessions, frames, trades = world
    crashed = {'n': 0}

    class Crashy(PrometheusEngine):
        def _tick(self):
            if self.state.status == 'in_trade' and not crashed['n'] and self._now() >= at(date(2026, 9, 24), '14:00:00'):
                crashed['n'] += 1
                raise RuntimeError('injected crash')
            super()._tick()
    day = date(2026, 9, 24)
    with replayed(world, day, engine_factory=lambda: Crashy(rc.RECORDED_CONFIG)) as (rep, h, made):
        assert crashed['n'] == 1 and len(made) >= 3                    # the probe, the first life, the resumed one
        assert rep.exact, rep.differences
        assert len(h.orders) == _baseline_orders(world, day)           # resumed from its saved state: not one order more


@needs_pull
@pytest.mark.parametrize('day, kill, restart', [(date(2026, 9, 21), '15:40:47', '15:42:20'), (date(2026, 9, 22), '09:35:21', '09:36:48')],
                         ids=['09-21', '09-22'])
def test_the_recorded_kill_switch_and_restart_with_a_position_open(world, day, kill, restart):
    """Live killed the process on these days (position left open) and restarted it minutes later; the engine must carry on from its
    saved state and take exactly the decisions live took afterwards."""
    sessions, frames, trades = world

    def setup(h):
        h.kernel.at(at(day, kill), lambda: h.send_command('prometheus', CommandKind.KILL))

        def relaunch():
            h.engine_state['prometheus'] = 'ended'
            h._launch('prometheus')
        h.kernel.at(at(day, restart), relaunch)
    with replayed(world, day, setup=setup) as (rep, h, made):
        assert len(made) >= 3                                          # the probe, the killed life, the relaunched one
        assert made[-1].state.trade_counter >= 31                      # it came back with the saved state, not a blank one
        assert rep.exact, rep.differences


@needs_pull
def test_a_rejected_first_exit_attempt_is_retried_and_the_day_still_reproduces(world):
    sessions, frames, trades = world
    day = date(2026, 9, 24)

    def setup(h):
        h.broker_behavior = lambda call: BrokerReply('reject') if call.request.request_id.endswith('-lot1-1') else BrokerReply('fill')
    with replayed(world, day, setup=setup) as (rep, h, made):
        ids = [o.request_id for o in h.orders]
        assert rep.exact, rep.differences
        assert any(i.endswith('-lot1-2') for i in ids) and not any(i.endswith('-lot1-1') for i in ids)      # the first never reached the book


@needs_pull
def test_an_unconfirmed_exit_is_never_sent_twice_and_the_day_still_reproduces(world):
    """The fill happened at the broker but its confirmation is lost for 90 s (past the 30 s fill-wait, so the engine is told UNCONFIRMED): the engine waits for Hestia's settlement."""
    sessions, frames, trades = world
    day = date(2026, 9, 24)

    def setup(h):
        h.broker_behavior = lambda call: (BrokerReply('unconfirmed', resolve_after=90.0) if call.request.request_id.endswith('-lot1-1')
                                          else BrokerReply('fill'))
    with replayed(world, day, setup=setup) as (rep, h, made):
        assert rep.exact, rep.differences
        assert any('not confirmed' in a.text for a in h.alerts)                # the unconfirmed path really ran
        first = next(o for o in h.orders if o.request_id.endswith('-lot1-1'))
        booked = next(d for d in rc.replay_decisions(h) if d.kind == 'exit' and d.lot == 1 and d.reason == 'target1')
        assert (booked.ts - first.ts).total_seconds() >= 60.0                  # booked only when Hestia settled it (about 90 s), not on a guess
        assert sum(1 for o in h.orders if '-lot1-' in o.request_id) == 2       # target1 on 13:45 and 20:57 (two trades), one order each
        s = made[-1].state
        held = h._ledger.get(('prometheus', rc.REF.token), [0])[0]
        want = 0 if s.status != 'in_trade' else (1 if s.direction == 'bullish' else -1) * s.open_lots()
        assert held == want                                                    # the engine and the ledger agree at the end of the day


@needs_pull
def test_four_consecutive_days_chained_on_one_fake_hestia_carry_the_engine_state_and_the_ledger(world):
    """Only the first day is seeded; the engine's saved state, the watermark and the ledger carry across the nights, and the
    end-of-window position is the one live Prometheus held when the window closed (trade 46, bearish 5 units at 8910)."""
    sessions, frames, trades = world
    reports, h, made = rc.check_chain([date(2026, 9, d) for d in (22, 23, 24, 25)], sessions, frames, trades)
    try:
        for rep in reports:
            assert rep.exact, (rep.day, rep.differences)
        assert sum(r.matched for r in reports) == 7 + 12 + 15 + 9
        s = made[-1].state
        assert (s.status, s.direction, s.units, s.trade_counter) == ('in_trade', 'bearish', 5, 46)
        assert s.entry_price == pytest.approx(8910.0, abs=6.0)
        assert h._ledger[('prometheus', rc.REF.token)][0] == -10
    finally:
        h.close()
