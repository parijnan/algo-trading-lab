"""
Fake Hestia, lifecycle side of the contract: crash containment and auto-resume, the restart protocol, silence alerts,
KILL / host kill / EXIT / session end, determinism, the one-thread-at-a-time guarantee and the deadlock guard.
"""
import json
import threading
import time
from datetime import datetime, timedelta

import pytest

from hestia_fake_helpers import (FRONT, NEXT, OTHER_FRONT, SESSION_DATE, SPEC_YY, DAYS, RecEngine, events_of, factory,
                                 minutes_frame, world)
import hestia_core.fake_kernel as fake_kernel
from hestia_core.fake import BrokerReply, ContractSpec, FakeConfig, FakeHestia
from hestia_core.fake_kernel import FakeHestiaDeadlock
from hestia_core.interface import (BarComplete, CloseRequest, CommandEvent, CommandKind, Direction, ExitReason,
                                   FlattenRequest, OpenRequest, OutcomeStatus, PendingRequest, RequestOutcome,
                                   SessionStart, Stop, StopReason, TRADE_RECORD_COLUMNS)

T0 = datetime(2026, 9, 3, 9, 0)
END = datetime(2026, 9, 3, 23, 40)


@pytest.fixture
def hestias():
    made = []

    def build(*a, **k):
        h = world(*a, **k)
        h.set_price('T1', 100.0)
        h.set_price('T2', 101.0)
        made.append(h)
        return h
    yield build
    for h in made:
        h.close()


def with_yy(h_kwargs=None):
    return [ContractSpec(OTHER_FRONT, lot_size=10, minutes=minutes_frame(50.0, 3))]


def open_req(rid='r1', lots=3, contract=FRONT):
    return OpenRequest(rid, contract, Direction.BULLISH, lots, trade_ref=1)


class ResumableEngine:
    """An engine following the restart protocol of plans/hestia-interface-spec.md: persist a PendingRequest BEFORE submit,
    on resume ask request_status and never send the same decision twice."""

    def __init__(self, shared, crash_when):
        self.shared, self.crash_when = shared, crash_when
        self.name, self.spec = 'a', __import__('hestia_fake_helpers').SPEC

    def run(self, ctx):
        raw = ctx.load_state()
        state = json.loads(raw) if raw else {'pending': None, 'applied': False}
        while True:
            ev = ctx.next_event(60)
            if isinstance(ev, SessionStart):
                ctx.set_trading_contract(FRONT)
                if state['pending'] is None and not state['applied']:
                    pr = PendingRequest('r1', 'open', 'entry', ctx.now().isoformat(), 1)
                    state['pending'] = pr.to_json()
                    ctx.save_state(json.dumps(state))
                    if self.crash_when == 'before_submit' and self.shared['crashes'] == 0:
                        self.shared['crashes'] += 1
                        raise RuntimeError('boom before submit')
                    ctx.submit(open_req('r1'))
                    if self.crash_when == 'after_submit' and self.shared['crashes'] == 0:
                        self.shared['crashes'] += 1
                        raise RuntimeError('boom after submit')
                elif state['pending'] is not None:
                    pr = PendingRequest.from_json(state['pending'])
                    st = ctx.request_status(pr.request_id)
                    self.shared['resume_saw'].append(None if st is None else st.status)
                    if st is None:
                        ctx.submit(open_req(pr.request_id))
                    elif st.confirmed:
                        state.update(pending=None, applied=True)
                        ctx.save_state(json.dumps(state))
            if isinstance(ev, RequestOutcome) and ev.confirmed and state['pending'] is not None:
                state.update(pending=None, applied=True)
                ctx.save_state(json.dumps(state))
                self.shared['applied'] += 1
            if isinstance(ev, Stop):
                return


def _resumable(crash_when, latency=0.2):
    shared = {'crashes': 0, 'resume_saw': [], 'applied': 0}
    h_broker = lambda c: BrokerReply('fill', latency=latency)      # noqa: E731
    return shared, h_broker


@pytest.mark.parametrize('crash_when,latency,expected_resume', [
    ('after_submit', 0.2, [OutcomeStatus.FILLED]),        # fill landed while the engine was down
    ('after_submit', 30.0, [OutcomeStatus.IN_FLIGHT]),    # still being worked when the engine came back
    ('before_submit', 0.2, [None]),                       # the request never reached Hestia
])
def test_restarted_engine_resumes_from_saved_state_without_a_second_order(hestias, crash_when, latency, expected_resume):
    shared, broker = _resumable(crash_when, latency)
    h = hestias([('a', lambda: ResumableEngine(shared, crash_when))], broker=broker)
    h.start_session(SESSION_DATE)
    h.run_until(T0 + timedelta(seconds=120))
    assert shared['crashes'] == 1 and shared['resume_saw'] == expected_resume
    assert len(h.orders) == 1 and h.held('a', FRONT) == 3
    assert any('auto-resuming' in a.text for a in h.alerts_for('warning'))
    state = json.loads(h._saved_state['a'])
    assert state['pending'] is None and state['applied'] is True, 'the engine ended up having applied the outcome exactly once'


def test_engine_that_keeps_crashing_is_contained_then_left_failed(hestias):
    launches, log_b = [], []

    class Crasher:
        name, spec = 'a', __import__('hestia_fake_helpers').SPEC

        def run(self, ctx):
            ev = ctx.next_event(60)
            launches.append(ctx.now())
            raise RuntimeError('always')

    h = hestias([('a', Crasher), ('b', lambda: RecEngine('b', spec=SPEC_YY, trade=OTHER_FRONT, log=log_b))],
                extra=with_yy())
    h.set_price('U1', None)
    h.seed_position('a', FRONT, 2, 98.0)
    h.start_session(SESSION_DATE)
    h.run_until(END)
    assert [(t - T0).total_seconds() for t in launches] == [0.0, 5.0, 35.0, 155.0]
    assert h.engine_state['a'] == 'failed' and h.engine_state['b'] == 'running'
    crit = h.alerts_for('critical', 'a')
    assert any('FAILED' in a.text for a in crit)
    assert sum('open position' in a.text for a in crit) >= 3, 'a failed engine holding a position keeps alerting'
    assert len(events_of(log_b, BarComplete)) == 58, 'the healthy engine never noticed'


def test_crash_count_resets_after_the_window(hestias):
    launches = []

    class Flaky:
        name, spec = 'a', __import__('hestia_fake_helpers').SPEC

        def run(self, ctx):
            n = len(launches)
            launches.append(ctx.now())
            ctx.set_trading_contract(FRONT)
            while True:
                ev = ctx.next_event(60)
                if ev is None:
                    continue
                if isinstance(ev, SessionStart) and n in (0, 1, 2):
                    raise RuntimeError('early')
                if isinstance(ev, BarComplete) and ev.boundary_ts == datetime(2026, 9, 3, 12, 0) and n == 3:
                    raise RuntimeError('late')
                if isinstance(ev, Stop):
                    return

    h = hestias([('a', Flaky)], config=FakeConfig(restart_window_s=1800.0))
    h.start_session(SESSION_DATE)
    h.run_until(END)
    assert len(launches) == 5 and h.engine_state['a'] == 'running', 'a crash three hours later starts a fresh count'
    assert launches[4] - datetime(2026, 9, 3, 12, 0) == timedelta(seconds=5)


def test_silent_engine_alerts_warn_then_critical_and_repeats_while_a_healthy_one_stays_quiet(hestias):
    log_b = []

    class Hung:
        name, spec = 'a', __import__('hestia_fake_helpers').SPEC

        def run(self, ctx):
            ctx.next_event(1)
            ctx.wait(10_000)

    h = hestias([('a', Hung), ('b', lambda: RecEngine('b', spec=SPEC_YY, trade=OTHER_FRONT, log=log_b, timeout=30))],
                extra=with_yy())
    h.start_session(SESSION_DATE)
    h.run_until(T0 + timedelta(minutes=21))
    warn = h.alerts_for('warning', 'a')
    crit = h.alerts_for('critical', 'a')
    assert len(warn) == 1 and timedelta(seconds=180) <= warn[0].ts - T0 < timedelta(seconds=240)
    gaps = [(b.ts - a.ts).total_seconds() for a, b in zip(crit, crit[1:])]
    assert len(crit) >= 4 and crit[0].ts - T0 >= timedelta(seconds=300) and all(g >= 300 for g in gaps)
    assert h.alerts_for(engine='b') == []


def test_kill_leaves_the_position_alone_abandons_queued_work_and_spares_other_engines(hestias):
    log_a, log_b = [], []

    def act_a(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.submit(open_req('in-flight', 2, FRONT))
            ctx.submit(open_req('queued', 1, NEXT))
    h = hestias([('a', lambda: RecEngine('a', act=act_a, log=log_a)),
                 ('b', lambda: RecEngine('b', spec=SPEC_YY, trade=OTHER_FRONT, log=log_b))],
                extra=with_yy(), config=FakeConfig(workers=1),
                broker=lambda c: BrokerReply('fill', latency=10.0))
    h.start_session(SESSION_DATE)
    h.kernel.at(T0 + timedelta(seconds=2), lambda: h.send_command('a', CommandKind.KILL))
    h.run_until(END)
    stops = events_of(log_a, Stop)
    assert len(stops) == 1 and stops[0].reason == StopReason.KILL and stops[0].leave_position
    st = {o.request_id: o.status for _, _, o in h.outcome_log}
    assert st == {'queued': OutcomeStatus.ABANDONED, 'in-flight': OutcomeStatus.FILLED}
    assert h.held('a', FRONT) == 2 and h.held('a', NEXT) == 0, 'the in-flight order finished; nothing was cancelled or reversed'
    assert h.engine_state['a'] == 'killed' and h.engine_state['b'] == 'running'
    assert len(events_of(log_b, BarComplete)) == 58 and events_of(log_b, Stop) == []
    assert len(events_of(log_a, BarComplete)) < 5


def test_host_kill_stops_every_engine_and_leaves_positions(hestias):
    log_a, log_b = [], []
    h = hestias([('a', lambda: RecEngine('a', log=log_a)), ('b', lambda: RecEngine('b', spec=SPEC_YY, trade=OTHER_FRONT, log=log_b))],
                extra=with_yy())
    h.seed_position('a', FRONT, 2, 98.0)
    h.start_session(SESSION_DATE)
    h.kernel.at(T0 + timedelta(minutes=30), h.host_kill)
    h.run_until(END)
    for log in (log_a, log_b):
        (stop,) = events_of(log, Stop)
        assert stop.reason == StopReason.HOST_KILL and stop.leave_position
    assert h.orders == [] and h.held('a', FRONT) == 2


def test_exit_command_reaches_the_engine_which_flattens_and_keeps_running(hestias):
    log = []

    def act(eng, ctx, ev):
        if isinstance(ev, CommandEvent) and ev.kind == CommandKind.EXIT:
            ctx.submit(FlattenRequest('exit-1', FRONT, ExitReason.MANUAL_EXIT))
    h = hestias([('a', factory(log=log, act=act))])
    h.seed_position('a', FRONT, 3, 98.0)
    h.start_session(SESSION_DATE)
    h.kernel.at(T0 + timedelta(minutes=5), lambda: h.send_command('a', CommandKind.EXIT))
    h.run_until(END)
    assert h.held('a', FRONT) == 0 and [o.side for o in h.orders] == ['SELL']
    assert h.engine_state['a'] == 'running' and len(events_of(log, BarComplete)) == 58


def test_session_end_stops_engines_and_the_next_session_relaunches_them_from_saved_state(hestias):
    launches = []

    class Daily:
        name, spec = 'a', __import__('hestia_fake_helpers').SPEC

        def run(self, ctx):
            launches.append((ctx.now(), ctx.load_state()))
            ctx.set_trading_contract(FRONT)
            while True:
                ev = ctx.next_event(60)
                if isinstance(ev, SessionStart):
                    ctx.save_state(f'state-of-{ev.session_date}')
                if isinstance(ev, Stop):
                    assert ev.reason == StopReason.SESSION_END
                    return
    h = hestias([('a', Daily)], start=datetime(2026, 9, 2, 8, 50))
    h.start_session(DAYS[1])
    h.end_session(datetime(2026, 9, 2, 23, 31))
    h.run_until(datetime(2026, 9, 2, 23, 45))
    assert h.engine_state['a'] == 'ended'
    h.start_session(DAYS[2])
    h.run_until(datetime(2026, 9, 3, 9, 30))
    assert [x[1] for x in launches] == [None, 'state-of-2026-09-02']
    assert h.engine_state['a'] == 'running'


def test_two_runs_of_the_same_scenario_are_identical(hestias):
    def build():
        log_a = []

        def act(eng, ctx, ev):
            if isinstance(ev, SessionStart):
                ctx.submit(open_req('o1', 2, FRONT))
            if isinstance(ev, BarComplete) and ev.boundary_ts == datetime(2026, 9, 3, 10, 0):
                ctx.submit(CloseRequest('c1', FRONT, Direction.BULLISH, ExitReason.TREND_FLIP))
        h = hestias([('a', factory(log=log_a, act=act)),
                     ('b', lambda: RecEngine('b', spec=SPEC_YY, trade=OTHER_FRONT))], extra=with_yy(),
                    broker=lambda c: BrokerReply('reject') if (c.request.request_id, c.attempt) == ('o1', 1) else BrokerReply('fill'))
        h.bar_delay[('T1', datetime(2026, 9, 3, 12, 0))] = 20.0
        h.start_session(SESSION_DATE)
        h.run_until(END)
        return h

    first, second = build(), build()
    assert first.event_log == second.event_log and len(first.event_log) > 100
    assert [(o.order_id, o.ts, o.request_id, o.lots) for o in first.orders] == \
           [(o.order_id, o.ts, o.request_id, o.lots) for o in second.orders]
    assert [(t, e, o.request_id, o.status) for t, e, o in first.outcome_log] == \
           [(t, e, o.request_id, o.status) for t, e, o in second.outcome_log]


def test_only_one_engine_thread_runs_at_a_time(hestias):
    state = {'active': 0, 'max': 0, 'steps': 0}

    class Busy:
        spec = __import__('hestia_fake_helpers').SPEC

        def __init__(self, name, spec):
            self.name, self.spec = name, spec

        def run(self, ctx):
            ctx.next_event(1)
            for _ in range(40):
                state['active'] += 1
                state['max'] = max(state['max'], state['active'])
                time.sleep(0.002)                        # real time passing inside a step: concurrency would overlap here
                state['steps'] += 1
                state['active'] -= 1
                ctx.wait(1)
    h = hestias([('a', lambda: Busy('a', __import__('hestia_fake_helpers').SPEC)), ('b', lambda: Busy('b', SPEC_YY))], extra=with_yy())
    h.start_session(SESSION_DATE)
    h.run_until(T0 + timedelta(minutes=5))
    assert state['steps'] == 80 and state['max'] == 1


def test_an_engine_that_blocks_on_the_real_clock_fails_fast(hestias, monkeypatch):
    monkeypatch.setattr(fake_kernel, 'WALL_TIMEOUT_S', 0.5)

    class Sleeper:
        name, spec = 'a', __import__('hestia_fake_helpers').SPEC

        def run(self, ctx):
            threading.Event().wait(3.0)                   # real waiting, not ctx.wait
    h = hestias([('a', Sleeper)])
    h.start_session(SESSION_DATE)
    started = time.monotonic()
    with pytest.raises(FakeHestiaDeadlock, match='did not yield'):
        h.run_until(T0 + timedelta(minutes=1))
    assert time.monotonic() - started < 2.0


def test_report_trade_reindexes_to_the_prometheus_columns_and_alerts_are_tagged(hestias):
    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.report_trade({'trade_id': 7, 'direction': 'bullish', 'units': 2, 'not_a_column': 1})
            ctx.alert('info', 'hello', channel='trade-alerts')
    h = hestias([('a', factory(act=act))])
    h.start_session(SESSION_DATE)
    h.run_until(T0 + timedelta(minutes=1))
    (name, rec), = h.trades
    assert name == 'a' and tuple(rec) == TRADE_RECORD_COLUMNS
    assert rec['trade_id'] == 7 and rec['lot2_pnl_rs'] is None and 'not_a_column' not in rec
    assert any('not_a_column' in w for w in h.warnings)
    assert [(a.level, a.engine, a.channel) for a in h.alerts_for(engine='a') if a.text == 'hello'] == [('info', 'a', 'trade-alerts')]


def test_an_engine_that_resubmits_on_every_outcome_fails_fast_instead_of_hanging(hestias):
    counter = {'n': 0}

    def act(eng, ctx, ev):
        if isinstance(ev, (SessionStart, RequestOutcome)):
            counter['n'] += 1
            ctx.submit(OpenRequest(f'loop-{counter["n"]}', FRONT, Direction.BULLISH, 1, trade_ref=counter['n']))
    h = hestias([('a', factory(act=act))], config=FakeConfig(available_cash=1e12))
    h.kernel.max_callbacks = 5_000
    h.start_session(SESSION_DATE)
    started = time.monotonic()
    with pytest.raises(FakeHestiaDeadlock, match='runaway simulation'):
        h.run_until(END)
    assert time.monotonic() - started < 10.0


def test_a_loop_that_never_advances_simulated_time_fails_fast(hestias):
    class Spinner:
        name, spec = 'a', __import__('hestia_fake_helpers').SPEC

        def run(self, ctx):
            while True:
                ctx.wait(0)
    h = hestias([('a', Spinner)])
    h.kernel.max_same_time = 500
    h.start_session(SESSION_DATE)
    with pytest.raises(FakeHestiaDeadlock, match='callbacks at this instant'):
        h.run_until(END)
