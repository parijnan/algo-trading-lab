"""Shared builders for the fake-Hestia tests: synthetic 1-minute data, contracts, and a recording engine."""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hestia_core.fake import ContractSpec, FakeConfig, FakeHestia  # noqa: E402
from hestia_core.interface import ContractRef, DataSpec, SessionStart, Stop  # noqa: E402

DAYS = (date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3))      # Tue, Wed, Thu
SESSION_DATE = DAYS[-1]
SESSION_OPEN = datetime(2026, 9, 3, 9, 0)
MINUTES_PER_DAY = 870                                             # 09:00 .. 23:29

FRONT = ContractRef('XX', 'T1', 'XX30OCT26FUT', date(2026, 10, 30))
NEXT = ContractRef('XX', 'T2', 'XX30NOV26FUT', date(2026, 11, 30))
NEAR = ContractRef('XX', 'T0', 'XX08SEP26FUT', date(2026, 9, 8))          # only 4 trading days left on 2026-09-03
OTHER_FRONT = ContractRef('YY', 'U1', 'YY30OCT26FUT', date(2026, 10, 30))

SPEC = DataSpec(instrument='XX', timeframe_min=15, st_period=10, st_multiplier=2.0)
SPEC_YY = DataSpec(instrument='YY', timeframe_min=15, st_period=10, st_multiplier=2.0)


def minutes_frame(base: float, seed: int, offset: float = 0.0) -> pd.DataFrame:
    """Three sessions of 1-minute bars along a slow sine wave with noise, so 15-minute Supertrend flips several times."""
    rng = np.random.default_rng(seed)
    rows, i = [], 0
    for d in DAYS:
        for m in range(MINUTES_PER_DAY):
            ts = datetime.combine(d, datetime.min.time()) + timedelta(hours=9, minutes=m)
            rows.append((ts, i))
            i += 1
    t = np.array([r[1] for r in rows], dtype=float)
    close = base * (1 + 0.06 * np.sin(t / 260.0) + 0.0008 * rng.normal(0, 1, len(t)).cumsum() / 30) + offset
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.02
    low = np.minimum(open_, close) - 0.02
    return pd.DataFrame({'time_stamp': [r[0] for r in rows], 'open': open_, 'high': high, 'low': low, 'close': close,
                         'volume': 10.0})


def world(engines=(), config=None, broker=None, contracts=(FRONT, NEXT), holidays=None, start=None, extra=()):
    """A FakeHestia with XX contracts loaded (and any `extra` ContractSpec) and `engines` registered as (name, factory)."""
    h = FakeHestia(start or datetime(2026, 9, 3, 8, 50), config or FakeConfig(), broker=broker, holidays=holidays)
    for k, ref in enumerate(contracts):
        h.add_contract(ContractSpec(ref, lot_size=10, tick_size=0.5, freeze_qty_lots=20,
                                    minutes=minutes_frame(100.0 + 5 * k, seed=11 + k)))
    for spec in extra:
        h.add_contract(spec)
    for item in engines:
        name, factory = item[0], item[1]
        kwargs = item[2] if len(item) > 2 else {}
        h.register(name, factory, **kwargs)
    return h


class RecEngine:
    """Records every event with its time; sets the trading contract on SessionStart; `act(engine, ctx, event)` is the test's
    hook. `log` is shared across instances so a restarted engine keeps appending to the same record."""

    def __init__(self, name='eng', spec=SPEC, trade=FRONT, act=None, log=None, timeout=60.0):
        self.name, self.spec, self.trade, self.act = name, spec, trade, act
        self.events = log if log is not None else []
        self.timeout = timeout
        self.ctx = None

    def run(self, ctx):
        self.ctx = ctx
        while True:
            ev = ctx.next_event(self.timeout)
            if ev is None:
                continue
            self.events.append((ctx.now(), ev))
            if isinstance(ev, SessionStart):
                ctx.set_trading_contract(self.trade)
            if self.act is not None:
                self.act(self, ctx, ev)
            if isinstance(ev, Stop):
                return

    def of(self, cls):
        return [e for _, e in self.events if isinstance(e, cls)]


def events_of(log, cls):
    return [e for _, e in log if isinstance(e, cls)]


def factory(log=None, **kw):
    return lambda: RecEngine(log=log, **kw)
