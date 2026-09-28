"""
The live runtime, in real time and on real threads: the reactor, engine threads with a locked context, the same contract
behaviours as the fake (fills, crash containment, KILL, silence alerts, priority, idempotency under concurrent threads) and
the teardown order. Durations are tens of milliseconds; nothing here talks to a broker.
"""
import os
import signal
import threading
import time
from datetime import datetime, timedelta

import pytest

from hestia_live_helpers import FRONT, NEXT, YY, Live, wait_until
from hestia_fake_helpers import SPEC, SPEC_YY, RecEngine, events_of
from hestia_core.fake_kernel import TaskAborted
from hestia_core.interface import (AckStatus, CloseRequest, CommandKind, Direction, ExitReason, OpenRequest, OutcomeStatus,
                                   RequestOutcome, SessionStart, Stop, StopReason)
from hestia_core.lifecycle import Lifecycle, LifecycleConfig
from hestia_core.reactor import RealReactor
from hestia_core.replay import BrokerReply
from hestia_core.thread_runner import LockedContext


def submit_on_start(*reqs, acks=None):
    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            for r in reqs:
                a = ctx.submit(r)
                if acks is not None:
                    acks.append(a)
    return act


def open_req(rid='r1', lots=3, contract=FRONT):
    return OpenRequest(rid, contract, Direction.BULLISH, lots, trade_ref=1)


# ---- the reactor -------------------------------------------------------------------------------------------------------

def test_reactor_runs_callbacks_in_time_order_on_one_thread_holding_the_lock():
    r = RealReactor()
    r.start()
    seen, threads, locked = [], set(), []
    try:
        r.after(0.10, lambda: seen.append('c'))
        r.after(0.02, lambda: seen.append('a'))
        r.after(0.05, lambda: (seen.append('b'), threads.add(threading.current_thread().name),
                               locked.append(r.lock._is_owned())))
        wait_until(lambda: len(seen) == 3)
        assert seen == ['a', 'b', 'c'] and threads == {'hestia-reactor'} and locked == [True]
    finally:
        r.stop()


def test_post_from_other_threads_is_safe_and_runs_on_the_dispatcher():
    r = RealReactor()
    r.start()
    got = []
    try:
        ts = [threading.Thread(target=lambda i=i: r.post(lambda: got.append((i, threading.current_thread().name))))
              for i in range(50)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        wait_until(lambda: len(got) == 50)
        assert {name for _, name in got} == {'hestia-reactor'} and sorted(i for i, _ in got) == list(range(50))
    finally:
        r.stop()


def test_a_cancelled_callback_does_not_run_and_a_raising_one_does_not_kill_the_dispatcher():
    errors, seen = [], []
    r = RealReactor(on_error=errors.append)
    r.start()
    try:
        h = r.after(0.05, lambda: seen.append('cancelled'))
        h.cancel()
        r.after(0.02, lambda: (_ for _ in ()).throw(RuntimeError('boom')))
        r.after(0.08, lambda: seen.append('after'))
        wait_until(lambda: seen == ['after'])
        assert len(errors) == 1 and isinstance(errors[0], RuntimeError) and r.alive()
    finally:
        r.stop()
    assert not r.alive()


def test_locked_context_takes_the_core_lock_except_for_the_blocking_calls():
    import threading as th
    lock = th.RLock()
    owned = {}

    class Ctx:
        def submit(self, x):
            owned['submit'] = lock._is_owned()

        def next_event(self, t):
            owned['next_event'] = lock._is_owned()

        def wait(self, s):
            owned['wait'] = lock._is_owned()

        def now(self):
            owned['now'] = lock._is_owned()
    c = LockedContext(Ctx(), lock)
    c.submit(1), c.next_event(0), c.wait(0), c.now()
    assert owned == {'submit': True, 'next_event': False, 'wait': False, 'now': False}


# ---- contract behaviours in real time ------------------------------------------------------------------------------------

def test_an_open_fills_through_the_live_core_and_the_outcome_reaches_the_engine_thread():
    log, acks = [], []
    with Live([('a', lambda: RecEngine('a', act=submit_on_start(open_req(), acks=acks), log=log))]) as live:
        wait_until(lambda: events_of(log, RequestOutcome), what='the outcome')
        (o,) = events_of(log, RequestOutcome)
        assert [a.status for a in acks] == [AckStatus.ACCEPTED]
        assert o.status == OutcomeStatus.FILLED and o.opened.lots == 3 and o.opened.avg_price == 100.0
        assert live.held('a', FRONT) == 3 and [(x.side, x.lots) for x in live.sim.orders] == [('BUY', 3)]


def test_a_crashing_engine_is_contained_and_resumed_while_the_other_keeps_running():
    launches, log_b = [], []

    class Flaky:
        name, spec = 'a', SPEC

        def run(self, ctx):
            n = len(launches)
            launches.append(time.monotonic())
            ctx.next_event(1.0)
            if n == 0:
                raise RuntimeError('first launch dies')
            while True:
                if isinstance(ctx.next_event(0.05), Stop):
                    return
    with Live([('a', Flaky), ('b', lambda: RecEngine('b', spec=SPEC_YY, trade=YY, log=log_b, timeout=0.05))]) as live:
        wait_until(lambda: len(launches) == 2, what='the auto-resume')
        assert 0.04 <= launches[1] - launches[0] < 1.5
        assert any('auto-resuming' in a.text for a in live.core.alerts_for('warning'))
        assert live.core.engine_state['b'] == 'running' and live.core.engine_state['a'] == 'running'
        assert len(events_of(log_b, SessionStart)) == 1, 'the healthy engine was never restarted'


def test_an_engine_that_keeps_crashing_is_left_failed_with_a_critical_alert():
    launches = []

    class Always:
        name, spec = 'a', SPEC

        def run(self, ctx):
            launches.append(1)
            ctx.next_event(1.0)
            raise RuntimeError('always')
    with Live([('a', Always)]) as live:
        wait_until(lambda: live.core.engine_state['a'] == 'failed', what='FAILED')
        assert len(launches) == 4 and any('FAILED' in a.text for a in live.core.alerts_for('critical'))


def test_kill_abandons_queued_work_lets_the_in_flight_order_finish_and_stops_only_that_engine():
    log_a, log_b = [], []

    def act_a(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.submit(open_req('in-flight', 2, FRONT))
            ctx.submit(open_req('queued', 1, NEXT))
    with Live([('a', lambda: RecEngine('a', act=act_a, log=log_a, timeout=0.05)),
               ('b', lambda: RecEngine('b', spec=SPEC_YY, trade=YY, log=log_b, timeout=0.05))],
              broker=lambda c: BrokerReply('fill', latency=0.4), workers=1) as live:
        wait_until(lambda: live.sim.orders, what='the in-flight order to be placed')
        with live.reactor.lock:
            live.core.send_command('a', CommandKind.KILL)
        wait_until(lambda: live.held('a', FRONT) == 2, what='the in-flight order to finish')
        st = {o.request_id: o.status for _, _, o in live.core.outcome_log}
        assert st == {'queued': OutcomeStatus.ABANDONED, 'in-flight': OutcomeStatus.FILLED}
        (stop,) = events_of(log_a, Stop)
        assert stop.reason == StopReason.KILL and stop.leave_position
        assert live.core.engine_state['a'] == 'killed' and live.core.engine_state['b'] == 'running'
        assert events_of(log_b, Stop) == []


def test_a_silent_engine_alerts_warning_then_critical_and_a_healthy_one_stays_quiet():
    class Hung:
        name, spec = 'a', SPEC

        def run(self, ctx):
            ctx.next_event(0.05)
            try:
                ctx.wait(10)
            except TaskAborted:
                raise
    with Live([('a', Hung), ('b', lambda: RecEngine('b', spec=SPEC_YY, trade=YY, timeout=0.05))]) as live:
        wait_until(lambda: len(live.core.alerts_for('critical', 'a')) >= 2, what='repeated critical alerts')
        assert len(live.core.alerts_for('warning', 'a')) == 1
        assert live.core.alerts_for(engine='b') == []


def test_concurrent_engine_threads_resubmitting_the_same_ids_place_each_order_once():
    acks = {'a': [], 'b': []}
    barrier = threading.Barrier(2)

    def burst(contract, name):
        def act(eng, ctx, ev):
            if isinstance(ev, SessionStart):
                barrier.wait(5)
                for round_ in range(2):
                    for i in range(15):
                        acks[name].append(ctx.submit(OpenRequest(f'{name}{i}', contract, Direction.BULLISH, 1, trade_ref=i)))
        return act
    with Live([('a', lambda: RecEngine('a', act=burst(FRONT, 'a'), timeout=0.05)),
               ('b', lambda: RecEngine('b', spec=SPEC_YY, trade=YY, act=burst(YY, 'b'), timeout=0.05))],
              workers=4) as live:
        wait_until(lambda: live.held('a', FRONT) == 15 and live.held('b', YY) == 15, what='all fills')
        for name in acks:
            statuses = [a.status for a in acks[name]]
            assert statuses.count(AckStatus.ACCEPTED) == 15 and statuses.count(AckStatus.DUPLICATE) == 15
        assert len(live.sim.orders) == 30


def test_a_stop_is_served_before_an_entry_submitted_at_the_same_moment_by_another_thread():
    barrier = threading.Barrier(2)
    stop = CloseRequest('stop', FRONT, Direction.BULLISH, ExitReason.STOP_LOSS)
    entry = OpenRequest('entry', YY, Direction.BULLISH, 2, trade_ref=1)

    def act(req):
        def go(eng, ctx, ev):
            if isinstance(ev, SessionStart):
                barrier.wait(5)
                ctx.submit(req)
        return go
    with Live([('b', lambda: RecEngine('b', spec=SPEC_YY, trade=YY, act=act(entry), timeout=0.05)),
               ('a', lambda: RecEngine('a', act=act(stop), timeout=0.05))], workers=1, dispatch_window_s=0.1) as live:
        with live.reactor.lock:
            live.core._ledger[('a', 'T1')] = [2, 98.0, live.reactor.now]
            live.sim.seed('T1', 2, 98.0)
        wait_until(lambda: len(live.sim.orders) == 2, what='both orders')
        assert {r for _, r in live.core.dispatch_log[0][1]} == {'stop', 'entry'}, 'both must be queued at one dispatch'
        assert [o.request_id for o in live.sim.orders] == ['stop', 'entry']


def test_context_calls_from_an_engine_thread_race_safely_with_the_dispatcher():
    errors = []

    def hammer(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            try:
                for i in range(300):
                    ctx.position(FRONT), ctx.margin(), ctx.sizing(), ctx.save_state(str(i)), ctx.load_state()
                    ctx.request_status(f'x{i % 5}')
                    if i % 20 == 0:
                        ctx.submit(OpenRequest(f'h{i}', FRONT, Direction.BULLISH, 1, trade_ref=i))
            except Exception as exc:                        # noqa: BLE001
                errors.append(exc)
    with Live([('a', lambda: RecEngine('a', act=hammer, timeout=0.05))]) as live:
        wait_until(lambda: live.held('a', FRONT) == 15, what='the 15 fills')
        assert errors == [] and live.reactor.alive()


# ---- teardown -----------------------------------------------------------------------------------------------------------

class Rig:
    def __init__(self, engines, lifecycle_cfg=None, **live_kwargs):
        self.live = Live(engines, **live_kwargs).start()
        self.calls = []
        self.lc = Lifecycle(self.live.core, self.live.reactor, self.live.executor,
                            terminate=lambda: self.calls.append(('terminate', time.monotonic())),
                            flush=lambda t: (self.calls.append(('flush', time.monotonic())), True)[1],
                            cfg=lifecycle_cfg or LifecycleConfig(engine_join_timeout_s=1.0, drain_timeout_s=1.0,
                                                                 poll_s=0.01))

    def names(self):
        return [c[0] for c in self.calls]


def test_teardown_stops_engines_then_flushes_then_terminates_once():
    finished = []

    class Tracked(RecEngine):
        def run(self, ctx):
            try:
                super().run(ctx)
            finally:
                time.sleep(0.05)
                finished.append(time.monotonic())
    rig = Rig([('a', lambda: Tracked('a', timeout=0.05)), ('b', lambda: Tracked('b', spec=SPEC_YY, trade=YY, timeout=0.05))])
    try:
        wait_until(lambda: all(s == 'running' for s in rig.live.core.engine_state.values()))
        report = rig.lc.teardown(StopReason.SESSION_END)
        assert rig.names() == ['flush', 'terminate'] and report.terminated and report.flushed
        assert len(finished) == 2 and max(finished) <= rig.calls[0][1], 'both engines finished before the flush and terminate'
        assert report.steps == ['stop_engines', 'engines_finished', 'drained', 'flushed', 'terminated', 'reactor_stopped']
        assert not rig.live.reactor.alive()
        assert rig.lc.teardown() is report and rig.names() == ['flush', 'terminate'], 'a second teardown does nothing'
    finally:
        rig.live.close()


def test_an_engine_crash_never_terminates_the_session():
    class Always:
        name, spec = 'a', SPEC

        def run(self, ctx):
            ctx.next_event(1.0)
            raise RuntimeError('always')
    rig = Rig([('a', Always), ('b', lambda: RecEngine('b', spec=SPEC_YY, trade=YY, timeout=0.05))])
    try:
        wait_until(lambda: rig.live.core.engine_state['a'] == 'failed', what='the engine to fail')
        assert rig.calls == [] and rig.live.reactor.alive(), 'a failed engine does not end the session'
        rig.lc.teardown()
        assert rig.names() == ['flush', 'terminate']
    finally:
        rig.live.close()


def test_a_hung_engine_blocks_terminate_and_raises_a_critical_alert():
    class Hung:
        name, spec = 'a', SPEC

        def run(self, ctx):
            ctx.next_event(0.05)
            time.sleep(3)                                    # ignores Stop, blocks in its own code
    rig = Rig([('a', Hung)], lifecycle_cfg=LifecycleConfig(engine_join_timeout_s=0.3, drain_timeout_s=0.2, poll_s=0.01))
    try:
        wait_until(lambda: rig.live.core.engine_state['a'] == 'running')
        time.sleep(0.1)
        report = rig.lc.teardown()
        assert report.engines_hung == ['a'] and not report.terminated and 'terminate' not in rig.names()
        assert 'terminate_skipped' in report.steps
        texts = [a.text for a in rig.live.core.alerts_for('critical')]
        assert any('did not stop' in t for t in texts) and any('NOT terminated' in t for t in texts)
    finally:
        rig.live.close()


def test_terminate_despite_hung_is_available_as_an_explicit_choice():
    class Hung:
        name, spec = 'a', SPEC

        def run(self, ctx):
            ctx.next_event(0.05)
            time.sleep(1)
    rig = Rig([('a', Hung)], lifecycle_cfg=LifecycleConfig(engine_join_timeout_s=0.2, poll_s=0.01, terminate_despite_hung=True))
    try:
        time.sleep(0.1)
        assert rig.lc.teardown().terminated and rig.names() == ['flush', 'terminate']
    finally:
        rig.live.close()


def test_teardown_lets_an_in_flight_order_finish_before_terminating():
    log = []
    rig = Rig([('a', lambda: RecEngine('a', act=submit_on_start(open_req()), log=log, timeout=0.05))],
              broker=lambda c: BrokerReply('fill', latency=0.4))
    try:
        wait_until(lambda: rig.live.sim.orders, what='the order to be placed')
        report = rig.lc.teardown()
        assert report.drained and report.terminated
        assert rig.live.held('a', FRONT) == 3, 'the fill landed before the session was terminated'
        assert rig.calls[-1][0] == 'terminate'
    finally:
        rig.live.close()


def test_requests_left_unconfirmed_at_shutdown_are_reported_and_termination_still_happens():
    rig = Rig([('a', lambda: RecEngine('a', act=submit_on_start(open_req()), timeout=0.05))],
              broker=lambda c: BrokerReply('unconfirmed', pending_for=60.0))
    try:
        wait_until(lambda: rig.live.core.unconfirmed_requests(), what='UNCONFIRMED')
        report = rig.lc.teardown()
        assert report.unconfirmed == [('a', 'r1')] and report.terminated
        assert any('UNCONFIRMED at shutdown' in a.text for a in rig.live.core.alerts_for('critical'))
    finally:
        rig.live.close()


def test_an_engine_crashing_while_stopping_is_not_brought_back():
    launches = []

    class DiesOnStop:
        name, spec = 'a', SPEC

        def run(self, ctx):
            launches.append(1)
            while True:
                if isinstance(ctx.next_event(0.05), Stop):
                    raise RuntimeError('crash during shutdown')
    rig = Rig([('a', DiesOnStop)])
    try:
        wait_until(lambda: launches)
        rig.lc.teardown()
        time.sleep(0.3)                                       # longer than the first restart backoff
        assert launches == [1] and rig.live.core.engine_state['a'] == 'ended'
    finally:
        rig.live.close()


# ---- signals ---------------------------------------------------------------------------------------------------------------

def test_sigterm_in_the_main_thread_sets_the_shutdown_flag_only_and_run_until_shutdown_returns():
    rig = Rig([('a', lambda: RecEngine('a', timeout=0.05))])
    try:
        assert rig.lc.install_signals()
        before = signal.getsignal(signal.SIGTERM)
        assert before == rig.lc.handle_signal
        os.kill(os.getpid(), signal.SIGTERM)
        started = time.monotonic()
        assert rig.lc.run_until_shutdown(poll_s=0.05) == StopReason.SHUTDOWN and time.monotonic() - started < 2
        assert rig.calls == [], 'the handler does not tear down; the main thread does'
        report = rig.lc.teardown()
        assert report.reason == StopReason.SHUTDOWN
    finally:
        rig.lc.restore_signals()
        rig.live.close()
    assert signal.getsignal(signal.SIGTERM) != rig.lc.handle_signal


def test_signals_cannot_be_installed_from_a_worker_thread():
    rig = Rig([])
    try:
        out = []
        t = threading.Thread(target=lambda: out.append(rig.lc.install_signals()))
        t.start(), t.join()
        assert out == [False]
    finally:
        rig.live.close()


def test_run_until_shutdown_ends_at_the_session_end_time():
    rig = Rig([])
    try:
        end = rig.live.reactor.now + timedelta(seconds=0.2)
        assert rig.lc.run_until_shutdown(session_end=end, poll_s=0.05) == StopReason.SESSION_END
    finally:
        rig.live.close()


def test_a_restart_already_scheduled_when_shutdown_begins_never_happens():
    """Engine a crashes and is due back in 0.5 s; shutdown starts at once but engine b takes 0.8 s to stop, so the reactor is
    still alive when a's restart time passes. a must stay down."""
    launches = []

    class DiesOnce:
        name, spec = 'a', SPEC

        def run(self, ctx):
            launches.append(1)
            ctx.next_event(0.05)
            raise RuntimeError('dies, restart scheduled for 0.5 s later')

    class SlowToStop(RecEngine):
        def run(self, ctx):
            super().run(ctx)
            time.sleep(0.8)
    rig = Rig([('a', DiesOnce), ('b', lambda: SlowToStop('b', spec=SPEC_YY, trade=YY, timeout=0.05))], restart_backoff_s=(0.5,),
              lifecycle_cfg=LifecycleConfig(engine_join_timeout_s=3.0, poll_s=0.01))
    try:
        wait_until(lambda: any('auto-resuming' in a.text for a in rig.live.core.alerts_for('warning')))
        rig.lc.teardown()
        assert launches == [1] and rig.live.core.engine_state['b'] == 'ended'
    finally:
        rig.live.close()


# ---- the Angel adapter on real worker threads ------------------------------------------------------------------------------

def _angel_live(script, engines):
    """A live core whose broker is the Angel adapter over the scripted double, blocking work on a thread pool."""
    from concurrent.futures import ThreadPoolExecutor
    from smartapi_double import FakeClock, ScriptedSmartConnect
    from hestia_live_helpers import FAST, StubData
    from hestia_core.angel_broker import AngelBrokerPort, AngelConfig
    from hestia_core.core import CoreConfig, HestiaCore
    from hestia_core.gateway import BrokerGateway
    from hestia_core.thread_runner import ThreadTask
    reactor = RealReactor()
    reactor.start()
    data = StubData(reactor, [FRONT, NEXT], {'T1': 100.0, 'T2': 101.0})
    real = FakeClock()
    sc = ScriptedSmartConnect(real)
    sc.place_script = list(script)
    gw = BrokerGateway(sc)                                                  # the real clock and the real sleep
    executor = ThreadPoolExecutor(max_workers=3)
    port = AngelBrokerPort(gw, reactor, executor, lambda tok: 10, data.info,
                           AngelConfig(order_timeout_s=0.3, poll_interval_s=0.02, ws_first_s=0.0), None, real.wall_now,
                           lambda: None, alert=lambda *a: None)
    port.refresh_cash_blocking()
    core = HestiaCore(reactor, data, port, CoreConfig(**{**FAST, 'reconcile_interval_s': 0.2, 'reconcile_retry_s': 0.1}),
                      ThreadTask)
    for name, factory in engines:
        core.register(name, factory)
    with reactor.lock:
        core.begin_session()

    def close():
        with reactor.lock:
            core.close()
        reactor.stop()
        executor.shutdown(wait=False)
    return core, sc, close


def test_the_angel_adapter_fills_through_worker_threads_and_the_reactor():
    log = []
    core, sc, close = _angel_live([], [('a', lambda: RecEngine('a', act=submit_on_start(open_req()), log=log, timeout=0.05))])
    try:
        wait_until(lambda: events_of(log, RequestOutcome), what='the outcome')
        (o,) = events_of(log, RequestOutcome)
        assert o.status == OutcomeStatus.FILLED and o.opened.lots == 3
        assert len(sc.orders) == 1 and sc.orders[0]['quantity'] == '30'
    finally:
        close()


def test_an_order_held_open_becomes_unconfirmed_then_settles_and_a_waiting_close_acts_on_it():
    log = []
    close_req = CloseRequest('c1', FRONT, Direction.BULLISH, ExitReason.TREND_FLIP)

    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.submit(open_req())
        if isinstance(ev, RequestOutcome) and ev.request_id == 'r1' and ev.status == OutcomeStatus.UNCONFIRMED:
            ctx.submit(close_req)
    core, sc, close = _angel_live(['hold'], [('a', lambda: RecEngine('a', act=act, log=log, timeout=0.05))])
    try:
        wait_until(lambda: any(o.status == OutcomeStatus.UNCONFIRMED for o in events_of(log, RequestOutcome)), what='UNCONFIRMED')
        time.sleep(0.3)                                       # the close waits: the order is still working at the broker
        assert [o.request_id for o in events_of(log, RequestOutcome) if o.status != OutcomeStatus.UNCONFIRMED] == []
        sc.complete_open_orders()
        wait_until(lambda: any(o.request_id == 'c1' for o in events_of(log, RequestOutcome)), what='the close')
        done = [(o.request_id, o.status) for o in events_of(log, RequestOutcome)]
        assert done == [('r1', OutcomeStatus.UNCONFIRMED), ('r1', OutcomeStatus.FILLED), ('c1', OutcomeStatus.FILLED)]
        assert [r['transactiontype'] for r in sc.orders] == ['BUY', 'SELL']
    finally:
        close()
