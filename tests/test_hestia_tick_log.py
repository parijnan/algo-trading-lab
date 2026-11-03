"""
The raw websocket tick log (hestia_core/tick_log.py) and its hook in websocket_feed.SharedFeed.

The properties that matter for a live process: the feed's price state and error count are untouched by the recorder (even a recorder that
raises), the buffer is bounded, a write failure is survived and alerted once, the kill flag works without a restart, a restart on the same day
appends under one header, and the day rolls to a new file.
"""

from datetime import date, datetime

import pytest

from hestia_core.tick_log import TICK_HEADER, TOKEN_HEADER, TickLog
from websocket_feed import SharedFeed

T0 = datetime(2026, 11, 2, 19, 23, 0).timestamp()


def make(tmp_path, **kw):
    return TickLog(tmp_path / 'ticks', **kw)


def lines(path):
    return path.read_text().splitlines()


def test_a_tick_is_written_with_every_field_under_one_header(tmp_path):
    log = make(tmp_path)
    log.put(T0, '573423', 9339.0, 1793700180123, 77, 5)
    log.put(T0 + 1.5, '573423', 9259.0, None, None, None)
    assert log.drain() == 2
    f = tmp_path / 'ticks' / 'ticks_2026-11-02.csv'
    assert lines(f) == [TICK_HEADER.strip(),
                        f'{int(T0 * 1000)},573423,9339.0,1793700180123,77,5',
                        f'{int((T0 + 1.5) * 1000)},573423,9259.0,,,']
    assert (log.received, log.written, log.dropped) == (2, 2, 0)


def test_a_restart_on_the_same_day_appends_and_writes_the_header_once(tmp_path):
    a = make(tmp_path)
    a.put(T0, '1', 10.0, 1, 1, 5)
    a.close()
    b = make(tmp_path)
    b.put(T0 + 60, '1', 11.0, 2, 2, 5)
    b.close()
    rows = lines(tmp_path / 'ticks' / 'ticks_2026-11-02.csv')
    assert rows.count(TICK_HEADER.strip()) == 1 and len(rows) == 3


def test_the_day_rolls_to_a_new_file_and_each_day_lists_its_tokens(tmp_path):
    log = make(tmp_path, symbol_of=lambda t: {'instrument': 'CRUDEOILM', 'symbol': 'CRUDEOILM19NOV26FUT', 'expiry': '2026-11-19'})
    nxt = datetime(2026, 11, 3, 0, 0, 5).timestamp()
    log.put(T0, '573423', 9339.0, 1, 1, 5)
    log.put(nxt, '573423', 9300.0, 2, 2, 5)
    log.drain()
    d = tmp_path / 'ticks'
    assert len(lines(d / 'ticks_2026-11-02.csv')) == 2 and len(lines(d / 'ticks_2026-11-03.csv')) == 2
    for day in ('2026-11-02', '2026-11-03'):
        assert lines(d / f'tokens_{day}.csv') == [TOKEN_HEADER.strip(), '573423,CRUDEOILM,CRUDEOILM19NOV26FUT,2026-11-19']


def test_a_symbol_lookup_that_fails_leaves_the_name_blank_and_the_ticks_still_written(tmp_path):
    def boom(token):
        raise RuntimeError('no master')
    log = make(tmp_path, symbol_of=boom)
    log.put(T0, '9', 1.0, 1, 1, 5)
    assert log.drain() == 1
    assert lines(tmp_path / 'ticks' / 'tokens_2026-11-02.csv')[1] == '9,,,'


def test_the_buffer_is_bounded_and_overflow_is_counted_not_queued(tmp_path):
    log = make(tmp_path, max_buffered=3)
    for i in range(5):
        log.put(T0 + i, '1', float(i), i, i, 5)
    assert log.received == 5 and log.dropped == 2          # received counts every arrival; dropped is the part that did not fit
    assert log.drain() == 3 and log.written == 3


def test_the_kill_flag_stops_recording_without_a_restart_and_removing_it_resumes(tmp_path):
    flag = tmp_path / 'tick_log_off.flag'
    log = make(tmp_path, off_flag=flag)
    log.put(T0, '1', 1.0, 1, 1, 5)
    log.drain()
    flag.write_text('')
    log.drain()                                   # the writer notices the flag
    log.put(T0 + 1, '1', 2.0, 2, 2, 5)
    log.put(T0 + 2, '1', 3.0, 3, 3, 5)
    log.drain()
    assert log.skipped == 2
    flag.unlink()
    log.drain()
    log.put(T0 + 3, '1', 4.0, 4, 4, 5)
    log.drain()
    rows = lines(tmp_path / 'ticks' / 'ticks_2026-11-02.csv')
    assert [r.split(',')[2] for r in rows[1:]] == ['1.0', '4.0']


def test_a_write_failure_is_survived_and_alerted_once_an_hour(tmp_path):
    (tmp_path / 'ticks').write_text('this is a file, not a directory')      # mkdir and open both fail
    alerts, clock = [], [1000.0]
    log = make(tmp_path, alert=alerts.append, monotonic=lambda: clock[0])
    log.put(T0, '1', 1.0, 1, 1, 5)
    assert log.drain() == 0 and log.write_errors == 1 and len(alerts) == 1
    log.put(T0 + 1, '1', 1.0, 1, 1, 5)
    clock[0] += 60
    log.drain()
    assert log.write_errors == 2 and len(alerts) == 1                         # same hour: no second alert
    log.put(T0 + 2, '1', 1.0, 1, 1, 5)
    clock[0] += 3600
    log.drain()
    assert len(alerts) == 2


def test_an_alert_callback_that_raises_does_not_escape(tmp_path):
    (tmp_path / 'ticks').write_text('x')
    log = make(tmp_path, alert=lambda m: 1 / 0)
    log.put(T0, '1', 1.0, 1, 1, 5)
    log.drain()                                   # must not raise


def test_the_writer_thread_flushes_on_its_own_and_close_flushes_the_rest(tmp_path):
    log = make(tmp_path, flush_s=0.05)
    log.start()
    log.put(T0, '1', 1.0, 1, 1, 5)
    log.close()
    assert log.written == 1
    assert len(lines(tmp_path / 'ticks' / 'ticks_2026-11-02.csv')) == 2


# ---- the hook in the shared feed -------------------------------------------------------------------------------------------------

MSG = {'token': '573423', 'last_traded_price': 923900, 'exchange_timestamp': 1793700180123, 'sequence_number': 41, 'exchange_type': 5}


def test_the_feed_hands_the_sink_every_field_and_keeps_its_own_state():
    feed, got = SharedFeed(), []
    feed.set_tick_sink(lambda *a: got.append(a))
    feed._on_data(None, MSG)
    ts, token, ltp, exch, seq, et = got[0]
    assert (token, ltp, exch, seq, et) == ('573423', 9239.0, 1793700180123, 41, 5) and ts > 1.7e9
    assert feed.get_ltp('573423') == 9239.0 and feed.get_tick_count('573423') == 1
    assert feed.get_ohlc('573423')['close'] == 9239.0


def test_a_sink_that_raises_changes_nothing_in_the_feed_and_is_not_a_tick_error():
    feed = SharedFeed()

    def boom(*a):
        raise RuntimeError('recorder fault')
    feed.set_tick_sink(boom)
    feed._on_data(None, MSG)
    assert feed.get_ltp('573423') == 9239.0 and feed.get_tick_count('573423') == 1
    assert feed._tick_errors == 0 and feed._tick_err_since_alert == 0


def test_without_a_sink_the_feed_behaves_as_before():
    feed = SharedFeed()
    assert feed._tick_sink is None
    feed._on_data(None, MSG)
    feed._on_data(None, {'token': '573423', 'last_traded_price': 924100})
    assert feed.get_ltp('573423') == 9241.0 and feed.get_tick_count('573423') == 2
    assert feed.get_ohlc('573423') == {'open': 9239.0, 'high': 9241.0, 'low': 9239.0, 'close': 9241.0}


def test_a_message_with_no_price_reaches_neither_the_sink_nor_the_state():
    feed, got = SharedFeed(), []
    feed.set_tick_sink(lambda *a: got.append(a))
    feed._on_data(None, {'token': '1'})
    feed._on_data(None, {'last_traded_price': 5})
    assert got == [] and feed.get_ltp('1') is None


def test_the_last_tick_time_the_watchdog_and_the_engines_read_is_still_set():
    feed = SharedFeed()
    feed._on_data(None, MSG)
    assert 0 <= feed.get_last_tick_age('573423') < 2
