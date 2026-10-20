import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'janus_backtest'))
import janus_configs as configs   # noqa: E402
import janus_engine as eng        # noqa: E402
import janus_machine as mach      # noqa: E402
import machine_run                # noqa: E402
from janus_levels import camarilla_levels  # noqa: E402

# Previous H=110 L=100 C=105: S4=99.5 S3=102.25 S2=103.1667 S1=104.0833 | R1=105.9167 R2=106.8333 R3=107.75 R4=110.5 R5=115.5 R6=121.34 R7=128.16
LV = camarilla_levels(110.0, 100.0, 105.0)
OFF = {'rebuy': False, 'counter': False, 'tsl': False}
TSL = {'rebuy': False, 'counter': False, 'tsl': True}
TSL_NO_RE = {'rebuy': False, 'counter': False, 'tsl': True, 'tsl_reentry': False}
REBUY = {'rebuy': True, 'counter': False, 'tsl': False}


def session(bars, pad=100):
    last = bars[-1][3]
    rows = list(bars) + [(last, last, last, last)] * pad
    idx = pd.date_range('2026-01-05 09:00', periods=len(rows), freq='min')
    df = pd.DataFrame(rows, columns=['open', 'high', 'low', 'close'], index=idx)
    df['volume'] = 1
    return {'symbol': 'CRUDEOILM', 'date': date(2026, 1, 5), 'expiry': date(2026, 1, 20), 'bars': df, 'prev': {}, 'levels': LV}


def run(bars, opts=OFF, order='neutral', pad=100):
    return mach.simulate_session(session(bars, pad), opts, order)


def trade_of(res, setup):
    return next(t for t in res['trades'] if t['setup'] == setup)


# bar 0 flat below S3; bar 1 rises through S3 (entry fill 102.25) and closes just above it
ENTER_FADE_LONG = [(101, 101.2, 100.8, 101), (101, 102.4, 100.9, 102.3)]


# ---------------------------------------------------------------- parity with the engine (the control)

def _random_session(seed):
    rs = np.random.RandomState(seed)
    n = 700
    px, rows = 105.0, []
    for _ in range(n):
        o = px + (rs.normal(0, 0.25) if rs.rand() < 0.1 else 0.0)
        c = o + rs.normal(0, 0.25)
        h = max(o, c) + abs(rs.normal(0, 0.12))
        l = min(o, c) - abs(rs.normal(0, 0.12))
        rows.append((o, h, l, c))
        px = c
    s = session(rows, pad=0)
    return s


def _sig(legs):
    return sorted((x['setup'], x['trade'], x['lot'], x['entry_bar'], round(x['entry'], 6), x['exit_bar'], round(x['exit'], 6), x['reason']) for x in legs)


@pytest.mark.parametrize('arm', [{'rebuy': False, 'counter': False}, {'rebuy': True, 'counter': False}, {'rebuy': False, 'counter': True},
                                 {'rebuy': True, 'counter': True}])
def test_machine_reproduces_the_engine_leg_for_leg_with_tsl_off(arm):
    total = 0
    for seed in range(30):
        s = _random_session(seed)
        for order in configs.BAR_ORDERS:
            ref = eng.simulate_session(s, arm, order, scn='touch')['legs']
            got = mach.simulate_session(s, {**arm, 'tsl': False}, order)['legs']
            assert _sig(got) == _sig(ref), (seed, order)
            total += len(ref)
    assert total > 300            # the random sessions really trade


# ---------------------------------------------------------------- base behaviour

def test_plain_thirds_full_ladder_and_trade_summary():
    r = run(ENTER_FADE_LONG + [(102.3, 108, 102.3, 107.9)])
    t = trade_of(r, 'F_L')
    assert t['reasons'] == 'T1,T2,T3'
    assert t['entry'] == pytest.approx(LV['S3']) and t['stop_initial'] == pytest.approx(LV['S4']) and t['stop_moves'] == 0
    assert t['pnl_r'] == pytest.approx(((LV['R1'] - LV['S3']) + (LV['R2'] - LV['S3']) + (LV['R3'] - LV['S3'])) / (LV['S3'] - LV['S4']))
    assert [x['exit'] for x in r['legs'] if x['setup'] == 'F_L'] == pytest.approx([LV['R1'], LV['R2'], LV['R3']])


def test_stop_takes_all_lots_and_reverses_into_the_breakout_short():
    r = run(ENTER_FADE_LONG + [(102.3, 102.4, 99.4, 99.6)])
    assert trade_of(r, 'F_L')['reasons'] == 'stop,stop,stop'
    assert any(t['setup'] == 'B_S' and t['entry'] == pytest.approx(LV['S4']) for t in r['trades'])


# ---------------------------------------------------------------- excursions

def test_entry_bar_counts_only_the_path_after_the_fill():
    r = run(ENTER_FADE_LONG + [(102.3, 102.3, 102.3, 102.3)])
    t = trade_of(r, 'F_L')
    assert t['mae_pts'] == pytest.approx(0.0)                     # the bar's low (100.9) came BEFORE the fill on the way up
    assert t['mfe_pts'] == pytest.approx(102.4 - 102.25)
    assert t['mfe_ts'] == pd.Timestamp('2026-01-05 09:01')


def test_excursion_stops_at_the_exit_not_at_the_bar_extreme():
    r = run(ENTER_FADE_LONG + [(102.3, 102.4, 99.4, 99.6)])      # the stop (99.5) is hit; the bar low 99.4 comes after the exit
    t = trade_of(r, 'F_L')
    assert t['mae_pts'] == pytest.approx(102.25 - 99.5)
    assert t['mae_ts'] == pd.Timestamp('2026-01-05 09:02')
    assert all(x['mae_pts'] == pytest.approx(102.25 - 99.5) for x in r['legs'] if x['setup'] == 'F_L')


def test_a_gap_counts_for_a_position_open_across_it():
    r = run(ENTER_FADE_LONG + [(101.5, 101.5, 101.5, 101.5), (101.5, 106, 101.5, 105.9)])     # a flat bar below the entry, above the stop: the gap is all there is
    t = trade_of(r, 'F_L')
    assert t['mae_pts'] == pytest.approx(102.25 - 101.5)
    assert t['mae_ts'] == pd.Timestamp('2026-01-05 09:02')


# a short: bar 0 flat above R3 (107.75), bar 1 falls through R3 (fill 107.75) after its high
ENTER_FADE_SHORT = [(109, 109.2, 108.8, 109), (109, 109.1, 107.6, 107.7)]


def test_short_excursions_and_unrealised_have_the_right_sign():
    r = run(ENTER_FADE_SHORT + [(107.7, 108.2, 107.7, 108.0)])
    t = trade_of(r, 'F_S')
    assert t['side'] == -1 and t['entry'] == pytest.approx(LV['R3'])
    assert t['mfe_pts'] == pytest.approx(0.15) and t['mae_pts'] == pytest.approx(108.2 - 107.75)
    assert t['mfe_ts'] == pd.Timestamp('2026-01-05 09:01') and t['mae_ts'] == pd.Timestamp('2026-01-05 09:02')
    rows = pd.DataFrame(r['minute_logs'][t['trade_key']])
    assert rows['unrealized_pts'].iloc[1] == pytest.approx(3 * (107.75 - 108.0))


def test_duplicate_trade_keys_get_a_suffix():
    sim = {'date': date(2026, 1, 5), 'ts': session(ENTER_FADE_LONG)['bars'].index.values, 'keys': set()}
    assert mach._unique_key(sim, 'F_L', 1) == '2026-01-05_F_L_0901'
    assert mach._unique_key(sim, 'F_L', 1) == '2026-01-05_F_L_0901_2'


def test_a_trade_opened_in_the_gap_does_not_count_the_gap():
    r = run([(101, 101.2, 100.8, 101), (102.6, 102.7, 102.5, 102.6)])      # gaps up through S3: filled at the open, 102.6
    t = trade_of(r, 'F_L')
    assert t['entry'] == pytest.approx(102.6)
    assert t['mfe_pts'] == pytest.approx(0.1) and t['mae_pts'] == pytest.approx(0.1)


def test_lot_extremes_are_the_lots_own():
    r = run(ENTER_FADE_LONG + [(102.3, 106, 102.3, 105.9), (105.9, 105.95, 100, 100.5)])    # T1 at R1 (lot 1 books), then a fall; stop S4 is below 100
    legs = {x['lot']: x for x in r['legs'] if x['setup'] == 'F_L'}
    assert legs[1]['reason'] == 'T1' and legs[1]['mfe_pts'] == pytest.approx(LV['R1'] - 102.25)
    assert legs[2]['mfe_pts'] >= legs[1]['mfe_pts'] and legs[2]['mae_pts'] > legs[1]['mae_pts']


# ---------------------------------------------------------------- trailing stop toggle

TO_T1_THEN_BACK = ENTER_FADE_LONG + [(102.3, 106, 102.3, 105.9), (105.9, 105.95, 102.0, 102.1)]      # T1 hit, then back through the entry


def test_tsl_off_keeps_the_original_stop():
    r = run(TO_T1_THEN_BACK)
    t = trade_of(r, 'F_L')
    assert t['stop_moves'] == 0 and t['stop_final'] == pytest.approx(LV['S4'])
    assert 'stop' not in t['reasons']                             # price never reached S4


def test_tsl_on_moves_the_stop_to_the_entry_after_t1_and_closes_there():
    r = run(TO_T1_THEN_BACK, TSL)
    t = trade_of(r, 'F_L')
    assert t['stop_moves'] == 1 and t['stop_final'] == pytest.approx(LV['S3'])
    assert t['reasons'] == 'T1,stop_trailed,stop_trailed'
    assert [x['exit'] for x in r['legs'] if x['reason'] == 'stop_trailed'] == pytest.approx([LV['S3'], LV['S3']])
    assert any(e['event'] == 'stop_moved' for e in r['events'])


def test_a_trailed_stop_does_not_reverse_into_the_sister_setup():
    r = run(TO_T1_THEN_BACK + [(102.1, 102.1, 99.0, 99.2)], TSL)    # after the trailed stop at S3 price runs on down through S4
    assert [t['setup'] for t in r['trades'] if t['setup'] != 'F_L'] == ['B_S']      # B_S enters at S4 on its own, not because a stop was hit there
    assert trade_of(r, 'B_S')['entry'] == pytest.approx(LV['S4'])
    assert trade_of(r, 'F_L')['stop_final'] == pytest.approx(LV['S3'])


def test_tsl_moves_to_t1_after_t2_and_never_backwards(monkeypatch):
    bars = ENTER_FADE_LONG + [(102.3, 107, 102.3, 106.9), (106.9, 106.95, 105.5, 105.6)]       # T1 and T2 hit, then back below T1
    r = run(bars, TSL)
    t = trade_of(r, 'F_L')
    assert t['stop_moves'] == 2 and t['stop_final'] == pytest.approx(LV['R1'])
    assert t['reasons'] == 'T1,T2,stop_trailed'
    monkeypatch.setattr(configs, 'TSL_RULES', {1: 'T1', 2: 'entry'})                          # a rule that would move it backwards is ignored
    t2 = trade_of(run(bars, TSL), 'F_L')
    assert t2['stop_final'] == pytest.approx(LV['R1'])


def test_tsl_rule_none_leaves_the_stop(monkeypatch):
    monkeypatch.setattr(configs, 'TSL_RULES', {1: None, 2: None})
    t = trade_of(run(TO_T1_THEN_BACK, TSL), 'F_L')
    assert t['stop_moves'] == 0


# ---------------------------------------------------------------- re-buy toggle

def test_rebuy_off_by_default_and_on_when_asked():
    bars = ENTER_FADE_LONG + [(102.3, 106, 102.3, 105.9), (105.9, 105.95, 102.0, 105.0)]       # T1 booked, price returns to the entry, then rises
    off = run(bars)
    on = run(bars, REBUY)
    assert not any(x['rebuy'] for x in off['legs'])
    assert any(x['rebuy'] for x in on['legs']) or any(e['event'] == 'rebuy' for e in on['events'])
    assert any(e['event'] == 'rebuy' for e in on['events']) and not any(e['event'] == 'rebuy' for e in off['events'])


def test_a_trailed_stop_at_the_entry_beats_the_rebuy_at_the_same_price():
    r = run(TO_T1_THEN_BACK, {'rebuy': True, 'counter': False, 'tsl': True})
    assert not any(e['event'] == 'rebuy' for e in r['events'])


# ---------------------------------------------------------------- the minute log and the event log

def test_minute_log_covers_every_minute_from_entry_to_exit():
    r = run(ENTER_FADE_LONG + [(102.3, 103, 102.0, 102.8), (102.8, 103, 102.6, 102.7), (102.7, 102.8, 99.4, 99.6)])
    t = trade_of(r, 'F_L')
    rows = pd.DataFrame(r['minute_logs'][t['trade_key']])
    assert list(rows['ts']) == list(pd.date_range('2026-01-05 09:01', '2026-01-05 09:04', freq='min'))
    assert rows['minute_in_trade'].tolist() == [0, 1, 2, 3]
    assert 'entry' in rows['events'].iloc[0] and 'stop' in rows['events'].iloc[-1]
    assert rows['trade_closed'].tolist() == [False, False, False, True]
    last = rows.iloc[-1]
    assert last['lots_open'] == 0 and last['unrealized_pts'] == 0 and last['total_pts'] == pytest.approx(t['pnl_pts'])
    assert rows['mfe_pts'].is_monotonic_increasing and rows['mae_pts'].is_monotonic_increasing
    assert rows['total_pts'].iloc[1] == pytest.approx(3 * (102.8 - 102.25))                    # all three lots still open, marked at the bar close


def test_minute_log_shows_the_live_stop_and_booked_lots():
    r = run(TO_T1_THEN_BACK, TSL)
    t = trade_of(r, 'F_L')
    rows = pd.DataFrame(r['minute_logs'][t['trade_key']])
    assert rows['stop'].iloc[0] == pytest.approx(LV['S4']) and rows['stop'].iloc[-1] == pytest.approx(LV['S3'])
    assert rows['lots_booked'].max() == 1


def test_trade_keys_are_unique_and_named_by_date_setup_and_minute():
    r = run(ENTER_FADE_LONG + [(102.3, 102.4, 99.4, 99.6)])
    keys = [t['trade_key'] for t in r['trades']]
    assert len(keys) == len(set(keys)) and keys[0] == '2026-01-05_F_L_0901'


def test_event_log_has_first_touches_once_in_path_order():
    r = run(ENTER_FADE_LONG + [(102.3, 103.2, 102.3, 103.0)])
    touches = [(e['level'], e['bar']) for e in r['events'] if e['event'] == 'level_touch']
    names = [n for n, _ in touches]
    assert len(names) == len(set(names))
    assert names.index('S3') < names.index('S2')                  # S3 is reached before S2 on the way up in bar 1
    assert dict(touches)['S3'] == 1


# ---------------------------------------------------------------- the runner's output folder

def test_write_run_empties_the_folder_first(tmp_path):
    res = {'trades': pd.DataFrame([{'a': 1}]), 'legs': pd.DataFrame([{'a': 1}]), 'events': pd.DataFrame([{'a': 1}]), 'sessions': pd.DataFrame([{'date': '2026-01-05', 'data_source': 'fyers'}]),
           'minute': {'k1': [{'x': 1}]}, 'n_sessions': 1, 'order': 'neutral', 'other': 'adverse'}
    out = machine_run.write_run(res, 'CRUDEOILM', 'range_setter', OFF, out_root=str(tmp_path))
    stale = Path(out) / 'minute' / 'old_trade.csv'
    stale.write_text('x')
    out2 = machine_run.write_run(res, 'CRUDEOILM', 'range_setter', OFF, out_root=str(tmp_path))
    assert out == out2 and not stale.exists() and (Path(out) / 'minute' / 'k1.csv').exists()
    assert machine_run.run_name('CRUDEOILM', 'range_setter', TSL).endswith('rebuy-off_counter-off_tsl-on')


# ---------------------------------------------------------------- no re-entry after a trailed stop

TRAILED_THEN_BACK_UP = TO_T1_THEN_BACK + [(102.1, 102.6, 102.1, 102.5)]            # the trailed stop at S3 closes the trade, then price rises through S3 again
NORMAL_STOP_THEN_BACK_UP = ENTER_FADE_LONG + [(102.3, 102.4, 99.4, 99.6), (99.6, 102.5, 99.6, 102.4)]


def test_a_trailed_stop_lets_the_setup_enter_again_by_default():
    r = run(TRAILED_THEN_BACK_UP, TSL)
    assert len([t for t in r['trades'] if t['setup'] == 'F_L']) == 2


def test_no_reentry_after_a_trailed_stop_blocks_that_setup_for_the_session():
    r = run(TRAILED_THEN_BACK_UP, TSL_NO_RE)
    assert len([t for t in r['trades'] if t['setup'] == 'F_L']) == 1
    assert trade_of(r, 'F_L')['reasons'] == 'T1,stop_trailed,stop_trailed' and trade_of(r, 'F_L')['tsl_reentry'] is False


def test_a_normal_stop_never_blocks_a_reentry():
    r = run(NORMAL_STOP_THEN_BACK_UP, TSL_NO_RE)
    assert len([t for t in r['trades'] if t['setup'] == 'F_L']) == 2


def test_the_block_changes_nothing_with_the_trailing_stop_off():
    r = run(TRAILED_THEN_BACK_UP, {'rebuy': False, 'counter': False, 'tsl': False, 'tsl_reentry': False})
    assert 'stop_trailed' not in ' '.join(t['reasons'] for t in r['trades'])


def test_run_name_marks_the_blocked_variant():
    assert machine_run.run_name('CRUDEOILM', 'range_setter', TSL_NO_RE).endswith('tsl-on-noreentry')
    assert machine_run.run_name('CRUDEOILM', 'range_setter', TSL).endswith('tsl-on')


# ---------------------------------------------------------------- per-run trailing rules

def test_tsl_rules_in_opts_override_the_config():
    bars = ENTER_FADE_LONG + [(102.3, 107, 102.3, 106.9), (106.9, 106.95, 105.5, 105.6)]       # T1 and T2 hit, then back below T1
    only_t2 = trade_of(run(bars, {**TSL, 'tsl_rules': {1: None, 2: 'T1'}}), 'F_L')
    assert only_t2['stop_moves'] == 1 and only_t2['stop_final'] == pytest.approx(LV['R1'])      # nothing moved after T1, one move after T2
    only_t1 = trade_of(run(bars, {**TSL, 'tsl_rules': {1: 'entry', 2: None}}), 'F_L')
    assert only_t1['stop_moves'] == 1 and only_t1['stop_final'] == pytest.approx(LV['S3'])
    assert only_t1['reasons'] != only_t2['reasons']


def test_run_name_spells_out_non_default_trail_rules():
    a = machine_run.run_name('CRUDEOILM', 'range_setter', {**TSL, 'tsl_rules': {1: 'entry', 2: None}})
    b = machine_run.run_name('CRUDEOILM', 'range_setter', {**TSL, 'tsl_rules': {1: None, 2: 'T1'}})
    assert a.endswith('tsl-on_t1-entry_t2-stay') and b.endswith('tsl-on_t1-stay_t2-T1') and a != b
    assert machine_run.run_name('CRUDEOILM', 'range_setter', {**TSL, 'tsl_rules': configs.TSL_RULES}).endswith('tsl-on')
