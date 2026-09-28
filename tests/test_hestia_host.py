"""
HestiaHost end to end against doubles on a fast simulated clock (10 simulated minutes per real second): gates, the one login, the
object graph, the session, flags, restart recovery and the teardown. No broker is reachable from any of this.
"""
import ast
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from hestia_host_helpers import HostRun, START, make_cfg, wait_until, write_market_files
from hestia_fake_helpers import FRONT, NEXT, SPEC, RecEngine
import hestia_config
from hestia_config import EngineEntry
from hestia_core.host import HestiaHost, HostDeps, LoginResult, seconds_until_evening_open
from hestia_core.interface import (AckStatus, Direction, OpenRequest, OutcomeStatus, RequestOutcome, SessionStart, Stop,
                                   StopReason)
from hestia_core.state_store import StateStore

REPO = Path(__file__).resolve().parents[1]
ENGINES = {'a': EngineEntry(instrument='XX', factory='unused:unused', enabled=True)}


def trader(log, seen):
    """Opens 3 lots once; on a restart it finds its earlier request already known and tries the same id again."""
    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            st = ctx.request_status('o1')
            seen['status'] = None if st is None else st.status
            seen['ack'] = ctx.submit(OpenRequest('o1', FRONT, Direction.BULLISH, 3, trade_ref=1)).status
    return lambda: RecEngine('a', act=act, log=log, timeout=0.05)


def drop_flag(run):
    (run.cfg.FLAG_DIR / 'hestia_active.flag').unlink()


# ---- gates: the host must not log in unless it means to trade -------------------------------------------------------------

def test_with_no_engine_enabled_the_host_never_logs_in(tmp_path):
    disabled = {n: EngineEntry(e.instrument, e.factory, enabled=False) for n, e in hestia_config.ENGINES.items()}
    run = HostRun(tmp_path, disabled, {}).start()
    r = run.join()
    assert not r.started and r.reason == 'no engines enabled' and run.login_calls() == 0
    assert not (run.cfg.SESSION_LOCK_FILE).exists() and not (run.cfg.FLAG_DIR / 'hestia_active.flag').exists()
    assert any('not logging in' in t for t in run.texts())


def test_the_shipped_configuration_starts_no_engines():
    assert not [n for n, e in hestia_config.ENGINES.items() if e.enabled], 'engines are enabled only when they exist (P5, P7)'


def test_a_market_holiday_or_weekend_is_not_a_trading_day_and_nothing_logs_in(tmp_path):
    run = HostRun(tmp_path, ENGINES, {}, start=datetime(2026, 9, 5, 8, 50)).start()               # a Saturday
    r = run.join()
    assert not r.started and r.reason == 'mcx closed' and run.login_calls() == 0
    holidays = tmp_path / 'holidays.csv'
    holidays.write_text('date,morning_session_closed,evening_session_closed,holiday_name\n2026-09-03,True,True,Test holiday\n')
    tmp2 = tmp_path / 'b'
    tmp2.mkdir()
    run2 = HostRun(tmp2, ENGINES, {}, holidays=holidays).start()
    assert run2.join().reason == 'mcx closed' and run2.login_calls() == 0


def test_a_session_held_by_another_owner_stops_the_host_before_any_login(tmp_path):
    run = HostRun(tmp_path, ENGINES, {})
    run.cfg.SESSION_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    run.cfg.SESSION_LOCK_FILE.write_text(json.dumps({'pid': os.getppid(), 'owner': 'data-downloader', 'since': 'x'}))
    r = run.start().join()
    assert not r.started and r.reason == 'session held by another process' and run.login_calls() == 0
    assert run.cfg.SESSION_LOCK_FILE.exists(), "the other owner's lock is left alone"
    assert any('data-downloader' in t for t in run.texts())


def test_a_live_standalone_prometheus_stops_the_host_before_any_login(tmp_path):
    pid_file = tmp_path / 'prometheus.pid'
    pid_file.write_text(str(os.getppid()))                                   # a live process, not ours
    run = HostRun(tmp_path, ENGINES, {}, LEGACY_PID_FILES={'standalone Prometheus': pid_file})
    r = run.start().join()
    assert not r.started and r.reason == 'another login is live' and run.login_calls() == 0
    assert any('standalone Prometheus is running' in t for t in run.texts())
    tmp2 = tmp_path / 'b'
    tmp2.mkdir()
    dead = tmp2 / 'prometheus.pid'
    dead.write_text('2147483000')                                              # a stale pid file names no live process
    from hestia_core.session_lock import pid_alive
    assert not pid_alive(2147483000)


def test_engines_gated_by_disable_or_kill_flags_do_not_start_the_host(tmp_path):
    run = HostRun(tmp_path, ENGINES, {})
    run.cfg.FLAG_DIR.mkdir(parents=True, exist_ok=True)
    (run.cfg.FLAG_DIR / 'a_command.flag').write_text('DISABLE\n')
    r = run.start().join()
    assert not r.started and r.reason == 'all engines gated' and run.login_calls() == 0


def test_a_failed_login_leaves_no_lock_or_flag_and_terminates_nothing(tmp_path):
    run = HostRun(tmp_path, ENGINES, {'a': lambda: RecEngine('a')}, login_error=RuntimeError('bad totp')).start()
    r = run.join()
    assert not r.started and r.reason == 'login failed' and run.double.terminated == []
    assert not run.cfg.SESSION_LOCK_FILE.exists() and not (run.cfg.FLAG_DIR / 'hestia_active.flag').exists()
    assert any('login failed' in t for t in run.texts())


def test_the_evening_only_deferral_wakes_shortly_before_the_open():
    open_dt = datetime(2026, 9, 14, 17, 0)
    assert seconds_until_evening_open(datetime(2026, 9, 14, 9, 0), open_dt, 5) == 8 * 3600 - 300
    assert seconds_until_evening_open(datetime(2026, 9, 14, 16, 58), open_dt, 5) == 0.0


def test_generate_session_appears_in_exactly_one_place_and_never_on_import():
    hits = []
    for path in [REPO / 'hestia.py', REPO / 'hestia_config.py', *sorted((REPO / 'hestia_core').glob('*.py'))]:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == 'generateSession':
                hits.append((path.name, node.lineno))
    assert [h[0] for h in hits] == ['hestia.py'], 'the only login site is real_login in hestia.py'
    import importlib
    import sys
    sys.path.insert(0, str(REPO))
    mod = importlib.import_module('hestia')
    assert mod._feeds == [] and callable(mod.real_login), 'importing the entry point logs in to nothing'


# ---- the whole session --------------------------------------------------------------------------------------------------

def test_a_full_session_logs_in_once_trades_and_tears_down_in_order(tmp_path):
    log, seen = [], {}
    run = HostRun(tmp_path, ENGINES, {'a': trader(log, seen)}).start()
    wait_until(lambda: run.double.orders.orders, what='the order at the broker')
    wait_until(lambda: any(isinstance(e, RequestOutcome) for _, e in log), what='the outcome')
    assert run.login_calls() == 1
    assert (run.cfg.FLAG_DIR / 'hestia_active.flag').exists() and json.loads(run.cfg.SESSION_LOCK_FILE.read_text())['owner'] == 'hestia'
    assert [(o['transactiontype'], o['quantity']) for o in run.double.orders.orders] == [('BUY', '30')]
    drop_flag(run)
    r = run.join()
    assert r.started and r.reason == 'ended: shutdown' and r.teardown.terminated
    assert r.teardown.steps == ['stop_engines', 'engines_finished', 'drained', 'flushed', 'terminated', 'reactor_stopped']
    assert run.double.terminated == ['CLIENT1'], 'terminateSession, once, with the client code'
    stops = [e for _, e in log if isinstance(e, Stop)]
    assert len(stops) == 1 and stops[0].reason == StopReason.SHUTDOWN and stops[0].leave_position
    assert r.core.held('a', FRONT) == 3, 'shutdown leaves the position open'
    assert not run.cfg.SESSION_LOCK_FILE.exists() and not (run.cfg.FLAG_DIR / 'hestia_active.flag').exists()
    texts = run.texts()
    assert any('logging in' in t for t in texts) and any('Hestia running' in t for t in texts)
    report = [t for t in texts if t.startswith('*Hestia session report*')]
    assert len(report) == 1 and 'a (live, ended): XX30OCT26FUT +3' in report[0]
    stopped = [t for t in texts if 'Hestia stopped' in t]
    assert len(stopped) == 1 and texts.index(report[0]) < texts.index(stopped[0]), 'the report goes out before the final message'


def test_state_and_journal_are_on_disk_and_a_restart_never_resends_and_trusts_the_broker_book(tmp_path):
    log1, seen1 = [], {}
    run1 = HostRun(tmp_path, ENGINES, {'a': trader(log1, seen1)}).start()
    wait_until(lambda: any(isinstance(e, RequestOutcome) for _, e in log1), what='the first outcome')
    drop_flag(run1)
    run1.join()
    assert seen1 == {'status': None, 'ack': AckStatus.ACCEPTED}
    store = StateStore(run1.cfg.STATE_DIR)
    assert store.load_ledger()[('a', 'T1')][:2] == [3, 100.0]
    assert [r['t'] for r in store.load_journal(date(2026, 9, 3))] == ['submit', 'placed', 'final', 'attempt_done']

    # a second process, same directories, the broker now showing 5 lots (an order landed that the first process never saw)
    def broker_with_five(clock, frames):
        from hestia_host_helpers import HostDouble
        d = HostDouble(clock, frames)
        d.orders.position_rows = [{'symboltoken': 'T1', 'netqty': '50', 'netprice': '101.0'}]
        return d
    log2, seen2 = [], {}
    run2 = HostRun(tmp_path, ENGINES, {'a': trader(log2, seen2)}, frames=run1.frames, make_double=broker_with_five).start()
    wait_until(lambda: seen2, what='the second run to start its engine')
    time.sleep(0.5)                                                   # let the ledger correction land
    drop_flag(run2)
    r2 = run2.join()
    assert seen2 == {'status': OutcomeStatus.FILLED, 'ack': AckStatus.DUPLICATE}
    assert run2.double.orders.orders == [], 'the same request id was never sent twice'
    assert r2.core.held('a', FRONT) == 5
    assert any('broker book is taken as the truth' in a.text for a in r2.core.alerts_for('critical'))
    assert not any('IN DOUBT' in t for t in run2.texts()), 'every request had a final line, so nothing is in doubt'


def test_a_kill_flag_written_mid_session_stops_only_that_engine_and_keeps_the_position(tmp_path):
    log, seen = [], {}
    run = HostRun(tmp_path, ENGINES, {'a': trader(log, seen)}).start()
    wait_until(lambda: any(isinstance(e, RequestOutcome) for _, e in log), what='the fill')
    (run.cfg.FLAG_DIR / 'a_command.flag').write_text('KILL\n')
    wait_until(lambda: any(isinstance(e, Stop) for _, e in log), what='the engine to be stopped by the flag')
    assert [e.reason for _, e in log if isinstance(e, Stop)] == [StopReason.KILL]
    drop_flag(run)
    r = run.join()
    assert r.core.held('a', FRONT) == 3 and (run.cfg.FLAG_DIR / 'a_command.flag').read_text().strip() == 'KILL'


def test_a_paper_engine_runs_through_the_same_host_without_touching_the_broker(tmp_path):
    log, seen = [], {}
    paper = {'a': EngineEntry(instrument='XX', factory='unused:unused', enabled=True, paper=True)}
    run = HostRun(tmp_path, paper, {'a': trader(log, seen)}).start()
    wait_until(lambda: any(isinstance(e, RequestOutcome) for _, e in log), what='the paper fill')
    drop_flag(run)
    r = run.join()
    assert run.double.orders.orders == [], 'a paper engine sends nothing to the broker'
    assert r.core.held('a', FRONT) == 3 and r.core.broker.pool_of('a') == 'paper:a'
    assert any('a (paper' in t for t in run.texts())


def test_the_unit_cap_from_configuration_is_enforced_at_admission(tmp_path):
    log, seen = [], {}
    capped = {'a': EngineEntry(instrument='XX', factory='unused:unused', enabled=True, unit_cap=2)}
    run = HostRun(tmp_path, capped, {'a': trader(log, seen)}).start()
    wait_until(lambda: any(isinstance(e, RequestOutcome) for _, e in log), what='the outcome')
    drop_flag(run)
    r = run.join()
    assert [o.status for _, _, o in r.core.outcome_log] == [OutcomeStatus.LIMIT_REFUSED] and run.double.orders.orders == []
