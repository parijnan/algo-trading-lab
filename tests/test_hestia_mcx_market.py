"""hestia_core.mcx_market against the production definitions (compiled from source, never imported) and the real files."""
import ast
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from hestia_core.mcx_market import (ContractCatalog, MarketCalendar, closing_time, closing_time_str,  # noqa: E402
                                    us_dst_transition_dates)

HOLIDAYS = REPO / 'data_pipeline' / 'data' / 'mcx_holidays.csv'
MASTER = REPO / 'data_pipeline' / 'data' / 'mcx_instrument_master.csv'
DATA_ROOT = REPO / 'data_pipeline' / 'data' / 'mcx'


def _compile(path, names, ns):
    for node in ast.parse((REPO / path).read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            exec(compile(ast.Module([node], []), path, 'exec'), ns)
    return ns


def test_closing_time_matches_production_on_every_day_of_five_years():
    ns = _compile('prometheus_production/prometheus_configs.py', {'_us_dst_transition_dates', '_resolve_closing_time'},
                  {'date': date, 'timedelta': timedelta})
    d = date(2024, 1, 1)
    seen = set()
    while d < date(2029, 1, 1):
        assert closing_time_str(d) == ns['_resolve_closing_time'](d), d
        seen.add(closing_time_str(d))
        d += timedelta(days=1)
    assert seen == {'23:30', '23:55'}
    assert closing_time(date(2026, 9, 3)) == time(23, 30) and closing_time(date(2026, 12, 1)) == time(23, 55)
    assert us_dst_transition_dates(2026) == (date(2026, 3, 8), date(2026, 11, 1))


@pytest.fixture(scope='module')
def cal():
    return MarketCalendar(HOLIDAYS)


def test_full_closures_and_evening_only_days_match_production_for_every_day_in_the_file(cal):
    df = pd.read_csv(HOLIDAYS)
    df['date'] = pd.to_datetime(df['date']).dt.date
    for c in ('morning_session_closed', 'evening_session_closed'):
        df[c] = df[c].astype(str).str.lower().isin(('true', '1'))
    ns = _compile('prometheus_production/prometheus_functions.py', {'mcx_fully_closed_today', 'mcx_evening_only_today'},
                  {'date': date, '_load_mcx_holidays': lambda: df})
    d = df['date'].min() - timedelta(days=7)
    checked, one_sided = 0, 0
    while d <= df['date'].max() + timedelta(days=7):
        assert cal.fully_closed(d) == ns['mcx_fully_closed_today'](d)[0], d
        assert cal.evening_only(d) == ns['mcx_evening_only_today'](d)[0], d
        one_sided += cal.evening_only(d)
        checked += 1
        d += timedelta(days=1)
    assert checked > 300 and not cal.missing


def test_session_open_is_1700_on_an_evening_only_day_and_0900_otherwise(cal):
    evening_only = [d for d in cal.rows if cal.evening_only(d)]
    if evening_only:                                        # the file's evening-only days, e.g. Ganesh Chaturthi 2026-09-14
        assert cal.session_open(evening_only[0]) == datetime.combine(evening_only[0], time(17, 0))
    assert cal.session_open(date(2026, 9, 3)) == datetime(2026, 9, 3, 9, 0)
    assert cal.session_close(date(2026, 9, 3)) == datetime(2026, 9, 3, 23, 30)


def test_a_missing_calendar_is_weekends_only_and_says_so(tmp_path):
    c = MarketCalendar(tmp_path / 'nope.csv')
    assert c.missing and c.fully_closed(date(2026, 9, 5)) and not c.fully_closed(date(2026, 9, 4))
    assert c.trading_days_left(date(2026, 9, 3), date(2026, 9, 8)) == 4


@pytest.fixture(scope='module')
def catalog():
    return ContractCatalog(MASTER, DATA_ROOT)


def test_catalog_reads_lot_size_freeze_lots_tick_and_the_pipeline_file_path(catalog):
    row = catalog.row('562058')
    assert (row.ref.instrument, row.ref.symbol, row.ref.expiry) == ('SILVERMIC', 'SILVERMIC30NOV26FUT', date(2026, 11, 30))
    assert row.lot_size == 1 and row.freeze_lots == 600 and row.tick_size == 1.0
    assert row.filepath == str(DATA_ROOT / 'SILVERMIC' / '2026-11-30_futures.csv')
    crude = catalog.row('569901')
    assert crude.lot_size == 10 and crude.freeze_lots == 1000, 'freeze quantity is in units: 10000 units / lot of 10'


def test_live_contracts_are_the_unexpired_ones_nearest_first_with_holiday_aware_days_left(catalog, cal):
    rows = catalog.live_rows('SILVERMIC', date(2026, 9, 3))
    assert [r.ref.expiry for r in rows] == sorted(r.ref.expiry for r in rows) and rows[0].ref.expiry >= date(2026, 9, 3)
    assert not catalog.live_rows('SILVERMIC', date(2030, 1, 1))
    infos = catalog.infos('SILVERMIC', date(2026, 9, 3), cal)
    assert [i.ref.token for i in infos] == [r.ref.token for r in rows]
    assert infos[0].trading_days_left == cal.trading_days_left(date(2026, 9, 3), infos[0].ref.expiry) > 0
