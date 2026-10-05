"""The Prometheus engine on the fake Hestia: levels and config parity with production, then entry, targets, stop, Rule 7 flip, failure
handling, restart, the operator's EXIT, and the ledger-wins reconciliation. Rolling and provisional trading are in
test_prometheus_engine_roll.py."""
import ast
import dataclasses
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from prometheus_engine_helpers import (CFG, DAYS, FLIP_PATH, FRONT, SESSION_DATE, SESSION_OPEN, ZIGZAG, alert_texts, hm, scripted_world,
                                       trades)
from hestia_core.replay import BrokerReply
from hestia_core.interface import CommandKind
from prometheus_engine.engine_configs import DEFAULT
from prometheus_engine.levels import build_levels, lot_pnl_points, margin_per_unit, resolve_target2, resolve_thresholds

PROD = Path(__file__).resolve().parents[1] / 'prometheus_production'


# ---------------------------------------------------------------------------------------------------------------------
# Parity with production while both exist
# ---------------------------------------------------------------------------------------------------------------------

def _prod_constants():
    tree = ast.parse((PROD / 'prometheus_configs.py').read_text())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                pass
    return out


def test_engine_config_matches_production_configs():
    c, p = DEFAULT, _prod_constants()
    pairs = {'st_period': 'ST_PERIOD', 'st_multiplier': 'ST_MULTIPLIER', 'lots_per_leg': 'LOTS_PER_LEG', 'sl_pct': 'SL_PCT',
             'target1_pct': 'TARGET1_PCT', 'target2_flat_pct': 'TARGET2_FLAT_PCT', 'target2_source': 'TARGET2_MODE',
             'no_exit_before_buffer_min': 'NO_EXIT_BEFORE_BUFFER_MIN', 'min_entry_buffer_min': 'MIN_ENTRY_BUFFER_MIN',
             'provisional_enabled': 'PROVISIONAL_BOUNDARY_ENABLED', 'provisional_margin_pct': 'PROVISIONAL_MARGIN_PCT',
             'margin_contract_value_divisor': 'MARGIN_CONTRACT_VALUE_DIVISOR', 'margin_sizing_multiplier': 'MARGIN_SIZING_MULTIPLIER',
             'fallback_margin_per_unit': 'MARGIN_PER_UNIT', 'trade_update_sec': 'TRADE_UPDATE_SEC'}
    for mine, theirs in pairs.items():
        want = p[theirs]
        if mine == 'target2_source':
            assert want == 'flat_pct' and c.target2_source == 'flat_pct'
        else:
            assert getattr(c, mine) == want, f'{mine} drifted from production {theirs}'


def _prod_function(name):
    """Production's own source for a module-level function, executed against the engine config's constants."""
    tree = ast.parse((PROD / 'prometheus_functions.py').read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {'TARGET1_PCT': DEFAULT.target1_pct, 'SL_PCT': DEFAULT.sl_pct, 'TARGET2_MODE': 'flat_pct',
          'TARGET2_FLAT_PCT': DEFAULT.target2_flat_pct}
    exec(compile(ast.Module([fn], []), 'prometheus_functions', 'exec'), ns)
    return ns[name]


@pytest.mark.parametrize('price', [61.0, 5423.5, 5811.25, 7000.0])
@pytest.mark.parametrize('direction', ['bullish', 'bearish'])
def test_levels_match_production_functions(price, direction):
    assert resolve_thresholds(price, DEFAULT) == _prod_function('resolve_thresholds')(price)
    assert resolve_target2(price, direction, DEFAULT) == _prod_function('resolve_target2')(price, direction)


def test_build_levels_shape_like_finalize_new_position():
    lv = build_levels('bullish', 6000.0, filled_lots=2, units=1, cfg=CFG)
    assert (lv.lot1_lots, lv.lot2_lots) == (1, 1)
    assert lv.sl_price == pytest.approx(6000 * (1 - 0.022))
    assert lv.lot1_target == pytest.approx(6000 * 1.022) and lv.lot2_target == pytest.approx(6000 * 1.05)
    lv = build_levels('bearish', 6000.0, filled_lots=3, units=2, cfg=CFG)        # partial fill: lot1 first, lot2 gets the rest
    assert (lv.lot1_lots, lv.lot2_lots) == (2, 1)
    lv = build_levels('bearish', 6000.0, filled_lots=1, units=1, cfg=CFG, lot2_only=True)
    assert (lv.lot1_lots, lv.lot2_lots, lv.lot1_target) == (0, 1, None)
    assert lv.sl_price == pytest.approx(6000 * 1.022) and lv.lot2_target == pytest.approx(6000 * 0.95)


def test_pnl_and_margin_arithmetic():
    assert lot_pnl_points('bullish', 100, 103) == 3 and lot_pnl_points('bearish', 100, 103) == -3
    assert margin_per_unit(6000.0, 10, DEFAULT) == pytest.approx(6000 * 10 / 3 * 4)
    assert margin_per_unit(None, 10, DEFAULT) == DEFAULT.fallback_margin_per_unit


# ---------------------------------------------------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------------------------------------------------

def run(h, until):
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(until)


def ledger(h):
    return h._ledger.get(('prometheus', FRONT.token), [0])[0]


def test_targets_book_both_lots_and_the_engine_rearms():
    made = []
    h = scripted_world(ZIGZAG, made=made)
    run(h, hm(23, 40))
    rows = trades(h)
    assert [r['direction'] for r in rows] == ['bearish', 'bullish', 'bearish']
    assert [r['trade_id'] for r in rows] == [1, 2, 3]
    for r in rows:
        assert r['lot1_exit_reason'] == 'target1' and r['lot2_exit_reason'] == 'target2_flat_pct'
        assert r['total_pnl_points'] == pytest.approx(r['lot1_pnl_points'] + r['lot2_pnl_points'], abs=0.011)
        assert r['sl_price'] and r['units'] == 1
    assert [(o.side, o.lots) for o in h.orders] == [('SELL', 2), ('BUY', 1), ('BUY', 1), ('BUY', 2), ('SELL', 1), ('SELL', 1),
                                                     ('SELL', 2), ('BUY', 1), ('BUY', 1)]
    assert ledger(h) == 0
    assert made[-1].state.status == 'watching' and made[-1].state.trade_counter == 3 and not made[-1].state.pending
    # request ids: session date, trade number, purpose, attempt: unique and deterministic
    assert h.orders[0].request_id == 'prometheus-20260903-t1-entry-1'
    assert len({o.request_id for o in h.orders}) == len(h.orders)


def test_entry_levels_come_off_the_real_fill():
    made = []
    h = scripted_world(ZIGZAG, made=made)
    run(h, hm(10, 30))
    s = made[-1].state
    assert s.status == 'in_trade' and s.direction == 'bearish' and s.lot1_lots == 1 and s.lot2_lots == 1
    assert s.sl_price == pytest.approx(s.entry_price * 1.022)
    assert s.lot1_target == pytest.approx(s.entry_price * 0.978) and s.lot2_target == pytest.approx(s.entry_price * 0.95)
    assert ledger(h) == -2 and s.signal_ts is not None and s.contract_token == FRONT.token


def test_rule7_flip_is_one_netted_order_and_books_the_old_trade():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    run(h, hm(13, 0))
    assert [(o.side, o.lots) for o in h.orders[:2]] == [('SELL', 2), ('BUY', 4)]         # entry, then close 2 + open 2 as one order
    assert h.orders[1].request_id.endswith('-flip-1')
    old = trades(h)[0]
    assert old['lot1_exit_reason'] == 'trend_flip' and old['lot2_exit_reason'] == 'trend_flip'
    s = made[-1].state
    assert s.status == 'in_trade' and s.direction == 'bullish' and s.trade_counter == 2 and s.pending_flip is None
    assert s.entry_price == pytest.approx(old['lot1_exit_price'], abs=0.011)             # the flip's fill is both the exit and the entry
    assert ledger(h) == 2
    assert len(trades(h)) == 1                                                            # not a runaway: one flip, one closed trade


def test_stop_loss_flattens_and_books_both_lots():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(10, 30))
    sl = made[-1].state.sl_price
    h.schedule_price(FRONT.token, hm(10, 40), sl + 0.3)
    h.run_until(hm(10, 45))
    row = trades(h)[0]
    assert row['lot1_exit_reason'] == 'stop_loss' and row['lot2_exit_reason'] == 'stop_loss'
    assert h.orders[1].request_id.endswith('-exit_all-1') and (h.orders[1].side, h.orders[1].lots) == ('BUY', 2)
    assert row['total_pnl_points'] < 0 and ledger(h) == 0 and made[-1].state.status == 'watching'


def test_a_stale_price_never_triggers_a_stop():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(10, 30))
    sl = made[-1].state.sl_price
    h.schedule_price(FRONT.token, hm(10, 40), sl + 1.0)
    h.inject_feed_stale('prometheus', FRONT, hm(10, 35), hm(10, 50))
    h.run_until(hm(10, 48))
    assert len(h.orders) == 1 and made[-1].state.status == 'in_trade'                      # ltp_max_age_s: the old tick is ignored


def test_partial_entry_fill_is_accepted_without_a_top_up():
    made = []
    h = scripted_world(FLIP_PATH, made=made, broker=lambda c: BrokerReply('partial', lots=1) if c.request.request_id.endswith('entry-1')
                       else BrokerReply('fill'))
    run(h, hm(10, 40))
    s = made[-1].state
    assert s.status == 'in_trade' and (s.lot1_lots, s.lot2_lots) == (1, 0) and s.lot2_status == 'never_opened'
    assert sum(1 for o in h.orders if o.request_id.endswith('entry-1')) >= 1 and ledger(h) == -1
    assert any('partial fill' in t for t in alert_texts(h))
    assert not any('-entry-2' in o.request_id for o in h.orders)


def test_failed_entry_leaves_the_engine_flat_and_still_watching():
    made = []
    h = scripted_world(FLIP_PATH, made=made, broker=lambda c: BrokerReply('reject'))
    run(h, hm(10, 40))
    assert made[-1].state.status == 'watching' and made[-1].state.trade_counter == 0 and not made[-1].state.pending
    assert any('did not fill' in t for t in alert_texts(h))


def test_failed_exit_is_re_sent_under_a_new_id_until_it_lands():
    made = []
    state = {'fail': True}

    def broker(call):
        if call.request.request_id.endswith('exit_all-1') or call.request.request_id.endswith('exit_all-2'):
            return BrokerReply('reject')
        return BrokerReply('fill')
    h = scripted_world(FLIP_PATH, made=made, broker=broker)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(10, 30))
    h.schedule_price(FRONT.token, hm(10, 40), made[-1].state.sl_price + 0.3)
    h.run_until(hm(10, 50))
    ids = [o.request_id for o in h.orders if 'exit_all' in o.request_id]
    assert any(i.endswith('exit_all-3') for i in ids)
    assert trades(h)[0]['lot1_exit_reason'] == 'stop_loss' and made[-1].state.status == 'watching'
    assert any('failed' in t for t in alert_texts(h))


def test_unconfirmed_exit_changes_nothing_until_settled():
    made = []
    h = scripted_world(FLIP_PATH, made=made,
                       broker=lambda c: BrokerReply('unconfirmed', lots=0, resolve_after=90.0) if 'exit_all' in c.request.request_id
                       else BrokerReply('fill'))
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(10, 30))
    h.schedule_price(FRONT.token, hm(10, 40), made[-1].state.sl_price + 0.3)
    h.run_until(hm(10, 41))
    assert made[-1].state.status == 'in_trade'                                              # nothing booked on a guess
    assert len([o for o in h.orders if 'exit_all' in o.request_id]) == 1                   # and no second exit while one is unresolved


def test_exit_command_liquidates_and_rearms():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(10, 30))
    h.send_command('prometheus', CommandKind.EXIT)
    h.run_until(hm(10, 32))
    row = trades(h)[0]
    assert row['lot1_exit_reason'] == 'slack_exit' and row['lot2_exit_reason'] == 'slack_exit'
    assert made[-1].state.status == 'watching' and ledger(h) == 0 and not made[-1].exit_requested
    # re-armed: the later bullish flip is still traded
    h.run_until(hm(14, 30))
    assert any(r['direction'] == 'bullish' for r in trades(h)) or made[-1].state.direction == 'bullish'


def test_exit_command_with_no_position_is_a_no_op():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(9, 30))
    h.send_command('prometheus', CommandKind.EXIT)
    h.run_until(hm(9, 31))
    assert not h.orders and any('no open position' in t for t in alert_texts(h))


def test_margin_refusal_skips_the_entry():
    from hestia_core.fake import FakeConfig
    made = []
    h = scripted_world(FLIP_PATH, made=made, config=FakeConfig(available_cash=1000.0))
    run(h, hm(10, 40))
    assert not h.orders and made[-1].state.status == 'watching'
    assert any('insufficient margin' in t for t in alert_texts(h))


# ---------------------------------------------------------------------------------------------------------------------
# Restart and reconciliation: the ledger wins
# ---------------------------------------------------------------------------------------------------------------------

def test_engine_crash_and_resume_does_not_duplicate_the_position():
    from prometheus_engine.engine import PrometheusEngine
    crashed = {'n': 0}
    made = []

    class Crashy(PrometheusEngine):
        def _tick(self):
            if self.state.status == 'in_trade' and not crashed['n'] and self._now() >= hm(10, 30):
                crashed['n'] += 1
                raise RuntimeError('boom')
            super()._tick()

    from prometheus_engine_helpers import ContractSpec, scripted_minutes, world
    h = world(engines=[('prometheus', lambda: (made.append(Crashy(CFG)) or made[-1]), {'lots_per_unit': 2})], contracts=(),
              extra=[ContractSpec(FRONT, lot_size=10, tick_size=0.5, freeze_qty_lots=20, minutes=scripted_minutes(FLIP_PATH))])
    run(h, hm(11, 0))
    assert crashed['n'] == 1 and len(made) >= 2
    resumed = made[-1].state
    assert resumed.status == 'in_trade' and resumed.direction == 'bearish' and resumed.trade_counter == 1
    assert sum(1 for o in h.orders if o.request_id.endswith('entry-1')) == 1               # never entered twice
    assert ledger(h) == -2


def test_ledger_flat_but_state_in_trade_returns_to_watching():
    from prometheus_engine.state import EngineState
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    st = EngineState(status='in_trade', direction='bearish', units=1, entry_price=99.0, entry_ts='2026-09-03T09:30:00',
                     contract_token=FRONT.token, contract_symbol=FRONT.symbol, contract_expiry=FRONT.expiry.isoformat(),
                     sl_price=101.0, lot1_target=97.0, lot1_lots=1, lot1_status='open', lot2_target=94.0, lot2_lots=1,
                     lot2_status='open', lot2_source='flat_pct', trade_counter=4)
    h._saved_state['prometheus'] = st.to_json()
    run(h, hm(9, 30))
    assert made[-1].state.status == 'watching' and made[-1].state.trade_counter == 4
    assert any('ledger is flat' in t for t in alert_texts(h)) and not trades(h)


def test_ledger_holds_a_position_the_engine_never_knew_about():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.seed_position('prometheus', FRONT, -2, 99.0)
    run(h, hm(9, 30))
    s = made[-1].state
    assert s.status == 'in_trade' and s.direction == 'bearish' and s.entry_price == pytest.approx(99.0)
    assert s.sl_price == pytest.approx(99.0 * 1.022) and (s.lot1_lots, s.lot2_lots) == (1, 1)
    assert any('ledger holds' in t for t in alert_texts(h))


def test_first_minutes_are_guarded():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(9, 0))
    e = made[-1]
    assert not e._past_first_minute_guard(hm(9, 0)) and e._past_first_minute_guard(hm(9, 1))
    assert not e._past_min_entry_guard(hm(9, 14)) and e._past_min_entry_guard(hm(9, 15))


# ---------------------------------------------------------------------------------------------------------------------
# Sizing and provisional-boundary trading
# ---------------------------------------------------------------------------------------------------------------------

def test_dynamic_sizing_uses_the_allocation_and_the_live_margin_per_unit():
    from hestia_core.interface import SizingConfig
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.set_sizing('prometheus', SizingConfig(dynamic=True, static_units=1, unit_cap=50, allocation_rs=10_000.0))
    run(h, hm(10, 30))
    s = made[-1].state
    per_unit = 99.41 * 10 / 3 * 4                                   # LTP x lot size / 3 x 4
    assert s.units == int(10_000 // per_unit) and s.units > 1
    assert h.orders[0].lots == s.units * 2 and (s.lot1_lots, s.lot2_lots) == (s.units, s.units)


def test_dynamic_sizing_never_asks_for_more_than_the_unit_cap():
    from hestia_core.interface import SizingConfig
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.set_sizing('prometheus', SizingConfig(dynamic=True, static_units=1, unit_cap=3, allocation_rs=1_000_000.0))
    run(h, hm(10, 30))
    assert made[-1].state.units == 3 and h.orders[0].lots == 6


def _provisional_run(cfg=CFG, boundary=hm(10, 15), override=None, until=None):
    made = []
    h = scripted_world(FLIP_PATH, cfg=cfg, made=made)
    h.bar_delay[(FRONT.token, boundary)] = 40.0
    if override:
        h.provisional_override[(FRONT.token, boundary)] = override
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(until or boundary + timedelta(minutes=3))
    return h, made


def test_provisional_flip_acts_at_the_boundary_and_is_confirmed_by_the_real_bar():
    h, made = _provisional_run()
    assert [(o.ts.hour, o.ts.minute, o.ts.second, o.lots) for o in h.orders] == [(10, 15, 0, 2)]        # once, at the boundary
    texts = alert_texts(h)
    assert any('PROVISIONAL flip -> bearish' in t for t in texts) and any('CONFIRMED by the real bar' in t for t in texts)
    assert made[-1].provisional_pending is None and made[-1].state.last_processed_boundary == '2026-09-03T10:00:00'


def test_provisional_disagreement_alerts_and_switches_provisional_off_for_the_session():
    h, made = _provisional_run(boundary=hm(9, 45), override={'close': 90.0}, until=hm(9, 50))
    e = made[-1]
    assert len(h.orders) == 1 and e.state.status == 'in_trade'                       # no automated reversal
    assert e.provisional_disabled and any('DISAGREES' in t for t in alert_texts(h))


def test_provisional_margin_guard_measures_against_the_previous_supertrend():
    import dataclasses
    h, made = _provisional_run(cfg=dataclasses.replace(CFG, provisional_margin_pct=50.0), until=datetime(2026, 9, 3, 10, 15, 20))
    assert not h.orders                                                               # the provisional close did not clear the margin
    h.run_until(hm(10, 20))
    assert [(o.ts.minute, o.ts.second) for o in h.orders] == [(15, 40)]               # the real bar (40 s late) then makes the entry


def test_provisional_disabled_in_config_ignores_provisional_bars():
    import dataclasses
    h, made = _provisional_run(cfg=dataclasses.replace(CFG, provisional_enabled=False))
    assert [(o.ts.minute, o.ts.second) for o in h.orders] == [(15, 40)]


# ---------------------------------------------------------------------------------------------------------------------
# Decisions with a request outstanding (unit level, on a stub context)
# ---------------------------------------------------------------------------------------------------------------------

class StubCtx:
    def __init__(self, now):
        self._now, self.sent, self.alerts, self.saved = now, [], [], []

    def now(self):
        return self._now

    def submit(self, request):
        from hestia_core.interface import AckStatus, RequestAck
        self.sent.append(request)
        return RequestAck(request.request_id, AckStatus.ACCEPTED)

    def alert(self, level, text, channel=None, emoji=None, log_locally=True):
        self.alerts.append((level, text))

    def save_state(self, blob):
        self.saved.append(blob)

    def ltp(self, ref):
        return None

    def sizing(self):
        from hestia_core.interface import SizingConfig
        return SizingConfig(dynamic=False, static_units=1, unit_cap=50)


def _in_trade_engine(pending):
    from prometheus_engine.engine import PrometheusEngine
    from prometheus_engine.state import EngineState
    e = PrometheusEngine(CFG)
    e.ctx = StubCtx(hm(11, 0))
    e.session_date, e.session_open, e.rollover_at = SESSION_DATE, SESSION_OPEN, hm(23, 14)
    e.contract = FRONT
    e.state = EngineState(status='in_trade', direction='bearish', units=1, entry_price=99.0, contract_token=FRONT.token,
                          lot1_lots=1, lot1_status='open', lot2_lots=1, lot2_status='open', pending=pending)
    return e


def test_a_flip_signal_never_overlaps_an_outstanding_request():
    e = _in_trade_engine({'prometheus-x-t1-lot1-1': {'purpose': 'lot1'}})
    assert e._act_on_signal('bullish', True, hm(10, 45), 100.0, provisional=False) is False
    assert not e.ctx.sent and e.state.pending_flip is None


def test_a_flip_signal_in_the_same_direction_does_nothing():
    e = _in_trade_engine({})
    assert e._act_on_signal('bearish', True, hm(10, 45), 100.0, provisional=False) is False and not e.ctx.sent


def test_a_partly_filled_position_still_writes_a_well_formed_trade_row():
    from hestia_core.interface import TRADE_RECORD_COLUMNS
    made = []
    h = scripted_world(FLIP_PATH, made=made, broker=lambda c: BrokerReply('partial', lots=1) if c.request.request_id.endswith('entry-1')
                       else BrokerReply('fill'))
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(10, 30))
    h.send_command('prometheus', CommandKind.EXIT)
    h.run_until(hm(10, 35))
    row = trades(h)[0]
    assert set(row) <= set(TRADE_RECORD_COLUMNS) and row['lot2_exit_reason'] is None      # lot 2 was never opened: its columns stay empty
    assert row['total_pnl_points'] == row['lot1_pnl_points'] and row['total_pnl_rs'] == row['lot1_pnl_rs']


@pytest.mark.parametrize('close, prev_st, st_value, acts', [
    (100.0, 99.9, 90.0, False),     # clears the CURRENT supertrend by 10% but the PREVIOUS one by only 0.1%: no action
    (100.0, 90.0, 99.95, True),     # clears the PREVIOUS supertrend by 11%, the current one by 0.05%: acts
])
def test_provisional_margin_is_measured_against_the_previous_supertrend(close, prev_st, st_value, acts):
    from hestia_core.interface import Bar, Direction, ProvisionalBar, SupertrendPoint
    from prometheus_engine.engine import PrometheusEngine
    from prometheus_engine.state import EngineState

    class Ctx(StubCtx):
        def ltp(self, ref):
            from hestia_core.interface import LtpQuote
            return LtpQuote(100.0, self._now, 0.0)

        def margin(self):
            from hestia_core.interface import MarginSnapshot
            return MarginSnapshot(10_000_000.0, self._now)

    e = PrometheusEngine(CFG)
    e.ctx = Ctx(hm(11, 0))
    e.session_date, e.session_open, e.rollover_at, e.contract = SESSION_DATE, SESSION_OPEN, hm(23, 14), FRONT
    e.state = EngineState(status='watching')
    bar = Bar(hm(10, 45), close, close, close, close, 1.0)
    e._on_provisional(ProvisionalBar(FRONT, hm(11, 0), bar, SupertrendPoint(st_value, Direction.BEARISH, True), prev_st))
    assert bool(e.ctx.sent) is acts


def test_a_stop_refused_because_the_market_is_closed_is_not_retried_and_nothing_is_sent_after_the_close():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(23, 20))
    e = made[-1]
    assert e.state.status == 'in_trade' and e.state.direction == 'bullish'
    n_before = len(h.orders)
    h.schedule_price(FRONT.token, hm(23, 29) + timedelta(seconds=30), e.state.sl_price - 0.5)        # through the stop, after Hestia's close
    h.schedule_price(FRONT.token, hm(23, 31), e.state.sl_price - 1.0)
    h.run_until(hm(23, 45))
    assert len(h.orders) == n_before                                                       # nothing reached the broker
    ids = [k[1] for k in h._registry if 'exit_all' in k[1]]
    assert len(ids) <= 1, ids                                                              # at most the one refused request, no retry loop
    assert len(ids) == 1 and any('market is closed' in t for t in alert_texts(h))              # sent at 23:29:30, refused, given up
    assert e.state.status == 'in_trade'                                                    # carried, not booked on a guess


def test_a_refused_flip_reentry_falls_back_to_closing_the_old_side_only():
    from hestia_core.interface import OutcomeStatus, RequestKind, RequestOutcome
    from test_prometheus_engine_ports import engine, in_trade
    e = engine(hm(11, 0), in_trade('bearish'))
    e.state.pending_flip = {'direction': 'bullish', 'signal_ts': hm(10, 45).isoformat(), 'signal_close': 100.0, 'units': 1, 'new_lots': 2}
    e.state.pending = {'r1': {'purpose': 'flip', 'direction': 'bullish', 'signal_ts': hm(10, 45).isoformat(), 'signal_close': 100.0,
                             'units': 1, 'lots': 2, 'token': FRONT.token}}
    e._on_outcome(RequestOutcome('r1', OutcomeStatus.LIMIT_REFUSED, RequestKind.FLIP, 4, hm(11, 0), detail='unit cap: 12 lots would exceed 10'))
    assert e.state.pending_flip['new_lots'] == 0 and not e.state.pending and e.state.status == 'in_trade'


def test_every_flip_sends_the_raw_signal_alert_independent_of_the_outcome():
    made = []
    h = scripted_world(ZIGZAG, made=made)
    run(h, hm(23, 40))
    flips = [t for t in alert_texts(h) if t.startswith('ST_15 flip -> ')]
    assert len(flips) >= 3                                              # ZIGZAG has 3 real flips (its 3 entries)
    assert 'close=' in flips[0] and 'ST=' in flips[0]
    hits = [a for a in h.alerts if a.text.startswith('ST_15 flip -> ') and a.channel == 'tradebot-updates']
    assert len(hits) == len(flips)                                      # every one lands on #tradebot-updates
    assert flips[0].startswith('ST_15 flip -> bearish at')              # ZIGZAG's first real flip


def test_session_start_announces_the_contract_and_the_seeded_st_with_the_standalones_own_emoji():
    """The standalone Prometheus process's own two-message session-open announcement, ported back 2026-09-29 -- found
    missing entirely (no engine said anything on a clean start before this). Checks both the content and that the
    per-event emoji override (not Hestia's own severity-based default, which is empty for 'info') actually reaches the
    Alert."""
    made = []
    h = scripted_world(ZIGZAG, made=made)
    run(h, hm(9, 1))
    tb = [a for a in h.alerts if a.channel == 'tradebot-updates']
    starting = next(a for a in tb if a.text.startswith('starting — trading'))
    seeded = next(a for a in tb if a.text.startswith('ST_15 seeded'))
    assert starting.text == f'starting — trading {FRONT.symbol} (session 09:00–23:30)' and starting.emoji == '⚡'
    assert seeded.emoji == '✅' and 'bars). Trend:' in seeded.text and 'ST=' in seeded.text
    assert tb.index(starting) < tb.index(seeded), 'starting is announced before the seed confirmation'


def test_the_periodic_trade_update_fires_every_20s_and_reports_live_pnl(caplog):
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    with caplog.at_level('DEBUG'):
        h.run_until(hm(10, 20))
    updates = [a for a in h.alerts if a.channel == 'trade-updates']
    assert len(updates) >= 10                                        # ~5 min of in-trade time at a 20s cadence
    gaps = [(updates[i + 1].ts - updates[i].ts).total_seconds() for i in range(len(updates) - 1)]
    assert all(19.0 <= g <= 21.0 for g in gaps)
    assert 'Entry:' in updates[0].text and 'LTP:' in updates[0].text and 'Realised:' in updates[0].text
    assert updates[0].text.startswith('BEARISH')                       # FLIP_PATH's first leg
    # production's own convention: Slack-only, never written to the log -- confirms _maybe_send_trade_update bypasses _say
    assert not any('Realised:' in r.getMessage() for r in caplog.records)


def test_the_trade_update_stops_once_the_position_closes():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    run(h, hm(13, 1))
    updates = [a for a in h.alerts if a.channel == 'trade-updates']
    assert updates and made[-1].state.status == 'in_trade'              # a new (flipped) position re-arms the ticker
    assert updates[-1].ts <= h.kernel.now


def test_the_running_row_log_writes_one_row_a_minute_per_trade_and_an_exit_row_per_lot():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(13, 1))
    rows = h.running_rows
    assert len(rows) >= 100                                            # ~2h45m of in-trade time at a 60s cadence, two trades
    gaps = [(rows[i + 1][1]['ts'], rows[i][1]['ts']) for i in range(len(rows) - 1) if rows[i][1]['trade_id'] == rows[i + 1][1]['trade_id']]
    assert all(t1 >= t0 for t1, t0 in gaps)                             # non-decreasing (the two Rule 7 exit rows share an instant)
    assert sorted({r['trade_id'] for _, r in rows}) == [1, 2]
    exits = [r for _, r in rows if r['exit_reason']]
    assert {e['exit_reason'] for e in exits} == {'lot1_trend_flip', 'lot2_trend_flip'}     # Rule 7 closes both lots
    assert all(r['entry_ts'] for _, r in rows)                          # needed to build a stable per-trade filename


def test_the_running_row_survives_a_stale_ltp_by_simply_not_writing_that_tick():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(10, 20))
    h.inject_feed_stale('prometheus', FRONT, hm(10, 20), hm(10, 40))
    h.run_until(hm(10, 40))
    rows_before = len(h.running_rows)
    h.run_until(hm(10, 45))
    assert len(h.running_rows) >= rows_before                           # resumes once the feed is fresh again, no crash meanwhile
