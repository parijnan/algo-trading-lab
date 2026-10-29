"""
The Oleus engine on the fake Hestia: levels from a crafted previous session, scripted price paths through the levels, the request flow
(entry, three targets, stop-and-reverse, hard exit), the minute-bar safety net, a crash that resumes from saved state, and the rules that
keep it from trading (a short previous session, a stale price).
"""
import dataclasses
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from hestia_fake_helpers import FRONT, NEXT, SESSION_DATE, MINUTES_PER_DAY, world
from hestia_core.fake import ContractSpec, FakeConfig
from hestia_core.interface import CommandKind, TRADE_RECORD_COLUMNS
from oleus_engine.engine import OleusEngine
from oleus_engine.engine_configs import DEFAULT, TRADE_EXTRA_COLUMNS

CFG = dataclasses.replace(DEFAULT, instrument='XX')
D0 = datetime(2026, 9, 3, 9, 0)
END = datetime(2026, 9, 3, 23, 25)

# Previous session H=110 L=100 C=105 -> S4=99.5 S3=102.25 S1=104.0833 | R1=105.9167 R2=106.8333 R3=107.75 R4=110.5 | S5=94.5
LEVELS = dict(S4=99.5, S3=102.25, R1=105.916667, R2=106.833333, R3=107.75, R4=110.5)


def prev_day(day: date, n=MINUTES_PER_DAY, high=110.0, low=100.0, close=105.0):
    rs = np.random.RandomState(5)
    mid = 105 + 3 * np.sin(np.arange(n) / 40.0)
    close_ = mid + rs.normal(0, 0.2, n)
    close_[-1] = close
    open_ = np.r_[close_[0], close_[:-1]]
    hi = np.maximum(open_, close_) + 0.05
    lo = np.minimum(open_, close_) - 0.05
    hi[100], lo[200] = high, low
    ts = [datetime.combine(day, datetime.min.time()) + timedelta(hours=9, minutes=m) for m in range(n)]
    return pd.DataFrame({'time_stamp': ts, 'open': open_, 'high': hi, 'low': lo, 'close': close_, 'volume': 10.0})


def today(waypoints, spikes=None, n=MINUTES_PER_DAY):
    """One bar per minute from (minute, price) waypoints, linear in between and flat after the last; `spikes` {minute: (low, high)}."""
    mins = np.arange(n + 1)
    xs, ys = zip(*waypoints)
    px = np.interp(mins, xs, ys)
    rows = []
    for m in range(n):
        o, c = px[m], px[m + 1]
        hi, lo = max(o, c) + 0.01, min(o, c) - 0.01
        if spikes and m in spikes:
            lo, hi = min(lo, spikes[m][0]), max(hi, spikes[m][1])
        rows.append((datetime.combine(SESSION_DATE, datetime.min.time()) + timedelta(hours=9, minutes=m), o, hi, lo, c, 10.0))
    return pd.DataFrame(rows, columns=['time_stamp', 'open', 'high', 'low', 'close', 'volume'])


def build(waypoints, spikes=None, prev=None, cfg=CFG, engine_cls=OleusEngine, config=None, reg=None, **kw):
    frames = [prev if prev is not None else prev_day(date(2026, 9, 2)), today(waypoints, spikes)]
    minutes = pd.concat([prev_day(date(2026, 9, 1))] + frames, ignore_index=True)
    h = world([('oleus', lambda: engine_cls(cfg, 'oleus'), {'lots_per_unit': 3, **(reg or {})})], contracts=(), config=config,
              extra=[ContractSpec(FRONT, lot_size=10, tick_size=0.5, freeze_qty_lots=20, minutes=minutes),
                     ContractSpec(NEXT, lot_size=10, tick_size=0.5, freeze_qty_lots=20, minutes=minutes)], **kw)
    return h


def run(h, until=END):
    h.start_session(SESSION_DATE)
    h.run_until(until)


def records(h):
    return [r for _, r in h.trades]


FADE_LONG_FULL = [(0, 101.0), (10, 101.0), (30, 103.0), (60, 107.0), (70, 108.2), (870, 108.2)]


def test_a_fade_long_runs_its_three_targets_and_is_recorded_with_the_extra_columns():
    h = build(FADE_LONG_FULL)
    run(h)
    (rec,) = records(h)
    assert tuple(rec) == TRADE_RECORD_COLUMNS + TRADE_EXTRA_COLUMNS
    assert rec['direction'] == 'bullish' and rec['setup'] == 'F_L' and rec['units'] == 1
    assert [rec[f'lot{n}_exit_reason'] for n in (1, 2, 3)] == ['T1', 'T2', 'T3']
    assert abs(rec['lot1_exit_price'] - 105.92) < 0.6 and abs(rec['lot3_exit_price'] - 107.75) < 0.6
    assert rec['total_pnl_points'] == pytest.approx(sum(rec[f'lot{n}_pnl_points'] for n in (1, 2, 3)), abs=0.02)
    assert rec['total_pnl_rs'] == pytest.approx(sum(rec[f'lot{n}_pnl_rs'] for n in (1, 2, 3)), abs=0.05) and rec['total_pnl_rs'] > 0
    assert rec['sl_price'] == 99.5 and rec['lot1_target'] == pytest.approx(105.92, abs=0.01) and rec['lot3_target'] == 107.75
    assert [(o.lots) for o in h.orders] == [3, 1, 1, 1], 'one entry of three lots, then one lot per target'
    assert h.held('oleus', FRONT) == 0 and h.alerts_for('critical') == []


def test_a_stop_that_is_the_sister_setups_entry_reverses_the_position():
    h = build([(0, 101.0), (10, 101.0), (30, 103.0), (45, 103.0), (65, 98.0), (870, 98.0)])
    run(h, datetime(2026, 9, 3, 11, 0))
    recs = records(h)
    assert recs and recs[0]['setup'] == 'F_L' and recs[0]['lot1_exit_reason'] == 'stop' and recs[0]['total_pnl_points'] < 0
    assert h.held('oleus', FRONT) < 0, 'the F_L stop at S4 is the B_S entry: a short is open'
    state = h.engine_state['oleus']
    assert state == 'running'
    sides = [o.side for o in h.orders]
    assert sides[:3] == ['BUY', 'SELL', 'SELL'], 'open the fade long, stop it, open the breakout short'


def test_everything_is_closed_at_the_hard_exit():
    h = build([(0, 101.0), (10, 101.0), (30, 103.0), (870, 103.0)])
    run(h, datetime(2026, 9, 3, 23, 20))
    (rec,) = records(h)
    assert rec['lot1_exit_reason'] == rec['lot2_exit_reason'] == rec['lot3_exit_reason'] == 'hard_exit'
    assert datetime.fromisoformat(rec['lot1_exit_ts']) >= datetime(2026, 9, 3, 23, 14)
    assert h.held('oleus', FRONT) == 0


def test_no_entry_in_the_last_hour_or_the_first_minute():
    # a touch of S3 at about 23:06 (after the 22:29 cutoff) and another in the very first minute
    h = build([(0, 103.0), (0.4, 101.0), (1.0, 101.0), (840, 101.0), (850, 103.0), (870, 103.0)])
    run(h, END)
    assert records(h) == [] and h.orders == []


def test_a_stop_the_ticks_missed_is_caught_by_the_minute_bar_net():
    # in minute 60 the price spikes down to 99 and is back at 103 by the minute's end: the polls see 103.0 only
    h = build([(0, 101.0), (10, 101.0), (30, 103.0), (870, 103.0)], spikes={60: (99.0, 103.1)})
    run(h, datetime(2026, 9, 3, 10, 5))
    (rec,) = records(h)
    assert rec['lot1_exit_reason'] == 'stop'
    assert any('late catch' in a.text for a in h.alerts_for('warning'))


def test_a_trade_survives_an_engine_crash_and_is_not_entered_twice():
    crashed = []

    class Crashy(OleusEngine):
        def _tick(self):
            if self.state.trades and not crashed:
                crashed.append(self._now())
                raise RuntimeError('boom')
            super()._tick()
    h = build(FADE_LONG_FULL, engine_cls=Crashy)
    run(h)
    assert crashed
    (rec,) = records(h)
    assert [rec[f'lot{n}_exit_reason'] for n in (1, 2, 3)] == ['T1', 'T2', 'T3']
    assert [o.lots for o in h.orders] == [3, 1, 1, 1], 'resumed from saved state: no second entry'


def test_a_previous_session_that_is_too_short_means_no_trading_today():
    short = prev_day(date(2026, 9, 2)).iloc[:50]
    h = build(FADE_LONG_FULL, prev=short)
    run(h)
    assert records(h) == [] and h.orders == []
    assert any('no trading today' in a.text for a in h.alerts_for('critical'))


def test_the_exit_command_flattens_and_the_engine_keeps_watching():
    h = build([(0, 101.0), (10, 101.0), (30, 103.0), (870, 103.0)])
    h.start_session(SESSION_DATE)
    h.kernel.at(D0 + timedelta(minutes=60), lambda: h.send_command('oleus', CommandKind.EXIT))
    h.run_until(D0 + timedelta(minutes=70))
    (rec,) = records(h)
    assert rec['lot1_exit_reason'] == 'manual_exit' and h.held('oleus', FRONT) == 0 and h.engine_state['oleus'] == 'running'


def test_units_scale_the_lots_of_every_leg():
    from hestia_core.interface import SizingConfig
    h = build(FADE_LONG_FULL)
    h.set_sizing('oleus', SizingConfig(dynamic=False, static_units=2, unit_cap=50))
    run(h)
    (rec,) = records(h)
    assert rec['units'] == 2 and [o.lots for o in h.orders] == [6, 2, 2, 2]


def test_the_session_report_shows_a_part_booked_oleus_trade_and_its_third_lot(tmp_path):
    from hestia_core.reporting import build_session_report
    from hestia_core.state_store import StateStore
    h = build(FADE_LONG_FULL, store=StateStore(tmp_path))
    run(h, datetime(2026, 9, 3, 10, 5))                     # T1 and T2 booked, lot 3 still open
    text = build_session_report(h, h.now, h.trades)
    assert 'still open' in text and 'Booked' in text and 'Entry: 09:2' in text, 'the true entry time, not the last ledger change'
    assert 'Realized   : *+0 Rs/unit*' not in text, 'the banked lots count as realised'
    h2 = build(FADE_LONG_FULL)
    run(h2)
    closed = build_session_report(h2, h2.now, h2.trades)
    assert 'T3' in closed and 'Units: 1' in closed


def test_decisions_requests_outcomes_levels_and_status_are_logged(caplog):
    import logging
    caplog.set_level(logging.INFO, logger='oleus_engine')
    h = build(FADE_LONG_FULL)
    run(h)
    text = '\n'.join(r.getMessage() for r in caplog.records if r.name == 'oleus_engine')
    for tag in ('LEVELS', 'STATUS', 'DECISION F_L entry', 'REQUEST oleus-', 'OUTCOME oleus-', 'fill minus trigger'):
        assert tag in text, tag
    assert text.count('DECISION') == 4 and text.count('OUTCOME') == 4, 'one entry and three targets'


def test_a_broker_square_off_before_the_hard_exit_sends_no_order_and_is_booked_as_an_estimate():
    h = build([(0, 101.0), (10, 101.0), (30, 103.0), (870, 103.0)], reg={'product': 'INTRADAY'})
    h.kernel.at(D0 + timedelta(hours=3), lambda: h._sim.seed('T1', 0, None, product='INTRADAY'))      # Angel One squares the INTRADAY row off
    run(h, datetime(2026, 9, 3, 23, 20))
    (rec,) = records(h)
    assert rec['lot1_exit_reason'] == rec['lot2_exit_reason'] == rec['lot3_exit_reason'] == 'broker_squareoff'
    assert [(o.side, o.lots) for o in h.orders] == [('BUY', 3)], 'no exit order was sent against a position that was already gone'
    assert h.held('oleus', FRONT) == 0
    assert any('already flat' in a.text and 'ESTIMATE' in a.text for a in h.alerts_for('critical'))


def test_a_partly_squared_off_position_exits_only_the_lots_the_broker_still_holds():
    h = build([(0, 101.0), (10, 101.0), (30, 103.0), (870, 103.0)], reg={'product': 'INTRADAY'})
    h.kernel.at(D0 + timedelta(hours=3), lambda: h._sim.seed('T1', 1, 103.0, product='INTRADAY'))     # two of three lots gone
    run(h, datetime(2026, 9, 3, 23, 20))
    assert [(o.side, o.lots) for o in h.orders] == [('BUY', 3), ('SELL', 1)]
    assert h.held('oleus', FRONT) == 0 and any('INTRADAY row shows +1' in a.text for a in h.alerts_for('critical'))
