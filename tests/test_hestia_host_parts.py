"""
The pieces slice 5 adds around the core: the Slack queue, the session lock, the state store and restart recovery, live sizing
overrides, flag files, alert routing, trade logs and the session report.
"""
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta

import pytest

from hestia_fake_helpers import FRONT, NEXT, SPEC, SESSION_DATE, RecEngine, events_of, factory, world
from hestia_core.alert_router import AlertRouter
from hestia_core.core import Alert
from hestia_core.fake import BrokerReply, FakeConfig
from hestia_core.flags import FlagFiles, FlagWatcher
from hestia_core.interface import (AckStatus, CloseRequest, CommandEvent, CommandKind, Direction, ExitReason, FlattenRequest,
                                   OpenRequest, OutcomeStatus, RequestOutcome, SessionStart, SizingConfig, TRADE_RECORD_COLUMNS)
from hestia_core.reporting import TradeLogWriter, build_session_report
from hestia_core.session_lock import LockHeld, SessionLock, holder, pid_alive, refuse_if_held, wait_for_pid_exit
from hestia_core.sizing import SizingStore
from hestia_core.slack_queue import SlackQueue
from hestia_core.state_store import StateStore, decode_outcome, encode_outcome

T0 = datetime(2026, 9, 3, 9, 0)


# ---- Slack queue ---------------------------------------------------------------------------------------------------------

def test_slack_messages_arrive_in_call_order_and_flush_waits_for_them():
    got = []
    q = SlackQueue(post=lambda ch, text: (time.sleep(0.005), got.append((ch, text))))
    for i in range(20):
        q.send('#c', f'm{i}')
    assert q.flush(5.0) and [t for _, t in got] == [f'm{i}' for i in range(20)] and q.sent == 20


def test_a_hung_post_cannot_block_the_caller_and_flush_reports_it():
    release = threading.Event()
    q = SlackQueue(post=lambda ch, text: release.wait(5))
    started = time.monotonic()
    for i in range(5):
        q.send('#c', f'm{i}')
    assert time.monotonic() - started < 0.5, 'sending never blocks'
    assert q.flush(0.2) is False
    release.set()
    assert q.flush(5.0)


def test_the_queue_is_bounded_and_keeps_the_newest_messages():
    release, got = threading.Event(), []
    q = SlackQueue(post=lambda ch, text: (release.wait(5), got.append(text)), maxsize=3)
    for i in range(12):
        q.send('#c', f'm{i}')
    release.set()
    assert q.flush(5.0) and q.dropped > 0 and got[-1] == 'm11'


def test_a_failing_post_is_counted_and_the_worker_carries_on():
    calls = []

    def post(ch, text):
        calls.append(text)
        if text == 'bad':
            raise RuntimeError('slack down')
    q = SlackQueue(post=post)
    q.send('#c', 'bad'), q.send('#c', 'good')
    assert q.flush(5.0) and calls == ['bad', 'good'] and q.failed == 1 and q.sent == 1


def test_a_disabled_queue_is_a_no_op():
    q = SlackQueue()
    q.send('#c', 'x')
    assert not q.enabled and q.flush(0.1)
    SlackQueue(post=lambda *a: None).send(None, 'no channel')                      # a missing channel is skipped too


# ---- session lock --------------------------------------------------------------------------------------------------------

def test_the_lock_records_the_owner_and_a_second_live_claimant_is_refused(tmp_path):
    lock = SessionLock(tmp_path / 'angel.lock', 'hestia')
    lock.claim()
    info = holder(tmp_path / 'angel.lock')
    assert info['pid'] == os.getpid() and info['owner'] == 'hestia'
    rival = SessionLock(tmp_path / 'angel.lock', 'downloader', pid=os.getppid())          # another live process
    with pytest.raises(LockHeld, match='hestia'):
        rival.claim()
    lock.claim()                                                                             # our own re-claim is fine


def test_a_dead_holders_lock_is_stale_and_reclaimed(tmp_path):
    proc = subprocess.Popen(['true'])
    proc.wait()
    (tmp_path / 'l.lock').write_text(json.dumps({'pid': proc.pid, 'owner': 'gone', 'since': 'x'}))
    assert not pid_alive(proc.pid) and holder(tmp_path / 'l.lock') is None
    SessionLock(tmp_path / 'l.lock').claim()
    assert holder(tmp_path / 'l.lock')['pid'] == os.getpid()


def test_other_login_sites_refuse_while_a_live_process_holds_the_session(tmp_path):
    p = tmp_path / 'l.lock'
    p.write_text(json.dumps({'pid': os.getppid(), 'owner': 'hestia', 'since': 'x'}))
    with pytest.raises(LockHeld):
        refuse_if_held(p, 'leto')
    refuse_if_held(tmp_path / 'absent.lock', 'leto')                                        # no lock: fine
    p.write_text('not json')
    refuse_if_held(p, 'leto')                                                                # unreadable lock: not a live holder


def test_release_only_removes_a_lock_that_is_still_ours(tmp_path):
    p = tmp_path / 'l.lock'
    mine = SessionLock(p, pid=os.getpid())
    mine.claim()
    p.write_text(json.dumps({'pid': os.getppid(), 'owner': 'successor', 'since': 'x'}))
    mine.release()
    assert p.exists(), "a successor's lock is left alone"
    p.write_text(json.dumps({'pid': os.getpid(), 'owner': 'me', 'since': 'x'}))
    mine.release()
    assert not p.exists()


def test_takeover_terminates_the_previous_holder_and_waits_until_it_is_really_gone(tmp_path):
    child = subprocess.Popen(['bash', '-c', 'sleep 30', 'hestia'])         # its command line names hestia
    reaper = threading.Thread(target=child.wait, daemon=True)
    reaper.start()
    p = tmp_path / 'l.lock'
    p.write_text(json.dumps({'pid': child.pid, 'owner': 'old-hestia', 'since': 'x'}))
    old = SessionLock(p, 'hestia').takeover(timeout=10.0)
    assert old == child.pid and not pid_alive(child.pid) and holder(p)['pid'] == os.getpid()
    assert child.returncode is not None


def test_takeover_never_signals_a_live_process_that_is_not_hestia(tmp_path):
    """Pids are reused: a stale lock naming the pid of, say, standalone Prometheus must not get that process SIGTERMed."""
    child = subprocess.Popen(['sleep', '30'])
    try:
        p = tmp_path / 'l.lock'
        p.write_text(json.dumps({'pid': child.pid, 'owner': 'old-hestia', 'since': 'x'}))
        assert SessionLock(p, 'hestia').takeover(timeout=0.5) is None
        assert child.poll() is None and pid_alive(child.pid), 'the unrelated process was left alone'
        assert holder(p)['pid'] == os.getpid(), 'and the lock is ours'
    finally:
        child.kill()
        child.wait()


def test_wait_for_pid_exit_times_out_on_a_live_pid():
    assert wait_for_pid_exit(os.getpid(), timeout=0.2, poll=0.05) is False


# ---- state store and restart recovery ------------------------------------------------------------------------------------

def test_engine_state_ledger_and_outcomes_round_trip_through_the_store(tmp_path):
    store = StateStore(tmp_path)
    assert store.load_engine_state('a') is None and store.load_ledger() == {}
    store.save_engine_state('a', '{"x": 1}')
    assert StateStore(tmp_path).load_engine_state('a') == '{"x": 1}'
    store.save_ledger({('a', 'T1'): [3, 100.5, T0], ('a', 'T2'): [0, None, T0], ('b', 'U1'): [-2, None, None]})
    assert StateStore(tmp_path).load_ledger() == {('a', 'T1'): [3, 100.5, T0], ('b', 'U1'): [-2, None, None]}, 'flat rows are not kept'
    from hestia_core.interface import FillSummary, RequestKind
    o = RequestOutcome('r1', OutcomeStatus.FILLED, RequestKind.FLIP, 6, T0, FillSummary(3, 99.0), FillSummary(3, 99.5), 'x')
    back = decode_outcome(json.loads(json.dumps(encode_outcome(o))))
    assert (back.request_id, back.status, back.kind, back.requested_lots, back.ts, back.detail) == ('r1', OutcomeStatus.FILLED,
                                                                                                    RequestKind.FLIP, 6, T0, 'x')
    assert (back.closed.lots, back.opened.avg_price) == (3, 99.5) and back.confirmed


def test_the_journal_survives_a_torn_last_line(tmp_path):
    store = StateStore(tmp_path)
    store.journal({'t': 'submit', 'engine': 'a', 'request_id': 'r1', 'kind': 'open'}, T0)
    store.journal({'t': 'final', 'engine': 'a', 'request_id': 'r1'}, T0)
    with open(store._journal_path(T0.date()), 'a') as f:
        f.write('{"t": "submit", "engine": "a", "requ')
    assert [r['t'] for r in store.load_journal(T0.date())] == ['submit', 'final']


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


def go(h, seconds=120):
    h.start_session(SESSION_DATE)
    h.run_until(T0 + timedelta(seconds=seconds))


def opener(rid='r1', lots=3, contract=FRONT):
    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.submit(OpenRequest(rid, contract, Direction.BULLISH, lots, trade_ref=1))
    return act


def test_engine_state_survives_a_restart_of_the_core(tmp_path):
    seen = {}

    def act1(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.save_state('decision-state-v1')

    def act2(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            seen['loaded'] = ctx.load_state()
    h1 = world([('a', factory(act=act1))], store=StateStore(tmp_path))
    go(h1, 10)
    h1.close()
    h2 = world([('a', factory(act=act2))], store=StateStore(tmp_path))
    go(h2, 10)
    h2.close()
    assert seen['loaded'] == 'decision-state-v1'


def test_a_restarted_core_knows_the_ledger_and_every_finished_request_and_never_resends(tmp_path):
    h1 = world([('a', factory(act=opener()))], store=StateStore(tmp_path))
    h1.set_price('T1', 100.0)
    go(h1)
    assert h1.held('a', FRONT) == 3 and len(h1.orders) == 1
    h1.close()

    acks, statuses, positions = [], [], []

    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            st = ctx.request_status('r1')
            statuses.append(st.status if st else None)
            acks.append(ctx.submit(OpenRequest('r1', FRONT, Direction.BULLISH, 3, trade_ref=1)))
            positions.append(ctx.position(FRONT).net_lots)
    h2 = world([('a', factory(act=act))], store=StateStore(tmp_path))
    h2.set_price('T1', 100.0)
    h2._sim.seed('T1', 3, 100.0)                                       # the broker still holds what the first process bought
    h2.restore()
    go(h2)
    assert statuses == [OutcomeStatus.FILLED] and positions == [3]
    assert [a.status for a in acks] == [AckStatus.DUPLICATE] and h2.orders == [], 'the same id is never sent twice across a restart'
    assert h2.alerts_for('critical') == []
    h2.close()


def test_a_request_in_flight_when_the_process_died_is_in_doubt_never_unknown_and_never_resent(tmp_path):
    store = StateStore(tmp_path)
    store.journal({'t': 'submit', 'engine': 'a', 'request_id': 'r1', 'kind': 'open', 'token': 'T1', 'pclass': 4, 'lots': 3}, T0)
    seen = []

    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            st = ctx.request_status('r1')
            seen.append((st.status, st.confirmed, st.detail))
            seen.append(ctx.submit(OpenRequest('r1', FRONT, Direction.BULLISH, 3, trade_ref=1)).status)
    h = world([('a', factory(act=act))], store=StateStore(tmp_path))
    h.restore()
    go(h)
    assert seen[0][0] == OutcomeStatus.UNCONFIRMED and seen[0][1] is False and 'in doubt' in seen[0][2]
    assert seen[1] == AckStatus.DUPLICATE and h.orders == []
    assert any('IN DOUBT' in a.text for a in h.alerts_for('critical'))
    h.close()


def test_on_restart_the_broker_book_overrides_a_stale_persisted_ledger_for_live_engines_only(tmp_path):
    from hestia_fake_helpers import SPEC_YY
    from hestia_core.interface import ContractRef
    store = StateStore(tmp_path)
    store.save_ledger({('a', 'T1'): [3, 100.0, T0], ('p', 'U1'): [2, 50.0, T0]})
    from hestia_core.fake import ContractSpec
    from hestia_fake_helpers import minutes_frame, OTHER_FRONT
    extra = [ContractSpec(OTHER_FRONT, lot_size=10, minutes=minutes_frame(50.0, 3))]
    h = world([('a', factory(name='a')), ('p', lambda: RecEngine('p', spec=SPEC_YY, trade=OTHER_FRONT), {'paper': True})],
              extra=extra, store=StateStore(tmp_path))
    h._sim.seed('T1', 5, 101.0)                                       # the broker now shows 5: an order landed just before the crash
    h.restore()
    assert h.held('a', FRONT) == 3
    h.bootstrap_ledger(authoritative=True)
    h.run_for(5)
    assert h.held('a', FRONT) == 5 and h._ledger[('a', 'T1')][1] == 101.0
    assert h.held('p', OTHER_FRONT) == 2, 'a paper position has no broker book to be corrected against'
    assert any('broker book is taken as the truth' in a.text for a in h.alerts_for('critical'))
    assert StateStore(tmp_path).load_ledger()[('a', 'T1')][0] == 5, 'and the correction is persisted'
    h.close()


def test_every_fill_is_persisted_as_it_happens(tmp_path):
    h = world([('a', factory(act=opener()))], store=StateStore(tmp_path))
    h.set_price('T1', 100.0)
    go(h)
    assert StateStore(tmp_path).load_ledger()[('a', 'T1')][:2] == [3, 100.0]
    journal = StateStore(tmp_path).load_journal(SESSION_DATE)
    assert [r['t'] for r in journal] == ['submit', 'placed', 'final', 'attempt_done'] and journal[2]['outcome']['status'] == 'filled'
    assert journal[1]['order_id'] and journal[1]['lots'] == 3 and journal[1]['attempt'] == 1
    h.close()


# ---- live sizing -----------------------------------------------------------------------------------------------------------

def test_the_sizing_override_is_read_live_and_never_raises_the_unit_cap(tmp_path):
    defaults = {'a': SizingConfig(dynamic=False, static_units=1, unit_cap=5)}
    sizing = SizingStore(tmp_path, defaults)
    assert sizing.get('a') == defaults['a']
    sizing.path('a').write_text(json.dumps({'static_units': 3, 'unit_cap': 999, 'dynamic': False}))
    got = sizing.get('a')
    assert got.static_units == 3 and got.unit_cap == 5, 'the cap only ever comes from configuration'
    sizing.path('a').write_text('{"static_units": 0}')
    assert sizing.get('a') == defaults['a'], 'an invalid override falls back to the defaults'
    sizing.path('a').write_text('not json')
    os.utime(sizing.path('a'), None)
    assert sizing.get('a') == defaults['a']
    sizing.path('a').unlink()
    assert sizing.get('a') == defaults['a']


def test_an_engine_sees_the_override_through_its_context_and_admission_still_uses_the_registered_cap(tmp_path):
    defaults = {'a': SizingConfig(dynamic=False, static_units=1, unit_cap=2)}
    store = SizingStore(tmp_path, defaults)
    seen = []

    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            seen.append(ctx.sizing())
            ctx.submit(OpenRequest('big', FRONT, Direction.BULLISH, 3, trade_ref=1))       # 3 lots > cap of 2
    store.path('a').write_text(json.dumps({'static_units': 7, 'unit_cap': 100}))
    h = world([('a', factory(act=act), {'sizing': defaults['a']})], sizing_provider=store.get)
    h.set_price('T1', 100.0)
    log = []
    go(h)
    assert seen[0].static_units == 7 and seen[0].unit_cap == 2
    assert [o.status for _, _, o in h.outcome_log] == [OutcomeStatus.LIMIT_REFUSED]
    h.close()


# ---- flags --------------------------------------------------------------------------------------------------------------

def flag_rig(tmp_path, engines, **kw):
    h = world(engines, **kw)
    h.set_price('T1', 100.0)
    flags = FlagFiles(tmp_path)
    flags.raise_host_flag()
    gone = []
    watcher = FlagWatcher(h, h.kernel, flags, lambda: gone.append(h.now), poll_s=1.0, exit_retry_s=20.0)
    return h, flags, watcher, gone


def test_removing_the_host_flag_asks_for_shutdown_once(tmp_path):
    h, flags, watcher, gone = flag_rig(tmp_path, [('a', factory())])
    watcher.start()
    go(h, 5)
    assert gone == []
    flags.drop_host_flag()
    h.run_until(T0 + timedelta(seconds=15))
    assert len(gone) == 1
    h.close()


def test_a_kill_flag_stops_only_that_engine_leaves_its_position_and_keeps_the_flag(tmp_path):
    from hestia_fake_helpers import SPEC_YY, OTHER_FRONT
    from hestia_core.fake import ContractSpec
    from hestia_fake_helpers import minutes_frame
    extra = [ContractSpec(OTHER_FRONT, lot_size=10, minutes=minutes_frame(50.0, 3))]
    la, lb = [], []
    h, flags, watcher, gone = flag_rig(tmp_path, [('a', factory(log=la)),
                                                  ('b', lambda: RecEngine('b', spec=SPEC_YY, trade=OTHER_FRONT, log=lb))],
                                       extra=extra)
    h.seed_position('a', FRONT, 2, 98.0)
    watcher.start()
    go(h, 5)
    flags.command_path('a').write_text('KILL\n')
    h.run_until(T0 + timedelta(seconds=30))
    assert h.engine_state['a'] == 'killed' and h.engine_state['b'] == 'running' and h.held('a', FRONT) == 2
    assert flags.read_command('a') == 'KILL', 'the flag stays until the operator clears it'
    assert len(events_of(la, __import__('hestia_core.interface', fromlist=['Stop']).Stop)) == 1
    h.close()


def test_an_exit_flag_liquidates_and_is_cleared_only_once_flat_and_idle(tmp_path):
    def act(eng, ctx, ev):
        if isinstance(ev, CommandEvent) and ev.kind == CommandKind.EXIT:
            ctx.submit(FlattenRequest('exit-1', FRONT, ExitReason.MANUAL_EXIT))
    h, flags, watcher, gone = flag_rig(tmp_path, [('a', factory(act=act, timeout=5))],
                                       broker=lambda c: BrokerReply('fill', latency=6.0))
    h.seed_position('a', FRONT, 3, 98.0)
    watcher.start()
    go(h, 3)
    flags.command_path('a').write_text('EXIT')
    h.run_until(T0 + timedelta(seconds=8))
    assert flags.read_command('a') == 'EXIT', 'not flat yet (the fill takes 6 s), so the flag stays'
    h.run_until(T0 + timedelta(seconds=30))
    assert h.held('a', FRONT) == 0 and flags.read_command('a') is None
    assert any('EXIT complete' in a.text for a in h.alerts_for('info'))
    h.close()


def test_an_exit_that_never_completes_is_re_sent_with_a_critical_alert(tmp_path):
    seen = []

    def act(eng, ctx, ev):
        if isinstance(ev, CommandEvent):
            seen.append(ctx.now())
    h, flags, watcher, gone = flag_rig(tmp_path, [('a', factory(act=act, timeout=5))])
    h.seed_position('a', FRONT, 3, 98.0)                              # the engine ignores EXIT, so it never gets flat
    watcher.start()
    go(h, 3)
    flags.command_path('a').write_text('EXIT')
    h.run_until(T0 + timedelta(seconds=50))
    assert len(seen) >= 3 and flags.read_command('a') == 'EXIT'
    assert any('still not flat' in a.text for a in h.alerts_for('critical'))
    h.close()


def test_disable_and_kill_flags_are_startup_gates(tmp_path):
    h, flags, watcher, gone = flag_rig(tmp_path, [('a', factory())])
    assert watcher.disabled_engines(['a', 'b']) == []
    flags.command_path('a').write_text('disable\n')
    assert watcher.disabled_engines(['a', 'b']) == ['a']
    h.close()


# ---- alert routing, trade logs, the report ---------------------------------------------------------------------------------

CHANNELS = {'info': '#tradebot-updates', 'warning': '#error-alerts', 'error': '#error-alerts', 'critical': '#error-alerts',
            'trade': '#trade-alerts', 'trade-updates': '#trade-updates'}


def test_alerts_go_to_the_channel_of_their_level_tagged_with_the_engine_and_repeats_are_cooled_down():
    sent = []
    router = AlertRouter(SlackQueue(post=lambda ch, t: sent.append((ch, t))), CHANNELS, cooldown_s=30.0)
    router(Alert(T0, 'info', 'a', 'engine started'))
    router(Alert(T0, 'critical', 'a', 'silent'))
    router(Alert(T0 + timedelta(seconds=10), 'critical', 'a', 'silent'))          # inside the cooldown: suppressed
    router(Alert(T0 + timedelta(seconds=40), 'critical', 'a', 'silent'))          # after it: sent again
    router(Alert(T0, 'warning', None, 'host thing', channel='trade-updates'))     # an explicit channel wins
    router.slack.flush(5)
    assert sent == [('#tradebot-updates', '*Hestia [a]*: engine started'),
                    ('#error-alerts', '\U0001f6a8\U0001f6a8 *Hestia [a]*: silent'),
                    ('#error-alerts', '\U0001f6a8\U0001f6a8 *Hestia [a]*: silent'),
                    ('#trade-updates', '⚠️ *Hestia*: host thing')]
    assert router.suppressed == 1
    router.trade('a', {'trade_id': 7, 'direction': 'bullish', 'total_pnl_rs': 1234.5})
    router.slack.flush(5)
    assert sent[-1] == ('#trade-alerts', '*Hestia [a]*: trade 7 bullish closed, P&L Rs 1,234')


def test_a_failing_alert_sink_never_stops_the_core(hestias):
    def boom(alert):
        raise RuntimeError('sink down')
    log = []
    h = hestias([('a', factory(log=log, act=opener()))])
    h.alert_sinks.append(boom)
    h.trade_sinks.append(lambda e, r: 1 / 0)
    go(h)
    h._alert('warning', None, 'x')
    assert h.held('a', FRONT) == 3 and h.alerts_for('warning')[-1].text == 'x'


def test_trade_records_are_written_in_the_fixed_column_format_and_reach_the_sinks(tmp_path, hestias):
    writer = TradeLogWriter(tmp_path)
    got = []

    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.report_trade({'trade_id': 7, 'direction': 'bullish', 'units': 2, 'total_pnl_rs': 500.0, 'stray': 1})
            ctx.report_trade({'trade_id': 8, 'direction': 'bearish', 'total_pnl_rs': -200.0})
    h = hestias([('a', factory(act=act))])
    h.trade_sinks += [writer.write, lambda e, r: got.append(r['trade_id'])]
    go(h, 10)
    lines = writer.path('a').read_text().splitlines()
    assert lines[0].split(',') == list(TRADE_RECORD_COLUMNS) and len(lines) == 3
    assert lines[1].startswith('7,') and got == [7, 8]


def test_the_session_report_is_one_message_covering_every_engine(hestias):
    from hestia_fake_helpers import SPEC_YY, OTHER_FRONT, minutes_frame
    from hestia_core.fake import ContractSpec
    extra = [ContractSpec(OTHER_FRONT, lot_size=10, minutes=minutes_frame(50.0, 3))]

    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.submit(OpenRequest('o1', FRONT, Direction.BULLISH, 2, trade_ref=1))
            ctx.report_trade({'trade_id': 1, 'total_pnl_rs': 700.0})
    h = hestias([('a', factory(act=act)), ('p', lambda: RecEngine('p', spec=SPEC_YY, trade=OTHER_FRONT), {'paper': True})],
                extra=extra)
    go(h)
    text = build_session_report(h, h.now, h.trades)
    assert text.startswith('*Hestia session report*')
    assert 'a (live, running): XX30OCT26FUT +2 @ 100.00; 1 trade(s), Rs 700' in text
    assert 'p (paper, running): flat; 0 trade(s), Rs 0' in text
    assert 'combined realised Rs 700' in text and 'account free cash' in text


# ---- restart: in-doubt requests are settled from the broker, never counted twice --------------------------------------------

def restart_harness(tmp_path, journal, ledger, position_rows, order_rows=(), paper=False):
    """A core on the Angel adapter and the scripted double, with a store already holding a previous process's journal."""
    from smartapi_double import FakeClock, ScriptedSmartConnect
    from hestia_fake_helpers import minutes_frame
    from hestia_core.angel_broker import AngelBrokerPort, AngelConfig
    from hestia_core.core import CoreConfig, HestiaCore
    from hestia_core.executors import InlineExecutor
    from hestia_core.fake_kernel import EngineTask, SimKernel
    from hestia_core.gateway import BrokerGateway
    from hestia_core.replay import ContractSpec, ReplayData
    from hestia_core.broker_router import BrokerRouter
    from hestia_core.paper_broker import PaperBroker
    store = StateStore(tmp_path)
    for rec in journal:
        store.journal(rec, T0)
    if ledger:
        store.save_ledger(ledger)
    clock = FakeClock()
    sc = ScriptedSmartConnect(clock)
    sc.price = 100.0
    for params, status, filled in order_rows:
        sc._new_order(params, status, filled)
    sc.position_rows = list(position_rows)
    kernel = SimKernel(datetime(2026, 9, 3, 8, 50))
    data = ReplayData(kernel)
    for ref in (FRONT, NEXT):
        data.add_contract(ContractSpec(ref, lot_size=10, tick_size=0.5, freeze_qty_lots=20, minutes=minutes_frame(100.0, 11)))
    data.set_price('T1', 100.0)
    port = AngelBrokerPort(BrokerGateway(sc, clock=clock.monotonic, sleep=clock.sleep), kernel, InlineExecutor(), lambda tok: 10,
                           data.info, AngelConfig(), None, clock.wall_now, lambda: None, clock.sleep, clock.monotonic,
                           alert=lambda *a: None)
    port.refresh_cash_blocking()
    broker = BrokerRouter(port, PaperBroker(kernel, data.price, lambda *a: 0.0)) if paper else port
    core = HestiaCore(kernel, data, broker, CoreConfig(ledger_reconcile_interval_s=None, reconcile_interval_s=None,
                                                        reconcile_retry_s=5.0), EngineTask, store=StateStore(tmp_path))
    seen = {}

    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            st = ctx.request_status('r1')
            seen['status'] = None if st is None else st.status
    core.register('a', factory(act=act), paper=paper)
    return core, kernel, data, sc, seen


ORDER = {'tradingsymbol': FRONT.symbol, 'symboltoken': 'T1', 'transactiontype': 'BUY', 'quantity': '30'}
SUBMIT = {'t': 'submit', 'engine': 'a', 'request_id': 'r1', 'kind': 'open', 'token': 'T1', 'pclass': 4, 'lots': 3}
PLACED = {'t': 'placed', 'engine': 'a', 'request_id': 'r1', 'order_id': '000000000001', 'side': 'BUY', 'lots': 3, 'attempt': 1}


def finish_startup(core, kernel, data, seconds=60, begin=True):
    core.bootstrap_ledger(authoritative=True)
    kernel.run_for(5)
    core.settle_restored()
    kernel.run_for(seconds)
    if begin:
        data.begin_session(SESSION_DATE)
        core.begin_session()
        kernel.run_until(T0 + timedelta(seconds=10))


@pytest.mark.parametrize('persisted_lots', [3, 0])
def test_an_order_journalled_before_the_crash_is_settled_by_reading_it_and_never_counted_twice(tmp_path, persisted_lots):
    """persisted 3: the fill reached the ledger before the crash. persisted 0: it did not. Either way the broker shows 3."""
    ledger = {('a', 'T1'): [persisted_lots, 100.0, T0]} if persisted_lots else None
    core, kernel, data, sc, seen = restart_harness(
        tmp_path, [SUBMIT, PLACED], ledger, [{'symboltoken': 'T1', 'netqty': '30', 'netprice': '100.0'}],
        [(ORDER, 'complete', 30)])
    try:
        core.restore()
        assert core.request_record('a', 'r1').outcome.status == OutcomeStatus.UNCONFIRMED
        assert core.unconfirmed_requests() == [('a', 'r1')], 'an in-doubt request is listed for the reports'
        finish_startup(core, kernel, data)
        out = core.request_record('a', 'r1').outcome
        assert out.status == OutcomeStatus.FILLED and out.opened.lots == 3 and 'after a restart' in out.detail
        assert core.held('a', FRONT) == 3, 'the broker net, not doubled by settling the request'
        assert core.unconfirmed_requests() == [] and seen['status'] == OutcomeStatus.FILLED
        assert [r['t'] for r in StateStore(tmp_path).load_journal(SESSION_DATE)][-2:] == ['attempt_done', 'final']
    finally:
        core.close()


def test_a_restored_order_still_working_at_the_broker_stays_unconfirmed_until_the_broker_finishes_it(tmp_path):
    core, kernel, data, sc, seen = restart_harness(
        tmp_path, [SUBMIT, PLACED], None, [{'symboltoken': 'T1', 'netqty': '30', 'netprice': '100.0'}], [(ORDER, 'open', 0)])
    try:
        sc._open_until['000000000001'] = sc.book_reads + 4
        core.restore()
        finish_startup(core, kernel, data, seconds=1, begin=False)
        assert core.request_record('a', 'r1').outcome.status == OutcomeStatus.UNCONFIRMED, 'still working: never "no fill"'
        kernel.run_for(60)
        assert core.request_record('a', 'r1').outcome.status == OutcomeStatus.FILLED and core.held('a', FRONT) == 3
    finally:
        core.close()


def test_a_restored_order_the_broker_shows_as_not_executed_is_rejected_and_the_ledger_is_the_books(tmp_path):
    core, kernel, data, sc, seen = restart_harness(tmp_path, [SUBMIT, PLACED], {('a', 'T1'): [3, 100.0, T0]}, [],
                                                   [(ORDER, 'cancelled', 0)])
    try:
        core.restore()
        finish_startup(core, kernel, data)
        assert core.request_record('a', 'r1').outcome.status == OutcomeStatus.REJECTED
        assert core.held('a', FRONT) == 0 and any('broker book is taken as the truth' in a.text for a in core.alerts_for('critical'))
    finally:
        core.close()


def test_a_request_with_no_order_id_on_record_ends_abandoned_after_the_ledger_is_taken_from_the_book(tmp_path):
    core, kernel, data, sc, seen = restart_harness(tmp_path, [SUBMIT], None, [{'symboltoken': 'T1', 'netqty': '30'}])
    try:
        core.restore()
        assert core.request_record('a', 'r1').outcome.status == OutcomeStatus.UNCONFIRMED
        finish_startup(core, kernel, data)
        out = core.request_record('a', 'r1').outcome
        assert out.status == OutcomeStatus.ABANDONED and 'no order id' in out.detail and not out.confirmed
        assert core.held('a', FRONT) == 3 and seen['status'] == OutcomeStatus.ABANDONED
    finally:
        core.close()


def test_a_paper_request_in_flight_at_the_restart_is_abandoned_and_keeps_the_persisted_paper_ledger(tmp_path):
    core, kernel, data, sc, seen = restart_harness(tmp_path, [SUBMIT, dict(PLACED, order_id='PAPER00001')],
                                                   {('a', 'T1'): [3, 100.0, T0]}, [], paper=True)
    try:
        core.restore()
        finish_startup(core, kernel, data)
        out = core.request_record('a', 'r1').outcome
        assert out.status == OutcomeStatus.ABANDONED and 'paper' in out.detail and core.held('a', FRONT) == 3
    finally:
        core.close()


# ---- the paper pool never stands in for the account ----------------------------------------------------------------------------

def test_with_no_engine_named_the_cash_is_the_live_account_even_when_only_a_paper_engine_is_registered(hestias):
    h = hestias([('p', factory(name='p'), {'paper': True})], config=FakeConfig(available_cash=7_000_000.0, paper_cash=123_000.0))
    assert h.available_cash() == 7_000_000.0 and h.available_cash('p') == 123_000.0
    go(h, 5)
    assert 'account free cash Rs 7,000,000' in build_session_report(h, h.now, [])


# ---- the real login, with the SDK and the TOTP library stubbed ------------------------------------------------------------------

def test_real_login_uses_the_credentials_file_and_returns_the_tokens_without_any_network(tmp_path, monkeypatch):
    import importlib
    import sys
    import types
    calls = {}

    class FakeSmartConnect:
        def __init__(self, api_key):
            calls['api_key'] = api_key

        def generateSession(self, client, password, totp):
            calls['session'] = (client, password, totp)
            return {'status': True, 'data': {'jwtToken': 'JWT'}}

        def getfeedToken(self):
            return 'FEED'
    monkeypatch.setitem(sys.modules, 'SmartApi', types.SimpleNamespace(SmartConnect=FakeSmartConnect))
    monkeypatch.setitem(sys.modules, 'pyotp', types.SimpleNamespace(TOTP=lambda secret: types.SimpleNamespace(now=lambda: f'otp-{secret}')))
    creds = tmp_path / 'creds.csv'
    creds.write_text('api_key,user_name,password,qr_code,slack_token\nKEY,USER,PW,SECRET,xoxb\n')
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)
    hestia = importlib.import_module('hestia')
    monkeypatch.setattr(hestia.cfg, 'CREDS_FILE', creds)
    got = hestia.real_login()
    assert (got.auth_token, got.feed_token, got.client_code, got.api_key) == ('JWT', 'FEED', 'USER', 'KEY')
    assert calls == {'api_key': 'KEY', 'session': ('USER', 'PW', 'otp-SECRET')}
    assert hestia._slack_token() == 'xoxb'

    class Refusing(FakeSmartConnect):
        def generateSession(self, *a):
            return {'status': False, 'message': 'bad'}
    monkeypatch.setitem(sys.modules, 'SmartApi', types.SimpleNamespace(SmartConnect=Refusing))
    with pytest.raises(RuntimeError, match='login failed'):
        hestia.real_login()


def test_the_shipped_credentials_path_is_the_one_prometheus_uses():
    import hestia_config
    assert str(hestia_config.CREDS_FILE).endswith('data/user_credentials.csv')
    src = (os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'prometheus_production', 'prometheus_configs.py'))
    assert "CREDS_FILE   = REPO_ROOT / 'data' / 'user_credentials.csv'" in open(src).read()
