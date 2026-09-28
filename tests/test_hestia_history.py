"""hestia_core.history against the production resampler and gap check (compiled from source), plus file handling."""
import ast
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from hestia_core.history import (TodayCache, find_gaps, format_timestamp, merge_and_save, read_past,  # noqa: E402
                                 resample_1m)


def _production(closing):
    src = (REPO / 'prometheus_production' / 'prometheus_functions.py').read_text()
    ns = {'pd': pd, 'timedelta': timedelta, 'datetime': datetime, 'SESSION_START_TIME': '09:00', 'CLOSING_TIME': closing}
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in ('_resample_1m_to_Nmin', '_find_Nmin_gaps'):
            exec(compile(ast.Module([node], []), 'pf', 'exec'), ns)
    return ns['_resample_1m_to_Nmin'], ns['_find_Nmin_gaps']


def frame(days, closing, seed=1, drop=()):
    rng = np.random.default_rng(seed)
    hh, mm = closing.split(':')
    rows = []
    price = 100.0
    for d in days:
        t = datetime.combine(d, datetime.min.time()) + timedelta(hours=9)
        end = datetime.combine(d, datetime.min.time()) + timedelta(hours=int(hh), minutes=int(mm))
        while t < end:
            if t not in drop:
                o = price
                price += rng.normal(0, 0.05)
                rows.append((t, o, max(o, price) + 0.01, min(o, price) - 0.01, price, float(rng.integers(1, 50))))
            t += timedelta(minutes=1)
    return pd.DataFrame(rows, columns=['time_stamp', 'open', 'high', 'low', 'close', 'volume'])


DAYS = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]


@pytest.mark.parametrize('closing', ['23:30', '23:55'])
@pytest.mark.parametrize('minutes', [15, 60])
@pytest.mark.parametrize('now', [datetime(2026, 9, 3, 12, 7, 30), datetime(2026, 9, 3, 23, 40), datetime(2026, 9, 4, 8, 0)])
def test_resample_matches_production_including_the_forming_bar_and_the_short_last_bucket(closing, minutes, now):
    prod_resample, _ = _production(closing)
    df = frame(DAYS, closing)
    df = df[df['time_stamp'] <= pd.Timestamp(now)]
    mine = resample_1m(df, minutes, now, '09:00', lambda d: closing)
    theirs = prod_resample(df, minutes, now)
    pd.testing.assert_frame_equal(mine, theirs)
    assert len(mine) > (100 if minutes == 15 else 10)


def test_resample_with_missing_minutes_and_a_short_last_bucket_agrees_with_production():
    prod_resample, prod_gaps = _production('23:55')
    drop = {datetime(2026, 9, 2, 12, m) for m in range(0, 15)}          # a whole 15-minute window missing
    drop |= {datetime(2026, 9, 3, 15, 4), datetime(2026, 9, 3, 15, 5)}
    df = frame(DAYS, '23:55', drop=drop)
    now = datetime(2026, 9, 4, 9, 0)
    mine, theirs = resample_1m(df, 15, now, '09:00', lambda d: '23:55'), prod_resample(df, 15, now)
    pd.testing.assert_frame_equal(mine, theirs)
    assert find_gaps(mine, 15) == prod_gaps(theirs, 15) and find_gaps(mine, 15), 'the missing window is a gap'
    last = mine[mine['time_stamp'] == datetime(2026, 9, 3, 23, 45)]
    assert len(last) == 1, 'the short last bucket on a 23:55 day is kept, not dropped'


def test_resample_of_nothing_is_an_empty_frame():
    assert resample_1m(pd.DataFrame(columns=['time_stamp', 'open', 'high', 'low', 'close', 'volume']).astype(
        {'time_stamp': 'datetime64[ns]'}), 15, datetime(2026, 9, 3, 12), '09:00', lambda d: '23:30').empty


def test_merge_keeps_older_rows_when_the_file_carries_the_offset_and_new_rows_are_naive(tmp_path):
    p = tmp_path / 'x.csv'
    a = frame([DAYS[0]], '10:00')
    assert merge_and_save(p, a) == len(a)
    assert p.read_text().splitlines()[1].split(',')[0] == '2026-09-01T09:00:00+05:30'
    b = frame([DAYS[1]], '10:00')
    assert merge_and_save(p, b) == len(b)
    back = pd.read_csv(p, parse_dates=['time_stamp'])
    assert back['time_stamp'].notna().all() and len(back) == len(a) + len(b), 'no older row turned into NaT'
    assert merge_and_save(p, b) == 0, 'idempotent'
    changed = b.copy()
    changed['close'] = 999.0
    merge_and_save(p, changed)
    assert (pd.read_csv(p)['close'] != 999.0).all(), 'an existing minute is kept, not overwritten'


def test_format_timestamp_appends_the_kolkata_offset():
    assert format_timestamp(pd.Timestamp('2026-09-03 12:34:00')) == '2026-09-03T12:34:00+05:30'


def test_read_past_returns_the_n_days_before_today_and_never_today(tmp_path):
    df = frame([date(2026, 8, 10)] + DAYS, '10:00')
    p = tmp_path / 'c.csv'
    merge_and_save(p, df)
    now = datetime(2026, 9, 3, 10, 0)
    got = read_past(p, now, 5)
    assert got['time_stamp'].dt.date.unique().tolist() == [date(2026, 9, 1), date(2026, 9, 2)], 'aug 10 is older than 5 days; today excluded'
    skipped = read_past(p, now, 5, skip_dates=['2026-09-01'])
    assert skipped['time_stamp'].dt.date.unique().tolist() == [date(2026, 9, 2)]
    assert read_past(tmp_path / 'missing.csv', now, 5).empty


def test_today_cache_is_per_token_self_pruning_and_idempotent(tmp_path):
    cache = TodayCache(tmp_path)
    yesterday = frame([DAYS[1]], '09:20')
    today = frame([DAYS[2]], '09:20')
    now = datetime(2026, 9, 3, 9, 30)
    cache.merge('T1', pd.concat([yesterday, today]))
    cache.merge('T2', today.assign(close=5.0))
    got = cache.read(now, 'T1')
    assert len(got) == len(today) and got['time_stamp'].dt.date.unique().tolist() == [DAYS[2]], 'yesterday pruned on read'
    assert len(pd.read_csv(cache.path('T1'))) == len(today), 'and pruned on disk'
    assert (cache.read(now, 'T2')['close'] == 5.0).all(), 'tokens do not share a file'
    assert cache.merge('T1', today) == 0
    assert cache.read(datetime(2026, 9, 4, 9, 30), 'T1').empty and not cache.path('T1').exists()


def test_read_past_of_a_file_shorter_than_the_tail_keeps_numeric_columns(tmp_path):
    """head + tail repeats the header line when the file is shorter than the tail; that row must not poison the dtypes."""
    p = tmp_path / 'short.csv'
    merge_and_save(p, frame([DAYS[1]], '09:30'))
    got = read_past(p, datetime(2026, 9, 3, 10, 0), 5)
    assert len(got) == 30 and all(got[c].dtype.kind == 'f' for c in ['open', 'high', 'low', 'close', 'volume'])
