"""The MCX option quote recorder (research/option_hedge/quote_recorder.py), websocket version: windows, token rule, universe allocation,
which messages are written, and the whole window on a fake clock and a fake socket. The properties that protect Hestia are pinned: it
makes no REST request of its own, subscribes exactly twice per connection and never unsubscribes, stays within the symbol limit, idles on
a stale or refused token or its kill flag, restarts a silent feed a bounded number of times, never writes the token anywhere, and writes
only under its own folders."""

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'research' / 'option_hedge'))

import quote_recorder as qr                                    # noqa: E402
import recorder_configs as cfg                                 # noqa: E402
from hestia_core.mcx_market import MarketCalendar             # noqa: E402

IST = qr.IST
SECRET = 'SECRET-TOKEN-VALUE-1234567890'
HOLIDAYS = cfg.HOLIDAYS_FILE
TODAY = date(2026, 10, 30)                                      # a Friday; US DST still in force, so the close is 23:30


def epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, 23, 30, tzinfo=IST).timestamp())


def master_record(symbol, und, kind, expiry, strike=-1.0, prev_oi=0):
    return {'symTicker': symbol, 'exSymbol': und, 'optType': kind if kind in ('CE', 'PE') else 'XX', 'expiryDate': str(epoch(expiry)),
            'strikePrice': strike, 'qtyMultiplier': 10.0, 'previousOi': prev_oi}


def fake_master(tmp_path, today=TODAY):
    e1, e2 = today + timedelta(days=18), today + timedelta(days=47)
    f1, f2 = today + timedelta(days=20), today + timedelta(days=49)
    rows = [master_record('MCX:CRUDEOILM26NOVFUT', 'CRUDEOILM', 'FUT', f1), master_record('MCX:CRUDEOILM26DECFUT', 'CRUDEOILM', 'FUT', f2),
            master_record('MCX:SILVERM26NOVFUT', 'SILVERM', 'FUT', f1), master_record('MCX:SILVERM26DECFUT', 'SILVERM', 'FUT', f2)]
    for tag, exp in (('NOV', e1), ('DEC', e2)):
        for strike in range(8000, 11650, 50):
            for kind in ('CE', 'PE'):
                rows.append(master_record(f'MCX:CRUDEOILM26{tag}{strike}{kind}', 'CRUDEOILM', kind, exp, float(strike), prev_oi=2878))
        for strike in range(180000, 200001, 1000):
            for kind in ('CE', 'PE'):
                rows.append(master_record(f'MCX:SILVERM26{tag}{strike}{kind}', 'SILVERM', kind, exp, float(strike)))
    rows.append(master_record('MCX:MCXBULLDEX26NOVFUT', 'MCXBULLDEX', 'FUT', f1))
    path = tmp_path / 'master.json'
    path.write_text(json.dumps({r['symTicker']: r for r in rows}))
    return path


def write_token(path: Path, now: datetime, secret=SECRET, mode=0o600, issued=None):
    issued = issued or now
    path.write_text(json.dumps({'app_id': 'APP-ID', 'access_token': secret, 'issued_at': issued.replace(tzinfo=IST).isoformat(),
                                'expires_at': issued.replace(hour=23, minute=59, tzinfo=IST).isoformat()}))
    os.chmod(path, mode)
    ts = now.replace(tzinfo=IST).timestamp()
    os.utime(path, (ts, ts))


class FakeNet:
    """A clock, a sleep that advances it, and the sockets the recorder opens. After every sleep each open socket delivers one message per
    subscribed symbol, built by `quote_fn(symbol, now)`; `silent_after` stops delivery from that time on."""

    def __init__(self, now, quote_fn=None):
        self.now, self.sockets, self.hooks, self.silent_after = now, [], [], None
        self.quote_fn = quote_fn or self.default_quote

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)
        for hook in self.hooks:
            hook(self.now)
        if self.silent_after is not None and self.now >= self.silent_after:
            return
        for sock in self.sockets:
            if sock.opened and not sock.closed:
                for sym in list(sock.subscribed):
                    sock.on_message(self.quote_fn(sym, self.now))

    @staticmethod
    def default_quote(sym, now):
        step = int(now.timestamp() // 5)                          # the quote changes every 5 seconds
        base = 9830.0 if sym.endswith('FUT') else 480.0 + step % 3
        return {'type': 'sf', 'symbol': sym, 'ltp': base, 'bid_price': base - 0.5, 'ask_price': base + 0.5, 'bid_size': 3, 'ask_size': 2,
                'vol_traded_today': 100 + step, 'tot_buy_qty': 1000, 'tot_sell_qty': 900, 'last_traded_qty': 1,
                'exch_feed_time': int(now.replace(tzinfo=IST).timestamp())}


class FakeSocket:
    def __init__(self, net, connect=True):
        self.net, self.connect, self.opened, self.closed = net, connect, False, False
        self.subscribed, self.subscribe_calls, self.unsubscribe_calls = [], [], 0
        net.sockets.append(self)

    def open(self, auth, on_message, on_connect, on_error, on_close):
        self.auth, self.on_message, self.on_error, self.on_close, self.opened = auth, on_message, on_error, on_close, True
        if self.connect:
            on_connect()

    def subscribe(self, symbols):
        self.subscribe_calls.append(list(symbols))
        self.subscribed.extend(symbols)

    def close(self):
        self.closed = True


@pytest.fixture
def setup(tmp_path):
    net = FakeNet(datetime(2026, 10, 30, 9, 0, 0))
    token = tmp_path / 'token.json'
    write_token(token, net.now)
    out = tmp_path / 'out'
    cal = MarketCalendar(HOLIDAYS)
    rec = qr.Recorder(out, cal, lambda: FakeSocket(net), token, net.clock, net.sleep,
                      fetch_master=lambda url: fake_master(tmp_path).read_bytes(), master_fallback=fake_master(tmp_path))
    window = qr.Window('open_15', datetime(2026, 10, 30, 9, 0, 0), datetime(2026, 10, 30, 9, 15, 0))
    return rec, net, token, out, tmp_path, window


# ---- windows and token ------------------------------------------------------------------------------------------------------------

def test_windows_follow_the_calendar():
    cal = MarketCalendar(HOLIDAYS)
    dst = qr.day_windows(cal, TODAY)
    assert [(w.name, f'{w.start:%H:%M}', f'{w.end:%H:%M}') for w in dst] == [('open_15', '09:00', '09:15'), ('open_45', '09:15', '09:45'),
                                                                              ('close', '22:45', '23:30')]
    assert qr.day_windows(cal, date(2026, 11, 2))[-1].end.strftime('%H:%M') == '23:55'            # after the US DST change
    assert qr.day_windows(cal, date(2026, 1, 15))[0].start.strftime('%H:%M') == '17:00'           # morning closed, evening open
    assert qr.day_windows(cal, date(2026, 10, 31)) == []                                          # Saturday
    assert qr.day_windows(cal, date(2026, 1, 26)) == []                                           # Republic Day, both closed


def test_token_rule(tmp_path):
    now = datetime(2026, 10, 30, 9, 0, 25)
    path = tmp_path / 't.json'
    assert qr.read_token(path, now) == (None, 'no token file')
    write_token(path, now)
    assert qr.read_token(path, now)[0] == f'APP-ID:{SECRET}'
    write_token(path, now, mode=0o644)
    assert 'not 600' in qr.read_token(path, now)[1]
    write_token(path, now, issued=now - timedelta(days=1))
    assert qr.read_token(path, now)[0] is None
    write_token(path, now)
    assert qr.read_token(path, now + timedelta(days=1))[0] is None
    for reason in (qr.read_token(path, now + timedelta(days=1))[1], qr.read_token(tmp_path / 'x', now)[1]):
        assert SECRET not in reason


# ---- the universe -----------------------------------------------------------------------------------------------------------------

PRICES = {'MCX:CRUDEOILM26NOVFUT': 9830.0, 'MCX:CRUDEOILM26DECFUT': 9900.0, 'MCX:SILVERM26NOVFUT': 190000.0, 'MCX:SILVERM26DECFUT': 190500.0}


def load(tmp_path):
    return qr.load_master(tmp_path / 'm', TODAY, fetch=lambda url: fake_master(tmp_path).read_bytes())


def test_master_keeps_recorded_underlyings_and_previous_oi(tmp_path):
    contracts, source = load(tmp_path)
    assert source == 'download' and {c.underlying for c in contracts} == {'CRUDEOILM', 'SILVERM'}        # MCXBULLDEX is not recorded
    assert {c.prev_oi for c in contracts if c.symbol.endswith('9850CE')} == {2878.0}


def test_universe_band_and_own_reference_future(tmp_path):
    contracts, _ = load(tmp_path)
    uni = qr.build_universe(contracts, PRICES, TODAY, 1000)
    assert all(abs(c.strike - PRICES[uni.futures[c.symbol]]) / PRICES[uni.futures[c.symbol]] * 100 <= cfg.BAND_PCT for c in uni.options)
    nov = [c for c in uni.options if 'CRUDEOILM26NOV' in c.symbol]
    assert nov and all(uni.futures[c.symbol] == 'MCX:CRUDEOILM26NOVFUT' for c in nov)               # struck against its own month's future
    assert min(abs(c.strike - 9830) for c in nov) == 20                                              # 9850 is the nearest strike
    missing = qr.build_universe(contracts, {k: v for k, v in PRICES.items() if 'NOV' not in k}, TODAY, 1000)
    assert missing.options and all('DEC' in c.symbol for c in missing.options)                       # the NOV pairs have no price: skipped


def test_allowance_is_shared_between_pairs_closest_first(tmp_path):
    contracts, _ = load(tmp_path)
    uni = qr.build_universe(contracts, PRICES, TODAY, 20)
    assert len(uni.options) == 20
    by_pair = {}
    for c in uni.options:
        by_pair.setdefault((c.underlying, c.expiry), []).append(c)
    assert len(by_pair) == 4 and {len(v) for v in by_pair.values()} == {5}                           # 20 over 4 pairs
    for (und, _), chosen in by_pair.items():                                                          # each pair keeps its own nearest strikes
        price = PRICES[uni.futures[chosen[0].symbol]]
        chain = [c for c in contracts if c.underlying == und and c.expiry == chosen[0].expiry and c.kind in ('CE', 'PE')]
        left = [c for c in chain if c not in chosen and abs(c.strike - price) / price * 100 <= cfg.BAND_PCT]
        assert min(abs(c.strike - price) for c in left) >= max(abs(c.strike - price) for c in chosen)


def test_unused_allowance_goes_to_the_other_pairs(tmp_path, monkeypatch):
    contracts, _ = load(tmp_path)
    monkeypatch.setattr(cfg, 'BAND_PCT', 1.0)                       # in band: crude Nov 8, crude Dec 6, silver Nov 6, silver Dec 8; an equal share of 28 is 7
    uni = qr.build_universe(contracts, PRICES, TODAY, 28)
    per_pair = {}
    for c in uni.options:
        per_pair[(c.underlying, c.expiry.month)] = per_pair.get((c.underlying, c.expiry.month), 0) + 1
    assert len(uni.options) == 28                                   # the whole allowance is used
    assert per_pair[('CRUDEOILM', 12)] == 6 and per_pair[('SILVERM', 11)] == 6          # a pair short of its share gives all it has
    assert per_pair[('CRUDEOILM', 11)] == 8 and per_pair[('SILVERM', 12)] == 8          # the two left over went to the pairs that had more


# ---- which messages are written -----------------------------------------------------------------------------------------------------

def test_change_filter_writes_changes_and_a_heartbeat_for_quiet_quotes():
    f = qr.ChangeFilter()
    t = datetime(2026, 10, 30, 9, 0, 0)
    msg = {'symbol': 'A', 'bid_price': 1, 'ask_price': 2, 'bid_size': 3, 'ask_size': 4, 'ltp': 1.5, 'vol_traded_today': 10}
    assert f.accept(msg, t) is False                                                                   # first sight: written as a change
    assert f.accept(msg, t + timedelta(seconds=10)) is None                                            # unchanged: dropped
    assert f.accept({**msg, 'tot_buy_qty': 99}, t + timedelta(seconds=20)) is None                     # totals alone are not a change
    assert f.accept(msg, t + timedelta(seconds=cfg.HEARTBEAT_S)) is True                               # quiet for HEARTBEAT_S: a heartbeat
    assert f.accept(msg, t + timedelta(seconds=cfg.HEARTBEAT_S + 5)) is None
    assert f.accept({**msg, 'ask_price': 2.5}, t + timedelta(seconds=70)) is False                     # any top-of-book change is written


def test_tick_row_converts_the_exchange_time():
    now = datetime(2026, 10, 30, 9, 0, 1, 500000)
    exch = int(datetime(2026, 10, 30, 9, 0, 1, tzinfo=IST).timestamp())
    row = qr.tick_row(now, {'symbol': 'A', 'exch_feed_time': exch, 'bid_price': 1}, True)
    assert row['exch_time'] == '2026-10-30T09:00:01' and row['ts'] == '2026-10-30T09:00:01.500' and row['heartbeat'] == 1


# ---- the whole window ------------------------------------------------------------------------------------------------------------

def test_window_subscribes_twice_never_unsubscribes_and_stays_in_the_limit(setup):
    rec, net, token, out, tmp, window = setup
    rec.record_window(window)
    assert len(net.sockets) == 1
    sock = net.sockets[0]
    assert sock.opened and sock.closed and sock.unsubscribe_calls == 0 and sock.auth == f'APP-ID:{SECRET}'
    assert len(sock.subscribe_calls) == 2                                                              # futures, then options: two REST symbol lookups
    futures, options = sock.subscribe_calls
    assert set(futures) == {'MCX:CRUDEOILM26NOVFUT', 'MCX:CRUDEOILM26DECFUT', 'MCX:SILVERM26NOVFUT', 'MCX:SILVERM26DECFUT'}
    assert not set(futures) & set(options)
    assert 0 < len(futures) + len(options) <= cfg.MAX_SYMBOLS
    day = out / '2026-10-30'
    assert {'ticks.csv', 'universe_open_15.csv', 'summary_open_15.csv'} <= {p.name for p in day.iterdir()}
    ticks = (day / 'ticks.csv').read_text().splitlines()
    assert ticks[0].split(',') == qr.TICK_FIELDS and len(ticks) > 100
    universe = (day / 'universe_open_15.csv').read_text()
    assert 'prev_oi' in universe.splitlines()[0] and '2878.0' in universe
    assert 'median_spread_pct_of_mid' in (day / 'summary_open_15.csv').read_text()


def test_only_changes_and_heartbeats_are_written_not_every_message(setup):
    rec, net, token, out, tmp, window = setup
    net.quote_fn = lambda sym, now: {**FakeNet.default_quote(sym, now), 'bid_price': 100.0, 'ask_price': 101.0, 'ltp': 100.5,
                                     'vol_traded_today': 5}                                            # a quote that never changes
    rec.record_window(window)
    sock = net.sockets[0]
    rows = list(__import__('csv').DictReader(open(out / '2026-10-30' / 'ticks.csv')))
    symbols = len(set(sock.subscribed))
    assert len(rows) < symbols * 20                                                                    # 15 minutes of 1 Hz messages would be 900 each
    assert {r['heartbeat'] for r in rows} == {'0', '1'}
    first = {r['symbol'] for r in rows if r['heartbeat'] == '0'}
    assert len(first) == symbols                                                                       # every symbol is written once as a change


def test_kill_flag_stops_the_window_and_closes_the_socket(setup):
    rec, net, token, out, tmp, window = setup
    out.mkdir(parents=True, exist_ok=True)
    net.hooks.append(lambda now: (out / cfg.OFF_FLAG_NAME).write_text('') if now >= datetime(2026, 10, 30, 9, 5, 0) else None)
    rec.record_window(window)
    assert len(net.sockets) == 1 and net.sockets[0].closed
    assert net.now < window.end                                                                        # it stopped, it did not run to the end


def test_a_refused_token_stops_the_window_without_reconnecting(setup):
    rec, net, token, out, tmp, window = setup
    net.hooks.append(lambda now: net.sockets and net.sockets[0].subscribed and net.sockets[0].on_error({'code': -16, 'message': SECRET})
                     if now >= datetime(2026, 10, 30, 9, 3, 0) else None)
    rec.record_window(window)
    assert len(net.sockets) == 1 and net.sockets[0].closed
    assert net.now < window.end - timedelta(minutes=5)                                                 # stopped when the error came, not at the end


def test_kill_flag_present_before_the_window_means_no_socket(setup):
    rec, net, token, out, tmp, window = setup
    out.mkdir(parents=True, exist_ok=True)
    (out / cfg.OFF_FLAG_NAME).write_text('')
    rec.record_window(window)
    assert net.sockets == [] and net.now >= window.end


def test_the_symbol_limit_binds_when_the_chains_are_larger_than_it(setup, monkeypatch):
    rec, net, token, out, tmp, window = setup
    monkeypatch.setattr(cfg, 'MAX_SYMBOLS', 30)
    rec.record_window(window)
    futures, options = net.sockets[0].subscribe_calls
    assert len(futures) + len(options) == 30


def test_a_silent_feed_restarts_the_connection_a_bounded_number_of_times(setup):
    rec, net, token, out, tmp, window = setup
    net.silent_after = datetime(2026, 10, 30, 9, 2, 0)
    rec.record_window(window)
    assert 1 < len(net.sockets) <= cfg.MAX_RESTARTS_PER_WINDOW + 1
    assert all(s.closed for s in net.sockets)


def test_no_connection_is_retried_a_bounded_number_of_times(setup):
    rec, net, token, out, tmp, window = setup
    rec.socket_factory = lambda: FakeSocket(net, connect=False)
    rec.record_window(window)
    assert 1 < len(net.sockets) <= cfg.MAX_RESTARTS_PER_WINDOW + 1 and all(s.closed and not s.subscribe_calls for s in net.sockets)


def test_no_priced_future_means_no_option_subscription(setup):
    rec, net, token, out, tmp, window = setup
    net.quote_fn = lambda sym, now: {**FakeNet.default_quote(sym, now), 'ltp': None} if sym.endswith('FUT') else FakeNet.default_quote(sym, now)
    rec.record_window(window)
    assert all(len(s.subscribe_calls) == 1 for s in net.sockets)                                       # only the futures were ever subscribed


def test_a_stale_token_means_no_socket_at_all(setup):
    rec, net, token, out, tmp, window = setup
    write_token(token, net.now, issued=net.now - timedelta(days=1))
    rec.record_window(window)
    assert net.sockets == [] and net.now >= window.end


def test_socket_is_closed_even_when_subscribe_fails(setup):
    rec, net, token, out, tmp, window = setup

    class Broken(FakeSocket):
        def subscribe(self, symbols):
            raise RuntimeError('boom')
    rec.socket_factory = lambda: Broken(net)
    with pytest.raises(RuntimeError):
        rec.record_window(window)
    assert net.sockets[0].closed


def test_token_never_reaches_a_file_or_the_log(setup, caplog):
    rec, net, token, out, tmp, window = setup
    net.hooks.append(lambda now: net.sockets and net.sockets[0].subscribed and net.sockets[0].on_error(f'bad token {SECRET}')
                     if now >= datetime(2026, 10, 30, 9, 2, 0) else None)
    with caplog.at_level(logging.DEBUG):
        rec.record_window(window)
    assert SECRET not in caplog.text and 'socket error' in caplog.text
    for p in out.rglob('*'):
        if p.is_file():
            assert SECRET not in p.read_text(), p


def test_writes_only_under_its_own_folders(setup):
    rec, net, token, out, tmp, window = setup
    before = {p for p in tmp.rglob('*')}
    rec.record_window(window)
    new = {p for p in tmp.rglob('*')} - before
    assert new and all(out in p.parents or p == out for p in new)


def test_run_skips_finished_windows_and_waits_for_the_next(setup):
    rec, net, token, out, tmp, window = setup
    net.now = datetime(2026, 10, 30, 9, 20, 0)                                                         # between the first window and the second
    write_token(token, net.now)
    seen = []
    rec.record_window = lambda w: seen.append(w.name)
    rec.run()
    assert seen == ['open_45', 'close']


def test_logging_is_its_own_and_does_not_propagate(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path / 'logs')
    logger = logging.getLogger('option_recorder')
    before = list(logger.handlers)
    try:
        qr.setup_logging(date(2026, 10, 30))
        assert logger.propagate is False
        assert (tmp_path / 'logs' / 'option_recorder_20261030.log').exists()
        assert not [h for h in logging.getLogger().handlers if getattr(h, 'baseFilename', '').endswith('option_recorder_20261030.log')]
    finally:
        for h in list(logger.handlers):
            if h not in before:
                logger.removeHandler(h)
                h.close()


def test_sdk_api_the_recorder_relies_on():
    data_ws = pytest.importorskip('fyers_apiv3.FyersWebsocket.data_ws')
    import inspect
    params = inspect.signature(data_ws.FyersDataSocket.__init__).parameters
    for name in ('access_token', 'log_path', 'litemode', 'write_to_file', 'reconnect', 'reconnect_retry', 'on_connect', 'on_message',
                 'on_error', 'on_close'):
        assert name in params
    assert 'data_type' in inspect.signature(data_ws.FyersDataSocket.subscribe).parameters
    assert callable(data_ws.FyersDataSocket.close_connection)
