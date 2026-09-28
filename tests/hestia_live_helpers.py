"""Builders for the real-time runtime tests: a live core on the RealReactor with engine threads and a stub data source."""
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hestia_core.broker_router import BrokerRouter  # noqa: E402
from hestia_core.core import CoreConfig, HestiaCore  # noqa: E402
from hestia_core.interface import ContractInfo, ContractRef, LtpQuote, TrackReady  # noqa: E402
from hestia_core.paper_broker import PaperBroker  # noqa: E402
from hestia_core.reactor import RealReactor  # noqa: E402
from hestia_core.replay import BrokerReply, SimBroker  # noqa: E402
from hestia_core.thread_runner import ThreadTask  # noqa: E402

FRONT = ContractRef('XX', 'T1', 'XX30OCT26FUT', date(2026, 10, 30))
NEXT = ContractRef('XX', 'T2', 'XX30NOV26FUT', date(2026, 11, 30))
YY = ContractRef('YY', 'U1', 'YY30OCT26FUT', date(2026, 10, 30))


class StubData:
    """A DataPort with fixed prices and no bars: the runtime tests drive engines through SessionStart and requests."""

    def __init__(self, reactor, refs, prices):
        self.reactor, self.refs = reactor, {r.token: r for r in refs}
        self.prices = dict(prices)
        self.session_date = date(2026, 9, 3)
        self.session_open = datetime(2020, 1, 1)                # already past: the core launches engines at once
        self.session_close = None
        self.core = None

    def attach(self, core):
        self.core = core

    def knows(self, token):
        return token in self.refs

    def infos(self, instrument):
        return tuple(ContractInfo(r, 10, 0.5, 20, 40) for r in self.refs.values() if r.instrument == instrument)

    def info(self, ref):
        return ContractInfo(ref, 10, 0.5, 20, 40) if ref.token in self.refs else None

    def ref_for(self, token):
        return self.refs.get(token)

    def seeded(self, instrument):
        return tuple(r for r in self.refs.values() if r.instrument == instrument)

    def price(self, token):
        return self.prices.get(token)

    def ltp_quote(self, token):
        p = self.prices.get(token)
        return None if p is None else LtpQuote(p, self.reactor.now, 0.0)

    def latest_bar(self, token, spec):
        return None

    def st_series(self, token, spec, last_n):
        return ()

    def price_near(self, token, ts, tol):
        return None

    def track(self, task, contract):
        self.reactor.after(0.01, lambda: self.core.deliver_to(task.name, TrackReady(contract)))

    def untrack(self, task, contract):
        task.tracked.discard(contract.token)


FAST = dict(dispatch_window_s=0.02, reject_retry_cooldown_s=0.02, restart_backoff_s=(0.05, 0.1, 0.2), restart_window_s=60.0,
            silence_warn_s=0.3, silence_critical_s=0.6, critical_repeat_s=0.3, monitor_period_s=0.05,
            reconcile_interval_s=None, ledger_reconcile_interval_s=None)


class Live:
    """A running live core. Use as a context manager, or call close()."""

    def __init__(self, engines, broker=None, refs=(FRONT, NEXT, YY), prices=None, **cfg):
        self.reactor = RealReactor()
        self.reactor.start()
        self.data = StubData(self.reactor, refs, prices or {'T1': 100.0, 'T2': 101.0, 'U1': 50.0})
        margin = lambda token, net, avg: abs(net) * avg * 5.0                       # noqa: E731
        self.sim = SimBroker(self.reactor, broker or (lambda c: BrokerReply('fill', latency=0.02)), self.data.price, margin,
                             10_000_000.0, 0.0, 0.3, 0.05, 0.02)
        self.paper = PaperBroker(self.reactor, self.data.price, margin, latency_s=0.02)
        self.executor = ThreadPoolExecutor(max_workers=2)
        params = {**FAST, **cfg}
        self.core = HestiaCore(self.reactor, self.data, BrokerRouter(self.sim, self.paper), CoreConfig(**params), ThreadTask)
        for item in engines:
            name, factory = item[0], item[1]
            self.core.register(name, factory, **(item[2] if len(item) > 2 else {}))

    def start(self):
        with self.reactor.lock:
            self.core.begin_session()
        return self

    def close(self):
        with self.reactor.lock:
            self.core.close()
        self.reactor.stop()
        self.executor.shutdown(wait=False)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    def held(self, engine, contract):
        with self.reactor.lock:
            return self.core.held(engine, contract)


def wait_until(cond, timeout=5.0, what='condition'):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.005)
    raise AssertionError(f'timed out after {timeout}s waiting for {what}')
