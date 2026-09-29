"""
HestiaHost: the process around the core (plans/hestia-p4-live-services.md, slice 5). It owns the whole life of a Hestia run:

  gates (MCX closed today, no engine enabled, another owner holding the Angel One session)
  -> the session lock -> the evening-only deferral -> the ONE login -> the object graph (reactor, gateway, Angel adapter, paper
  broker, live data, core, flags, Slack) -> restore from the state directory -> seed the data -> take the broker's book as the
  truth for the ledger -> start the engines -> run until the host flag is removed, a signal arrives, or the session ends
  -> the teardown order of hestia_core.lifecycle (the session report goes out just before the Slack flush)
  -> release the lock and the flags.

Everything external is injected through HostDeps (the login, the feeds, the Slack poster, the clock, sleep), so the entire run is
exercised in tests against doubles with no broker in reach. `hestia.py` at the repo root is the only place that builds the real
ones, and `real_login` there is the only place `generateSession` appears.

Safety property, tested: with no engine enabled the host returns before it logs in, so starting it cannot evict another process's
Angel One session.
"""

from __future__ import annotations

import importlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional

from hestia_core.alert_router import AlertRouter
from hestia_core.angel_broker import AngelBrokerPort, AngelConfig
from hestia_core.broker_router import BrokerRouter
from hestia_core.core import Alert, CoreConfig, HestiaCore
from hestia_core.feed_port import FeedPort
from hestia_core.flags import FlagFiles, FlagWatcher
from hestia_core.gateway import BrokerGateway
from hestia_core.interface import SizingConfig, StopReason
from hestia_core.lifecycle import Lifecycle, LifecycleConfig, TeardownReport
from hestia_core.live_data import LiveData, LiveDataConfig
from hestia_core.mcx_market import ContractCatalog, MarketCalendar
from hestia_core.order_feed import OrderUpdateFeed
from hestia_core.paper_broker import PaperBroker
from hestia_core.reactor import RealReactor
from hestia_core.reporting import RunningRowWriter, TradeLogWriter, build_session_report
from hestia_core.session_lock import LockHeld, SessionLock, holder, pid_alive
from hestia_core.sizing import SizingStore
from hestia_core.slack_queue import SlackQueue
from hestia_core.state_store import StateStore
from hestia_core.thread_runner import ThreadTask

import os
from pathlib import Path

log = logging.getLogger('hestia_host')


@dataclass
class LoginResult:
    obj: object
    auth_token: str
    feed_token: str
    client_code: str
    api_key: str


@dataclass
class HostDeps:
    login: Callable[[], LoginResult]
    make_feed: Callable[[LoginResult, Callable[[str], None]], Optional[FeedPort]] = lambda lr, alert: None
    make_order_feed: Callable[[LoginResult], Optional[OrderUpdateFeed]] = lambda lr: None
    stop_feeds: Callable[[], None] = lambda: None
    slack_post: Optional[Callable[[str, str], None]] = None
    slack_token: str = ''
    clock: Callable[[], datetime] = datetime.now
    time_scale: float = 1.0                                   # tests only
    sleep: Callable[[float], None] = time.sleep
    install_signals: bool = True
    engine_factories: Dict[str, Callable] = field(default_factory=dict)      # overrides the config's dotted paths
    executor_workers: int = 4


@dataclass
class HostResult:
    started: bool
    reason: str = ''
    teardown: Optional[TeardownReport] = None
    core: Optional[HestiaCore] = None
    session_report: str = ''
    events: List[str] = field(default_factory=list)


def load_factory(path: str) -> Callable:
    module, _, attr = path.partition(':')
    return getattr(importlib.import_module(module), attr)


def seconds_until_evening_open(now: datetime, open_dt: datetime, buffer_min: float) -> float:
    return max(0.0, (open_dt - now).total_seconds() - buffer_min * 60)


class HestiaHost:

    def __init__(self, cfg, deps: HostDeps):
        self.cfg, self.deps = cfg, deps

    # ---- gates -------------------------------------------------------------------------------------------------------------

    def _enabled(self) -> Dict[str, object]:
        return {name: e for name, e in self.cfg.ENGINES.items() if e.enabled}

    def run(self) -> HostResult:
        cfg, deps = self.cfg, self.deps
        for d in (cfg.STATE_DIR, cfg.FLAG_DIR, cfg.CACHE_DIR, cfg.TRADES_DIR, cfg.RUNNING_ROW_DIR):
            d.mkdir(parents=True, exist_ok=True)
        today = deps.clock().date()
        slack = SlackQueue(deps.slack_token, deps.slack_post)
        router = AlertRouter(slack, cfg.ALERT_CHANNELS, cfg.ALERT_COOLDOWN_S)

        def host_alert(level: str, text: str, emoji: Optional[str] = None) -> None:
            router(Alert(deps.clock(), level, None, text, emoji=emoji))

        calendar = MarketCalendar(cfg.MCX_HOLIDAYS_FILE)
        if calendar.missing:
            host_alert('warning', f'{cfg.MCX_HOLIDAYS_FILE.name} not found: trading-day counts will only exclude weekends')
        if calendar.fully_closed(today):
            host_alert('info', f'MCX is closed today ({today:%A %d %b}); not starting')
            slack.flush(5.0)
            return HostResult(False, 'mcx closed')
        engines = self._enabled()
        if not engines:
            host_alert('info', 'no engine is enabled in hestia_config.ENGINES; not starting (and not logging in)')
            slack.flush(5.0)
            return HostResult(False, 'no engines enabled')

        flags = FlagFiles(cfg.FLAG_DIR)
        gated = {n: flags.read_command(n) for n in engines if flags.read_command(n) in ('DISABLE', 'KILL')}
        live_engines = {n: e for n, e in engines.items() if n not in gated}
        if not live_engines:
            host_alert('info', f'every enabled engine is gated by its flag ({gated}); not starting')
            slack.flush(5.0)
            return HostResult(False, 'all engines gated')

        for label, pid_file in getattr(cfg, 'LEGACY_PID_FILES', {}).items():
            try:
                pid = int(Path(pid_file).read_text().strip())
            except (FileNotFoundError, ValueError):
                continue
            if pid_alive(pid):
                host_alert('critical', f'{label} is running (pid {pid}) on the same Angel One account; not starting, a second login '
                                       f'would evict its orders')
                slack.flush(5.0)
                return HostResult(False, 'another login is live')
        lock = SessionLock(cfg.SESSION_LOCK_FILE, 'hestia')
        existing = holder(cfg.SESSION_LOCK_FILE)
        if existing is not None and existing.get('owner') != 'hestia' and int(existing['pid']) != os.getpid():
            host_alert('critical', f"the Angel One session is held by {existing.get('owner')} (pid {existing['pid']}); not starting")
            slack.flush(5.0)
            return HostResult(False, 'session held by another process')
        replaced = lock.takeover(sleep=deps.sleep)
        if replaced:
            host_alert('warning', f'replaced a previous Hestia process (pid {replaced})')
        flags.raise_host_flag()

        result = HostResult(True)
        login: Optional[LoginResult] = None
        runtime = SimpleNamespace(reactor=None, executor=None)
        try:
            if calendar.evening_only(today):
                wait = seconds_until_evening_open(deps.clock(), calendar.session_open(today), cfg.EVENING_SESSION_WAKE_BUFFER_MIN)
                host_alert('info', f'MCX morning session closed today; evening session only. Deferring start by {wait / 60:.0f} min')
                if wait > 0:
                    deps.sleep(wait / deps.time_scale)
            host_alert('info', 'logging in to Angel One')
            try:
                login = deps.login()
            except Exception as exc:                                    # noqa: BLE001
                host_alert('critical', f'Angel One login failed: {exc!r}')
                result.started, result.reason = False, 'login failed'
                return result
            host_alert('info', f'Angel One login successful (client {login.client_code})')
            self._run_session(login, live_engines, gated, today, calendar, slack, router, flags, result, runtime)
        except Exception as exc:                                        # noqa: BLE001
            log.exception('unhandled exception in the host')
            host_alert('critical', f'Hestia crashed: {exc!r}')
            result.reason = f'crashed: {exc!r}'
            self._emergency_stop(login, runtime, result, host_alert)
        finally:
            flags.drop_host_flag()
            lock.release()
            try:
                deps.stop_feeds()
            except Exception:                                           # noqa: BLE001
                log.exception('stopping the feeds failed')
            slack.flush(5.0)
        return result

    def _emergency_stop(self, login, runtime, result, host_alert) -> None:
        """A host crash before or during the session: stop what is running and terminate the session if no engine can still use
        it. (An engine crash never gets here: it is contained by the core.)"""
        core = result.core
        try:
            if core is not None and runtime.reactor is not None:
                with runtime.reactor.lock:
                    core.stop_all(StopReason.SHUTDOWN)
        finally:
            if runtime.reactor is not None:
                runtime.reactor.stop()
            if runtime.executor is not None:
                runtime.executor.shutdown(wait=False)

    # ---- the session -------------------------------------------------------------------------------------------------------

    def _run_session(self, login, engines, gated, today, calendar, slack, router, flags, result, rt) -> None:
        cfg, deps = self.cfg, self.deps
        scale = deps.time_scale
        clock = deps.clock
        rt.reactor = reactor = RealReactor(clock=clock, time_scale=scale,
                                           on_error=lambda exc: router(Alert(reactor.now, 'critical', None,
                                                                             f'scheduled callback failed: {exc!r}')))
        rt.executor = executor = ThreadPoolExecutor(max_workers=deps.executor_workers, thread_name_prefix='hestia-io')

        def host_alert(level: str, text: str, emoji: Optional[str] = None) -> None:
            router(Alert(reactor.now, level, None, text, emoji=emoji))

        gateway = BrokerGateway(login.obj)
        feed = deps.make_feed(login, lambda m: host_alert('warning', m))
        order_feed = deps.make_order_feed(login)
        catalog = ContractCatalog(cfg.INSTRUMENT_MASTER_FILE, cfg.MCX_DATA_DIR)
        data = LiveData(reactor, gateway, executor, catalog, calendar, feed, cfg.CACHE_DIR, LiveDataConfig(**cfg.LIVE_DATA),
                        sleep=deps.sleep, alert=host_alert)

        def lot_size(token: str) -> Optional[int]:
            row = catalog.row(token)
            return row.lot_size if row else None

        angel = AngelBrokerPort(gateway, reactor, executor, lot_size, data.info, AngelConfig(**cfg.ANGEL), order_feed,
                                lambda: reactor.now, lambda: data.session_close, deps.sleep, time.monotonic, alert=host_alert)
        core_ref: Dict[str, HestiaCore] = {}

        def paper_margin(token: str, net: int, avg: float) -> float:
            info = data.info(data.ref_for(token)) if data.ref_for(token) else None
            return 0.0 if info is None else abs(net) * core_ref['core']._margin_per_lot(info, avg)
        paper = PaperBroker(reactor, data.price, paper_margin, cfg.PAPER_CASH)
        defaults = {n: SizingConfig(e.dynamic, e.static_units, e.unit_cap) for n, e in engines.items()}
        sizing = SizingStore(cfg.STATE_DIR, defaults)
        store = StateStore(cfg.STATE_DIR)
        core = HestiaCore(reactor, data, BrokerRouter(angel, paper), CoreConfig(**cfg.CORE), ThreadTask, store=store,
                          sizing_provider=sizing.get)
        core_ref['core'] = core
        result.core = core
        trade_log = TradeLogWriter(cfg.TRADES_DIR)
        running_rows = RunningRowWriter(cfg.RUNNING_ROW_DIR)
        core.alert_sinks.append(router)
        core.trade_sinks += [trade_log.write, router.trade]
        core.running_row_sinks.append(running_rows.write)

        for name, entry in engines.items():
            factory = deps.engine_factories.get(name) or load_factory(entry.factory)
            core.register(name, factory, lots_per_unit=entry.lots_per_unit, sizing=defaults[name], paper=entry.paper)
        for name, flag in gated.items():
            core.engine_state[name] = 'killed'
            host_alert('warning', f'{name} is gated at startup by its {flag} flag and will not run')

        core.restore()
        restored = bool(core._ledger) or bool(core._registry)
        seeded = data.prepare(sorted({e.instrument for e in engines.values()}))
        host_alert('info', f'data seeded: {seeded}')

        def watcher_stop() -> None:
            lifecycle.request_shutdown()
        watcher = FlagWatcher(core, reactor, flags, watcher_stop, cfg.FLAG_POLL_S, cfg.EXIT_RETRY_S)

        def before_flush() -> None:
            text = build_session_report(core, reactor.now, list(core.trades))
            result.session_report = text
            slack.send(cfg.SLACK_TRADEBOT_CHANNEL, text)

        lifecycle = Lifecycle(core, reactor, executor, terminate=lambda: login.obj.terminateSession(login.client_code),
                              flush=slack.flush, cfg=LifecycleConfig(**cfg.LIFECYCLE), sleep=deps.sleep,
                              before_flush=before_flush)
        if deps.install_signals:
            lifecycle.install_signals()
        try:
            reactor.start()
            angel.refresh_cash_blocking()
            with reactor.lock:
                angel.start_cash_refresh()
                core.bootstrap_ledger(authoritative=restored)
            waited = 0.0
            while not core.bootstrap_done and waited < cfg.BOOTSTRAP_WAIT_S:
                deps.sleep(0.02)
                waited += 0.02
            if not core.bootstrap_done:
                host_alert('critical', 'the broker position book could not be read at startup; engines start on the persisted ledger')
            with reactor.lock:
                core.settle_restored()                                    # in-doubt requests, now that the ledger is the broker's
            with reactor.lock:
                data.begin_session(today)
                core.begin_session()
                watcher.start()
            host_alert('info', f'Hestia running: engines {sorted(engines)} (session {data.session_open:%H:%M}-{data.session_close:%H:%M})')
            reason = lifecycle.run_until_shutdown(data.session_close)
            result.reason = f'ended: {reason.value}'
            result.teardown = lifecycle.teardown(reason)
            host_alert('info', f'Hestia stopped ({reason.value}); session terminated: {result.teardown.terminated}', emoji='⏹')
        finally:
            watcher.stop()
            lifecycle.restore_signals()
            if result.teardown is None:                                  # an exception before the normal teardown
                result.teardown = lifecycle.teardown()
