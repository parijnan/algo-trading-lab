"""Builders for the Prometheus engine tests: a scripted price path (so flips land where the test needs them) inside a FakeHestia."""
import dataclasses
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from hestia_fake_helpers import DAYS, FRONT, MINUTES_PER_DAY, NEXT, SESSION_DATE, SESSION_OPEN, world  # noqa: E402,F401
from hestia_core.fake import ContractSpec  # noqa: E402
from prometheus_engine.engine import PrometheusEngine  # noqa: E402
from prometheus_engine.engine_configs import DEFAULT  # noqa: E402

# The scripted price paths and hand-computed levels in these tests are built around ST 2.0 with SL 2.2 / T1 2.2 / T2 5.0 (the geometry the engine
# logic is exercised on), so the test config pins those values explicitly instead of following the engine's live DEFAULT, which moved to 2.5 with
# SL 1.0 / T1 1.25 / T2 4.0 on 2026-10-05. test_engine_config_matches_production_configs separately pins DEFAULT to production.
FIXTURE_LEVELS = dict(st_multiplier=2.0, sl_pct=2.2, target1_pct=2.2, target2_flat_pct=5.0)
CFG = dataclasses.replace(DEFAULT, instrument='XX', **FIXTURE_LEVELS)


def hm(h: int, m: int = 0) -> datetime:
    return datetime(2026, 9, 3, h, m)


def scripted_minutes(waypoints, base: float = 100.0, seed: int = 5) -> pd.DataFrame:
    """Two flat seed days, then a session day whose price follows `waypoints` = [(hour, minute, price), ...] linearly."""
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


# down a leg, then a long recovery, then down again: the ST flips bearish then bullish then bearish
ZIGZAG = [(9, 0, 100), (10, 0, 100), (11, 30, 92), (13, 0, 92), (15, 30, 104), (17, 0, 104), (19, 0, 92), (23, 29, 92)]


def scripted_world(waypoints=ZIGZAG, cfg=CFG, engines=True, contracts=(FRONT,), extra_next=None, made=None, front=FRONT, **kw):
    """A FakeHestia whose FRONT contract follows `waypoints`; the Prometheus engine registered as 'prometheus' (1 unit = 2 lots)."""
    def make():
        e = PrometheusEngine(cfg)
        if made is not None:
            made.append(e)
        return e
    h = world(engines=[('prometheus', make, {'lots_per_unit': 2})] if engines else (),
              contracts=(), extra=[ContractSpec(front, lot_size=10, tick_size=0.5, freeze_qty_lots=20, minutes=scripted_minutes(waypoints))]
              + list(extra_next or ()), **kw)
    return h


def next_contract(waypoints, ref=NEXT, shift=5.0):
    """The next contract: the same path, shifted by a constant basis."""
    return ContractSpec(ref, lot_size=10, tick_size=0.5, freeze_qty_lots=20,
                        minutes=scripted_minutes([(h, m, p + shift) for h, m, p in waypoints], base=100.0 + shift))


# a small drop that reverses before the first target: bearish entry ~10:15, bullish flip ~12:45, a Rule 7 flip
FLIP_PATH = [(9, 0, 100), (10, 0, 100), (11, 0, 97.6), (12, 30, 97.6), (14, 0, 100.5), (23, 29, 100.5)]
# a small drop and then nothing: the position is still open at the fallback rollover time
HOLD_PATH = [(9, 0, 100), (10, 0, 100), (11, 0, 98.6), (23, 29, 98.6)]



def trades(h):
    return [row for _, row in h.trades]


def alert_texts(h):
    return [a.text for a in h.alerts]
