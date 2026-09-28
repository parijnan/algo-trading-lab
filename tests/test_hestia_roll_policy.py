"""
hestia_core.roll_policy: the roll rules as pure functions, pinned to production where production has a pure counterpart (contract
resolution, the basis lookup, the rollover time) and unit-tested branch by branch where it does not (the veto, the coincident
flip, the restart cases: the repo's Prometheus suite has no rollover tests, and the recorded live days contain no roll).
"""
import ast
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from hestia_core import roll_policy as rp  # noqa: E402
from hestia_core.interface import ContractRef, Direction  # noqa: E402
from hestia_core.mcx_market import ContractCatalog, MarketCalendar, closing_time  # noqa: E402

MASTER = REPO / 'data_pipeline' / 'data' / 'mcx_instrument_master.csv'
HOLIDAYS = REPO / 'data_pipeline' / 'data' / 'mcx_holidays.csv'
BULL, BEAR = Direction.BULLISH, Direction.BEARISH


def ref(token, expiry, name='XX'):
    return ContractRef(name, token, f'{name}{expiry:%d%b%y}FUT'.upper(), expiry)


FRONT, NEXT, FAR = ref('T1', date(2026, 10, 19)), ref('T2', date(2026, 11, 19)), ref('T3', date(2026, 12, 18))
NONE_CLOSED = frozenset()


# ---- parity with production's contract resolution ---------------------------------------------------------------------------

def _production_resolver():
    src = (REPO / 'prometheus_production' / 'prometheus_functions.py').read_text()
    holidays = pd.read_csv(HOLIDAYS)
    holidays['date'] = pd.to_datetime(holidays['date']).dt.date
    ns = {'pd': pd, 'date': date, 'datetime': datetime, 'INSTRUMENT_MASTER_FILE': MASTER, 'TENDER_ROLL_TRADING_DAYS': 5,
          'SYMBOL': 'CRUDEOILM', 'logger': SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
          '_load_mcx_holidays': lambda: holidays,
          'dl': SimpleNamespace(get_futures_filepath=lambda name, expiry: f'{name}/{expiry:%Y-%m-%d}')}
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in ('_parse_expiry_from_master', '_count_trading_days_inclusive',
                                                               '_contract_dict_from_row', 'resolve_effective_contract'):
            exec(compile(ast.Module([node], []), 'pf', 'exec'), ns)
    return ns['resolve_effective_contract']


@pytest.mark.parametrize('instrument', ['CRUDEOILM', 'SILVERMIC'])
def test_effective_contract_matches_production_for_every_date_in_a_year(instrument):
    prod = _production_resolver()
    cal = MarketCalendar(HOLIDAYS)
    cat = ContractCatalog(MASTER, REPO / 'data_pipeline' / 'data' / 'mcx')
    refs = [r.ref for r in cat.live_rows(instrument, date(2026, 1, 1))]
    closed = cal.fully_closed_dates()
    d, seen_early, seen_plain = date(2026, 9, 1), 0, 0
    while d < date(2027, 3, 1):
        try:
            want = prod(instrument, d)
        except RuntimeError:
            d += timedelta(days=1)
            continue
        got = rp.effective_contract(refs, d, closed)
        assert got.contract.token == want['token'], d
        assert got.rolled_early == want['rolled_early'], d
        seen_early += got.rolled_early
        seen_plain += not got.rolled_early
        d += timedelta(days=1)
    assert seen_early >= 10 and seen_plain > 100, 'the range must cover both sides of several rolls or the check is vacuous'


def test_rollover_time_matches_the_productions_definition_on_both_closing_times():
    assert rp.rollover_time(date(2026, 9, 3), closing_time(date(2026, 9, 3))) == datetime(2026, 9, 3, 23, 15)
    assert rp.rollover_time(date(2026, 12, 1), closing_time(date(2026, 12, 1))) == datetime(2026, 12, 1, 23, 40)


def test_basis_price_matches_the_productions_historical_lookup(tmp_path):
    src = (REPO / 'prometheus_production' / 'prometheus_functions.py').read_text()
    ns = {'pd': pd, 'datetime': datetime, 'logger': SimpleNamespace(error=lambda *a, **k: None)}
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == 'historical_basis_price':
            exec(compile(ast.Module([node], []), 'pf', 'exec'), ns)
    ts = pd.date_range('2026-09-03 09:00', periods=400, freq='min')
    ts = ts[~((ts >= '2026-09-03 11:00') & (ts < '2026-09-03 11:20'))]                    # a hole
    frame = pd.DataFrame({'time_stamp': ts.strftime('%Y-%m-%dT%H:%M:%S+05:30'), 'close': [100.0 + i * 0.37 for i in range(len(ts))]})
    path = tmp_path / 'c.csv'
    frame.to_csv(path, index=False)
    naive = pd.to_datetime(frame['time_stamp'], format='ISO8601').dt.tz_localize(None)
    closes = list(zip([t.to_pydatetime() for t in naive], frame["close"]))
    for entry in [datetime(2026, 9, 3, 9, 0), datetime(2026, 9, 3, 10, 59, 30), datetime(2026, 9, 3, 11, 10), datetime(2026, 9, 3, 11, 24),
                  datetime(2026, 9, 3, 11, 26, 30), datetime(2026, 9, 3, 12, 30, 20), datetime(2026, 9, 3, 15, 39),
                  datetime(2026, 9, 3, 15, 50), datetime(2026, 9, 4, 3, 0), datetime(2026, 9, 2, 20, 0)]:
        want = ns['historical_basis_price']({'filepath': str(path), 'symbol': 'X'}, entry)
        assert rp.basis_price(closes, entry) == want, entry


# ---- effective contract and the roll window ------------------------------------------------------------------------------------

def test_the_front_is_kept_until_it_has_five_trading_days_left_counting_today():
    exp = date(2026, 10, 19)                                                      # a Monday
    def eff(d):
        return rp.effective_contract([FRONT, NEXT], d, NONE_CLOSED)
    assert eff(date(2026, 10, 12)).contract == FRONT and eff(date(2026, 10, 12)).days_left == 6
    assert eff(date(2026, 10, 13)).contract == NEXT and eff(date(2026, 10, 13)).days_left == 5, '13,14,15,16,19: five days left'
    assert eff(date(2026, 10, 13)).rolled_early and not eff(date(2026, 10, 12)).rolled_early


def test_a_holiday_inside_the_window_moves_the_roll_a_day_earlier():
    closed = frozenset({date(2026, 10, 15)})
    assert rp.effective_contract([FRONT, NEXT], date(2026, 10, 12), closed).contract == NEXT      # 12,13,14,16,19: five
    assert rp.effective_contract([FRONT, NEXT], date(2026, 10, 9), closed).contract == FRONT


def test_a_contract_that_has_expired_is_never_effective_and_nothing_live_is_an_error():
    assert rp.effective_contract([FRONT, NEXT], date(2026, 10, 20), NONE_CLOSED).contract == NEXT
    assert not rp.effective_contract([FRONT, NEXT], date(2026, 10, 20), NONE_CLOSED).rolled_early
    with pytest.raises(rp.NoContract):
        rp.effective_contract([FRONT], date(2026, 10, 20), NONE_CLOSED)


def test_inside_the_window_with_no_next_contract_listed_the_policy_is_flatten_not_carry():
    got = rp.effective_contract([FRONT], date(2026, 10, 14), NONE_CLOSED)
    assert got.contract == FRONT and got.no_next and not got.rolled_early
    assert rp.NoNextAction.FLATTEN.value == 'flatten' and rp.NoNextAction.CARRY.value == 'carry'
    need = rp.roll_needed_tonight(FRONT, date(2026, 10, 13), [FRONT], NONE_CLOSED)
    assert need.new_contract is None and need.no_next, 'tomorrow is in the window and there is nothing to roll to'


def test_roll_is_needed_tonight_only_when_tomorrows_trading_day_resolves_elsewhere():
    assert rp.roll_needed_tonight(FRONT, date(2026, 10, 9), [FRONT, NEXT], NONE_CLOSED).new_contract is None
    assert rp.roll_needed_tonight(FRONT, date(2026, 10, 12), [FRONT, NEXT], NONE_CLOSED).new_contract == NEXT
    assert rp.roll_needed_tonight(NEXT, date(2026, 10, 12), [FRONT, NEXT], NONE_CLOSED).new_contract is None, 'already on it'


def test_the_roll_check_looks_at_the_next_trading_day_across_a_weekend_and_a_holiday():
    # Friday 09 Oct: tomorrow's trading day is Monday 12 Oct (6 days left: no roll); if Monday is a holiday it is Tuesday 13 (roll)
    assert rp.roll_needed_tonight(FRONT, date(2026, 10, 9), [FRONT, NEXT], NONE_CLOSED).new_contract is None
    assert rp.roll_needed_tonight(FRONT, date(2026, 10, 9), [FRONT, NEXT], frozenset({date(2026, 10, 12)})).new_contract == NEXT


def test_arm_action_is_a_switch_now_when_flat_and_an_evening_arm_when_in_a_trade():
    need = rp.RollNeed(NEXT)
    assert rp.arm_action(need, in_trade=False) == rp.ArmAction.SWITCH_NOW
    assert rp.arm_action(need, in_trade=True) == rp.ArmAction.ARM_EVENING
    assert rp.arm_action(rp.RollNeed(None), in_trade=True) == rp.ArmAction.NONE


def test_the_flat_switch_self_heals_only_from_a_plain_watching_state_with_no_flip_in_flight():
    assert rp.flat_switch_due(True, True, False)
    assert not rp.flat_switch_due(True, True, True), 'a Rule 7 flip is still mid-flight on the old contract'
    assert not rp.flat_switch_due(True, False, False) and not rp.flat_switch_due(False, True, False)


# ---- timing ----------------------------------------------------------------------------------------------------------------------

def test_entries_are_suppressed_from_the_rollover_time_once_a_roll_is_armed():
    at = datetime(2026, 10, 12, 23, 15)
    assert not rp.entry_suppressed(True, at - timedelta(seconds=1), at)
    assert rp.entry_suppressed(True, at, at)
    assert not rp.entry_suppressed(False, at + timedelta(hours=1), at)
    assert rp.before_rollover(at - timedelta(minutes=1), at) and not rp.before_rollover(at, at)


# ---- basis and reopen size -----------------------------------------------------------------------------------------------------------

def test_the_basis_is_the_nearest_close_within_five_minutes_and_never_a_guess():
    t = datetime(2026, 9, 3, 10, 0)
    closes = [(t + timedelta(minutes=m), 100.0 + m) for m in (-7, -3, 2, 9)]
    assert rp.basis_price(closes, t) == 102.0, 'nearest is +2'
    assert rp.basis_price(closes, t + timedelta(minutes=20)) is None, 'nothing within five minutes of 10:20'
    assert rp.basis_price(closes, t + timedelta(minutes=14)) == 109.0, 'exactly five minutes from +9 is still trusted'
    assert rp.basis_price(closes, t + timedelta(minutes=14, seconds=1)) is None, 'a second more is not'
    assert rp.basis_price([], t) is None
    tie = [(t - timedelta(minutes=2), 1.0), (t + timedelta(minutes=2), 2.0)]
    assert rp.basis_price(tie, t) == 1.0, 'the earliest wins a tie, as idxmin does'


def test_the_reopen_is_sized_to_the_lots_that_survived_to_the_roll():
    assert rp.reopen_plan(True, 5, True, 5) == rp.ReopenPlan(10, False)
    assert rp.reopen_plan(False, 5, True, 5) == rp.ReopenPlan(5, True), 'only the far-target lot is left: reopen it alone'
    assert rp.reopen_plan(True, 5, False, 5) == rp.ReopenPlan(5, False)
    assert rp.reopen_plan(False, 5, False, 5) == rp.ReopenPlan(0, False)
    assert rp.reopen_plan(True, None, True, 3) == rp.ReopenPlan(3, False)


# ---- the fallback decision at the rollover time --------------------------------------------------------------------------------

def test_a_flat_rollover_is_housekeeping_with_nothing_to_flatten_or_reopen():
    d = rp.decide_rollover(None, BULL, 100.0)
    assert (d.flatten, d.reopen) == (False, False)


@pytest.mark.parametrize('position,new,basis,align,flatten,reopen', [
    (BULL, BULL, 100.0, True, True, True),          # go
    (BEAR, BEAR, 100.0, True, True, True),
    (BULL, BEAR, 100.0, True, True, False),         # the new contract disagrees: flatten only
    (BEAR, BULL, 100.0, True, True, False),
    (BULL, None, 100.0, True, True, False),         # the new contract's supertrend is unavailable: no-go, never a guess
    (BULL, BULL, 100.0, False, True, False),        # the optional alignment filter disagrees
    (BULL, BULL, None, True, True, False),          # go, but no basis price for the stop: flatten only
])
def test_the_veto_table(position, new, basis, align, flatten, reopen):
    d = rp.decide_rollover(position, new, basis, align)
    assert (d.flatten, d.reopen) == (flatten, reopen) and d.reason


def test_the_old_position_is_always_flattened_whatever_the_veto_says():
    for new in (BULL, BEAR, None):
        for basis in (100.0, None):
            assert rp.decide_rollover(BULL, new, basis).flatten


# ---- the coincident flip -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('flipped,new_dir,flip_dir,rows,expected', [
    (True, BEAR, BEAR, 15, True),
    (True, BEAR, BULL, 15, False),                  # flipped, but to the other side
    (False, BEAR, BEAR, 15, False),                 # agrees with the direction but did not flip on this bar
    (True, BEAR, BEAR, 14, False),                  # an incomplete window never claims a coincidence
    (True, None, BEAR, 15, False),                  # warm-up
    (True, BULL, BULL, 20, True),
])
def test_coincident_flip(flipped, new_dir, flip_dir, rows, expected):
    assert rp.coincident_flip(flipped, new_dir, flip_dir, rows) is expected


# ---- restart cases ------------------------------------------------------------------------------------------------------------------

def test_a_restart_with_the_position_on_todays_contract_needs_nothing():
    assert rp.restart_action('T1', date(2026, 10, 19), 'T1', date(2026, 10, 19)) == rp.RestartAction.NONE
    assert rp.restart_action(None, None, 'T1', date(2026, 10, 19)) == rp.RestartAction.NONE


def test_a_position_on_a_later_contract_is_a_catch_up_never_a_backwards_roll():
    assert rp.restart_action('T2', date(2026, 11, 19), 'T1', date(2026, 10, 19)) == rp.RestartAction.CATCH_UP
    assert rp.restart_action('T2', date(2026, 11, 19), 'T1', date(2026, 10, 19), catch_up_lookup_ok=False) == rp.RestartAction.REFUSE


def test_a_position_on_an_earlier_contract_is_a_missed_roll():
    assert rp.restart_action('T1', date(2026, 10, 19), 'T2', date(2026, 11, 19)) == rp.RestartAction.MISSED_ROLL
    assert rp.restart_action('T1', None, 'T2', date(2026, 11, 19)) == rp.RestartAction.MISSED_ROLL, 'unknown expiry: roll'


def test_the_module_is_pure():
    src = (REPO / 'hestia_core' / 'roll_policy.py').read_text()
    tree = ast.parse(src)
    imports = {n.module if isinstance(n, ast.ImportFrom) else a.name for n in ast.walk(tree)
               if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (n.names if isinstance(n, ast.Import) else [n])}
    assert imports <= {'__future__', 'dataclasses', 'datetime', 'enum', 'typing', 'hestia_core', 'hestia_core.interface'}, imports
    assert 'datetime.now' not in src and 'time.sleep' not in src


def test_effective_from_days_left_agrees_with_the_calendar_version_including_tomorrow():
    refs = [FRONT, NEXT, FAR]
    closed = frozenset({date(2026, 10, 15)})
    d = date(2026, 10, 1)
    checked = 0
    while d < date(2026, 10, 22):
        if hcal_is_trading(d, closed):
            pairs = [(r, rp.days_left(d, r.expiry, closed)) for r in refs if r.expiry >= d]
            assert rp.effective_from_days_left(pairs).contract == rp.effective_contract(refs, d, closed).contract, d
            tomorrow = rp.hcal.next_trading_day(d, closed)
            if tomorrow <= FRONT.expiry:
                assert rp.effective_from_days_left(pairs, days_offset=1).contract == rp.effective_contract(refs, tomorrow, closed).contract, d
            checked += 1
        d += timedelta(days=1)
    assert checked > 10


def hcal_is_trading(d, closed):
    return rp.hcal.is_trading_day(d, closed)
