"""
hestia_core.recorded: the parser on real log lines, and (when the pulled recorded days are present under hestia_data/replay_pull/)
the P5 plan's findings pinned as tests: the data-path table, the cross-check of the logs against the trades file, and a fake
Hestia driven by the logged bars.
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'tests'))
from hestia_core import recorded as rec  # noqa: E402
from hestia_core.fake import ContractSpec, FakeHestia  # noqa: E402
from hestia_core.interface import BarComplete, BarQuality, ContractRef, DataSpec, ProvisionalBar  # noqa: E402
from hestia_fake_helpers import RecEngine, events_of  # noqa: E402

PULL = REPO / 'hestia_data' / 'replay_pull'
PIPE = REPO / 'data_pipeline' / 'data' / 'mcx'
needs_pull = pytest.mark.skipif(not (PULL / 'logs').exists() or not (PIPE / 'CRUDEOILM' / '2026-10-19_futures.csv').exists(),
                                reason='the pulled recorded days (hestia_data/replay_pull) are not present')


# ---- the parser, on real lines ----------------------------------------------------------------------------------------------

def one(line):
    events = rec.parse_lines([line])
    assert len(events) == 1, f'{len(events)} events from {line!r}'
    return events[0]


def test_a_bar_line_with_and_without_a_flip():
    e = one('2026-09-24 11:45:00  INFO      prometheus  15m bar 11:30 — ST=8854.63 close=8846.00  no flip')
    assert e.kind == rec.BAR and e.ts == datetime(2026, 9, 24, 11, 45) and e.data['bar_start'] == datetime(2026, 9, 24, 11, 30)
    assert (e.data['st'], e.data['close'], e.data['flip'], e.data['direction']) == (8854.63, 8846.0, False, None)
    f = one('2026-09-24 12:00:03  INFO      prometheus  15m bar 11:45 — ST=8788.36 close=8884.00  FLIP -> bullish')
    assert f.data['flip'] and f.data['direction'] == 'bullish'


def test_order_fill_and_exit_lines_including_thousands_separators_and_both_formats():
    assert one('2026-09-24 12:00:03  INFO      prometheus_functions  Order placed: BUY 200 x CRUDEOILM19OCT26FUT -> orderid=260924000413740').data \
        == {'side': 'BUY', 'qty': 200, 'symbol': 'CRUDEOILM19OCT26FUT', 'orderid': '260924000413740'}
    f = one('2026-09-24 12:00:04  INFO      prometheus_functions  Fill (WS): CRUDEOILM19OCT26FUT avg=8885.2 qty=200 (20 lot(s)) across 1 order(s)')
    assert (f.data['avg'], f.data['qty'], f.data['lots'], f.data['source']) == (8885.2, 200, 20, 'WS')
    new = one('2026-09-24 12:00:04  INFO      prometheus  ✅ *Prometheus [CRUDEOILM]*: Lot2 exit — trend_flip  (Units: 5)  Entry 8787.47 -> Exit 8885.20 | P&L: -97.73 pts  Rs.-977/unit')
    assert new.kind == rec.EXIT and new.data == {'lot': 2, 'reason': 'trend_flip', 'units': 5, 'entry': 8787.47, 'exit': 8885.2, 'pnl_pts': -97.73}
    old = one('2026-09-16 09:15:01  INFO      prometheus  ✅ *Prometheus [CRUDEOILM]*: Lot2 exit — trend_flip  Entry 9449.50 -> Exit 9591.00 | P&L: +141.50 pts  Rs.+1,415')
    assert old.data['units'] is None and old.data['pnl_pts'] == 141.5
    flat = one('2026-09-18 22:00:00  INFO      prometheus  ✅ *Prometheus [CRUDEOILM]*: Lot2 exit — target2_flat_pct  (Units: 1)  Entry 9303.50 -> Exit 9042.00 | P&L: +1.00 pts  Rs.+10')
    assert flat.data['reason'] == 'target2_flat_pct'


def test_entry_lines_with_a_basis_a_rollover_marker_and_a_lone_lot2():
    e = one('2026-09-24 12:00:04  INFO      prometheus  ⚡ *Prometheus [CRUDEOILM]*: Entered BULLISH  CRUDEOILM19OCT26FUT | Units: 5 (10 lots)  Entry: 8885.20 | SL: 8689.73  Lot1 target: 9080.67 | Lot2 target: 9329.46 (flat_pct)')
    assert e.data == {'direction': 'bullish', 'rollover': False, 'symbol': 'CRUDEOILM19OCT26FUT', 'units': 5, 'lots': 10, 'entry': 8885.2,
                      'basis': None, 'sl': 8689.73, 'lot1_target': 9080.67, 'lot2_target': 9329.46, 'lot2_source': 'flat_pct'}
    r = one('2026-10-13 23:15:01  INFO      prometheus  ⚡ *Prometheus [CRUDEOILM]*: Entered BEARISH (rollover)  CRUDEOILM19NOV26FUT | Units: 1 (1 lot)  Entry: 9000.00 (recalibration basis 8990.50) | SL: 9200.00  Lot1 target: n/a (lot2-only, §8) | Lot2 target: 8500.00 (flat_pct)')
    assert r.data['rollover'] and r.data['basis'] == 8990.5 and r.data['lot1_target'] is None and r.data['lots'] == 1


def test_provisional_lines_in_the_old_and_the_new_format():
    old = one('2026-09-16 11:00:04  INFO      prometheus  Provisional boundary 10:45: close=9577.00 ST=9653.84 direction=bearish flip=False band_dist_pct=0.802 (margin=0.15) clears_margin=True gating=ON — SHADOW LOG, reconciled against the real bar once it computes.')
    assert old.kind == rec.PROVISIONAL and old.data['bar_start'] == datetime(2026, 9, 16, 10, 45) and old.data['prev_st'] is None
    assert (old.data['close'], old.data['st'], old.data['direction'], old.data['clears_margin'], old.data['gating']) == \
        (9577.0, 9653.84, 'bearish', True, True)
    new = one('2026-09-30 11:00:04  INFO      prometheus  Provisional boundary 10:45: close=9577.00 ST=9653.84 prev_ST=9600.10 direction=bearish flip=True clear_prev_st_pct=0.062 (margin=0.15) clears_margin=False gating=ON — SHADOW LOG')
    assert new.data['prev_st'] == 9600.1 and new.data['flip'] and not new.data['clears_margin']


def test_session_lines_seed_resume_reconcile_kill_missed_flip_and_incomplete_windows():
    assert one('2026-09-16 09:00:04  INFO      prometheus_functions  Seeded: 664 15-min bars from CRUDEOILM19OCT26FUT (12 trading day(s) of 1-min history) | trend=True ST=9648.13').data \
        == {'bars': 664, 'symbol': 'CRUDEOILM19OCT26FUT', 'days': 12, 'trend': 'True', 'st': 9648.13}
    assert one('2026-09-16 09:00:04  INFO      prometheus  Resuming in-trade state.').kind == rec.RESUME
    assert one('2026-09-16 09:00:04  INFO      prometheus  Position reconciliation OK — broker netqty +10 matches state (token=569901).').data == {'ok': True, 'netqty': 10}
    assert one('2026-09-21 15:40:47  CRITICAL  prometheus  🚨 Prometheus [CRUDEOILM]: Slack `Kill Switch` detected. Dropping control immediately.').kind == rec.KILL
    assert one('2026-09-15 16:34:25  CRITICAL  prometheus  ⚠️ Prometheus [CRUDEOILM]: Slack `Exit Trade` detected. Liquidating...').kind == rec.EXIT_COMMAND
    m = one('2026-09-23 09:00:06  CRITICAL  prometheus  Missed flip detected: ST_15 flipped -> bearish at 2026-09-22 23:15:00 -- session ended before this bar went live. Reconciling at startup.')
    assert m.data == {'direction': 'bearish', 'bar_start': datetime(2026, 9, 22, 23, 15)}
    inc = one('2026-09-16 13:15:04  WARNING   prometheus  15m bar 13:00-13:15 still incomplete (14/15) after 1min cutoff — building from what is on hand.')
    assert inc.data == {'window_start': datetime(2026, 9, 16, 13, 0), 'rows': 14}
    assert one('2026-09-15 09:00:03  INFO      prometheus  Effective contract: CRUDEOILM19OCT26FUT (token 569901, expiry 2026-10-19) (rolled early, tender-margin window)').data['rolled_early']
    assert one('2026-09-15 16:30:03  CRITICAL  prometheus  Rule 7 pending flip stuck: combined order FAILED to place (requested 4 lots).').kind == rec.RULE7_STUCK
    assert one('2026-09-15 16:00:01  ERROR     prometheus_functions  Order rejected (1/3): CRUDEOILM19OCT26FUT — Invalid Token').data['reason'] == 'Invalid Token'


def test_chatter_is_ignored_and_a_bar_logged_twice_after_a_restart_counts_once():
    lines = ['2026-09-24 09:00:01  INFO      prometheus_functions  Fetch OK [2026-09-24 08:55 -> 2026-09-24 09:00] attempt 1/5 (5 candle(s))',
             'garbage line',
             '2026-09-24 09:15:00  INFO      prometheus  15m bar 09:00 — ST=8873.52 close=8773.00  no flip',
             '2026-09-24 09:16:00  INFO      prometheus  15m bar 09:00 — ST=8873.52 close=8773.00  no flip']
    events = rec.parse_lines(lines)
    assert [e.kind for e in events] == [rec.BAR, rec.BAR]
    sr = rec.SessionRecord(date(2026, 9, 24), Path('x'), events)
    assert len(sr.bars) == 1


# ---- the pulled recorded days ------------------------------------------------------------------------------------------------

@pytest.fixture(scope='module')
def sessions():
    return rec.load_sessions(PULL)


@pytest.fixture(scope='module')
def minute_frames():
    return rec.load_minute_files(PIPE, 'CRUDEOILM')


@needs_pull
def test_the_ten_recorded_sessions_parse_with_a_full_day_of_bars_each(sessions):
    assert [s.day for s in sessions][0] == date(2026, 9, 15) and len(sessions) == 10
    full = [s for s in sessions if s.day != date(2026, 9, 28)]
    assert all(len(s.bars) >= 54 for s in full), [len(s.bars) for s in full]
    last = sessions[-1]                                                                       # 09-28: stopped out at 21:50, killed by the user at 21:55
    assert last.day == date(2026, 9, 28) and len(last.bars) == 51 and len(last.of(rec.KILL)) == 1
    assert sum(len(s.of(rec.ENTRY)) for s in sessions) == 31 and sum(len(s.of(rec.EXIT)) for s in sessions) == 60


@needs_pull
def test_the_data_path_table_of_the_p5_plan_is_reproduced_exactly(sessions, minute_frames):
    rows = {s.day: rec.compare_bar_path(s, minute_frames) for s in sessions}
    rows = {d: c for d, c in rows.items() if c is not None and c.bars}
    assert sum(c.bars for c in rows.values()) == 561 and sum(c.matched for c in rows.values()) == 353
    assert sum(c.close_differs for c in rows.values()) == 51 and sum(c.flip_differs for c in rows.values()) == 2
    assert sum(c.st_differs for c in rows.values()) == 179
    clean = rows[date(2026, 9, 24)]
    assert clean.st_differs == 0 and clean.worst_st_diff < 0.006 and clean.flip_differs == 0, 'a clean day matches to the logged precision'
    assert rows[date(2026, 9, 23)].flip_differs == 2, 'the one thin cross'
    also_clean = rows[date(2026, 9, 28)]                        # the second pull's day: also matches ST exactly (51 bars, session ends at the kill)
    assert also_clean.st_differs == 0 and also_clean.worst_st_diff < 0.006 and also_clean.bars == 51


@needs_pull
def test_every_logged_exit_is_a_trade_and_the_only_unmatched_entry_is_the_known_one(sessions):
    cc = rec.check_logs_against_trades(sessions, rec.load_trades(PULL / 'data' / 'prometheus_trades.csv'))
    assert cc.exits_logged == cc.exits_matched == 60
    assert cc.entries_logged == 31 and cc.entries_matched == 30 and cc.trades_in_window == cc.trades_with_entry_logged == 30
    assert len(cc.problems) == 1
    assert any('2026-09-15 09:45' in p for p in cc.problems), 'trade 21 (09-15, the expired-token day) has a running-row file but no trades row'


def _fake_over(sessions, minute_frames, day, start_hm='08:50'):
    ref = ContractRef('CRUDEOILM', '569901', 'CRUDEOILM19OCT26FUT', date(2026, 10, 19))
    spec = DataSpec('CRUDEOILM', 15, 10, 2.0)
    log = []
    holder = {}

    def factory(kernel, holidays):
        data = rec.LoggedReplayData(kernel, sessions, minute_frames, holidays)
        data.add_logged_contract(ContractSpec(ref, lot_size=10, tick_size=1.0, freeze_qty_lots=1000,
                                              minutes=minute_frames['2026-10-19']), '2026-10-19')
        holder['data'] = data
        return data
    h = FakeHestia(datetime.combine(day, datetime.strptime(start_hm, '%H:%M').time()), data_factory=factory)
    h.register('a', lambda: RecEngine('a', spec=spec, trade=ref, log=log))
    return h, log, holder['data']


@needs_pull
def test_a_fake_hestia_on_logged_replay_data_delivers_exactly_the_logged_bars(sessions, minute_frames):
    day = date(2026, 9, 24)
    h, log, data = _fake_over(sessions, minute_frames, day)
    try:
        h.start_session(day)
        h.run_until(datetime.combine(day, datetime.strptime('23:40', '%H:%M').time()))
        got = [(e.bar.ts, e.bar.close, e.st.value, e.st.flip) for e in events_of(log, BarComplete)]
        logged = [(e.data['bar_start'], e.data['close'], e.data['st'], e.data['flip'])
                  for e in next(s for s in sessions if s.day == day).bars]
        assert got == logged and len(got) == 57
        assert sum(f for *_, f in got) == 5, 'the day had five flips (five entries in the trades file: 12:00, 16:00, 19:15, 22:00, 23:15)'
        session = next(s for s in sessions if s.day == day)
        provisional_bars = {e.data['bar_start'] for e in session.of(rec.PROVISIONAL)}
        assert len(provisional_bars) == 2
        for e in events_of(log, BarComplete):
            want = BarQuality.RECOVERED if e.bar.ts in provisional_bars else BarQuality.COMPLETE
            assert e.quality == want, (e.bar.ts, e.quality)
    finally:
        h.close()


@needs_pull
def test_logged_partial_bars_and_provisional_verdicts_are_replayed_as_such(sessions, minute_frames):
    day = date(2026, 9, 16)
    session = next(s for s in sessions if s.day == day)
    h, log, data = _fake_over(sessions, minute_frames, day)
    try:
        h.start_session(day)
        h.run_until(datetime.combine(day, datetime.strptime('23:40', '%H:%M').time()))
        provs = events_of(log, ProvisionalBar)
        logged = session.of(rec.PROVISIONAL)
        assert len(logged) == 3 and [p.bar.ts for p in provs] == [e.data['bar_start'] for e in logged]
        assert [p.bar.close for p in provs] == [e.data['close'] for e in logged]
        real = {b.boundary_ts - timedelta(minutes=15): b for b in events_of(log, BarComplete)}
        for e in logged:
            assert real[e.data['bar_start']].reconciles_provisional, 'the real bar follows and reconciles the provisional one'
    finally:
        h.close()


@needs_pull
def test_logged_partial_bars_are_replayed_as_partial_with_their_minute_counts(sessions, minute_frames):
    day = date(2026, 9, 15)
    session = next(s for s in sessions if s.day == day)
    h, log, data = _fake_over(sessions, minute_frames, day)
    try:
        h.start_session(day)
        h.run_until(datetime.combine(day, datetime.strptime('23:40', '%H:%M').time()))
        partial = [(b.bar.ts, b.minutes_present) for b in events_of(log, BarComplete) if b.quality == BarQuality.PARTIAL]
        logged = [(e.data['window_start'], e.data['rows']) for e in session.of(rec.INCOMPLETE)]
        assert logged == [(datetime(2026, 9, 15, 12, 15), 14), (datetime(2026, 9, 15, 13, 45), 14)]
        assert partial == logged, 'the two 14-of-15 bars live built from what was on hand are delivered PARTIAL with 14 minutes'
    finally:
        h.close()
