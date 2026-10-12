import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'janus_backtest'))
import janus_configs as configs  # noqa: E402
import janus_engine as eng       # noqa: E402
from janus_levels import camarilla_levels  # noqa: E402

# Previous H=110 L=100 C=105: S4=99.5 S3=102.25 S2=103.1667 S1=104.0833 | R1=105.9167 R2=106.8333 R3=107.75 R4=110.5 R5=115.5 R6=121.34 R7=128.16
LV = camarilla_levels(110.0, 100.0, 105.0)
A0 = {'rebuy': False, 'counter': False}
A1 = {'rebuy': True, 'counter': False}
A2 = {'rebuy': False, 'counter': True}
A3 = {'rebuy': True, 'counter': True}


def run(bars, arm=A0, order='neutral', pad=100, symbol='CRUDEOILM', scn='touch'):
    """bars: (open, high, low, close) tuples. A flat pad at the last close follows, so the cut-off and the hard exit fall in the pad."""
    last = bars[-1][3]
    rows = list(bars) + [(last, last, last, last)] * pad
    idx = pd.date_range('2026-01-05 09:00', periods=len(rows), freq='min')
    df = pd.DataFrame(rows, columns=['open', 'high', 'low', 'close'], index=idx)
    df['volume'] = 1
    s = {'symbol': symbol, 'date': date(2026, 1, 5), 'expiry': date(2026, 1, 20), 'bars': df, 'prev': {}, 'levels': LV}
    return eng.simulate_session(s, arm, order, scn=scn)


def legs(res, setup=None):
    return [x for x in res['legs'] if setup is None or x['setup'] == setup]


def reasons(res, setup):
    return [(x['lot'], x['reason']) for x in legs(res, setup)]


# bar 0 flat below S3; bar 1 rises through S3 (entry fill 102.25) and closes just above it
ENTER_FADE_LONG = [(101, 101.2, 100.8, 101), (101, 102.4, 100.9, 102.3)]


def test_fade_long_runs_the_full_ladder():
    r = run(ENTER_FADE_LONG + [(102.3, 108, 102.3, 107.9)])
    assert reasons(r, 'F_L') == [(1, 'T1'), (2, 'T2'), (3, 'T3')]
    assert [round(x['exit'], 4) for x in legs(r, 'F_L')] == [round(LV['R1'], 4), round(LV['R2'], 4), round(LV['R3'], 4)]
    assert legs(r, 'F_L')[0]['entry'] == LV['S3']
    assert legs(r, 'F_L')[0]['pnl_r'] == pytest.approx((LV['R1'] - LV['S3']) / (LV['S3'] - LV['S4']))


def test_stop_takes_all_open_lots_at_the_stop_level():
    r = run(ENTER_FADE_LONG + [(102.3, 102.4, 99.4, 99.6)])
    assert reasons(r, 'F_L') == [(1, 'stop'), (2, 'stop'), (3, 'stop')]
    assert all(x['pnl_r'] == pytest.approx(-1.0) and x['exit'] == LV['S4'] for x in legs(r, 'F_L'))


def test_stop_and_reverse_enters_the_opposite_setup_at_the_same_price():
    r = run(ENTER_FADE_LONG + [(102.3, 102.4, 99.4, 99.6)])           # the long's stop S4 is the breakout short's entry
    b = legs(r, 'B_S')
    assert len(b) == 3 and all(x['entry'] == LV['S4'] and x['kind'] == 'entry' for x in b)
    assert b[0]['trade'] > legs(r, 'F_L')[0]['trade']


def test_rebuy_restores_the_booked_lots_for_their_own_targets():
    bars = ENTER_FADE_LONG + [(102.3, 106.9, 102.3, 106.5),           # T1 and T2 book
                              (106.5, 106.6, 102.0, 102.1),           # falls back through the entry level: two lots re-bought
                              (102.1, 106.0, 102.1, 105.95)]          # the re-bought T1 lot books again
    r = run(bars, A1)
    first = [(x['lot'], x['reason']) for x in legs(r, 'F_L') if not x['rebuy']]
    again = [(x['lot'], x['reason'], x['cycle']) for x in legs(r, 'F_L') if x['rebuy']]
    assert first == [(1, 'T1'), (2, 'T2'), (3, 'hard_exit')]
    assert again == [(1, 'T1', 1), (2, 'hard_exit', 1)]
    assert all(x['entry'] == LV['S3'] for x in legs(r, 'F_L') if x['rebuy'])


def test_rebuy_after_only_t1_buys_one_lot_back():
    bars = ENTER_FADE_LONG + [(102.3, 106.2, 102.3, 106.0), (106.0, 106.1, 102.0, 102.1)]
    r = run(bars, A1)
    assert sum(1 for x in legs(r, 'F_L') if x['rebuy']) == 1 and [x['lot'] for x in legs(r, 'F_L') if x['rebuy']] == [1]


def test_no_rebuy_without_the_option():
    bars = ENTER_FADE_LONG + [(102.3, 106.9, 102.3, 106.5), (106.5, 106.6, 102.0, 102.1)]
    assert not any(x['rebuy'] for x in legs(run(bars, A0), 'F_L'))
    assert not any(x['rebuy'] for x in legs(run(bars, A2), 'F_L'))


def test_counter_trade_opens_at_t3_for_a_fade_only():
    bars = ENTER_FADE_LONG + [(102.3, 108, 102.3, 107.9)]
    c = [x for x in legs(run(bars, A2), 'F_S')]
    assert len(c) == 3 and all(x['kind'] == 'counter' and x['entry'] == LV['R3'] for x in c)
    assert not legs(run(bars, A0), 'F_S')
    br = [(106, 106.2, 105.8, 106), (106, 111, 105.9, 110.9), (110.9, 130, 110.9, 129)]     # breakout long runs to R7
    r = run(br, A3)
    assert reasons(r, 'B_L') == [(1, 'T1'), (2, 'T2'), (3, 'T3')]
    assert not any(x['kind'] == 'counter' for x in r['legs'])


def test_hard_exit_closes_what_is_open_at_the_pad():
    r = run(ENTER_FADE_LONG)
    assert reasons(r, 'F_L') == [(1, 'hard_exit'), (2, 'hard_exit'), (3, 'hard_exit')]
    assert all(x['exit'] == 102.3 for x in legs(r, 'F_L'))


def test_a_stop_crossed_in_the_jump_fills_at_the_open():
    r = run(ENTER_FADE_LONG + [(98.0, 98.5, 97.5, 98.2)])
    st = legs(r, 'F_L')
    assert [x['reason'] for x in st] == ['stop'] * 3 and all(x['exit'] == 98.0 for x in st)


def test_an_entry_crossed_in_the_jump_fills_at_the_open_but_is_skipped_if_that_is_past_t1():
    r = run([(101, 101.2, 100.8, 101), (106.0, 106.1, 105.9, 106.0)])      # gap from 101 to 106: past T1 (105.9167)
    assert not legs(r, 'F_L')
    r = run([(101, 101.2, 100.8, 101), (103.0, 103.1, 102.9, 103.0)])      # gap to 103: entered at the open
    assert legs(r, 'F_L')[0]['entry'] == 103.0


def test_no_entry_in_the_first_minute(monkeypatch):
    bars = [(101, 103, 100.9, 102.9)]
    assert not run(bars)['legs']
    monkeypatch.setattr(configs, 'AT_OPEN_MINUTES', 0)
    assert legs(run(bars), 'F_L')


def test_no_entry_after_the_cutoff(monkeypatch):
    assert legs(run(ENTER_FADE_LONG), 'F_L')
    monkeypatch.setattr(configs, 'ENTRY_CUTOFF_MIN', 10_000)
    assert not run(ENTER_FADE_LONG)['legs']


def test_rebuy_is_blocked_after_the_cutoff(monkeypatch):
    bars = ENTER_FADE_LONG + [(102.3, 106.9, 102.3, 106.5), (106.5, 106.6, 102.0, 102.1)]
    monkeypatch.setattr(configs, 'ENTRY_CUTOFF_MIN', 101)           # 104 rows: only bar 1 may enter; the pullback at bar 3 is after the cut-off
    r = run(bars, A1)
    assert legs(r, 'F_L') and not any(x['rebuy'] for x in legs(r, 'F_L'))
    monkeypatch.setattr(configs, 'ENTRY_CUTOFF_MIN', 60)
    assert any(x['rebuy'] for x in legs(run(bars, A1), 'F_L'))


def test_counter_trade_is_blocked_after_the_cutoff(monkeypatch):
    bars = ENTER_FADE_LONG + [(102.3, 108, 102.3, 107.9)]
    monkeypatch.setattr(configs, 'ENTRY_CUTOFF_MIN', 100)           # 103 rows: only bar 1 may enter; T3 at bar 2 is after the cut-off
    r = run(bars, A2)
    assert reasons(r, 'F_L') == [(1, 'T1'), (2, 'T2'), (3, 'T3')] and not legs(r, 'F_S')


def test_nothing_is_re_bought_once_t3_has_closed_the_trade():
    bars = ENTER_FADE_LONG + [(102.3, 108, 102.3, 107.9), (107.9, 107.95, 102.0, 102.1)]
    r = run(bars, A1)
    assert not any(x['rebuy'] for x in legs(r, 'F_L'))


def test_hard_exit_fills_at_the_open_of_the_first_bar_at_or_after_the_time():
    rows = ENTER_FADE_LONG + [(102.3, 102.3, 102.3, 102.3)] * 70 + [(102.3, 102.9, 102.3, 102.8)] + [(102.8, 102.8, 102.8, 102.8)] * 15
    idx = pd.date_range('2026-01-05 09:00', periods=len(rows), freq='min')
    df = pd.DataFrame(rows, columns=['open', 'high', 'low', 'close'], index=idx)
    df['volume'] = 1
    s = {'symbol': 'CRUDEOILM', 'date': date(2026, 1, 5), 'expiry': date(2026, 1, 20), 'bars': df, 'prev': {}, 'levels': LV}
    res = eng.simulate_session(s, A0, 'neutral')
    assert [x['exit'] for x in res['legs'] if x['setup'] == 'F_L'] == [102.3] * 3 and legs(res, 'F_L')[0]['exit_bar'] == 72


def test_touching_a_level_from_the_wrong_side_does_not_enter():
    # price rises to exactly R3 and turns down without ever having been above it: no fade short
    r = run([(106, 106.1, 105.9, 106), (106, 107.75, 106, 107.0)])
    assert not legs(r, 'F_S')


def test_bar_order_modes_differ_on_a_bar_that_spans_the_stop_and_t1():
    bars = ENTER_FADE_LONG + [(102.3, 106.0, 99.0, 99.2)]            # bearish bar reaching T1 (106.0) and the stop (99.0)
    def first_trade(res):
        f = legs(res, 'F_L')
        return [(x['lot'], x['reason']) for x in f if x['trade'] == f[0]['trade']]
    neutral, adverse = first_trade(run(bars, A0, 'neutral')), first_trade(run(bars, A0, 'adverse'))
    assert neutral == [(1, 'T1'), (2, 'stop'), (3, 'stop')]            # bearish bar: high first
    assert adverse == [(1, 'stop'), (2, 'stop'), (3, 'stop')]          # net long: low first


def test_trading_one_tick_through_the_level_is_required_when_configured(monkeypatch):
    bars = ENTER_FADE_LONG + [(102.3, 106.5, 102.3, 106.4)]          # reaches T1 (105.92) but not T1 + 1 tick
    assert (1, 'T1') in reasons(run(bars), 'F_L')
    monkeypatch.setattr(configs, 'TICK_THROUGH', 1)
    assert (1, 'T1') not in reasons(run(bars), 'F_L')


def test_event_loop_cap_raises(monkeypatch):
    monkeypatch.setattr(configs, 'MAX_EVENTS_PER_BAR', 1)
    with pytest.raises(RuntimeError):
        run(ENTER_FADE_LONG + [(102.3, 108, 102.3, 107.9)])


def test_entry_limit_per_setup_is_respected():
    bars = ENTER_FADE_LONG + [(102.3, 102.4, 99.4, 99.6), (99.6, 102.5, 99.6, 102.4)]     # stopped, then reclaims S3 again
    last = bars[-1][3]
    rows = bars + [(last, last, last, last)] * 100
    idx = pd.date_range('2026-01-05 09:00', periods=len(rows), freq='min')
    df = pd.DataFrame(rows, columns=['open', 'high', 'low', 'close'], index=idx)
    df['volume'] = 1
    s = {'symbol': 'CRUDEOILM', 'date': date(2026, 1, 5), 'expiry': date(2026, 1, 20), 'bars': df, 'prev': {}, 'levels': LV}
    assert eng.simulate_session(s, A0, 'neutral')['setup_entries']['F_L'] == 2
    assert eng.simulate_session(s, A0, 'neutral', entry_limit=1)['setup_entries']['F_L'] == 1


# ---- close-armed watcher with a limit order at the level ---------------------------------------------------------------------------------
ARM_LONG = (101, 101.2, 100.8, 101)                     # closes below S3 (102.25): the watcher arms
SIGNAL_LONG = (101, 103, 100.9, 102.8)                  # closes above S3: the limit order is placed


def test_close_entry_waits_for_the_retest_and_fills_exactly_at_the_level():
    bars = [ARM_LONG, SIGNAL_LONG, (102.8, 103, 102.2, 102.9)]
    r = run(bars, A0, scn='c1')
    f = legs(r, 'F_L')
    assert len(f) == 3 and all(x['entry'] == LV['S3'] and x['entry_bar'] == 2 for x in f)
    assert legs(run(bars, A0, scn='touch'), 'F_L')[0]['entry_bar'] == 1            # the touch design entered on the way up


def test_no_retest_means_no_trade():
    assert not legs(run([ARM_LONG, SIGNAL_LONG, (102.8, 103, 102.6, 102.9)], A0, scn='c1'), 'F_L')


def test_a_wick_above_the_level_without_a_close_does_not_signal():
    r = run([ARM_LONG, (101, 103, 100.9, 102.0), (102.0, 102.1, 101.9, 102.0)], A0, scn='c1')
    assert not legs(r, 'F_L')


def test_a_wick_below_the_level_without_a_close_does_not_arm():
    r = run([(103, 103.2, 102.0, 103), (103, 103.5, 102.2, 103.2), (103.2, 103.3, 102.2, 103.0)], A0, scn='c1')
    assert not legs(r, 'F_L')


def test_the_order_is_cancelled_if_t1_is_reached_before_the_fill(monkeypatch):
    bars = [ARM_LONG, SIGNAL_LONG, (102.8, 106, 102.8, 105.9), (105.9, 105.95, 102.0, 102.1)]
    r = run(bars, A0, scn='c1')
    assert not legs(r, 'F_L') and r['cancels'] == 1
    monkeypatch.setattr(configs, 'CANCEL_AT_T1', False)
    assert legs(run(bars, A0, scn='c1'), 'F_L')[0]['entry'] == LV['S3']


def test_the_short_side_mirrors_the_long():
    bars = [(108.5, 108.7, 108.3, 108.5), (108.5, 108.6, 107.0, 107.4), (107.4, 107.8, 107.2, 107.5)]    # close above R3, close below R3, retest up
    f = legs(run(bars, A0, scn='c1'), 'F_S')
    assert len(f) == 3 and all(x['side'] == -1 and x['entry'] == LV['R3'] and x['entry_bar'] == 2 for x in f)


def test_only_the_close_of_the_signal_timeframe_counts():
    flat = lambda p: (p, p, p, p)                      # noqa: E731
    bars = [flat(101)] * 15 + [flat(103)] * 5 + [(103, 103, 101.5, 103)] + [flat(103)] * 9 + [flat(103)] + [(103, 103, 102.2, 102.5)]
    # bars 0-14 close below S3 (bucket 0 arms at bar 14); bars 15-29 close above it at the bucket end; bar 20 dips to 101.5 mid-bucket
    assert legs(run(bars, A0, scn='c1'), 'F_L')[0]['entry_bar'] == 20             # 1-minute closes: pending from bar 15, filled by the dip at bar 20
    assert legs(run(bars, A0, scn='c15'), 'F_L')[0]['entry_bar'] == 31            # 15-minute closes: pending from bar 30 (after the bucket closed at bar 29)


def test_literal_reading_buys_at_the_level_on_the_signal_close():
    f = legs(run([ARM_LONG, SIGNAL_LONG], A0, scn='l1'), 'F_L')
    assert len(f) == 3 and all(x['entry'] == LV['S3'] and x['entry_bar'] == 1 for x in f)


def test_a_finished_trade_needs_a_new_close_below_before_the_next_signal():
    bars = [ARM_LONG, SIGNAL_LONG, (102.8, 103, 102.2, 102.9), (102.9, 108, 102.9, 107.9), (107.9, 107.95, 102.0, 102.4)]
    r = run(bars, A0, scn='c1')                        # T3 reached, then price dips to S3 again without ever closing below it
    assert len({x['trade'] for x in legs(r, 'F_L')}) == 1


def test_counter_trade_still_opens_at_t3_in_close_mode_and_clears_the_sisters_order():
    bars = [ARM_LONG, SIGNAL_LONG, (102.8, 103, 102.2, 102.9), (102.9, 108, 102.9, 107.9)]
    r = run(bars, A2, scn='c1')
    assert any(x['kind'] == 'counter' and x['setup'] == 'F_S' and x['entry'] == LV['R3'] for x in r['legs'])


def test_signals_are_counted():
    assert run([ARM_LONG, SIGNAL_LONG, (102.8, 103, 102.6, 102.9)], A0, scn='c1')['signals'] == 1


def test_a_limit_fills_exactly_at_its_level_even_when_the_next_bar_gaps_through_it():
    f = legs(run([ARM_LONG, SIGNAL_LONG, (102.0, 102.1, 101.8, 102.0)], A0, scn='c1'), 'F_L')
    assert f and all(x['entry'] == LV['S3'] for x in f)


def test_a_working_order_does_not_fill_after_the_entry_cutoff(monkeypatch):
    bars = [ARM_LONG, SIGNAL_LONG] + [(102.9, 102.9, 102.9, 102.9)] * 3 + [(102.9, 103, 102.2, 102.9)]       # the retest comes at bar 5
    assert legs(run(bars, A0, scn='c1'), 'F_L')
    monkeypatch.setattr(configs, 'ENTRY_CUTOFF_MIN', 101)          # 106 rows: entries allowed only before bar 4
    assert not legs(run(bars, A0, scn='c1'), 'F_L')
