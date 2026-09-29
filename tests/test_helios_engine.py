"""The Helios engine on the fake Hestia: levels and config parity with the backtest, then entry, stop, Rule 7 flip, failure
handling, restart, the operator's EXIT, ledger-wins reconciliation, and the post-close guard. Rolling is in
test_helios_engine_roll.py. lots_per_unit scaling (1 unit = 20 lots) is in test_helios_engine_lots_per_unit.py -- CFG here
overrides lots_per_unit to 1 so these tests port Selene's own numeric assertions unchanged."""
import ast
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from helios_engine_helpers import CFG, FLIP_PATH, FRONT, SESSION_DATE, SESSION_OPEN, ZIGZAG, alert_texts, hm, scripted_world, trades
from hestia_core.replay import BrokerReply
from hestia_core.interface import CommandKind
from helios_engine.engine_configs import DEFAULT
from helios_engine.levels import build_levels, lot_pnl_points, margin_per_unit, stop_distance

BACKTEST = Path(__file__).resolve().parents[1] / 'helios_backtest'


# ---------------------------------------------------------------------------------------------------------------------
# Parity with the backtest's decided config
# ---------------------------------------------------------------------------------------------------------------------

def _backtest_constants():
    tree = ast.parse((BACKTEST / 'helios_configs.py').read_text())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                pass
    return out


def test_engine_config_matches_the_decided_backtest_config():
    c, p = DEFAULT, _backtest_constants()
    assert c.st_period == p['ST_PERIOD']
    assert c.st_multiplier == p['DECIDED_MULTIPLIER']
    assert c.sl_pct == p['DECIDED_SL_PCT']
    assert c.lots_per_unit == p['UNIT_LOTS']
    assert c.min_entry_buffer_min == p['MIN_ENTRY_BUFFER_MIN']
    assert c.no_exit_before_buffer_min == p['NO_EXIT_BEFORE_BUFFER_MIN']
    assert c.roll_window_days == p['TENDER_ROLL_TRADING_DAYS']
    assert c.basis_tolerance_min == p['BASIS_MAX_GAP_MIN']
    assert c.rollover_buffer_min == p['ROLLOVER_BUFFER_MIN']
    assert c.margin_contract_value_divisor == p['MARGIN_CONTRACT_VALUE_DIVISOR']
    # MARGIN_SIZING_MULTIPLIER is written as a division expression (1380 / 14893) in helios_configs.py, not a bare
    # literal, so ast.literal_eval can't recover it from the AST (it only handles Constant nodes) -- computed directly
    # here instead, matching that file's own source exactly (plan §2/§4g's single-point margin derivation).
    assert c.margin_sizing_multiplier == pytest.approx(1380 / 14893)
    assert c.instrument == p['SYMBOL']


@pytest.mark.parametrize('price', [61.0, 15423.5, 258811.25])
@pytest.mark.parametrize('direction', ['bullish', 'bearish'])
def test_levels_match_the_backtests_own_stop_arithmetic(price, direction):
    """helios_backtest/parity_backtest_helios.py: sl = ref * (1 - sign * sl_frac)."""
    sign = 1 if direction == 'bullish' else -1
    want = price * (1 - sign * (DEFAULT.sl_pct / 100))
    lv = build_levels(direction, price, DEFAULT)
    assert lv.sl_price == pytest.approx(want)
    assert stop_distance(price, DEFAULT) == pytest.approx(price * DEFAULT.sl_pct / 100)


def test_pnl_and_margin_arithmetic():
    assert lot_pnl_points('bullish', 100, 103) == 3 and lot_pnl_points('bearish', 100, 103) == -3
    per_lot = 6000.0 * 1 * DEFAULT.margin_sizing_multiplier / DEFAULT.margin_contract_value_divisor
    assert margin_per_unit(6000.0, 1, DEFAULT) == pytest.approx(per_lot * DEFAULT.lots_per_unit)
    assert margin_per_unit(None, 1, DEFAULT) == DEFAULT.fallback_margin_per_unit


def test_margin_per_unit_is_20x_margin_per_lot():
    """The thing Helios's own margin formula adds over Selene's/Prometheus's shape (engine_configs.py's own docstring):
    lots_per_unit is an explicit factor, not folded into the divisor/multiplier."""
    from dataclasses import replace
    one_lot_cfg = replace(DEFAULT, lots_per_unit=1)
    assert margin_per_unit(6000.0, 1, DEFAULT) == pytest.approx(margin_per_unit(6000.0, 1, one_lot_cfg) * 20)


# ---------------------------------------------------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------------------------------------------------

def run(h, until):
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(until)


def ledger(h):
    return h._ledger.get(('helios', FRONT.token), [0])[0]


def test_trend_flip_exits_book_the_trade_and_the_engine_rearms():
    made = []
    h = scripted_world(ZIGZAG, made=made)
    run(h, hm(23, 40))
    rows = trades(h)
    assert [r['direction'] for r in rows] == ['bearish', 'bullish']              # the third leg (bearish again) is still open at 23:29
    for r in rows:
        assert r['lot1_exit_reason'] == 'trend_flip' and r['total_pnl_points'] == r['lot1_pnl_points']
        assert r['lot1_target'] is None and r['lot2_target'] is None and r['lot2_exit_reason'] is None
    assert [(o.side, o.lots) for o in h.orders] == [('SELL', 1), ('BUY', 2), ('SELL', 2)]
    assert ledger(h) == -1
    s = made[-1].state
    assert s.status == 'in_trade' and s.direction == 'bearish' and s.trade_counter == 3
    assert h.orders[0].request_id == 'helios-20260903-t1-entry-1'


def test_entry_levels_come_off_the_real_fill():
    made = []
    h = scripted_world(ZIGZAG, made=made)
    run(h, hm(10, 30))
    s = made[-1].state
    assert s.status == 'in_trade' and s.direction == 'bearish' and s.lots == 1
    assert s.sl_price == pytest.approx(s.entry_price * (1 + DEFAULT.sl_pct / 100))
    assert ledger(h) == -1


def test_rule7_flip_is_one_netted_request_and_books_the_old_trade():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    run(h, hm(13, 1))
    assert [(o.side, o.lots) for o in h.orders[:2]] == [('SELL', 1), ('BUY', 2)]
    assert h.orders[1].request_id.endswith('-flip-1')
    old = trades(h)[0]
    assert old['lot1_exit_reason'] == 'trend_flip'
    s = made[-1].state
    assert s.status == 'in_trade' and s.direction == 'bullish' and s.trade_counter == 2 and s.pending_flip is None
    assert ledger(h) == 1 and len(trades(h)) == 1


def test_stop_loss_flattens_and_books_the_trade():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(10, 30))
    sl = made[-1].state.sl_price
    h.schedule_price(FRONT.token, hm(10, 40), sl + 0.3)
    h.run_until(hm(10, 45))
    row = trades(h)[0]
    assert row['lot1_exit_reason'] == 'stop_loss' and row['total_pnl_points'] < 0
    assert h.orders[1].request_id.endswith('-exit_all-1') and (h.orders[1].side, h.orders[1].lots) == ('BUY', 1)
    assert ledger(h) == 0 and made[-1].state.status == 'watching'


def test_a_stale_price_never_triggers_a_stop():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(10, 30))
    sl = made[-1].state.sl_price
    h.schedule_price(FRONT.token, hm(10, 40), sl + 1.0)
    h.inject_feed_stale('helios', FRONT, hm(10, 35), hm(10, 50))
    h.run_until(hm(10, 48))
    assert len(h.orders) == 1 and made[-1].state.status == 'in_trade'


def test_partial_entry_fill_is_accepted_without_a_top_up():
    made = []
    h = scripted_world(FLIP_PATH, made=made, broker=lambda c: BrokerReply('partial', lots=0) if c.request.request_id.endswith('entry-1')
                       else BrokerReply('fill'))
    run(h, hm(10, 40))
    assert made[-1].state.status == 'watching'                            # zero filled: treated as no fill at all
    assert any('did not fill' in t for t in alert_texts(h))


def test_failed_entry_leaves_the_engine_flat_and_still_watching():
    made = []
    h = scripted_world(FLIP_PATH, made=made, broker=lambda c: BrokerReply('reject'))
    run(h, hm(10, 40))
    assert made[-1].state.status == 'watching' and made[-1].state.trade_counter == 0 and not made[-1].state.pending


def test_failed_exit_is_re_sent_until_it_lands():
    made = []

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


def test_exit_command_liquidates_and_rearms():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(10, 30))
    h.send_command('helios', CommandKind.EXIT)
    h.run_until(hm(10, 32))
    row = trades(h)[0]
    assert row['lot1_exit_reason'] == 'slack_exit'
    assert made[-1].state.status == 'watching' and ledger(h) == 0 and not made[-1].exit_requested


def test_exit_command_with_no_position_is_a_no_op():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(9, 30))
    h.send_command('helios', CommandKind.EXIT)
    h.run_until(hm(9, 31))
    assert not h.orders and any('no open position' in t for t in alert_texts(h))


def test_margin_refusal_skips_the_entry():
    from hestia_core.fake import FakeConfig
    made = []
    mpu = 100 * 1 * DEFAULT.margin_sizing_multiplier / DEFAULT.margin_contract_value_divisor  # lots_per_unit=1 in CFG
    h = scripted_world(FLIP_PATH, made=made, config=FakeConfig(available_cash=mpu / 2))
    run(h, hm(10, 40))
    assert not h.orders and made[-1].state.status == 'watching'
    assert any('insufficient margin' in t for t in alert_texts(h))


def test_the_bar_at_the_close_is_never_traded_and_a_post_close_stop_is_not_retried():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(23, 20))
    e = made[-1]
    n_before = len(h.orders)
    h.schedule_price(FRONT.token, hm(23, 29) + timedelta(seconds=30), e.state.sl_price - 0.5)
    h.schedule_price(FRONT.token, hm(23, 31), e.state.sl_price - 1.0)
    h.run_until(hm(23, 45))
    assert len(h.orders) == n_before
    ids = [k[1] for k in h._registry if 'exit_all' in k[1]]
    assert len(ids) == 1 and any('market is closed' in t for t in alert_texts(h))
    assert e.state.status == 'in_trade'


# ---------------------------------------------------------------------------------------------------------------------
# Restart and reconciliation
# ---------------------------------------------------------------------------------------------------------------------

def test_engine_crash_and_resume_does_not_duplicate_the_position():
    from helios_engine.engine import HeliosEngine
    crashed = {'n': 0}
    made = []

    class Crashy(HeliosEngine):
        def _tick(self):
            if self.state.status == 'in_trade' and not crashed['n'] and self._now() >= hm(10, 30):
                crashed['n'] += 1
                raise RuntimeError('boom')
            super()._tick()

    from helios_engine_helpers import ContractSpec, scripted_minutes, world
    h = world(engines=[('helios', lambda: (made.append(Crashy(CFG)) or made[-1]), {'lots_per_unit': 1})], contracts=(),
              extra=[ContractSpec(FRONT, lot_size=1, tick_size=1.0, freeze_qty_lots=10000, minutes=scripted_minutes(FLIP_PATH))])
    run(h, hm(11, 0))
    assert crashed['n'] == 1 and len(made) >= 2
    resumed = made[-1].state
    assert resumed.status == 'in_trade' and resumed.direction == 'bearish' and resumed.trade_counter == 1
    assert sum(1 for o in h.orders if o.request_id.endswith('entry-1')) == 1
    assert ledger(h) == -1


def test_ledger_flat_but_state_in_trade_returns_to_watching():
    from helios_engine.state import EngineState
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    st = EngineState(status='in_trade', direction='bearish', units=1, entry_price=99.0, entry_ts='2026-09-03T09:30:00',
                     contract_token=FRONT.token, contract_symbol=FRONT.symbol, contract_expiry=FRONT.expiry.isoformat(),
                     sl_price=101.0, lots=1, trade_counter=4)
    h._saved_state['helios'] = st.to_json()
    run(h, hm(9, 30))
    assert made[-1].state.status == 'watching' and made[-1].state.trade_counter == 4
    assert any('ledger is flat' in t for t in alert_texts(h)) and not trades(h)


def test_ledger_holds_a_position_the_engine_never_knew_about():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.seed_position('helios', FRONT, -1, 99.0)
    run(h, hm(9, 30))
    s = made[-1].state
    assert s.status == 'in_trade' and s.direction == 'bearish' and s.entry_price == pytest.approx(99.0)
    assert s.sl_price == pytest.approx(99.0 * (1 + DEFAULT.sl_pct / 100)) and s.lots == 1
    assert any('ledger holds' in t for t in alert_texts(h))


def test_first_minutes_are_guarded():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(9, 0))
    e = made[-1]
    assert not e._past_first_minute_guard(hm(9, 0)) and e._past_first_minute_guard(hm(9, 1))
    assert not e._past_min_entry_guard(hm(9, 14)) and e._past_min_entry_guard(hm(9, 15))


def test_every_flip_sends_the_raw_signal_alert_independent_of_the_outcome():
    made = []
    h = scripted_world(ZIGZAG, made=made)
    run(h, hm(23, 40))
    flips = [t for t in alert_texts(h) if t.startswith('ST_15 flip -> ')]
    assert len(flips) >= 3
    assert 'close=' in flips[0] and 'ST=' in flips[0]
    hits = [a for a in h.alerts if a.text.startswith('ST_15 flip -> ') and a.channel == 'tradebot-updates']
    assert len(hits) == len(flips)
    assert flips[0].startswith('ST_15 flip -> bearish at')


def test_session_start_announces_the_contract_and_the_seeded_st_with_the_standalones_own_emoji():
    """Ported back from the standalone Prometheus process 2026-09-29 -- found missing entirely (no engine said anything on
    a clean start before this). Checks both the content and that the per-event emoji override (not Hestia's own
    severity-based default, which is empty for 'info') actually reaches the Alert."""
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
    assert len(updates) >= 10
    gaps = [(updates[i + 1].ts - updates[i].ts).total_seconds() for i in range(len(updates) - 1)]
    assert all(19.0 <= g <= 21.0 for g in gaps)
    assert 'Entry:' in updates[0].text and 'LTP:' in updates[0].text and 'Realised: +0.00 pts' in updates[0].text
    assert updates[0].text.startswith('BEARISH')
    assert not any('Realised:' in r.getMessage() for r in caplog.records)


def test_the_running_row_log_writes_one_row_a_minute_per_trade_and_an_exit_row_at_close():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(13, 1))
    rows = h.running_rows
    assert len(rows) >= 100
    assert sorted({r['trade_id'] for _, r in rows}) == [1, 2]
    exits = [r for _, r in rows if r['exit_reason']]
    assert {e['exit_reason'] for e in exits} == {'trend_flip'} and len(exits) == 1     # a single lot-group, one exit row
    assert all(r['entry_ts'] for _, r in rows)
    assert all(r['lot2_target'] is None and r['lot2_pnl_points'] is None for _, r in rows)  # Helios has no lot2, same as Selene
