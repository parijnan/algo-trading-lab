"""The session report lists the whole day's closed trades, not only those closed since this process started (2026-10-05: a mid-session
restart made four Prometheus trades and two Selene trades vanish from the 23:30 report)."""
import csv
from datetime import date

import pytest

from hestia_core.interface import TRADE_RECORD_COLUMNS
from hestia_core.reporting import TradeLogWriter, build_session_report, merge_session_trades, read_closed_on
from datetime import timedelta

from hestia_fake_helpers import SESSION_DATE, factory, world
from hestia_core.interface import SessionStart

DAY = date(2026, 10, 5)
T0 = None


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
    from datetime import datetime
    h.start_session(SESSION_DATE)
    h.run_until(datetime(2026, 9, 3, 9, 0) + timedelta(seconds=seconds))


def rec(trade_id, exit_ts, **kw):
    base = {'trade_id': trade_id, 'contract_expiry': '2026-10-19', 'direction': 'bearish', 'units': 1, 'entry_ts': '2026-10-05T09:15:02.3',
            'entry_price': 8676.0, 'lot1_exit_ts': exit_ts, 'lot1_exit_price': 8745.75, 'lot1_exit_reason': 'trend_flip',
            'lot1_pnl_points': -69.75, 'lot1_pnl_rs': -697.5, 'lot2_exit_ts': exit_ts, 'lot2_exit_price': 8745.75,
            'lot2_exit_reason': 'trend_flip', 'lot2_pnl_points': -69.75, 'lot2_pnl_rs': -697.5, 'total_pnl_points': -139.5,
            'total_pnl_rs': -1395.0}
    base.update(kw)
    return base


def test_only_trades_whose_last_exit_is_today_come_back_with_typed_values(tmp_path):
    w = TradeLogWriter(tmp_path)
    w.write('prometheus', rec(60, '2026-10-05T09:04:00.7', entry_ts='2026-10-01T20:15:01'))       # entered earlier, closed today: counts
    w.write('prometheus', rec(61, '2026-10-04T18:00:00'))                                          # closed yesterday: does not
    w.write('prometheus', rec(62, ''))                                                              # no exit recorded: does not
    got = read_closed_on(tmp_path, ['prometheus', 'selene'], DAY)                                   # selene has no file: contributes nothing
    assert [(e, r['trade_id']) for e, r in got] == [('prometheus', 60)]
    r = got[0][1]
    assert isinstance(r['trade_id'], int) and r['units'] == 1 and r['total_pnl_rs'] == -1395.0 and r['direction'] == 'bearish'
    assert r['lot1_exit_ts'] == '2026-10-05T09:04:00.7' and r['parent_trade_id'] is None


def test_a_hand_added_row_in_the_trades_file_is_read_like_any_other(tmp_path):
    with open(tmp_path / 'prometheus_trades.csv', 'w', newline='') as f:
        cw = csv.writer(f, lineterminator='\r\n')                 # the file's own CRLF endings, as on Delos
        cw.writerow(TRADE_RECORD_COLUMNS)
        cw.writerow([63, '2026-10-19', 'bearish', 1, '2026-10-05T18:45:01.269329', 8630.75, '2026-10-05T18:30:00', 8634.0, 3.25, 8820.63,
                     8440.87, 8199.21, 'flat_pct', '', '2026-10-05T20:37:17', 8770.5, 'manual_broker_exit', -139.75, -1397.5,
                     '2026-10-05T20:37:17', 8770.5, 'manual_broker_exit', -139.75, -1397.5, -279.5, -2795.0])
    (e, r), = read_closed_on(tmp_path, ['prometheus'], DAY)
    assert r['trade_id'] == 63 and r['total_pnl_rs'] == -2795.0 and r['lot2_exit_reason'] == 'manual_broker_exit'


def test_a_corrupt_or_unreadable_file_never_raises(tmp_path):
    (tmp_path / 'prometheus_trades.csv').write_bytes(b'\x00\x01\x02 not,a,csv\n"unterminated')
    (tmp_path / 'selene_trades.csv').mkdir()                                                        # a directory where a file should be
    assert read_closed_on(tmp_path, ['prometheus', 'selene'], DAY) == []


def test_merge_keeps_one_entry_per_trade_and_prefers_the_live_record():
    old = ('prometheus', rec(61, '2026-10-05T14:15:02', total_pnl_rs=-1.0))
    live = ('prometheus', rec(61, '2026-10-05T14:15:02', total_pnl_rs=-1395.0))
    other = ('selene', rec(61, '2026-10-05T11:45:00'))
    got = merge_session_trades([live], [old, other])
    assert len(got) == 2 and dict(((e, r['trade_id']), r['total_pnl_rs']) for e, r in got)[('prometheus', 61)] == -1395.0


def test_a_report_built_from_the_merged_trades_lists_trades_closed_before_a_restart(hestias, tmp_path):
    """The failing day, reproduced: this process only closed trade 3, the files hold trades 1 and 2 from before the restart."""
    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.report_trade(rec(3, '2026-10-05T21:00:00', units=1, entry_ts='2026-10-05T20:50:00', total_pnl_rs=-500.0, total_pnl_points=-50.0))
    h = hestias([('a', factory(act=act))])
    go(h)
    w = TradeLogWriter(tmp_path)
    w.write('a', rec(1, '2026-10-05T09:04:00', total_pnl_rs=-3980.0, total_pnl_points=-398.0))
    w.write('a', rec(2, '2026-10-05T14:15:00', total_pnl_rs=-1395.0, total_pnl_points=-139.5))
    files = read_closed_on(tmp_path, ['a'], date(2026, 10, 5))
    text = build_session_report(h, h.now, merge_session_trades(list(h.trades), files))
    assert '*Trade #1*' in text and '*Trade #2*' in text and '*Trade #3*' in text
    assert 'No trade today' not in text.split('*A*')[1].split('Account free cash')[0]
    assert '  ↳ Realized   : *-5,875 Rs/unit*' in text                       # -3980 - 1395 - 500
    only_live = build_session_report(h, h.now, list(h.trades))
    assert '*Trade #1*' not in only_live                                           # the old behaviour, for contrast


def test_the_host_builds_the_final_report_from_the_days_trade_files_too(tmp_path):
    """End to end: trades of the day sitting in the engine's trades file (closed by an earlier process, or entered by hand) are in the
    report the host sends at teardown, trades of other days are not."""
    import time
    from hestia_host_helpers import HostRun
    from hestia_config import EngineEntry
    from hestia_fake_helpers import RecEngine
    engines = {'a': EngineEntry(instrument='XX', factory='unused:unused', enabled=True)}
    run = HostRun(tmp_path, engines, {'a': lambda: RecEngine('a', timeout=0.05)})
    run.cfg.TRADES_DIR.mkdir(parents=True, exist_ok=True)
    w = TradeLogWriter(run.cfg.TRADES_DIR)
    w.write('a', rec(7, '2026-09-03T11:00:00', total_pnl_rs=-1234.0, total_pnl_points=-123.4, entry_ts='2026-09-03T10:00:00'))
    w.write('a', rec(8, '2026-09-02T11:00:00', total_pnl_rs=-999.0, total_pnl_points=-99.9, entry_ts='2026-09-02T10:00:00'))
    run.start()
    deadline = time.time() + 20
    while not (run.cfg.FLAG_DIR / 'hestia_active.flag').exists() and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    (run.cfg.FLAG_DIR / 'hestia_active.flag').unlink()
    r = run.join()
    assert r.started and r.session_report
    assert '*Trade #7*' in r.session_report and '-1,234 Rs/unit' in r.session_report
    assert '*Trade #8*' not in r.session_report
