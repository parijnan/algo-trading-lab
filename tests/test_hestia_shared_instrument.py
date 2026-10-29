"""
Two engines on one instrument (2026-10-29, for the Oleus / Janus engine beside Prometheus on CRUDEOILM).

The broker keeps one position row per token and product type, so a live engine may share an instrument with another live engine
only under a different product (Oleus trades INTRADAY, Prometheus CARRYFORWARD); a paper engine has a book of its own and may
share with anything. The ledger stays keyed by engine and token; the broker book Hestia reconciles with is keyed by
`ports.book_key(token, product)`: the bare token for CARRYFORWARD, so every existing book is unchanged.

Also pinned here: the shared feed does not break when either engine is absent from the start, killed mid-session or crashed.
"""
from datetime import datetime, timedelta

import pytest

from hestia_fake_helpers import FRONT, SESSION_DATE, RecEngine, events_of, world
from smartapi_double import FakeClock, ScriptedSmartConnect
from hestia_core.angel_broker import AngelBrokerPort
from hestia_core.executors import InlineExecutor
from hestia_core.fake_kernel import SimKernel
from hestia_core.gateway import BrokerGateway
from hestia_core.interface import (BarComplete, CommandKind, ContractInfo, Direction, OpenRequest, SessionStart, Stop)
from hestia_core.ports import DEFAULT_PRODUCT, OrderSpec, PositionRow, book_key, split_book_key

T0 = datetime(2026, 9, 3, 9, 0)
END = datetime(2026, 9, 3, 23, 40)


def opener(lots, rid):
    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.submit(OpenRequest(rid, FRONT, Direction.BULLISH, lots, trade_ref=1))
    return act


def two(log_a=None, log_b=None, act_a=None, act_b=None, a=None, b=None):
    """Prometheus-like 'a' and Oleus-like 'b' on the same contract; `a` / `b` are register() keyword overrides."""
    return [('a', lambda: RecEngine('a', act=act_a, log=log_a), a or {}),
            ('b', lambda: RecEngine('b', act=act_b, log=log_b), b or {'paper': True})]


@pytest.fixture
def hestias():
    made = []

    def build(engines, **k):
        h = world(engines, **k)
        h.set_price('T1', 100.0)
        made.append(h)
        return h
    yield build
    for h in made:
        h.close()


# ---- the key ---------------------------------------------------------------------------------------------------------------

def test_the_default_product_keeps_the_bare_token_so_existing_books_are_unchanged():
    assert book_key('T1') == 'T1' == book_key('T1', DEFAULT_PRODUCT)
    assert book_key('T1', 'INTRADAY') == 'T1:INTRADAY'
    assert split_book_key('T1') == ('T1', DEFAULT_PRODUCT) and split_book_key('T1:INTRADAY') == ('T1', 'INTRADAY')


# ---- registration ----------------------------------------------------------------------------------------------------------

def test_a_paper_engine_may_share_an_instrument_with_a_live_one(hestias):
    log_a, log_b = [], []
    h = hestias(two(log_a, log_b, opener(3, 'a1'), opener(2, 'b1')))
    h.start_session(SESSION_DATE)
    h.run_until(T0 + timedelta(seconds=120))
    assert h.held('a', FRONT) == 3 and h.held('b', FRONT) == 2
    assert [(o.engine, o.lots) for o in h.orders] == [('a', 3)], 'the paper engine never reaches the live broker'
    assert [(s.engine, s.lots) for _, _, s in h.paper_orders] == [('b', 2)]
    assert h._sim.book == {'T1': [3, 100.0]}


def test_two_live_engines_on_one_instrument_and_one_product_are_refused(hestias):
    h = hestias([('a', lambda: RecEngine('a'), {})])
    with pytest.raises(ValueError, match='one live engine per instrument and product'):
        h.register('b', lambda: RecEngine('b'))
    with pytest.raises(ValueError, match='one live engine per instrument and product'):
        h.register('c', lambda: RecEngine('c'), product='CARRYFORWARD')


def test_an_unknown_product_is_refused(hestias):
    h = hestias([])
    with pytest.raises(ValueError, match='not one of'):
        h.register('a', lambda: RecEngine('a'), product='NRML')


def test_two_live_engines_under_different_products_each_have_a_row_of_their_own(hestias):
    h = hestias(two(act_a=opener(3, 'a1'), act_b=opener(2, 'b1'), b={'product': 'INTRADAY'}))
    h.start_session(SESSION_DATE)
    h.run_until(T0 + timedelta(seconds=120))
    assert h.held('a', FRONT) == 3 and h.held('b', FRONT) == 2
    assert h._sim.book == {'T1': [3, 100.0], 'T1:INTRADAY': [2, 100.0]}
    h.reconcile_ledger()
    h.run_for(10)
    assert h.ledger_mismatches == {} and h.alerts_for('critical') == [], 'each engine matches its own row'


def test_a_mismatch_on_the_intraday_row_is_found_and_names_the_product(hestias):
    h = hestias(two(act_a=opener(3, 'a1'), act_b=opener(2, 'b1'), b={'product': 'INTRADAY'}))
    h.start_session(SESSION_DATE)
    h.run_until(T0 + timedelta(seconds=120))
    h._sim.seed('T1', 5, 100.0, product='INTRADAY')        # the INTRADAY row changed by hand; the carry row is untouched
    h.reconcile_ledger()
    h.run_for(10)
    assert h.ledger_mismatches == {'T1:INTRADAY': (2, 5)}
    (a,) = [x for x in h.alerts_for('critical') if 'ledger mismatch' in x.text]
    assert '[INTRADAY]' in a.text and '+2' in a.text and '+5' in a.text


def test_startup_attributes_each_broker_row_to_the_engine_that_owns_its_product(hestias):
    h = hestias(two(b={'product': 'INTRADAY'}))
    h._sim.seed('T1', -4, 99.5)                              # carried overnight: Prometheus's
    h._sim.seed('T1', 2, 101.0, product='INTRADAY')          # a leftover intraday row: Oleus's
    h.bootstrap_ledger()
    h.run_for(5)
    assert h.held('a', FRONT) == -4 and h.held('b', FRONT) == 2
    assert not [x for x in h.alerts_for('critical')], 'neither row is an orphan'


def test_a_restart_corrects_each_engine_against_its_own_row_only(hestias):
    h = hestias(two(b={'product': 'INTRADAY'}))
    h.seed_position('a', FRONT, 3, 100.0)
    h.seed_position('b', FRONT, 2, 100.0)
    h._sim.seed('T1', 3, 100.0)
    h._sim.seed('T1', 0, None, product='INTRADAY')           # Oleus's row is flat: the broker squared it off
    h.bootstrap_ledger(authoritative=True)
    h.run_for(5)
    assert h.held('a', FRONT) == 3, 'the carry row agrees, so Prometheus is not touched'
    assert h.held('b', FRONT) == 0, 'Oleus is corrected to its own flat row, not to the carry row'
    crit = h.alerts_for('critical')
    assert len(crit) == 1 and crit[0].engine == 'b'


# ---- the shared feed -------------------------------------------------------------------------------------------------------

def _bars_after(log, t):
    return [ev for ts, ev in log if isinstance(ev, BarComplete) and ts > t]


@pytest.mark.parametrize('survivor,leaver', [('a', 'b'), ('b', 'a')])
def test_killing_either_engine_leaves_the_other_with_its_prices_and_bars(hestias, survivor, leaver):
    logs = {'a': [], 'b': []}
    h = hestias(two(logs['a'], logs['b']))
    h.start_session(SESSION_DATE)
    t_kill = T0 + timedelta(minutes=40)
    h.kernel.at(t_kill, lambda: h.send_command(leaver, CommandKind.KILL))
    h.run_until(END)
    assert h.engine_state[leaver] == 'killed' and h.engine_state[survivor] == 'running'
    assert len(_bars_after(logs[survivor], t_kill + timedelta(minutes=1))) > 40, 'the survivor keeps getting 15-minute bars'
    assert not _bars_after(logs[leaver], t_kill + timedelta(minutes=1))
    assert events_of(logs[survivor], Stop) == []
    assert h.alerts_for('critical') == [] and h.data.price('T1') is not None


@pytest.mark.parametrize('only', ['a', 'b'])
def test_either_engine_alone_gets_the_feed_when_the_other_is_not_registered(hestias, only):
    log = []
    engines = [e for e in two(log, log) if e[0] == only]
    h = hestias(engines)
    h.start_session(SESSION_DATE)
    h.run_until(END)
    assert len(events_of(log, BarComplete)) > 50 and h.alerts_for('critical') == []


def test_a_crashing_engine_leaves_the_other_engines_feed_alone(hestias):
    log_a = []

    class Crasher:
        name, spec = 'b', __import__('hestia_fake_helpers').SPEC

        def run(self, ctx):
            ctx.next_event(60)
            raise RuntimeError('always')

    h = hestias([('a', lambda: RecEngine('a', log=log_a), {}), ('b', Crasher, {'paper': True})])
    h.start_session(SESSION_DATE)
    h.run_until(END)
    assert h.engine_state['b'] == 'failed' and h.engine_state['a'] == 'running'
    assert len(events_of(log_a, BarComplete)) > 50


# ---- the Angel One adapter -------------------------------------------------------------------------------------------------

LOT = 10


class Rig:
    def __init__(self):
        self.clock = FakeClock()
        self.sc = ScriptedSmartConnect(self.clock)
        self.gw = BrokerGateway(self.sc, clock=self.clock.monotonic, sleep=self.clock.sleep)
        self.kernel = SimKernel(datetime(2026, 9, 3, 12, 0))
        info = ContractInfo(FRONT, LOT, 0.5, 10, 40)
        self.port = AngelBrokerPort(self.gw, self.kernel, InlineExecutor(), lambda tok: LOT if tok == 'T1' else None,
                                    lambda ref: info if ref.token == 'T1' else None, None, None, self.clock.wall_now,
                                    lambda: None, self.clock.sleep, self.clock.monotonic)

    def place(self, engine, lots=3, side='BUY'):
        out = []
        self.port.place(OrderSpec(engine, f'r-{engine}', FRONT, side, lots, 1, 4), out.append)
        self.kernel.run_for(0)
        return out[0]

    def placed(self):
        return [c for c in self.sc.calls if c[0] == 'place']


def test_each_engines_orders_carry_its_own_product_type():
    r = Rig()
    r.port.register_engine('prometheus', False)
    r.port.register_engine('oleus', False, 'INTRADAY')
    r.place('prometheus')
    r.place('oleus')
    r.place('unregistered')
    assert [c[1]['producttype'] for c in r.placed()] == ['CARRYFORWARD', 'INTRADAY', 'CARRYFORWARD']


def test_an_unknown_product_is_refused_by_the_adapter():
    with pytest.raises(ValueError, match='not one of'):
        Rig().port.register_engine('x', False, 'NRML')


def test_ghost_recovery_does_not_claim_an_order_of_the_other_product():
    r = Rig()
    r.port.register_engine('oleus', False, 'INTRADAY')
    other = r.sc._new_order({'tradingsymbol': FRONT.symbol, 'symboltoken': 'T1', 'transactiontype': 'BUY', 'quantity': '30',
                             'producttype': 'CARRYFORWARD'}, 'complete', 30)      # Prometheus's identical order, same minute
    r.sc.place_script = ['ghost']
    res = r.place('oleus')
    assert res.order_id != other['orderid'] and res.kind == 'filled'


def test_the_position_book_is_keyed_by_token_and_product():
    r = Rig()
    r.sc.position_rows = [{'symboltoken': 'T1', 'producttype': 'CARRYFORWARD', 'netqty': '-30', 'netprice': '99.5'},
                          {'symboltoken': 'T1', 'producttype': 'INTRADAY', 'netqty': '20', 'netprice': '101.0'},
                          {'symboltoken': 'T1x', 'netqty': '7', 'netprice': '0'}]
    out = []
    r.port.read_positions(out.append)
    r.kernel.run_for(0)
    assert out[0] == {'T1': PositionRow(-3, 99.5), 'T1:INTRADAY': PositionRow(2, 101.0), 'T1x': PositionRow(7, None)}


# ---- trade records with extra columns --------------------------------------------------------------------------------------

def test_an_engine_may_declare_extra_trade_columns_after_the_shared_ones(hestias, tmp_path):
    from hestia_core.interface import TRADE_RECORD_COLUMNS
    from hestia_core.reporting import TradeLogWriter

    class Three(RecEngine):
        trade_extra_columns = ('setup', 'lot3_exit_price')

    def act(eng, ctx, ev):
        if isinstance(ev, SessionStart):
            ctx.report_trade({'trade_id': 1, 'direction': 'bullish', 'setup': 'F_L', 'lot3_exit_price': 107.75, 'bogus': 1})
    h = hestias([('a', lambda: Three('a', act=act), {})])
    h.start_session(SESSION_DATE)
    h.run_until(T0 + timedelta(seconds=30))
    (name, row), = h.trades
    assert tuple(row) == TRADE_RECORD_COLUMNS + ('setup', 'lot3_exit_price')
    assert row['setup'] == 'F_L' and any('bogus' in w for w in h.warnings)
    w = TradeLogWriter(tmp_path)
    w.write('a', row)
    header = (tmp_path / 'a_trades.csv').read_text().splitlines()[0].split(',')
    assert header == list(TRADE_RECORD_COLUMNS) + ['setup', 'lot3_exit_price']


# ---- the real LiveData service (what Delos runs), two engines on one token ------------------------------------------------------

@pytest.fixture
def worlds(tmp_path):
    from hestia_data_helpers import LiveWorld
    made = []

    def build(engines, **kw):
        w = LiveWorld(tmp_path / f'w{len(made)}', engines, **kw)
        made.append(w)
        return w
    yield build
    for w in made:
        w.close()


def _live_pair(log_a, log_b, act_b=None):
    return [('a', lambda: RecEngine('a', log=log_a), {}), ('b', lambda: RecEngine('b', log=log_b, act=act_b), {'paper': True})]


def _bar_keys(log, after=None):
    return [(ev.boundary_ts, ev.bar.close if ev.bar else None) for ts, ev in log
            if isinstance(ev, BarComplete) and (after is None or ts > after)]


def test_livedata_gives_both_engines_the_same_bars_and_prices_on_one_token(worlds):
    log_a, log_b = [], []
    w = worlds(_live_pair(log_a, log_b)).start()
    w.run_until(END)
    assert _bar_keys(log_a) == _bar_keys(log_b) and len(_bar_keys(log_a)) == 58
    assert w.core.alerts_for('critical') == [] and len(w.data.streams) >= 1


@pytest.mark.parametrize('survivor,leaver', [('a', 'b'), ('b', 'a')])
def test_livedata_keeps_serving_the_survivor_when_the_other_engine_is_killed(worlds, survivor, leaver):
    logs = {'a': [], 'b': []}
    w = worlds(_live_pair(logs['a'], logs['b'])).start()
    t_kill = T0 + timedelta(minutes=40)
    w.kernel.at(t_kill, lambda: w.core.send_command(leaver, CommandKind.KILL))
    w.run_until(END)
    after = _bar_keys(logs[survivor], t_kill + timedelta(minutes=1))
    assert len(after) > 40, 'the survivor keeps getting 15-minute bars from the shared stream'
    assert not _bar_keys(logs[leaver], t_kill + timedelta(minutes=1))
    assert w.core.engine_state[survivor] == 'running' and w.core.alerts_for('critical') == []
    assert w.data.price('T1') is not None and w.data.ltp_quote('T1') is not None


@pytest.mark.parametrize('only', ['a', 'b'])
def test_livedata_serves_either_engine_alone(worlds, only):
    log = []
    engines = [e for e in _live_pair(log, log) if e[0] == only]
    w = worlds(engines).start()
    w.run_until(END)
    assert len(_bar_keys(log)) == 58 and w.core.alerts_for('critical') == []


def test_livedata_minute_bars_are_completed_minutes_of_the_traded_contract(worlds):
    seen = {}

    def act(eng, ctx, ev):
        if isinstance(ev, BarComplete) and ev.boundary_ts == datetime(2026, 9, 3, 9, 30):
            seen['bars'] = ctx.minute_bars(FRONT, datetime(2026, 9, 2, 9, 0), datetime(2026, 9, 3, 23, 0))
            seen['now'] = ctx.now()
    w = worlds(_live_pair([], [], act)).start()
    w.run_until(datetime(2026, 9, 3, 9, 40))
    bars = seen['bars']
    stamps = [b.ts for b in bars]
    assert stamps == sorted(stamps) and stamps[0] == datetime(2026, 9, 2, 9, 0), 'the seeded previous day is there'
    assert stamps[-1] < seen['now'].replace(second=0, microsecond=0)
    assert datetime(2026, 9, 2, 23, 29) in stamps, 'the whole previous session, to its last minute'


# ---- the whole host (start report, seeding, one login, teardown) with two engines on one instrument -----------------------------

def test_the_host_starts_seeds_trades_and_tears_down_with_two_engines_on_one_instrument(tmp_path):
    from hestia_config import EngineEntry
    from hestia_core.interface import RequestOutcome, StopReason
    from hestia_host_helpers import HostRun, wait_until

    log_a, log_b = [], []

    def opener_for(log, lots, rid):
        def act(eng, ctx, ev):
            if isinstance(ev, SessionStart):
                ctx.submit(OpenRequest(rid, FRONT, Direction.BULLISH, lots, trade_ref=1))
        return lambda: RecEngine(rid[0], act=act, log=log, timeout=0.05)
    engines = {'a': EngineEntry(instrument='XX', factory='unused:unused', enabled=True),
               'b': EngineEntry(instrument='XX', factory='unused:unused', enabled=True, paper=True)}
    run = HostRun(tmp_path, engines, {'a': opener_for(log_a, 3, 'a1'), 'b': opener_for(log_b, 2, 'b1')}).start()
    wait_until(lambda: any(isinstance(e, RequestOutcome) for _, e in log_a) and any(isinstance(e, RequestOutcome) for _, e in log_b),
               what='both outcomes')
    assert run.login_calls() == 1
    assert [(o['transactiontype'], o['quantity']) for o in run.double.orders.orders] == [('BUY', '30')], 'only the live engine reaches the broker'
    (run.cfg.FLAG_DIR / 'hestia_active.flag').unlink()
    r = run.join()
    assert r.started and r.reason == 'ended: shutdown' and r.teardown.terminated
    assert r.core.held('a', FRONT) == 3 and r.core.held('b', FRONT) == 2
    texts = run.texts()
    assert any('Hestia running: engines A, B' in t for t in texts) and any('data seeded' in t for t in texts)
    assert [e.reason for _, e in log_a if isinstance(e, Stop)] == [StopReason.SHUTDOWN] == [e.reason for _, e in log_b if isinstance(e, Stop)]
    assert r.core.alerts_for('critical') == []
    report = [t for t in texts if t.startswith('\U0001f4ca *Hestia — Session Report*')]
    assert len(report) == 1 and '*A* [XX]' in report[0] and '*B* [XX]' in report[0]
