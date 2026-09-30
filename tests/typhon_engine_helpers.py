"""Builders for the Typhon engine tests: a scripted price path inside a FakeHestia. Mirrors tests/prometheus_engine_helpers.py,
simplified for a single position group (CFG overrides lots_per_unit to 1 so ported numeric assertions carry over; Typhon's real value is 2, DEFAULT.lots_per_unit, exercised in test_typhon_engine_lots_per_unit.py) -- unlike Selene's/Helios's own single-lot tests, Typhon carries ONE target alongside
the stop (plan Step 4), so CFG's own target_pct is exercised too, not just its sl_pct."""
import dataclasses
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from hestia_fake_helpers import DAYS, FRONT, MINUTES_PER_DAY, NEXT, SESSION_DATE, SESSION_OPEN, world  # noqa: E402,F401
from hestia_core.fake import ContractSpec  # noqa: E402
from typhon_engine.engine import TyphonEngine  # noqa: E402
from typhon_engine.engine_configs import DEFAULT  # noqa: E402

CFG = dataclasses.replace(DEFAULT, instrument='XX', lots_per_unit=1)


def hm(h: int, m: int = 0) -> datetime:
    return datetime(2026, 9, 3, h, m)


def scripted_minutes(waypoints, base: float = 100.0, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    xs = [(h - 9) * 60 + m for h, m, _ in waypoints]
    ys = [p for _, _, p in waypoints]
    rows = []
    for d_i, d in enumerate(DAYS):
        for m in range(MINUTES_PER_DAY):
            ts = datetime.combine(d, datetime.min.time()) + timedelta(hours=9, minutes=m)
            price = float(np.interp(m, xs, ys)) if d_i == len(DAYS) - 1 else base + 0.15 * np.sin(m / 40.0)
            rows.append((ts, price + 0.02 * rng.normal()))
    close = np.array([r[1] for r in rows])
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({'time_stamp': [r[0] for r in rows], 'open': open_, 'high': np.maximum(open_, close) + 0.02,
                         'low': np.minimum(open_, close) - 0.02, 'close': close, 'volume': 10.0})


# down a leg, then a recovery, then down again: bearish -> bullish -> bearish flips
ZIGZAG = [(9, 0, 100), (10, 0, 100), (11, 30, 92), (13, 0, 92), (15, 30, 104), (17, 0, 104), (19, 0, 92), (23, 29, 92)]
# a small drop that reverses before the stop: bearish entry ~10:15, bullish flip ~12:45, a Rule 7 flip
FLIP_PATH = [(9, 0, 100), (10, 0, 100), (11, 0, 97.6), (12, 30, 97.6), (14, 0, 100.5), (23, 29, 100.5)]
# a small drop and then nothing: the position is still open at the fallback rollover time
HOLD_PATH = [(9, 0, 100), (10, 0, 100), (11, 0, 98.6), (23, 29, 98.6)]


def scripted_world(waypoints=ZIGZAG, cfg=CFG, engines=True, extra_next=None, made=None, front=FRONT, lots_per_unit=1, **kw):
    """lots_per_unit defaults to 1 (matching CFG's override) so the numeric assertions ported from Selene's tests carry over
    unchanged; pass lots_per_unit=DEFAULT.lots_per_unit with cfg=DEFAULT (or a replace of it) to exercise the real 2-lot unit,
    test_typhon_engine_lots_per_unit.py."""
    def make():
        e = TyphonEngine(cfg)
        if made is not None:
            made.append(e)
        return e
    h = world(engines=[('typhon', make, {'lots_per_unit': lots_per_unit})] if engines else (), contracts=(),
              extra=[ContractSpec(front, lot_size=1, tick_size=1.0, freeze_qty_lots=600, minutes=scripted_minutes(waypoints))]
              + list(extra_next or ()), **kw)
    return h


def next_contract(waypoints, ref=NEXT, shift=5.0):
    return ContractSpec(ref, lot_size=1, tick_size=1.0, freeze_qty_lots=600,
                        minutes=scripted_minutes([(h, m, p + shift) for h, m, p in waypoints], base=100.0 + shift))


def trades(h):
    return [row for _, row in h.trades]


def alert_texts(h):
    return [a.text for a in h.alerts]
