import csv
import io
import json
import re
import sys
from pathlib import Path

import pytest

HP = Path(__file__).resolve().parents[1] / 'hestia_performance'
sys.path.insert(0, str(HP))
import build_tracker as bt  # noqa: E402

HEADER = ['trade_id', 'contract_expiry', 'direction', 'units', 'entry_ts', 'entry_price', 'signal_ts', 'signal_close', 'entry_slippage_points', 'sl_price', 'lot1_target', 'lot2_target',
          'lot2_target_source', 'parent_trade_id', 'lot1_exit_ts', 'lot1_exit_price', 'lot1_exit_reason', 'lot1_pnl_points', 'lot1_pnl_rs', 'lot2_exit_ts', 'lot2_exit_price',
          'lot2_exit_reason', 'lot2_pnl_points', 'lot2_pnl_rs', 'total_pnl_points', 'total_pnl_rs']

SELENE = {'name': 'Selene', 'instrument': 'SILVERMIC', 'lots_per_unit': 1, 'rs_per_point_per_lot': 1, 'first_live_trade_id': 7, 'live_since': '2026-10-01T09:15:06', 'note': ''}
PROM = {'name': 'Prometheus', 'instrument': 'CRUDEOILM', 'lots_per_unit': 2, 'rs_per_point_per_lot': 10, 'first_live_trade_id': 64, 'live_since': '2026-10-05T20:41:21', 'note': ''}
TYPHON = {'name': 'Typhon', 'instrument': 'NATGASMINI', 'lots_per_unit': 2, 'rs_per_point_per_lot': 250, 'first_live_trade_id': 3, 'live_since': '2026-10-05T09:15:03', 'note': ''}


def csv_text(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HEADER)
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, '') for k in HEADER})
    return buf.getvalue()


def single(trade_id, direction, entry_ts, entry, exit_ts, exit_, pnl_pts, rs, units=1, reason='trend_flip', slip=0.0):
    return {'trade_id': trade_id, 'contract_expiry': '2026-11-30', 'direction': direction, 'units': units, 'entry_ts': entry_ts, 'entry_price': entry, 'signal_close': entry - slip,
            'entry_slippage_points': slip, 'lot1_exit_ts': exit_ts, 'lot1_exit_price': exit_, 'lot1_exit_reason': reason, 'lot1_pnl_points': pnl_pts, 'lot1_pnl_rs': rs,
            'total_pnl_points': pnl_pts, 'total_pnl_rs': rs}


def two_lot(trade_id, direction, entry_ts, entry, l1, l2, units=1):
    """l1, l2: (exit_ts, exit_price, reason, pts); Prometheus lot value 10 Rs per point per lot."""
    return {'trade_id': trade_id, 'contract_expiry': '2026-10-19', 'direction': direction, 'units': units, 'entry_ts': entry_ts, 'entry_price': entry, 'signal_close': entry, 'entry_slippage_points': 0.0,
            'lot1_exit_ts': l1[0], 'lot1_exit_price': l1[1], 'lot1_exit_reason': l1[2], 'lot1_pnl_points': l1[3], 'lot1_pnl_rs': l1[3] * 10,
            'lot2_exit_ts': l2[0], 'lot2_exit_price': l2[1], 'lot2_exit_reason': l2[2], 'lot2_pnl_points': l2[3], 'lot2_pnl_rs': l2[3] * 10,
            'total_pnl_points': l1[3] + l2[3], 'total_pnl_rs': (l1[3] + l2[3]) * 10}


def trade(engine, trade_id, exit_ts, rs_unit, units=1, entry_ts='2026-10-01T09:00:00', pts_=None, slip=None, hold=None):
    """A parsed-trade dict for the pure functions (stats, series, daily)."""
    from datetime import datetime
    e, x = datetime.fromisoformat(entry_ts), datetime.fromisoformat(exit_ts)
    return {'engine': engine, 'trade_id': trade_id, 'exit_ts': x, 'entry_ts': e, 'rs_unit': rs_unit, 'rs_actual': rs_unit * units, 'pts': rs_unit if pts_ is None else pts_,
            'slippage': slip, 'hold_h': (x - e).total_seconds() / 3600.0}


# ---------- parsing ----------

def test_a_single_exit_trade_is_parsed_with_its_fields():
    txt = csv_text([single(7, 'bullish', '2026-10-01T09:15:07', 227986.0, '2026-10-01T13:00:06', 226804.0, -1182.0, -1182.0, slip=-5.0)])
    t = bt.parse_trades(txt, 'selene', SELENE)[0]
    assert (t['trade_id'], t['direction'], t['units'], t['lots']) == (7, 'bullish', 1, 1)
    assert t['pts'] == -1182.0 and t['rs_unit'] == -1182.0 and t['rs_actual'] == -1182.0 and t['slippage'] == -5.0
    assert len(t['exits']) == 1 and t['reasons'] == 'trend_flip'
    assert t['hold_h'] == pytest.approx(3 + 44 / 60 + 59 / 3600, abs=1e-6)


def test_a_two_lot_trade_keeps_both_exits_and_ends_at_the_later_one():
    txt = csv_text([two_lot(66, 'bearish', '2026-10-07T20:30:01', 8686.33, ('2026-10-07T22:46:09', 8578.0, 'target1', 108.33), ('2026-10-08T09:15:01', 8690.0, 'trend_flip', -3.67))])
    t = bt.parse_trades(txt, 'prometheus', PROM)[0]
    assert [x['lot'] for x in t['exits']] == [1, 2]
    assert t['exit_ts'].isoformat() == '2026-10-08T09:15:01'
    assert t['reasons'] == 'target1/trend_flip'
    assert t['pts'] == pytest.approx(104.66) and t['rs_unit'] == pytest.approx(1046.6)
    assert t['lots'] == 2


def test_actual_rupees_are_per_unit_rupees_times_units_and_lots_follow_units():
    txt = csv_text([single(8, 'bearish', '2026-10-02T10:00:00', 100.0, '2026-10-02T12:00:00', 90.0, 10.0, 10.0, units=3)])
    t = bt.parse_trades(txt, 'selene', SELENE)[0]
    assert t['rs_unit'] == 10.0 and t['rs_actual'] == 30.0 and t['lots'] == 3


def test_a_row_with_no_exit_is_not_a_closed_trade():
    row = single(9, 'bullish', '2026-10-02T10:00:00', 100.0, '', None, None, None)
    row.update({'lot1_exit_ts': ''})
    assert bt.parse_trades(csv_text([row]), 'selene', SELENE) == []


def test_live_split_uses_the_first_live_trade_id():
    rows = [single(i, 'bullish', '2026-10-01T09:00:00', 100.0, '2026-10-01T10:00:00', 101.0, 1.0, 1.0) for i in (5, 6, 7, 8)]
    live, out = bt.split_live(bt.parse_trades(csv_text(rows), 'selene', SELENE), SELENE)
    assert [t['trade_id'] for t in live] == [7, 8] and [t['trade_id'] for t in out] == [5, 6]


# ---------- rupee check ----------

def test_rupee_check_passes_valid_trades_of_each_shape():
    p = bt.parse_trades(csv_text([two_lot(64, 'bearish', '2026-10-06T11:45:00', 8615.0, ('2026-10-06T14:07:00', 8504.0, 'target1', 111.0), ('2026-10-06T21:00:00', 8525.0, 'trend_flip', 90.0))]), 'prometheus', PROM)[0]
    ty = bt.parse_trades(csv_text([single(3, 'bullish', '2026-10-05T09:15:00', 292.5, '2026-10-06T09:15:00', 293.5, 1.0, 500.0)]), 'typhon', TYPHON)[0]
    assert bt.check_rupees(p, PROM) is None and bt.check_rupees(ty, TYPHON) is None


def test_rupee_check_flags_a_mismatch():
    ty = bt.parse_trades(csv_text([single(3, 'bullish', '2026-10-05T09:15:00', 292.5, '2026-10-06T09:15:00', 293.5, 1.0, 400.0)]), 'typhon', TYPHON)[0]
    msg = bt.check_rupees(ty, TYPHON)
    assert msg and 'typhon #3' in msg and '400.0' in msg and '500.0' in msg


# ---------- stats ----------

def test_stats_on_a_known_sequence():
    ts = ['2026-10-01T10:00:00', '2026-10-01T11:00:00', '2026-10-01T12:00:00', '2026-10-01T13:00:00', '2026-10-01T14:00:00']
    trades = [trade('selene', i, t, v) for i, (t, v) in enumerate(zip(ts, [100, -50, -30, 20, -10]), 1)]
    s = bt.stats(trades, 'rs_unit')
    assert s['trades'] == 5 and s['wins'] == 2 and s['losses'] == 3 and s['win_rate'] == pytest.approx(0.4)
    assert s['total'] == 30 and s['best'] == 100 and s['worst'] == -50
    assert s['avg_win'] == 60 and s['avg_loss'] == pytest.approx(-30)
    assert s['profit_factor'] == pytest.approx(120 / 90)
    assert s['max_drawdown'] == 80            # cumulative 100, 50, 20, 40, 30: peak 100 to trough 20
    assert s['longest_losing'] == 2 and s['streak'] == {'kind': 'loss', 'count': 1}
    assert s['expectancy'] == pytest.approx(6.0)


def test_drawdown_counts_from_a_starting_value_of_zero():
    trades = [trade('selene', 1, '2026-10-01T10:00:00', -40), trade('selene', 2, '2026-10-01T11:00:00', 10)]
    assert bt.stats(trades, 'rs_unit')['max_drawdown'] == 40


def test_stats_are_in_exit_order_not_input_order():
    a = trade('selene', 1, '2026-10-02T10:00:00', -100)
    b = trade('selene', 2, '2026-10-01T10:00:00', 50)
    assert bt.stats([a, b], 'rs_unit')['max_drawdown'] == 100        # +50 then -100: peak 50, trough -50
    assert bt.stats([b, a], 'rs_unit')['max_drawdown'] == 100


def test_empty_stats_are_zero_not_an_error():
    s = bt.stats([], 'rs_unit')
    assert s['trades'] == 0 and s['total'] == 0.0 and s['win_rate'] is None and s['max_drawdown'] == 0.0


def test_a_run_of_wins_and_profit_factor_without_losses():
    trades = [trade('prometheus', i, f'2026-10-0{i}T10:00:00', 100) for i in (1, 2, 3)]
    s = bt.stats(trades, 'rs_unit')
    assert s['profit_factor'] is None and s['streak'] == {'kind': 'win', 'count': 3} and s['max_drawdown'] == 0


def test_actual_stats_use_the_units_each_trade_ran_at():
    trades = [trade('selene', 1, '2026-10-01T10:00:00', 100, units=2), trade('selene', 2, '2026-10-01T11:00:00', -50, units=1)]
    assert bt.stats(trades, 'rs_unit')['total'] == 50 and bt.stats(trades, 'rs_actual')['total'] == 150


# ---------- series, daily ----------

def test_series_is_cumulative_in_exit_order():
    t = [trade('selene', 2, '2026-10-02T10:00:00', -30), trade('prometheus', 1, '2026-10-01T10:00:00', 100)]
    out = bt.series(t)
    assert [p['trade_id'] for p in out] == [1, 2] and [p['cum_unit'] for p in out] == [100, 70]


def test_daily_groups_by_the_date_of_the_final_exit():
    t = [trade('prometheus', 1, '2026-10-02T09:15:00', 50, entry_ts='2026-10-01T20:00:00'), trade('selene', 2, '2026-10-02T22:00:00', 20), trade('selene', 3, '2026-10-01T22:00:00', -5)]
    d = bt.daily(t)
    assert [x['date'] for x in d] == ['2026-10-01', '2026-10-02']
    assert d[1]['rs_unit'] == 70 and d[1]['trades'] == 2 and d[0]['rs_unit'] == -5


# ---------- open positions ----------

def state_single(direction, entry, lots=1, units=1, **kw):
    s = {'status': 'in_trade', 'direction': direction, 'units': units, 'entry_price': entry, 'entry_ts': '2026-10-08T11:30:05', 'sl_price': entry - 100, 'lots': lots * units, 'trade_counter': 17}
    s.update(kw)
    return s


def test_open_bullish_and_bearish_unrealised_signs():
    up = bt.open_position('selene', SELENE, state_single('bullish', 1000.0), {'ts': 'x', 'close': 1030.0})
    dn = bt.open_position('selene', SELENE, state_single('bearish', 1000.0), {'ts': 'x', 'close': 1030.0})
    assert up['unreal_pts'] == 30 and up['unreal_unit'] == 30
    assert dn['unreal_pts'] == -30 and dn['unreal_unit'] == -30


def test_open_value_uses_lot_value_and_units():
    pos = bt.open_position('typhon', TYPHON, state_single('bullish', 292.5, lots=2, units=3, target_price=336.375), {'ts': 'x', 'close': 317.1})
    assert pos['unreal_pts'] == pytest.approx(24.6)
    assert pos['unreal_unit'] == pytest.approx(24.6 * 250 * 2) and pos['unreal_actual'] == pytest.approx(24.6 * 250 * 2 * 3)
    assert pos['targets'] == [{'label': 'Target', 'price': 336.375, 'hit': False}]


def test_a_two_lot_position_with_lot_1_booked_values_only_the_open_lot_and_reports_the_booked_part():
    st = {'status': 'in_trade', 'direction': 'bullish', 'units': 1, 'entry_price': 8690.0, 'entry_ts': '2026-10-08T09:15:01', 'sl_price': 8603.1, 'trade_counter': 67,
          'lot1_target': 8798.625, 'lot1_lots': 1, 'lot1_status': 'booked', 'lot1_exit_price': 8799.0, 'lot2_target': 9037.6, 'lot2_lots': 1, 'lot2_status': 'open',
          'trade_row': {'lot1_pnl_points': 109.0, 'lot1_pnl_rs': 1090.0}}
    pos = bt.open_position('prometheus', PROM, st, {'ts': 'x', 'close': 8839.0})
    assert pos['unreal_pts'] == 149.0 and pos['unreal_unit'] == 1490.0          # one lot of two still open, 10 Rs per point
    assert pos['booked_unit'] == 1090.0 and pos['open_lots_per_unit'] == 1
    assert [t['hit'] for t in pos['targets']] == [True, False]


def test_flat_engine_has_no_open_position_and_a_missing_price_leaves_value_empty():
    assert bt.open_position('selene', SELENE, {'status': 'watching'}, None) is None
    assert bt.open_position('selene', SELENE, None, None) is None
    pos = bt.open_position('selene', SELENE, state_single('bullish', 1000.0), None)
    assert pos['unreal_pts'] is None and pos['unreal_unit'] is None and pos['unreal_actual'] is None


# ---------- dataset and page ----------

def snapshot():
    sel_rows = [single(i, 'bullish', '2026-10-01T09:00:00', 100.0, f'2026-10-0{i - 4}T10:00:00', 101.0, 10.0 * (-1) ** i, 10.0 * (-1) ** i) for i in (5, 6, 7, 8)]
    prom_rows = [two_lot(63, 'bearish', '2026-10-05T18:45:00', 8630.75, ('2026-10-05T20:37:17', 8770.5, 'manual_broker_exit', -139.75), ('2026-10-05T20:37:17', 8770.5, 'manual_broker_exit', -139.75)),
                 two_lot(64, 'bearish', '2026-10-06T11:45:00', 8615.0, ('2026-10-06T14:07:00', 8504.0, 'target1', 111.0), ('2026-10-06T21:00:00', 8525.0, 'trend_flip', 90.0))]
    return {'pulled_at': '2026-10-08T12:34:52', 'ledger': None, 'engines': {
        'prometheus': {'trades_csv': csv_text(prom_rows), 'state': {'status': 'watching'}, 'last_price': None},
        'selene': {'trades_csv': csv_text(sel_rows), 'state': state_single('bullish', 1000.0), 'last_price': {'ts': '2026-10-08T12:33:00+05:30', 'close': 1010.0}},
        'helios': {'trades_csv': csv_text([]), 'state': None, 'last_price': None},
        'typhon': {'trades_csv': None, 'state': None, 'last_price': None}}}


CONFIG = {'engines': {'prometheus': PROM, 'selene': SELENE, 'helios': dict(SELENE, name='Helios', instrument='GOLDPETAL', lots_per_unit=20, first_live_trade_id=6), 'typhon': TYPHON}}


def test_dataset_leaves_out_paper_and_old_trades_and_adds_up():
    d = bt.build_dataset(snapshot(), CONFIG)
    assert d['excluded'] == {'prometheus': 1, 'selene': 2, 'helios': 0, 'typhon': 0}
    assert [t['trade_id'] for t in d['trades'] if t['engine'] == 'prometheus'] == [64]
    assert d['stats']['prometheus']['actual']['total'] == pytest.approx(2010.0)
    assert d['stats']['portfolio']['actual']['trades'] == 3 and d['warnings'] == []
    assert d['stats']['portfolio']['actual']['total'] == pytest.approx(2010.0 + sum(t['rs_actual'] for t in d['trades'] if t['engine'] == 'selene'))
    assert len(d['open']) == 1 and d['open'][0]['engine'] == 'selene' and d['open'][0]['unreal_unit'] == 10.0
    assert d['series']['portfolio'][-1]['cum_actual'] == d['stats']['portfolio']['actual']['total']
    assert [t['exit_ts'] for t in d['trades']] == sorted((t['exit_ts'] for t in d['trades']), reverse=True)     # newest first


def test_dataset_is_json_serialisable_with_no_datetime_objects():
    json.dumps(bt.build_dataset(snapshot(), CONFIG))


def test_render_embeds_the_dataset_and_cannot_be_broken_out_of_by_data():
    ds = {'x': '</script><script>alert(1)</script>', 'n': 1}
    page = bt.render_html(ds, '<script>const DATA = __DATA__;</script>')
    assert '__DATA__' not in page and '</script><script>' not in page
    blob = re.search(r'const DATA = (.*);</script>', page).group(1)
    assert json.loads(blob.replace('<\\/', '</')) == ds


def test_render_refuses_a_template_with_no_placeholder():
    with pytest.raises(ValueError):
        bt.render_html({}, '<script>const DATA = {};</script>')


def test_the_real_template_renders_with_the_real_config_and_a_synthetic_snapshot():
    cfg = bt.load_config()
    tpl = (HP / 'tracker_template.html').read_text()
    assert tpl.count('__DATA__') == 1
    snap = snapshot()
    snap['engines']['helios'] = {'trades_csv': None, 'state': None, 'last_price': None}
    page = bt.render_html(bt.build_dataset(snap, cfg), tpl)
    assert '__DATA__' not in page and '<title>Hestia Dashboard</title>' in page


def test_config_pins_the_live_starts_and_lot_values():
    cfg = bt.load_config()['engines']
    assert {e: c['first_live_trade_id'] for e, c in cfg.items()} == {'prometheus': 64, 'selene': 7, 'helios': 6, 'typhon': 3}
    assert {e: c['lots_per_unit'] for e, c in cfg.items()} == {'prometheus': 2, 'selene': 1, 'helios': 20, 'typhon': 2}
    assert {e: c['rs_per_point_per_lot'] for e, c in cfg.items()} == {'prometheus': 10, 'selene': 1, 'helios': 1, 'typhon': 250}
