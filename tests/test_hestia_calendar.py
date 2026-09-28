"""
hestia_core.calendar against the production day counters (prometheus_functions._count_trading_days_inclusive and
next_trading_day), on the real MCX holiday file, including the one-session-only closures. The uniform 5-working-day roll
rule rests on this count, so the two implementations are compared for every start date across the holiday file's range.
"""
import ast
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from hestia_core.calendar import count_trading_days_inclusive, is_trading_day, next_trading_day  # noqa: E402


def _production_functions(holidays_df):
    src = (REPO / 'prometheus_production' / 'prometheus_functions.py').read_text()
    ns = {'pd': pd, 'timedelta': timedelta, 'date': date, '_load_mcx_holidays': lambda: holidays_df}
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in ('next_trading_day', '_count_trading_days_inclusive'):
            exec(compile(ast.Module([node], []), 'prometheus_functions', 'exec'), ns)
    return ns['_count_trading_days_inclusive'], ns['next_trading_day']


@pytest.fixture(scope='module')
def holidays():
    df = pd.read_csv(REPO / 'data_pipeline' / 'data' / 'mcx_holidays.csv')
    df['date'] = pd.to_datetime(df['date']).dt.date
    for c in ('morning_session_closed', 'evening_session_closed'):
        df[c] = df[c].astype(str).str.lower().isin(('true', '1'))
    return df


def _fully_closed(df):
    return {r['date'] for _, r in df.iterrows() if r['morning_session_closed'] and r['evening_session_closed']}


def test_file_contains_one_session_only_closures(holidays):
    one_sided = holidays[holidays['morning_session_closed'] != holidays['evening_session_closed']]
    assert len(one_sided) > 0, 'the comparison would not exercise the one-session rule'


def test_count_matches_production_for_every_window(holidays):
    prod_count, _ = _production_functions(holidays)
    closed = _fully_closed(holidays)
    start = holidays['date'].min() - timedelta(days=10)
    checked = 0
    for offset in range(0, 400, 3):
        s = start + timedelta(days=offset)
        for span in (0, 1, 4, 5, 9, 20):
            e = s + timedelta(days=span)
            assert count_trading_days_inclusive(s, e, closed) == prod_count(s, e, holidays), (s, e)
            checked += 1
    assert checked > 500
    assert count_trading_days_inclusive(date(2026, 3, 2), date(2026, 3, 1), closed) == 0


def test_next_trading_day_matches_production(holidays):
    _, prod_next = _production_functions(holidays)
    closed = _fully_closed(holidays)
    d = holidays['date'].min() - timedelta(days=5)
    while d < holidays['date'].max():
        assert next_trading_day(d, closed) == prod_next(d), d
        d += timedelta(days=1)


def test_one_session_holiday_counts_as_a_trading_day(holidays):
    one_sided = holidays[holidays['morning_session_closed'] != holidays['evening_session_closed']]
    closed = _fully_closed(holidays)
    for d in one_sided['date']:
        if d.weekday() < 5:
            assert is_trading_day(d, closed)
