"""Harness for running the whole HestiaHost end to end against doubles, on a fast simulated clock. Nothing here can reach a broker."""
import threading
import time
import types
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from hestia_data_helpers import write_master
from hestia_fake_helpers import FRONT, NEXT, RecEngine, minutes_frame
from smartapi_double import CandleSmartConnect, FakeClock, FakeFeed, ScriptedSmartConnect
import hestia_config
from hestia_core.history import merge_and_save
from hestia_core.host import HestiaHost, HostDeps, LoginResult
from hestia_core.reactor import scaled_clock

START = datetime(2026, 9, 3, 8, 50)                       # a Thursday, US DST in force: the session closes at 23:30
SCALE = 600.0                                             # 10 simulated minutes per real second


class HostDouble:
    """One object standing in for the logged-in SmartConnect: candles and quotes from prepared frames as of the simulated
    clock, orders and positions from the scripted order book, and a record of terminateSession."""

    def __init__(self, now_fn, frames):
        self.candle = CandleSmartConnect(now_fn, frames)
        self.orders = ScriptedSmartConnect(FakeClock())
        self.orders.price = 100.0
        self.terminated = []
        self.logins = 0

    def getCandleData(self, params):
        return self.candle.getCandleData(params)

    def ltpData(self, *a):
        return self.candle.ltpData(*a)

    def getMarketData(self, **kw):
        return self.candle.getMarketData(**kw)

    def placeOrderFullResponse(self, params):
        return self.orders.placeOrderFullResponse(params)

    def orderBook(self):
        return self.orders.orderBook()

    def position(self):
        return self.orders.position()

    def rmsLimit(self):
        return self.orders.rmsLimit()

    def terminateSession(self, code):
        self.terminated.append(code)

    def getfeedToken(self):
        return 'feed-token'


def make_cfg(tmp: Path, engines, holidays=None, **overrides):
    """hestia_config with every path moved under `tmp` and the timings made test-sized."""
    ns = types.SimpleNamespace(**{k: getattr(hestia_config, k) for k in dir(hestia_config) if k.isupper()})
    ns.STATE_DIR, ns.FLAG_DIR, ns.CACHE_DIR, ns.TRADES_DIR = tmp / 'state', tmp / 'flags', tmp / 'cache', tmp / 'trades'
    ns.SESSION_LOCK_FILE = tmp / 'angel_session.lock'
    ns.MCX_DATA_DIR, ns.INSTRUMENT_MASTER_FILE = tmp / 'mcx', tmp / 'master.csv'
    ns.MCX_HOLIDAYS_FILE = holidays or tmp / 'no_holidays.csv'
    ns.ENGINES = engines
    ns.CORE = dict(hestia_config.CORE, reconcile_interval_s=None, ledger_reconcile_interval_s=None, restart_backoff_s=(0.5, 1.0, 2.0))
    ns.LIVE_DATA = dict(hestia_config.LIVE_DATA, seed_days=2, poll_stagger_s=0.5, seed_retry_attempts=1, ltp_refresh_s=0)
    ns.ANGEL = dict(order_timeout_s=30.0, poll_interval_s=0.02)
    ns.LIFECYCLE = dict(engine_join_timeout_s=5.0, drain_timeout_s=5.0, flush_timeout_s=2.0, terminate_despite_hung=False)
    ns.FLAG_POLL_S = 0.5
    ns.BOOTSTRAP_WAIT_S = 5.0
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def write_market_files(tmp: Path, refs=(FRONT, NEXT), pipeline_days=(date(2026, 9, 1), date(2026, 9, 2))):
    write_master(tmp / 'master.csv', refs)
    frames = {}
    for k, ref in enumerate(refs):
        df = minutes_frame(100.0 + 5 * k, seed=11 + k)
        frames[ref.token] = df
        merge_and_save(tmp / 'mcx' / ref.instrument / f'{ref.expiry:%Y-%m-%d}_futures.csv',
                       df[df['time_stamp'].dt.date.isin(pipeline_days)])
    return frames


class HostRun:
    """Runs HestiaHost.run() on a background thread against a HostDouble; `clock` is the shared simulated clock."""

    def __init__(self, tmp: Path, engines, factories, frames=None, start=START, holidays=None, login_error=None, feed=None,
                 make_double=None, **cfg_overrides):
        self.tmp = tmp
        self.frames = frames if frames is not None else write_market_files(tmp)
        self.clock = scaled_clock(start, SCALE)
        self.double = make_double(self.clock, self.frames) if make_double else HostDouble(self.clock, self.frames)
        self.slack = []
        self.feed = feed
        self.cfg = make_cfg(tmp, engines, holidays, **cfg_overrides)

        def login():
            self.double.logins += 1
            if login_error:
                raise login_error
            return LoginResult(self.double, 'auth', 'feed', 'CLIENT1', 'key')
        self.login_calls = lambda: self.double.logins
        self.deps = HostDeps(login=login, make_feed=lambda lr, alert: self.feed, slack_post=lambda ch, t: self.slack.append((ch, t)),
                             clock=self.clock, time_scale=SCALE, install_signals=False, engine_factories=factories,
                             executor_workers=3)
        self.result = None
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            self.result = HestiaHost(self.cfg, self.deps).run()
        except BaseException as exc:                          # noqa: BLE001
            self.error = exc

    def start(self):
        self.thread.start()
        return self

    def join(self, timeout=30.0):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), 'the host did not finish'
        if self.error:
            raise self.error
        return self.result

    def texts(self):
        return [t for _, t in self.slack]


def wait_until(cond, timeout=20.0, what='condition'):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError(f'timed out after {timeout}s waiting for {what}')
